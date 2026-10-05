"""Delay retries of blocked targets. Run while holding the runner's tick lock."""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timedelta
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
from uuid import uuid4

# Reuse the shared quota contract without changing Claude-owned scripts.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "epic"))
import github_quota  # noqa: E402
import target_lock  # noqa: E402

SKIP = 3
BLOCKED = 3
FAILED = 75
BLOCK_LIMIT = 3
ESCALATION_LABEL = "needs-anton"
REPO = "phaabe/live.moafunk.de"
ISSUE_LINE = re.compile(
    rf"Issue:[ \t]*(https://github\.com/{re.escape(REPO)}/issues/[1-9][0-9]*)[ \t]*"
)


class StaleAction(ValueError):
    """The selected PR head changed before its cooldown could transfer."""


class QuotaWait(Exception):
    """Another runner stored a quota wait before notification delivery."""


class Escalation(TypedDict):
    action: dict[str, object]
    id: str
    label_added: bool
    owns_label: bool
    comment_posted: bool


class Entry(TypedDict, total=False):
    at: float
    # When the retry delay ends. Older entries have none; they use at + ttl.
    until: float
    reason: str
    blocked_count: int
    escalation: Escalation


def expires(entry: Entry, ttl: int) -> float:
    return entry.get("until", entry["at"] + ttl)


def target_key(action: dict[str, object]) -> str:
    if action.get("pr"):
        pr = action["pr"]
        sha = action.get("sha")
        if type(pr) is not int or pr <= 0 or not isinstance(sha, str) or not sha:
            raise ValueError("invalid PR target")
        if action.get("action") == "resolve-conflict":
            tip = action.get("target_tip")
            if not isinstance(tip, str) or re.fullmatch(r"[0-9a-f]{40}", tip) is None:
                raise ValueError("resolve-conflict needs a pinned target tip")
            return f"pr:{pr}:{sha}:base:{tip}"
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
    for key, entry in entries.items():
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
        count = entry.get("blocked_count", 0)
        if type(count) is not int or not 0 <= count <= BLOCK_LIMIT:
            raise ValueError("invalid blocked count")
        escalation = entry.get("escalation")
        if escalation is not None:
            if (
                count != BLOCK_LIMIT
                or not isinstance(escalation, dict)
                or not isinstance(escalation.get("action"), dict)
                or target_key(escalation["action"]) != key
                or not isinstance(escalation.get("id"), str)
                or re.fullmatch(r"[0-9a-f]{32}", escalation["id"]) is None
                or any(
                    type(escalation.get(field)) is not bool
                    for field in ("label_added", "owns_label", "comment_posted")
                )
            ):
                raise ValueError("invalid blocked escalation")
        elif count == BLOCK_LIMIT:
            raise ValueError("missing blocked escalation")
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
    import github_state

    if github_state.enabled():
        reader = github_state.FreshReader("backoff", github_state.settings().recheck)
        pull = reader.pull(action["pr"])
        head = pull.get("head")
        if (
            "body" not in pull
            or not isinstance(pull["body"], str | None)
            or not isinstance(head, dict)
            or not isinstance(head.get("sha"), str)
            or github_state.SHA.fullmatch(head["sha"]) is None
        ):
            raise github_state.ReadBlocked("invalid PR metadata")
        metadata = {"body": pull["body"] or "", "headRefOid": head["sha"]}
        if metadata["headRefOid"] != action["sha"]:
            raise StaleAction("PR head changed since action selection")
    else:
        metadata = legacy_pr_metadata(action)
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


def legacy_pr_metadata(action: dict[str, object]) -> dict[str, object]:
    """Keep the old read path until both runners enable the shared reader."""
    gh = github_quota.resolve_gh()
    response = subprocess.run(
        [
            gh or "gh",
            "pr",
            "view",
            str(action["pr"]),
            "--repo",
            REPO,
            "--json",
            "body,headRefOid",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if github_quota.is_quota_error(
        ["pr", "view"], response.stdout or "", response.stderr or ""
    ):
        raise github_quota.QuotaExhausted(
            response.stderr or "GraphQL RATE_LIMITED", gh_path=gh
        )
    response.check_returncode()
    metadata = json.loads(response.stdout)
    if not isinstance(metadata, dict) or not isinstance(metadata.get("body"), str):
        raise ValueError("invalid PR metadata")
    return metadata


def check(action: dict[str, object], state_dir: Path, ttl: int, now: float) -> int:
    entries = load_entries(state_dir)
    key = target_key(action)
    entry = entries.get(key)
    if entry is not None and entry.get("blocked_count", 0) >= BLOCK_LIMIT:
        logging.info("backoff: waiting for Anton after three blocked results")
        return SKIP
    if entry is not None and now < expires(entry, ttl):
        logging.info("backoff: skip blocked target until its retry delay expires")
        return SKIP
    if action.get("action") == "continue" and action.get("pr"):
        active_issues = {
            name: entry
            for name, entry in entries.items()
            if name.startswith("issue:")
            and now < expires(entry, ttl)
            and "escalation" not in entry
        }
        if active_issues:
            issue = pr_issue(action)
            source = f"issue:{issue}" if issue is not None else None
            if source is not None and source in active_issues:
                count = entry.get("blocked_count", 0) if entry is not None else 0
                entries[key] = entries.pop(source)
                # Transfer only the wait. The PR/head is a different count key.
                if "blocked_count" in entries[key] or count:
                    entries[key]["blocked_count"] = count
                save_entries(state_dir, entries)
                logging.info(
                    "backoff: transferred issue retry delay to selected PR head"
                )
                return SKIP
    return 0


def escalation_api(quota_dir: Path, endpoint: str, *options: str) -> object:
    waiting, _ = github_quota.check(quota_dir, time.time())
    if waiting == github_quota.DEFERRED:
        raise QuotaWait
    output = github_quota.run_gh(["api", endpoint, *options], timeout=30)
    return json.loads(output) if output.strip() else None


def reconcile_entry(
    state_dir: Path, entries: dict[str, Entry], key: str, quota_dir: Path
) -> None:
    """Deliver or reset one escalation while its target lock is held."""
    entry = entries[key]
    escalation = entry["escalation"]
    action = escalation["action"]
    if action.get("pr"):
        number = action["pr"]
        target = f"repos/{REPO}/pulls/{number}"
    else:
        match = ISSUE_LINE.fullmatch(f"Issue: {action.get('issue', '').rstrip('/')}")
        if match is None:
            raise ValueError("invalid escalation issue URL")
        number = match.group(1).rsplit("/", 1)[1]
        target = f"repos/{REPO}/issues/{number}"
    metadata = escalation_api(quota_dir, target)
    if not isinstance(metadata, dict) or metadata.get("state") not in (
        "open",
        "closed",
    ):
        raise ValueError("invalid escalation target")
    labels = metadata.get("labels")
    if not isinstance(labels, list) or any(
        not isinstance(label, dict) or not isinstance(label.get("name"), str)
        for label in labels
    ):
        raise ValueError("invalid escalation labels")
    labeled = any(label["name"] == ESCALATION_LABEL for label in labels)
    changed_head = False
    if action.get("pr"):
        head = metadata.get("head")
        if (
            not isinstance(head, dict)
            or not isinstance(head.get("sha"), str)
            or re.fullmatch(r"[0-9a-f]{40}", head["sha"]) is None
        ):
            raise ValueError("invalid escalation PR head")
        changed_head = head["sha"] != action["sha"]
    endpoint = f"repos/{REPO}/issues/{number}"
    if (
        metadata["state"] == "closed"
        or changed_head
        or (escalation["label_added"] and not labeled)
    ):
        # The selector excludes labeled targets. Release only our own label
        # when a new head arrives; labels on closed targets remain as history.
        if (
            changed_head
            and labeled
            and escalation["owns_label"]
            and metadata["state"] == "open"
        ):
            escalation_api(
                quota_dir, f"{endpoint}/labels/{ESCALATION_LABEL}", "--method", "DELETE"
            )
        del entries[key]
        save_entries(state_dir, entries)
        return
    if not escalation["label_added"]:
        if not labeled:
            # Save intent before the write so a lost response can be retried.
            escalation["owns_label"] = True
            save_entries(state_dir, entries)
            escalation_api(
                quota_dir,
                f"{endpoint}/labels",
                "--method",
                "POST",
                "-f",
                f"labels[]={ESCALATION_LABEL}",
            )
        escalation["label_added"] = True
        save_entries(state_dir, entries)
    if not escalation["comment_posted"]:
        marker = f"<!-- codex-blocked:{escalation['id']} -->"
        pages = escalation_api(
            quota_dir, f"{endpoint}/comments?per_page=100", "--paginate", "--slurp"
        )
        if not isinstance(pages, list) or any(
            not isinstance(page, list)
            or any(
                not isinstance(row, dict) or not isinstance(row.get("body"), str)
                for row in page
            )
            for page in pages
        ):
            raise ValueError("invalid escalation comments")
        if not any(marker in row["body"] for page in pages for row in page):
            body = (
                "Codex stopped after 3 blocked results for this target.\n\n"
                f"Last blocked reason: {entry['reason']}\n\n"
                "Anton: remove needs-anton to retry. A new PR head also resets the count.\n\n"
                f"{marker}"
            )
            escalation_api(
                quota_dir,
                f"{endpoint}/comments",
                "--method",
                "POST",
                "-f",
                f"body={body}",
            )
        escalation["comment_posted"] = True
        save_entries(state_dir, entries)


def reconcile(state_dir: Path, quota_dir: Path) -> None:
    """Run before selection: labeled targets are absent from its candidates."""
    entries = load_entries(state_dir)
    for key, entry in list(entries.items()):
        if "escalation" not in entry:
            continue
        with ExitStack() as stack:
            locks = [
                stack.enter_context(path.open("a"))
                for path in target_lock.paths(
                    entry["escalation"]["action"], target_lock.lock_dir()
                )
            ]
            if target_lock.acquire([lock.fileno() for lock in locks]):
                reconcile_entry(state_dir, entries, key, quota_dir)


def result_outcome(result_file: Path, exit_code: int) -> tuple[int, str | None]:
    try:
        result = json.loads(result_file.read_text())
    except (OSError, ValueError):
        return FAILED, (
            f"model exited {exit_code}"
            if exit_code
            else "missing or invalid final result"
        )
    if (
        not isinstance(result, dict)
        or set(result)
        not in (
            {"status", "summary"},
            {"status", "summary", "reason_code", "retry_at"},
        )
        or result["status"] not in ("completed", "blocked")
        or not isinstance(result["summary"], str)
    ):
        return FAILED, (
            f"model exited {exit_code}" if exit_code else "invalid final result schema"
        )
    reason_code, retry_at = result.get("reason_code"), result.get("retry_at")
    if reason_code == "github_rate_limit" and result["status"] == "blocked":
        if retry_at is not None:
            try:
                if not isinstance(retry_at, str):
                    raise ValueError("retry time must be a string")
                at = datetime.fromisoformat(retry_at.replace("Z", "+00:00"))
                if at.utcoffset() != timedelta(0):
                    raise ValueError("retry time must be UTC")
            except ValueError:
                return FAILED, (
                    f"model exited {exit_code}"
                    if exit_code
                    else "invalid quota retry time"
                )
        return github_quota.QUOTA, "model reported GitHub GraphQL quota exhausted"
    if reason_code is not None or retry_at is not None:
        return FAILED, (
            f"model exited {exit_code}"
            if exit_code
            else "invalid quota result metadata"
        )
    if exit_code != 0:
        return FAILED, f"model exited {exit_code}"
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
    quota_dir: Path | None = None,
) -> int:
    outcome, reason = result_outcome(result_file, exit_code)
    if outcome == github_quota.QUOTA:
        quota_state = quota_dir or state_dir
        waiting, existing_retry = github_quota.check(quota_state, now)
        if waiting == github_quota.DEFERRED:
            logging.warning("quota: %s; retry at %s", reason, existing_retry)
            return outcome
        retry_at = json.loads(result_file.read_text())["retry_at"]
        # Model times beyond one quota window need a fresh authoritative reset.
        if (
            retry_at is not None
            and github_quota.parse_iso(retry_at) > now + 3600 + github_quota.MARGIN
        ):
            retry_at = None
        # The result names the retry time (already including the shared margin).
        reset_at = (
            github_quota.iso(github_quota.parse_iso(retry_at) - github_quota.MARGIN)
            if retry_at is not None
            else None
        )
        wait = github_quota.record(quota_state, now, reset_at, origin="model-result")
        logging.warning("quota: %s; retry at %s", reason, wait["retry_at"])
        return outcome
    key = target_key(action)
    entries = load_entries(state_dir)
    if reason is None:
        entries.pop(key, None)
    else:
        previous = entries.get(key, {})
        if "escalation" in previous:
            return BLOCKED
        count = previous.get("blocked_count", 0) + (outcome == BLOCKED)
        entries[key] = {
            "at": now,
            "until": now + ttl,
            "reason": reason,
            "blocked_count": count,
        }
        if count == BLOCK_LIMIT:
            entries[key]["escalation"] = {
                "action": action,
                "id": uuid4().hex,
                "label_added": False,
                "owns_label": False,
                "comment_posted": False,
            }
    save_entries(state_dir, entries)
    if "escalation" in entries.get(key, {}):
        reconcile_entry(state_dir, entries, key, quota_dir or state_dir)
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
    parser.add_argument("command", choices=("check", "record", "reconcile"))
    parser.add_argument("--action-file", type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--quota-dir", type=Path)
    parser.add_argument("--ttl", required=True, type=positive_int)
    parser.add_argument("--result-file", type=Path)
    parser.add_argument("--exit-code", type=int)
    args = parser.parse_args()
    if args.command != "reconcile" and args.action_file is None:
        parser.error("check and record require --action-file")
    quota_dir = args.quota_dir or Path(
        os.environ.get("EPIC_QUOTA_DIR") or args.state_dir
    )
    if args.command == "record" and (
        args.result_file is None or args.exit_code is None
    ):
        parser.error("record requires --result-file and --exit-code")
    try:
        if args.command == "reconcile":
            reconcile(args.state_dir, quota_dir)
            return 0
        action = json.loads(args.action_file.read_text())
        if not isinstance(action, dict):
            raise ValueError("invalid action")
        if args.command == "check":
            # The shared reader imports the selector. Reconciliation and result
            # recording must not load it before the tick's one selection.
            import github_state

            try:
                return check(action, args.state_dir, args.ttl, time.time())
            except github_state.ConfigError as error:
                logging.error("backoff: %s", error)
                return 2
            except github_state.ReadBlocked as error:
                logging.error("backoff: read blocked: %s", error)
                return 5
        return record(
            action,
            args.state_dir,
            args.result_file,
            args.exit_code,
            time.time(),
            args.ttl,
            quota_dir,
        )
    except QuotaWait:
        return github_quota.QUOTA
    except github_quota.QuotaExhausted as error:
        return github_quota.stop_on_quota(error, quota_dir)
    except StaleAction as error:
        logging.info("backoff: skipped: %s", error)
        return 6
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        logging.error("backoff: %s", error)
        return FAILED


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.exit(main())
