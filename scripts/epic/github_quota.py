"""Shared GitHub GraphQL quota wait for both epic runners.

One host and one account share the GraphQL quota, so one wait file is enough:
`<state dir>/github-quota-wait.json`. When a GitHub read hits the GraphQL quota
(also inside an HTTP 200 response), the tick stops and stores the reset time in
UTC. Until reset plus MARGIN, every tick makes zero GitHub calls and starts zero
models. The wait never touches target cooldowns or repeat-gate records.

The reset time comes from one `rateLimit { resetAt }` GraphQL query, never from
REST `rate_limit` data (it showed graphql=0 while GraphQL was exhausted). When it
cannot be read, the wait is FALLBACK_SECONDS from now.

Interface (both runners):
  github_quota.py check  [--state-dir D]
      exit 0 proceed, exit 3 deferred (prints the retry time), exit 2 bad file.
      Makes no GitHub call.
  github_quota.py record [--state-dir D] [--reset-at 2026-09-28T13:00:00Z]
      stores the wait and prints the retry time. Without --reset-at it runs
      one rateLimit query. A newer exhaustion replaces the old wait.

Scripts that read GitHub (next_action.py, tick_gate.py, tick_verify.py) call
gh through run_gh(). On a quota error they record the wait and exit QUOTA (4).

Wait file:
  {"reset_at": "2026-09-28T13:00:00Z", "retry_at": "2026-09-28T13:01:00Z",
   "recorded_at": "2026-09-28T12:10:00Z", "source": "rateLimit"}
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# The quota belongs to the GitHub user, so every agent shares one wait.
# Registered agents point EPIC_STATE_DIR at their own folder; the runners set
# EPIC_QUOTA_DIR to the shared state dir.
STATE_DIR = Path(
    os.environ.get("EPIC_QUOTA_DIR")
    or os.environ.get("EPIC_STATE_DIR", Path.home() / ".local" / "state" / "epic-loop")
)
WAIT_FILE = "github-quota-wait.json"
MARGIN = 60
FALLBACK_SECONDS = 15 * 60
PROCEED, BAD_FILE, DEFERRED, QUOTA = 0, 2, 3, 4

# gh prints GraphQL errors as "GraphQL: <message>"; `gh api graphql` prints the
# raw response, whose errors carry "type": "RATE_LIMITED".
QUOTA_TEXT = re.compile(r"API rate limit (?:already )?exceeded", re.IGNORECASE)
GRAPHQL_COMMANDS = {"pr", "project", "issue"}
RESET_QUERY = "query{rateLimit{resetAt}}"


class QuotaExhausted(Exception):
    """A GitHub read hit the GraphQL quota. The tick must stop."""


def is_graphql(args: list[str]) -> bool:
    return bool(args) and (
        args[0] in GRAPHQL_COMMANDS or args[:2] == ["api", "graphql"]
    )


def rate_limited_body(text: str) -> bool:
    """True when a GraphQL JSON response has a RATE_LIMITED error."""
    try:
        body = json.loads(text)
    except ValueError:
        return False
    if not isinstance(body, dict):
        return False
    errors = body.get("errors")
    return isinstance(errors, list) and any(
        isinstance(e, dict) and e.get("type") == "RATE_LIMITED" for e in errors
    )


def is_quota_error(args: list[str], stdout: str, stderr: str) -> bool:
    if rate_limited_body(stdout):
        return True
    if "secondary rate limit" in stderr.lower():
        return False
    return is_graphql(args) and bool(QUOTA_TEXT.search(stderr))


def run_gh(args: list[str], timeout: int = 120) -> str:
    """Run gh; raise QuotaExhausted on a GraphQL quota error, even with exit 0.

    Other failures raise CalledProcessError as before, so auth, network and
    unrelated GraphQL errors stay visible.
    """
    out = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout)
    if is_quota_error(args, out.stdout, out.stderr):
        raise QuotaExhausted(out.stderr.strip() or "GraphQL RATE_LIMITED")
    if out.returncode != 0:
        raise subprocess.CalledProcessError(
            out.returncode, ["gh", *args], out.stdout, out.stderr
        )
    return out.stdout


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> float:
    at = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if at.tzinfo is None:
        raise ValueError(f"time without zone: {text}")
    return at.timestamp()


def query_reset_at() -> str | None:
    """One rateLimit query. None when it fails or has no resetAt."""
    try:
        out = subprocess.run(
            ["gh", "api", "graphql", "-f", f"query={RESET_QUERY}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        reset = json.loads(out.stdout)["data"]["rateLimit"]["resetAt"]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return None
    return reset if isinstance(reset, str) else None


def check(state_dir: Path, now: float) -> tuple[int, str | None]:
    """(PROCEED, None) or (DEFERRED, retry_at). Raises ValueError on a bad file."""
    try:
        wait = json.loads((state_dir / WAIT_FILE).read_text())
    except FileNotFoundError:
        return PROCEED, None
    retry_at = wait.get("retry_at") if isinstance(wait, dict) else None
    if not isinstance(retry_at, str):
        raise ValueError(f"bad quota wait file {state_dir / WAIT_FILE}")
    if now < parse_iso(retry_at):
        return DEFERRED, retry_at
    return PROCEED, None


def record(
    state_dir: Path,
    now: float,
    reset_at: str | None = None,
    lookup: Callable[[], str | None] = query_reset_at,
) -> dict[str, Any]:
    """Store the wait and return it. A later reset replaces the old wait."""
    source = "caller"
    reset: float | None = None
    if reset_at is None:
        reset_at, source = lookup(), "rateLimit"
    if reset_at is not None:
        try:
            reset = parse_iso(reset_at)
        except ValueError:
            reset = None
    if reset is None or reset + MARGIN <= now:
        # Unknown or already past: a bounded backoff, never an immediate retry.
        wait = {
            "reset_at": None,
            "retry_at": iso(now + FALLBACK_SECONDS),
            "source": "fallback",
        }
    else:
        wait = {
            "reset_at": iso(reset),
            "retry_at": iso(reset + MARGIN),
            "source": source,
        }
    wait["recorded_at"] = iso(now)
    state_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=state_dir, prefix=".quota-", delete=False
    ) as f:
        json.dump(wait, f, indent=1)
    os.replace(f.name, state_dir / WAIT_FILE)
    return wait


def stop_on_quota(error: QuotaExhausted, state_dir: Path = STATE_DIR) -> int:
    """Record the wait for a quota error, log it and return QUOTA."""
    wait = record(state_dir, time.time())
    print(
        f"quota: GitHub GraphQL quota exhausted ({error}); "
        f"retry at {wait['retry_at']} ({wait['source']})",
        file=sys.stderr,
    )
    return QUOTA


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=["check", "record"])
    parser.add_argument("--state-dir", type=Path, default=STATE_DIR)
    parser.add_argument("--reset-at", help="reset time, UTC ISO 8601")
    args = parser.parse_args()
    now = time.time()
    if args.command == "record":
        wait = record(args.state_dir, now, args.reset_at)
        print(f"quota: wait stored, retry at {wait['retry_at']} ({wait['source']})")
        return PROCEED
    try:
        result, retry_at = check(args.state_dir, now)
    except (OSError, ValueError) as error:
        print(f"quota: {error}", file=sys.stderr)
        return BAD_FILE
    if result == DEFERRED:
        print(f"quota: GitHub GraphQL quota wait, retry at {retry_at}")
    return result


if __name__ == "__main__":
    sys.exit(main())
