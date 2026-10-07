"""Blocked-target cooldown of the Claude runner.

The repeat gate (tick_gate.py) skips an action only while its target's
`updated_at` is unchanged, so any comment restarts an action that cannot
succeed. A cooldown holds a blocked action back for a fixed time instead.

Evidence (never words in the log). claude-tick.sh runs the model with
`--output-format json --json-schema claude-result-schema.json`, catches its
exit code and timeout, runs tick_verify.py, then calls `record`:

  model result `blocked`                        cooldown
  model result `quota`                          no cooldown, no gate record (exit 4)
  model API error, not landed                   no cooldown, no gate record (exit 6)
  verify exit 1, PR open with the selected head  cooldown ("target unchanged")
  verify passed                                 done: gate record, cooldown cleared
  anything else                                 no cooldown; the gate record as before
                                                (only after a model exit 0)

A verify read error (tick_verify.py exit 5) is no evidence: no cooldown.

This holds for every model exit: 0, nonzero, and timeout (124/137). A nonzero
exit alone is no evidence: for an action verify does not check (`continue`,
`claim`, `escalate`) it only ends the tick. Quota waits, GitHub read failures,
pause and lock contention never reach `record`, so they never set a cooldown.

Key (agent kind, action, target): `claude:<action>:pr:<n>:<head sha>`, plus
`:base:<base sha>` for `resolve-conflict`, or `claude:<action>:issue:<url>`.
A new PR head or a new base tip gives a new key, so it clears the cooldown.
Comments, reviews and verdicts change neither, so they never reset or extend
it. The Codex key (.codex/tick_backoff.py) is a separate contract and a
separate file.

State: `claude-cooldown.json` in the shared state dir (EPIC_QUOTA_DIR, the
registry dir), locked with `claude-cooldown.lock`. Registered Claude agents
(EPIC_AGENT_ID) share it: a target blocked for one Claude runner is blocked
for all of them. Each entry names the agent id that stored it.

Duration: EPIC_BLOCKED_COOLDOWN_SECONDS, default 4 hours. It ends at expiry,
or when a later tick of the same key lands (`record` clears it).

Repeat gate: a blocked tick writes no gate record, so after expiry or a new
base the retry reaches the model even when `updated_at` is unchanged. A tick
that lands still writes the gate record, so duplicate suppression of no-op
actions stays. The cooldown applies to `continue` too, although the gate
never skips it (tick_gate.NEVER_SKIP).

Issue to PR: a `claim` or `continue` cooldown on an issue moves to the
`continue` of a PR whose single `Issue:` line names that issue. All entries
of that issue move at once, as one entry with the latest end time, keyed to
the PR head seen then; a later push clears it.

Environment hold: a blocked result with `reason_code: "environment"` means
the runner's environment stopped the model (sandbox, permissions, classifier,
missing tool, protected config), not the ticket. It cools down like any
block, and `record` writes an `env-block` tick event (tick_events.py) when the
runner passes --events-file and --tick. Environment blocks on 2 different
targets (PR or issue) within HOLD_WINDOW (6 hours) set the hold:
`claude-hold.json` in the shared state dir (reason, targets, time). While the
file exists, `hold` exits 3 and the runner starts no model; selection and
closing still run. Only Anton clears it, by deleting the file. Setting the
hold uses up the blocks seen so far, so after the delete it takes 2 new ones.
The blocks seen are kept in `claude-env-blocks.json`. The Codex runner uses
the same hold format and event name in its own `codex-hold.json`.

Usage:
  tick_cooldown.py check  --action-file A --state-dir D --seen-file S
  tick_cooldown.py record --action-file A --state-dir D --seen-file S
                          --result-file R --model-exit N --verify-exit M
                          [--events-file E --tick T]
  tick_cooldown.py hold   --state-dir D
  tick_cooldown.py status --state-dir D

check exits 0 (run), 3 (skip: cooling down), 4 (GraphQL quota, wait stored),
5 (GitHub read failed: no action), 2 (bad input or state). record exits 0
(done), 1 (did not land, no evidence: record the gate), 3 (cooldown stored),
4 (model reported the GitHub quota), 6 (no evidence: no gate record), 2.
hold exits 0 (no hold) or 3 (held: no model).
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

import tick_events
from github_quota import QuotaExhausted, run_gh, stop_on_quota

REPO = "phaabe/live.moafunk.de"
AGENT = "claude"
FILE = f"{AGENT}-cooldown.json"
HOLD_FILE = f"{AGENT}-hold.json"
ENV_FILE = f"{AGENT}-env-blocks.json"
HOLD_WINDOW = 6 * 60 * 60
HOLD_TARGETS = 2
ENVIRONMENT = "environment"
DEFAULT_SECONDS = 4 * 60 * 60
ENV_SECONDS = "EPIC_BLOCKED_COOLDOWN_SECONDS"
# The actions tick_verify.py checks (tick_verify.CHECKED). Kept here so this
# module does not import next_action.py; a test keeps both sets equal.
LANDING = {
    "merge",
    "review",
    "fix",
    "adopt",
    "fix-checks",
    "resolve-conflict",
    "refine",
    "review-refinement",
    "set-ready",
}
# Issue cooldowns of these actions move to the `continue` of the issue's PR.
TRANSFER = {"claim", "continue"}
TIMEOUT_EXITS = {124, 137}
ISSUE_URL = re.compile(rf"https://github\.com/{re.escape(REPO)}/issues/[1-9][0-9]*")
ISSUE_LINE = re.compile(rf"Issue:[ \t]*({ISSUE_URL.pattern})[ \t]*")
SHA = re.compile(r"[0-9a-f]{40}")

RUN, SKIP, QUOTA, READ_FAILED, BAD = 0, 3, 4, 5, 2
DONE, MISSED, BLOCKED, UNKNOWN = 0, 1, 3, 6


class ReadFailed(Exception):
    """A GitHub read failed: the tick takes no action."""


def seconds() -> int:
    value = os.environ.get(ENV_SECONDS, str(DEFAULT_SECONDS))
    if not value.isdigit() or int(value) <= 0:
        raise ValueError(f"{ENV_SECONDS} must be a positive integer")
    return int(value)


def base_key(action: dict[str, Any]) -> str:
    """The key without the base SHA."""
    kind = action.get("action")
    if not isinstance(kind, str) or not kind:
        raise ValueError("action has no name")
    pr, sha = action.get("pr"), action.get("sha")
    if pr is not None:
        if type(pr) is not int or pr <= 0 or not isinstance(sha, str):
            raise ValueError("invalid PR target")
        if SHA.fullmatch(sha) is None:
            raise ValueError("invalid PR head")
        return f"{AGENT}:{kind}:pr:{pr}:{sha}"
    issue = action.get("issue")
    if isinstance(issue, str) and ISSUE_URL.fullmatch(issue.rstrip("/")):
        return f"{AGENT}:{kind}:issue:{issue.rstrip('/')}"
    raise ValueError("action has no issue or PR target")


def read(args: list[str]) -> str:
    try:
        return run_gh(args, timeout=60).strip()
    except (subprocess.SubprocessError, OSError) as error:
        raise ReadFailed(f"gh {' '.join(args[:2])} failed") from error


def base_sha(pr: int) -> str:
    """The current tip of the PR's base branch (two REST reads)."""
    ref = read(["api", f"repos/{REPO}/pulls/{pr}", "--jq", ".base.ref"])
    if not ref or "\n" in ref:
        raise ReadFailed(f"PR {pr} has no base branch")
    sha = read(["api", f"repos/{REPO}/git/ref/heads/{ref}", "--jq", ".object.sha"])
    if SHA.fullmatch(sha) is None:
        raise ReadFailed(f"base {ref} has no commit SHA")
    return sha


def key(action: dict[str, Any]) -> str:
    """The full key. Reads the base tip for `resolve-conflict`."""
    found = base_key(action)
    if action["action"] == "resolve-conflict":
        found += f":base:{base_sha(action['pr'])}"
    return found


def valid(entry: Any) -> bool:
    return (
        isinstance(entry, dict)
        and all(
            type(entry.get(k)) in (int, float) and math.isfinite(entry[k])
            for k in ("at", "until")
        )
        and isinstance(entry.get("reason"), str)
    )


def load(state_dir: Path) -> dict[str, dict[str, Any]]:
    try:
        entries = json.loads((state_dir / FILE).read_text())
    except FileNotFoundError:
        return {}
    if not isinstance(entries, dict) or not all(valid(e) for e in entries.values()):
        raise ValueError(f"invalid {FILE}")
    return entries


def save(state_dir: Path, entries: dict[str, dict[str, Any]], now: float) -> None:
    """Atomic write; expired entries are dropped."""
    live = {k: v for k, v in entries.items() if v["until"] > now}
    with tempfile.NamedTemporaryFile(
        "w", dir=state_dir, prefix=f".{FILE}-", delete=False
    ) as out:
        json.dump(live, out, indent=1, sort_keys=True, allow_nan=False)
        out.write("\n")
    os.replace(out.name, state_dir / FILE)


@contextmanager
def locked(state_dir: Path) -> Generator[None]:
    """Registered Claude runners share the file: one writer at a time."""
    state_dir.mkdir(parents=True, exist_ok=True)
    with open(state_dir / f"{AGENT}-cooldown.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def active(entries: dict[str, dict[str, Any]], now: float) -> dict[str, Any]:
    return {k: v for k, v in entries.items() if v["until"] > now}


def pr_issue(pr: int) -> str | None:
    """The issue named by the PR's single `Issue:` line, or None."""
    body = read(["api", f"repos/{REPO}/pulls/{pr}", "--jq", '.body // ""'])
    lines = [line for line in body.splitlines() if line.startswith("Issue:")]
    if len(lines) != 1:
        return None
    match = ISSUE_LINE.fullmatch(lines[0].rstrip())
    return match.group(1) if match else None


def when(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def check(action: dict[str, Any], state_dir: Path, seen_file: Path, now: float) -> int:
    """SKIP while the action's key cools down. Saves the key for `record`."""
    seen_file.unlink(missing_ok=True)
    found = key(action)
    with locked(state_dir):
        entries = load(state_dir)
        entry = active(entries, now).get(found)
        if entry is None and action["action"] == "continue" and action.get("pr"):
            # Issue cooldowns that move to this PR's `continue`.
            prefixes = tuple(f"{AGENT}:{kind}:issue:" for kind in TRANSFER)
            waiting = [k for k in active(entries, now) if k.startswith(prefixes)]
            issue = pr_issue(action["pr"]) if waiting else None
            # Consume every entry of that issue at once: a later head of the
            # same PR must not pick up a second one.
            sources = [k for k in waiting if issue and k.endswith(f":issue:{issue}")]
            if sources:
                moved = [entries.pop(k) for k in sources]
                entry = {**max(moved, key=lambda e: e["until"]), "from": sources}
                entries[found] = entry
                save(state_dir, entries, now)
                print(
                    f"cooldown: moved {', '.join(sources)} to PR {action['pr']}",
                    file=sys.stderr,
                )
        if entry is not None:
            print(
                f"cooldown: skip {found} until {when(entry['until'])}: "
                f"{entry['reason']}",
                file=sys.stderr,
            )
            return SKIP
    seen_file.write_text(json.dumps({"key": found}))
    return RUN


def model_result(result_file: Path) -> dict[str, str] | None:
    """The structured result of `claude -p --output-format json`, or None."""
    try:
        output = json.loads(result_file.read_text())
    except (OSError, ValueError):
        return None
    result = output.get("structured_output") if isinstance(output, dict) else None
    if (
        not isinstance(result, dict)
        or not {"status", "summary"}
        <= set(result)
        <= {"status", "summary", "reason_code"}
        or result["status"] not in ("completed", "blocked", "quota")
        or not isinstance(result["summary"], str)
        or result.get("reason_code", ENVIRONMENT) != ENVIRONMENT
    ):
        return None
    return result


def target_of(action: dict[str, Any]) -> str:
    """The target without head or base: `pr:<n>` or `issue:<url>`."""
    base_key(action)  # validates the target
    if action.get("pr") is not None:
        return f"pr:{action['pr']}"
    return f"issue:{action['issue'].rstrip('/')}"


def api_error(result_file: Path) -> bool:
    """The session ended on a model API error (for example 529 Overloaded).

    `claude -p --output-format json` says so in `terminal_reason`. Such an
    error is temporary and costs no tokens, so the next tick simply retries.
    """
    try:
        output = json.loads(result_file.read_text())
    except (OSError, ValueError):
        return False
    return isinstance(output, dict) and output.get("terminal_reason") == "api_error"


def unchanged(action: dict[str, Any]) -> bool | None:
    """True when the PR is still open at the selected head; None if unknown."""
    try:
        out = read(
            ["api", f"repos/{REPO}/pulls/{action['pr']}", "--jq", ".state, .head.sha"]
        ).split()
    except ReadFailed:
        return None
    if len(out) != 2:
        return None
    return out[0] == "open" and out[1] == action["sha"]


def ended(model_exit: int) -> str:
    if model_exit in TIMEOUT_EXITS:
        return "model timed out"
    return f"model exited {model_exit}" if model_exit else "model exited 0"


def classify(
    action: dict[str, Any],
    result: dict[str, str] | None,
    model_exit: int,
    verify_exit: int,
    api_failed: bool = False,
) -> tuple[int, str]:
    """(outcome, reason) from runner evidence only."""
    if result is not None and result["status"] == "quota":
        return QUOTA, "model reported the GitHub quota"
    if result is not None and result["status"] == "blocked":
        summary = " ".join(result["summary"].split())[:240]
        return BLOCKED, f"model reported blocked: {summary}"
    if verify_exit == 0:
        if model_exit == 0 or action["action"] in LANDING:
            return DONE, "landed"
        return UNKNOWN, f"{ended(model_exit)}; {action['action']} is not verified"
    if api_failed:
        return UNKNOWN, f"{ended(model_exit)} on a model API error; retry next tick"
    if verify_exit == 1 and action.get("pr") and action["action"] in LANDING:
        state = unchanged(action)
        if state:
            return BLOCKED, (
                f"{ended(model_exit)}; {action['action']} did not land "
                "and the target is unchanged"
            )
    if model_exit == 0:
        return MISSED, f"{action['action']} did not land"
    return UNKNOWN, f"{ended(model_exit)}; no evidence"


def held(state_dir: Path) -> dict[str, Any] | None:
    """The hold, or None. Any file is a hold, also one that cannot be read."""
    path = state_dir / HOLD_FILE
    try:
        hold = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        return {"reason": f"unreadable {HOLD_FILE}: {error}", "targets": []}
    if not isinstance(hold, dict):
        return {"reason": f"invalid {HOLD_FILE}", "targets": []}
    return hold


def env_blocks(state_dir: Path, now: float) -> list[dict[str, Any]]:
    """Environment blocks inside HOLD_WINDOW; a bad file counts as none."""
    try:
        blocks = json.loads((state_dir / ENV_FILE).read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(blocks, list):
        return []
    return [
        b
        for b in blocks
        if isinstance(b, dict)
        and type(b.get("at")) in (int, float)
        and math.isfinite(b["at"])
        and now - HOLD_WINDOW < b["at"] <= now
        and isinstance(b.get("target"), str)
        and isinstance(b.get("reason"), str)
    ]


def write_json(path: Path, data: Any) -> None:
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, prefix=f".{path.name}-", delete=False
    ) as out:
        json.dump(data, out, indent=1, sort_keys=True, allow_nan=False)
        out.write("\n")
    os.replace(out.name, path)


def env_block(state_dir: Path, target: str, reason: str, now: float) -> bool:
    """Store one environment block. True when it sets the hold.

    Call with the cooldown lock held.
    """
    blocks = [
        *env_blocks(state_dir, now),
        {"at": now, "target": target, "reason": reason},
    ]
    targets = sorted({b["target"] for b in blocks})
    if len(targets) < HOLD_TARGETS or held(state_dir) is not None:
        write_json(state_dir / ENV_FILE, blocks)
        return False
    write_json(
        state_dir / HOLD_FILE,
        {"at": when(now), "reason": reason, "targets": targets},
    )
    # The hold uses up these blocks: after Anton deletes it, 2 new ones count.
    write_json(state_dir / ENV_FILE, [])
    print(
        f"hold: environment blocks on {', '.join(targets)}; no model until "
        f"{state_dir / HOLD_FILE} is deleted",
        file=sys.stderr,
    )
    return True


def record(
    action: dict[str, Any],
    state_dir: Path,
    seen_file: Path,
    result_file: Path,
    model_exit: int,
    verify_exit: int,
    now: float,
    events_file: Path | None = None,
    tick: str | None = None,
) -> int:
    seen = json.loads(seen_file.read_text())
    found = seen.get("key")
    if not isinstance(found, str) or not found.startswith(base_key(action)):
        raise ValueError("cooldown: record does not match the checked action")
    seen_file.unlink()
    result = model_result(result_file)
    outcome, reason = classify(
        action,
        result,
        model_exit,
        verify_exit,
        api_error(result_file),
    )
    print(f"cooldown: {found}: {reason}", file=sys.stderr)
    if outcome not in (DONE, BLOCKED):
        return outcome
    environment = (
        outcome == BLOCKED
        and result is not None
        and result["status"] == "blocked"
        and result.get("reason_code") == ENVIRONMENT
    )
    hold = False
    with locked(state_dir):
        entries = load(state_dir)
        if outcome == DONE and found not in entries:
            return outcome
        entries.pop(found, None)
        if outcome == BLOCKED:
            until = now + seconds()
            entries[found] = {
                "at": now,
                "until": until,
                "reason": reason,
                "by": os.environ.get("EPIC_AGENT_ID") or AGENT,
            }
            print(f"cooldown: stored until {when(until)}", file=sys.stderr)
        save(state_dir, entries, now)
        if environment:
            hold = env_block(state_dir, target_of(action), reason, now)
    if environment and events_file is not None and tick is not None:
        # The event is for the monitor only: a failed write changes nothing.
        try:
            tick_events.env_block(
                events_file, tick, action, target_of(action), reason, hold
            )
        except (OSError, ValueError) as error:
            print(f"cooldown: env-block event not written: {error}", file=sys.stderr)
    return outcome


def status_lines(state_dir: Path, now: float) -> list[str]:
    """Active cooldowns for --status; unreadable state is shown, not raised."""
    try:
        entries = active(load(state_dir), now)
    except (OSError, ValueError) as error:
        return [f"  cannot read {FILE}: {error}"]
    return [
        f"  {k}  until {when(v['until'])}  {v['reason']}"
        for k, v in sorted(entries.items(), key=lambda kv: kv[1]["until"])
    ] or ["  none"]


def hold_lines(state_dir: Path) -> list[str]:
    """The model hold for --status."""
    hold = held(state_dir)
    if hold is None:
        return ["  none"]
    targets = hold.get("targets")
    listed = ", ".join(map(str, targets)) if isinstance(targets, list) else "?"
    return [
        f"  HELD since {hold.get('at', '?')}: {hold.get('reason', '?')}",
        f"  targets: {listed or '?'}",
        f"  delete {state_dir / HOLD_FILE} to resume",
    ]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=["check", "record", "hold", "status"])
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--action-file", type=Path)
    parser.add_argument("--seen-file", type=Path)
    parser.add_argument("--result-file", type=Path)
    parser.add_argument("--model-exit", type=int)
    parser.add_argument("--verify-exit", type=int)
    parser.add_argument("--events-file", type=Path)
    parser.add_argument("--tick")
    args = parser.parse_args()
    now = time.time()
    if args.command == "status":
        print("\n".join(status_lines(args.state_dir, now)))
        return 0
    if args.command == "hold":
        if held(args.state_dir) is None:
            return RUN
        print("\n".join(hold_lines(args.state_dir)), file=sys.stderr)
        return SKIP
    if args.action_file is None or args.seen_file is None:
        parser.error("check and record need --action-file and --seen-file")
    try:
        seconds()
        action = json.loads(args.action_file.read_text())
        if not isinstance(action, dict):
            raise ValueError("invalid action")
        if args.command == "check":
            return check(action, args.state_dir, args.seen_file, now)
        if None in (args.result_file, args.model_exit, args.verify_exit):
            parser.error("record needs --result-file, --model-exit, --verify-exit")
        return record(
            action,
            args.state_dir,
            args.seen_file,
            args.result_file,
            args.model_exit,
            args.verify_exit,
            now,
            args.events_file,
            args.tick,
        )
    except QuotaExhausted as error:
        return stop_on_quota(error, args.state_dir)
    except ReadFailed as error:
        print(f"cooldown: GitHub read failed: {error}; no action", file=sys.stderr)
        return READ_FAILED
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"cooldown: {error}", file=sys.stderr)
        return BAD


if __name__ == "__main__":
    sys.exit(main())
