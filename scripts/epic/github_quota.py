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
   "recorded_at": "2026-09-28T12:10:00Z", "source": "rateLimit",
   "provenance": {...}}

source says where the reset came from: "caller" (given to record, not measured),
"rateLimit" (the lookup) or "fallback" (no usable reset). The optional
provenance block says who wrote the wait (see provenance()). It is diagnostic
only: it never changes the wait, and files without it stay valid.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
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
# origin in the provenance block: a code path, or a retry time a model claimed.
ORIGINS = ("code", "model-result")


class QuotaExhausted(Exception):
    """A GitHub read hit the GraphQL quota. The tick must stop.

    gh_path is the resolved gh executable of the call that hit the quota, or
    None when the caller did not report it.
    """

    def __init__(self, message: str = "", gh_path: str | None = None) -> None:
        super().__init__(message)
        self.gh_path = gh_path


def resolve_gh() -> str | None:
    """The gh executable on PATH now. Callers run exactly this path."""
    return shutil.which("gh")


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
    gh = resolve_gh()
    out = subprocess.run(
        [gh or "gh", *args], capture_output=True, text=True, timeout=timeout
    )
    if is_quota_error(args, out.stdout, out.stderr):
        raise QuotaExhausted(out.stderr.strip() or "GraphQL RATE_LIMITED", gh)
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


def query_reset_at() -> tuple[str | None, str | None]:
    """One rateLimit query: (resetAt, gh path).

    resetAt is None when the query fails or has no resetAt. The gh path is None
    when gh cannot be resolved; then no query runs.
    """
    gh = resolve_gh()
    if gh is None:
        return None, None
    try:
        out = subprocess.run(
            [gh, "api", "graphql", "-f", f"query={RESET_QUERY}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        reset = json.loads(out.stdout)["data"]["rateLimit"]["resetAt"]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return None, gh
    return (reset if isinstance(reset, str) else None), gh


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


def writer() -> dict[str, Any]:
    """The process writing a wait. No arguments or environment values."""
    # The main module's file, not argv: no arguments reach the wait file.
    script = getattr(sys.modules.get("__main__"), "__file__", None)
    try:
        cwd: str | None = os.getcwd()
    except OSError:
        cwd = None
    return {
        "pid": os.getpid(),
        "executable": sys.executable or None,
        "script": os.path.abspath(script) if script else None,
        "cwd": cwd,
        "written_at": iso(time.time()),
    }


def provenance(
    origin: str,
    lookup: dict[str, Any],
    quota_gh: str | None,
) -> dict[str, Any]:
    """Who wrote the wait and which gh answered. Diagnostic only.

    reset_lookup: {"attempted": false} or {"attempted": true, "gh_path": ...,
    "result": "reset" | "failed"}; gh_path None means gh was not resolvable.
    quota_call: gh_path of the call that hit the quota, None when not reported.
    The gh path is local attribution, not proof that GitHub answered.
    """
    return {
        "origin": origin,
        "writer": writer(),
        "reset_lookup": lookup,
        "quota_call": {"gh_path": quota_gh},
    }


def describe(wait: dict[str, Any]) -> str:
    """One line on who wrote a wait, for logs and the check command."""
    prov = wait.get("provenance")
    if not isinstance(prov, dict):
        return "provenance unavailable (older wait file)"
    w = prov.get("writer")
    w = w if isinstance(w, dict) else {}
    lookup = prov.get("reset_lookup")
    lookup = lookup if isinstance(lookup, dict) else {}
    call = prov.get("quota_call")
    call = call if isinstance(call, dict) else {}

    def known(value: Any) -> str:
        return str(value) if value not in (None, "") else "unavailable"

    if lookup.get("attempted") is True:
        gh, result = known(lookup.get("gh_path")), known(lookup.get("result"))
        looked = f"reset lookup gh {gh} ({result})"
    elif lookup.get("attempted") is False:
        looked = "no reset lookup"
    else:
        looked = "reset lookup unavailable"
    failing = call.get("gh_path")
    return (
        f"origin {known(prov.get('origin'))}; writer pid {known(w.get('pid'))} "
        f"{known(w.get('script') or w.get('executable'))} in {known(w.get('cwd'))} "
        f"at {known(w.get('written_at'))}; {looked}; quota call gh "
        f"{failing if failing else 'not reported'}"
    )


def record(
    state_dir: Path,
    now: float,
    reset_at: str | None = None,
    lookup: Callable[[], tuple[str | None, str | None]] | None = None,
    origin: str = "code",
    quota_gh: str | None = None,
) -> dict[str, Any]:
    """Store the wait and return it. A later reset replaces the old wait.

    origin "model-result" marks a reset_at a model claimed. quota_gh is the gh
    path of the call that hit the quota (QuotaExhausted.gh_path).
    """
    if origin not in ORIGINS:
        raise ValueError(f"unknown quota wait origin: {origin}")
    source = "caller"
    reset: float | None = None
    looked: dict[str, Any] = {"attempted": False}
    if reset_at is None:
        reset_at, gh = (lookup or query_reset_at)()
        source = "rateLimit"
        looked = {
            "attempted": True,
            "gh_path": gh,
            "result": "reset" if reset_at is not None else "failed",
        }
    if reset_at is not None:
        try:
            reset = parse_iso(reset_at)
        except ValueError:
            reset = None
    if reset is None or reset + MARGIN <= now:
        # Unknown or already past: a bounded backoff, never an immediate retry.
        wait: dict[str, Any] = {
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
    wait["provenance"] = provenance(origin, looked, quota_gh)
    state_dir.mkdir(parents=True, exist_ok=True)
    # mkstemp creates the file 0600; os.replace publishes it whole or not at all.
    with tempfile.NamedTemporaryFile(
        "w", dir=state_dir, prefix=".quota-", delete=False
    ) as f:
        try:
            json.dump(wait, f, indent=1)
            f.flush()
            os.replace(f.name, state_dir / WAIT_FILE)
        except BaseException:
            os.unlink(f.name)
            raise
    return wait


def stop_on_quota(error: QuotaExhausted, state_dir: Path = STATE_DIR) -> int:
    """Record the wait for a quota error, log it and return QUOTA."""
    wait = record(state_dir, time.time(), quota_gh=error.gh_path)
    print(
        f"quota: GitHub GraphQL quota exhausted ({error}); "
        f"retry at {wait['retry_at']} ({wait['source']}; {describe(wait)})",
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
        print(
            f"quota: wait stored, retry at {wait['retry_at']} "
            f"({wait['source']}; {describe(wait)})"
        )
        return PROCEED
    try:
        result, retry_at = check(args.state_dir, now)
    except (OSError, ValueError) as error:
        print(f"quota: {error}", file=sys.stderr)
        return BAD_FILE
    if result == DEFERRED:
        try:
            wait = json.loads((args.state_dir / WAIT_FILE).read_text())
            about = describe(wait) if isinstance(wait, dict) else None
        except (OSError, ValueError):
            about = None  # replaced or removed since check(); the wait stands
        print(
            f"quota: GitHub GraphQL quota wait, retry at {retry_at} "
            f"({about or 'provenance unavailable'})"
        )
    return result


if __name__ == "__main__":
    sys.exit(main())
