"""Skip a tick that would repeat the last no-op action. Saves model tokens.

A runner calls `check` after next_action.py and before starting a model session,
and `record` after a session that exited 0. `check` exits 3 ("skip") when:
  - the action JSON is the same as the recorded one,
  - its PR or issue on GitHub has not changed since the record (updated_at), and
  - the record is younger than the TTL (EPIC_REPEAT_TTL_SECONDS, default 3 hours).
A push changes the PR head SHA in the action, and a comment changes updated_at,
so real progress always gets a new session. `continue` is never skipped: local
work between ticks does not show on GitHub.

Usage:
  tick_gate.py check  --agent claude|codex --action-file action.json
  tick_gate.py record --agent claude|codex --action-file action.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO = "phaabe/live.moafunk.de"
STATE_DIR = Path(
    os.environ.get("EPIC_STATE_DIR", Path.home() / ".local" / "state" / "epic-loop")
)
DEFAULT_TTL = 3 * 60 * 60
NEVER_SKIP = {"continue", "idle", "stop"}
SKIP = 3


def fingerprint(action: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(action, sort_keys=True).encode()).hexdigest()


def target_number(action: dict[str, Any]) -> int | None:
    if action.get("pr"):
        return int(action["pr"])
    issue = action.get("issue") or ""
    tail = issue.rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


def should_skip(
    action: dict[str, Any],
    updated_at: str | None,
    record: dict[str, Any] | None,
    now: float,
    ttl: int,
) -> bool:
    if action.get("action") in NEVER_SKIP or not record:
        return False
    return (
        record.get("fingerprint") == fingerprint(action)
        and updated_at is not None
        and record.get("updated_at") == updated_at
        and now - float(record.get("at", 0)) < ttl
    )


def updated_at(action: dict[str, Any]) -> str | None:
    number = target_number(action)
    if number is None:
        return None
    out = subprocess.run(
        ["gh", "api", f"repos/{REPO}/issues/{number}", "--jq", ".updated_at"],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return out.stdout.strip() or None


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=["check", "record"])
    parser.add_argument("--agent", required=True, choices=["claude", "codex"])
    parser.add_argument("--action-file", required=True, type=Path)
    args = parser.parse_args()

    action = json.loads(args.action_file.read_text())
    record_file = STATE_DIR / f"{args.agent}-gate.json"
    if args.command == "record":
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        record = {
            "fingerprint": fingerprint(action),
            "updated_at": updated_at(action),
            "at": time.time(),
            "action": action,
        }
        record_file.write_text(json.dumps(record, indent=1))
        return 0

    record = json.loads(record_file.read_text()) if record_file.exists() else None
    ttl = int(os.environ.get("EPIC_REPEAT_TTL_SECONDS", DEFAULT_TTL))
    if should_skip(action, updated_at(action), record, time.time(), ttl):
        print(
            "gate: skip, same action as the last tick and no change on GitHub",
            file=sys.stderr,
        )
        return SKIP
    return 0


if __name__ == "__main__":
    sys.exit(main())
