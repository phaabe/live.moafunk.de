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

Records are kept per target (PR or issue number), so a suppressed repeat on one
target never blocks another: the runner tries the next candidate action. The
top-level fields of `<agent>-gate.json` stay the last recorded session
(monitor.py reads them); `targets` holds one record per target.

`check` runs after the runner took the target lock, and also rechecks the start
state: it exits 3 when the target is closed or its `updated_at` differs from the
one in the action (the selector read it). Another runner acted in between.
For a PR action it also reads the PR (REST, both reader modes): it exits 3 when
the head is no longer the action's `sha`, when a conflict appeared for `fix`,
`fix-checks`, `merge` or `review`, or when it cleared for `resolve-conflict`.
The selector gives another action then (next_action.py). A base push can make a
PR conflict without changing its `updated_at`. GitHub's `mergeable: null`
(still computing) is UNKNOWN and changes nothing; a malformed PR fails the
check, like any other failed read.

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
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable

from github_quota import QuotaExhausted, run_gh, stop_on_quota

# Standalone: the Codex runner tests copy this file with github_quota.py only.

REPO = "phaabe/live.moafunk.de"
STATE_DIR = Path(
    os.environ.get("EPIC_STATE_DIR", Path.home() / ".local" / "state" / "epic-loop")
)
DEFAULT_TTL = 3 * 60 * 60
NEVER_SKIP = {"continue", "idle", "stop"}
SKIP = 3
SHA = re.compile(r"[0-9a-f]{40}")
# Actions the selector no longer gives on a conflicted PR, which gets
# `resolve-conflict` (owner) or no review (peer) instead.
NOT_ON_CONFLICT = {"fix", "fix-checks", "merge", "review"}
MERGEABLE = {True: "MERGEABLE", False: "CONFLICTING", None: "UNKNOWN"}


def fingerprint(action: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(action, sort_keys=True).encode()).hexdigest()


def target_number(action: dict[str, Any]) -> int | None:
    if action.get("pr"):
        return int(action["pr"])
    issue = action.get("issue") or ""
    tail = issue.rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


def target_key(action: dict[str, Any]) -> str:
    number = target_number(action)
    return "none" if number is None else str(number)


def stale(action: dict[str, Any], seen: dict[str, Any] | None) -> str | None:
    """Why the action no longer fits the target, or None when it still does."""
    if not seen:
        return None
    if seen.get("state") == "closed":
        return "the target is closed"
    expected = action.get("updated_at")
    if expected and seen.get("updated_at") != expected:
        return "the target changed after selection"
    kind, head, merge = action.get("action"), seen.get("head"), seen.get("mergeable")
    if head and action.get("sha") and head != action["sha"]:
        return "the PR head changed after selection"
    if merge == "CONFLICTING" and kind in NOT_ON_CONFLICT:
        return f"the PR conflicts with its base; no {kind} on this head"
    if merge == "MERGEABLE" and kind == "resolve-conflict":
        return "the PR no longer conflicts with its base"
    return None


def target_record(
    records: dict[str, Any] | None, action: dict[str, Any]
) -> dict[str, Any] | None:
    """The record for this action's target. Old files hold one top-level record."""
    if not records:
        return None
    key = target_key(action)
    by_target = records.get("targets")
    if isinstance(by_target, dict):
        return by_target.get(key)
    top = records.get("action")
    if isinstance(top, dict) and target_key(top) == key:
        return records
    return None


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


def pull_state(number: int) -> dict[str, Any]:
    """Head SHA and mergeable state of a PR. ValueError when malformed."""
    pull = json.loads(run_gh(["api", f"repos/{REPO}/pulls/{number}"], timeout=60))
    if not isinstance(pull, dict):
        raise ValueError(f"gate: PR {number} is not an object")
    head = pull.get("head")
    head = head.get("sha") if isinstance(head, dict) else None
    if not isinstance(head, str) or not SHA.fullmatch(head):
        raise ValueError(f"gate: PR {number} has no valid head SHA")
    # Only null means "still computing"; a missing key is malformed.
    merge = pull.get("mergeable", "missing")
    if merge is not None and not isinstance(merge, bool):
        raise ValueError(f"gate: PR {number} has no valid mergeable state")
    return {"head": head, "mergeable": MERGEABLE[merge]}


def target_state(action: dict[str, Any]) -> dict[str, Any] | None:
    """`updated_at` and `state` (open/closed) of the action's PR or issue.
    For an open PR with a selected head, also its `head` and `mergeable`."""
    number = target_number(action)
    if number is None:
        return None
    out = run_gh(
        ["api", f"repos/{REPO}/issues/{number}", "--jq", ".updated_at, .state"],
        timeout=60,
    ).split()
    if not out:
        return None
    found = {"updated_at": out[0], "state": out[1] if len(out) > 1 else None}
    if action.get("pr") and action.get("sha") and found["state"] == "open":
        found.update(pull_state(number))
    return found


def check(
    agent: str,
    action: dict[str, Any],
    lookup: Callable[[dict[str, Any]], dict[str, Any] | None],
    now: float,
    ttl: int,
    state_dir: Path,
) -> int:
    """0 = run, SKIP = skip. Saves the GitHub state this tick starts from."""
    record_file = state_dir / f"{agent}-gate.json"
    records = json.loads(record_file.read_text()) if record_file.exists() else None
    target = lookup(action)
    reason = stale(action, target)
    if reason:
        print(f"gate: skip, {reason}", file=sys.stderr)
        return SKIP
    seen = target.get("updated_at") if target else None
    if should_skip(action, seen, target_record(records, action), now, ttl):
        print(
            "gate: skip, same action as the last tick and no change on GitHub",
            file=sys.stderr,
        )
        return SKIP
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / f"{agent}-gate-seen.json").write_text(
        json.dumps({"fingerprint": fingerprint(action), "updated_at": seen})
    )
    return 0


def record(
    agent: str,
    action: dict[str, Any],
    now: float,
    state_dir: Path,
    ttl: int = DEFAULT_TTL,
) -> int:
    """Record the state saved by `check`, not a fresh one: a comment that
    arrived during the session was not seen, so the next tick must run.
    Target records older than the TTL are dropped; they no longer skip."""
    seen_file = state_dir / f"{agent}-gate-seen.json"
    seen = json.loads(seen_file.read_text())
    if seen.get("fingerprint") != fingerprint(action):
        raise ValueError("gate: record does not match the checked action")
    record_file = state_dir / f"{agent}-gate.json"
    old = json.loads(record_file.read_text()) if record_file.exists() else {}
    targets = old.get("targets") if isinstance(old.get("targets"), dict) else {}
    entry = {
        "fingerprint": seen["fingerprint"],
        "updated_at": seen["updated_at"],
        "at": now,
        "action": action,
    }
    targets = {
        key: value
        for key, value in targets.items()
        if isinstance(value, dict) and now - float(value.get("at", 0)) < ttl
    }
    targets[target_key(action)] = entry
    record_file.write_text(json.dumps({**entry, "targets": targets}, indent=1))
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
    ttl = int(os.environ.get("EPIC_REPEAT_TTL_SECONDS", DEFAULT_TTL))
    if args.command == "record":
        return record(args.agent, action, time.time(), STATE_DIR, ttl)
    try:
        return check(args.agent, action, target_state, time.time(), ttl, STATE_DIR)
    except QuotaExhausted as error:
        # Stop before any gate record is written.
        return stop_on_quota(error)


if __name__ == "__main__":
    sys.exit(main())
