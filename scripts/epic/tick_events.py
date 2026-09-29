"""Write the runner's tick events: one JSON line per tick start and finish.

The monitor reads `<kind>-ticks.jsonl` in the runner's state dir instead of
guessing outcomes from the log. The runner knows why a tick ended, so it
names the outcome and the phase. Each line is one write() below 4 KiB on a
file opened for appending, so lines of two writers never interleave.

  tick_events.py start  --file F --tick 2026-09-28T15:39:39Z --log L
      prints the log size, the offset where this tick's output begins
  tick_events.py finish --file F --tick T --exit N --phase P [--outcome O|auto]
                        [--action-file A] [--log L --since OFFSET]

Only enums, numbers, timestamps and the selector's action/PR/issue are
written: no model text, prompts or commands.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

OUTCOMES = ("ok", "blocked", "timeout", "killed", "error")
PHASES = (
    "lock",
    "refresh",
    "select",
    "quota",
    "backoff",
    "gate",
    "model",
    "result",
    "verify",
    "record",
    "unknown",
)
TICK = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
ACTION = re.compile(r"[a-z][a-z-]{0,31}")
ISSUE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/issues/\d{1,9}")
TOKENS = re.compile(r"[0-9]{1,3}(?:,[0-9]{3}){0,4}|[0-9]{1,15}")
MAX_LINE = 4096
MAX_ACTION_FILE = 65_536
# Only the end of the session's output is scanned for the token count.
MAX_SCAN = 1_048_576


def outcome_of(exit_code: int) -> str:
    """Outcome from the exit code when the runner did not name one."""
    if exit_code == 0:
        return "ok"
    if exit_code == 124:
        return "timeout"
    if exit_code in (129, 130, 137, 143):
        return "killed"
    return "error"


def action_fields(path: Path | None) -> dict[str, Any]:
    """Selector decision fields, typed and bounded; empty when unreadable."""
    fields: dict[str, Any] = {"action": None, "pr": None, "issue": None}
    if path is None:
        return fields
    try:
        with path.open("rb") as stream:
            data = json.loads(stream.read(MAX_ACTION_FILE).decode("utf-8"))
    except (OSError, ValueError):
        return fields
    if not isinstance(data, dict):
        return fields
    if isinstance(data.get("action"), str) and ACTION.fullmatch(data["action"]):
        fields["action"] = data["action"]
    pr = data.get("pr")
    if type(pr) is int and 0 < pr < 10**9:
        fields["pr"] = pr
    if isinstance(data.get("issue"), str) and ISSUE.fullmatch(data["issue"]):
        fields["issue"] = data["issue"]
    return fields


def tokens_since(log: Path | None, offset: int | None) -> int | None:
    """Last `tokens used` count this tick printed; best effort."""
    if log is None or offset is None:
        return None
    try:
        with log.open("rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            stream.seek(max(offset, size - MAX_SCAN, 0))
            text = stream.read().decode("utf-8", "replace")
    except OSError:
        return None
    lines = text.splitlines()
    found = None
    for i, line in enumerate(lines[:-1]):
        if line == "tokens used" and TOKENS.fullmatch(lines[i + 1]):
            found = int(lines[i + 1].replace(",", ""))
    return found


def append(path: Path, event: dict[str, Any]) -> None:
    line = (json.dumps(event, separators=(",", ":")) + "\n").encode("utf-8")
    if len(line) >= MAX_LINE:
        raise ValueError("event line is too long")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        if os.write(fd, line) != len(line):
            raise OSError("short event write")
    finally:
        os.close(fd)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    finish = commands.add_parser("finish")
    for command in (start, finish):
        command.add_argument("--file", type=Path, required=True)
        command.add_argument("--tick", required=True)
        command.add_argument("--log", type=Path)
    finish.add_argument("--exit", type=int, required=True, dest="exit_code")
    finish.add_argument("--phase", choices=PHASES, required=True)
    # "auto": derive from the exit code (bash 3.2 cannot pass empty arrays).
    finish.add_argument("--outcome", choices=(*OUTCOMES, "auto"), default="auto")
    finish.add_argument("--action-file", type=Path)
    finish.add_argument("--since", type=int)
    args = parser.parse_args(argv)
    if not TICK.fullmatch(args.tick):
        parser.error("--tick must look like 2026-09-28T15:39:39Z")
    try:
        if args.command == "start":
            append(
                args.file,
                {"v": 1, "event": "start", "tick": args.tick, "pid": os.getppid()},
            )
            # The runner passes this back to finish, to find its token count.
            size = args.log.stat().st_size if args.log and args.log.exists() else 0
            print(size)
            return 0
        if not 0 <= args.exit_code <= 255:
            parser.error("--exit must be 0-255")
        append(
            args.file,
            {
                "v": 1,
                "event": "finish",
                "tick": args.tick,
                "at": now_iso(),
                "exit": args.exit_code,
                "outcome": (
                    outcome_of(args.exit_code)
                    if args.outcome == "auto"
                    else args.outcome
                ),
                "phase": args.phase,
                **action_fields(args.action_file),
                "tokens": tokens_since(args.log, args.since),
            },
        )
    except (OSError, ValueError) as error:
        print(f"tick: event write failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
