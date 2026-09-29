"""Delay retries of blocked targets. Run while holding the runner's tick lock."""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import TypedDict

SKIP = 3
BLOCKED = 3
FAILED = 75
REPO = "phaabe/live.moafunk.de"
ISSUE_LINE = re.compile(
    rf"Issue:[ \t]*(https://github\.com/{re.escape(REPO)}/issues/[1-9][0-9]*)[ \t]*"
)


class Entry(TypedDict, total=False):
    at: float
    # When the retry delay ends. Older entries have none; they use at + ttl.
    until: float
    reason: str


def expires(entry: Entry, ttl: int) -> float:
    return entry.get("until", entry["at"] + ttl)


def target_key(action: dict[str, object]) -> str:
    if action.get("pr"):
        pr = action["pr"]
        sha = action.get("sha")
        if type(pr) is not int or pr <= 0 or not isinstance(sha, str) or not sha:
            raise ValueError("invalid PR target")
        return f"pr:{pr}:{sha}"
    issue = action.get("issue")
    if isinstance(issue, str) and issue:
        return f"issue:{issue.rstrip('/')}"
    raise ValueError("action has no issue or PR target")


def load_entries(state_dir: Path) -> dict[str, Entry]:
    path = state_dir / "codex-backoff.json"
    try:
        entries = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    if not isinstance(entries, dict):
        raise ValueError("invalid backoff state")
    for entry in entries.values():
        if (
            not isinstance(entry, dict)
            or type(entry.get("at")) not in (int, float)
            or not math.isfinite(entry["at"])
            or not isinstance(entry.get("reason"), str)
            or (
                "until" in entry
                and (
                    type(entry["until"]) not in (int, float)
                    or not math.isfinite(entry["until"])
                )
            )
        ):
            raise ValueError("invalid backoff entry")
    return entries


def save_entries(state_dir: Path, entries: dict[str, Entry]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=state_dir, prefix=".codex-backoff-", delete=False
        ) as output:
            temporary = Path(output.name)
            json.dump(entries, output, indent=2, allow_nan=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, state_dir / "codex-backoff.json")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def pr_issue(action: dict[str, object]) -> str | None:
    response = subprocess.run(
        [
            "gh",
            "pr",
            "view",
            str(action["pr"]),
            "--repo",
            REPO,
            "--json",
            "body,headRefOid",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    metadata = json.loads(response.stdout)
    if not isinstance(metadata, dict) or not isinstance(metadata.get("body"), str):
        raise ValueError("invalid PR metadata")
    if metadata.get("headRefOid") != action["sha"]:
        raise ValueError("PR head changed since action selection")
    declarations = [
        line for line in metadata["body"].splitlines() if line.startswith("Issue:")
    ]
    if not declarations:
        return None
    if len(declarations) != 1:
        raise ValueError("ambiguous PR Issue metadata")
    match = ISSUE_LINE.fullmatch(declarations[0])
    if match is None:
        raise ValueError("invalid PR Issue metadata")
    return match.group(1)


def check(action: dict[str, object], state_dir: Path, ttl: int, now: float) -> int:
    entries = load_entries(state_dir)
    key = target_key(action)
    entry = entries.get(key)
    if entry is not None and now < expires(entry, ttl):
        logging.info("backoff: skip blocked target until its retry delay expires")
        return SKIP
    if action.get("action") == "continue" and action.get("pr"):
        active_issues = {
            name: entry
            for name, entry in entries.items()
            if name.startswith("issue:") and now < expires(entry, ttl)
        }
        if active_issues:
            issue = pr_issue(action)
            source = f"issue:{issue}" if issue is not None else None
            if source is not None and source in active_issues:
                entries[key] = entries.pop(source)
                save_entries(state_dir, entries)
                logging.info(
                    "backoff: transferred issue retry delay to selected PR head"
                )
                return SKIP
    return 0


def result_outcome(result_file: Path, exit_code: int) -> tuple[int, str | None]:
    if exit_code != 0:
        return FAILED, f"model exited {exit_code}"
    try:
        result = json.loads(result_file.read_text())
    except (OSError, ValueError):
        return FAILED, "missing or invalid final result"
    if (
        not isinstance(result, dict)
        or set(result) != {"status", "summary"}
        or result["status"] not in ("completed", "blocked")
        or not isinstance(result["summary"], str)
    ):
        return FAILED, "invalid final result schema"
    if result["status"] == "blocked":
        summary = " ".join(result["summary"].split())[:240]
        return BLOCKED, f"model reported blocked: {summary}"
    return 0, None


def record(
    action: dict[str, object],
    state_dir: Path,
    result_file: Path,
    exit_code: int,
    now: float,
    ttl: int,
) -> int:
    key = target_key(action)
    entries = load_entries(state_dir)
    outcome, reason = result_outcome(result_file, exit_code)
    if reason is None:
        entries.pop(key, None)
    else:
        entries[key] = {"at": now, "until": now + ttl, "reason": reason}
    save_entries(state_dir, entries)
    if reason is not None:
        logging.warning("backoff: %s", reason)
    return outcome


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "record"))
    parser.add_argument("--action-file", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--ttl", required=True, type=positive_int)
    parser.add_argument("--result-file", type=Path)
    parser.add_argument("--exit-code", type=int)
    args = parser.parse_args()
    if args.command == "record" and (
        args.result_file is None or args.exit_code is None
    ):
        parser.error("record requires --result-file and --exit-code")
    try:
        action = json.loads(args.action_file.read_text())
        if not isinstance(action, dict):
            raise ValueError("invalid action")
        if args.command == "check":
            return check(action, args.state_dir, args.ttl, time.time())
        return record(
            action,
            args.state_dir,
            args.result_file,
            args.exit_code,
            time.time(),
            args.ttl,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        logging.error("backoff: %s", error)
        return FAILED


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.exit(main())
