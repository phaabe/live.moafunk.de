"""Skip a tick that would repeat the last no-op action. Saves model tokens.

A runner calls `check` after next_action.py and before starting a model session,
and `record` after a session that exited 0. `record` saves the GitHub state
that `check` saw before the session, so feedback that arrived during the session
still starts the next tick. `check` exits 3 ("skip") when:
  - the action JSON is the same as the recorded one,
  - its PR or issue on GitHub has not changed since the record (updated_at), and
  - the record is younger than the TTL (EPIC_REPEAT_TTL_SECONDS, default 3 hours).
A push changes the PR head SHA in the action, and a comment changes updated_at,
so real progress always gets a new session. `continue` is never skipped: local
work between ticks does not show on GitHub.

`check` exits 4 when its GitHub read hits the GraphQL quota. It then stores the
shared quota wait (github_quota.py) and writes no gate state.

Usage:
  tick_gate.py check  --agent claude|codex --action-file action.json
  tick_gate.py record --agent claude|codex --action-file action.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

from github_quota import QuotaExhausted, run_gh, stop_on_quota

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
    out = run_gh(
        ["api", f"repos/{REPO}/issues/{number}", "--jq", ".updated_at"], timeout=60
    )
    return out.strip() or None


def check(
    agent: str,
    action: dict[str, Any],
    lookup: Callable[[dict[str, Any]], str | None],
    now: float,
    ttl: int,
    state_dir: Path,
) -> int:
    """0 = run, SKIP = skip. Saves the GitHub state this tick starts from."""
    record_file = state_dir / f"{agent}-gate.json"
    record = json.loads(record_file.read_text()) if record_file.exists() else None
    seen = lookup(action)
    if should_skip(action, seen, record, now, ttl):
        return SKIP
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / f"{agent}-gate-seen.json").write_text(
        json.dumps({"fingerprint": fingerprint(action), "updated_at": seen})
    )
    return 0


def record(agent: str, action: dict[str, Any], now: float, state_dir: Path) -> int:
    """Record the state saved by `check`, not a fresh one: a comment that
    arrived during the session was not seen, so the next tick must run."""
    seen_file = state_dir / f"{agent}-gate-seen.json"
    seen = json.loads(seen_file.read_text())
    if seen.get("fingerprint") != fingerprint(action):
        raise ValueError("gate: record does not match the checked action")
    (state_dir / f"{agent}-gate.json").write_text(
        json.dumps(
            {
                "fingerprint": seen["fingerprint"],
                "updated_at": seen["updated_at"],
                "at": now,
                "action": action,
            },
            indent=1,
        )
    )
    seen_file.unlink()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=["check", "record"])
    parser.add_argument("--agent", required=True, choices=["claude", "codex"])
    parser.add_argument("--action-file", required=True, type=Path)
    args = parser.parse_args()

    action = json.loads(args.action_file.read_text())
    if args.command == "record":
        return record(args.agent, action, time.time(), STATE_DIR)
    ttl = int(os.environ.get("EPIC_REPEAT_TTL_SECONDS", DEFAULT_TTL))
    try:
        result = check(args.agent, action, updated_at, time.time(), ttl, STATE_DIR)
    except QuotaExhausted as error:
        # Stop before any gate record is written.
        return stop_on_quota(error)
    if result == SKIP:
        print(
            "gate: skip, same action as the last tick and no change on GitHub",
            file=sys.stderr,
        )
    return result


if __name__ == "__main__":
    sys.exit(main())
