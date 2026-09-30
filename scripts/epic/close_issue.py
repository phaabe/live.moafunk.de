"""Close the `Issue:` ticket of a merged epic PR, once per merge.

Both runners call this after a verified merge; it also works by hand.

  close_issue.py queue --pr N    queue PR N only (no GitHub call); the runner
                                 calls it before a merge session starts
  close_issue.py record --pr N   queue PR N, then run the queue (by hand)
  close_issue.py retry           run the queue; the runner calls it every tick
                                 and after a merge session

For each queued PR it reads the PR and its one `Issue:` ticket and then:

  close   post one evidence comment (PR URL, merge commit, target, tests) and
          close the ticket as completed
  note    the ticket has unchecked leaves the PR's `Leaf IDs:` do not name:
          keep it open and say so on the ticket
  skip    `(partial)`, the epic, a ticket with sub-issues, a PR, or not exactly
          one `Issue:` line: nothing on GitHub, only a log line
  done    the ticket is already closed

An unmerged PR leaves the queue unhandled, so its later merge can queue it.

A PR is queued once per state dir; it is never queued again after it left the
queue. Old merges are never re-scanned, so a reopened ticket stays open until
the next PR for it merges. A failed GitHub call keeps the entry and its progress
(comment posted or not) in `<state dir>/close-queue.json`; the next run retries
it. Each comment carries a marker for its merge. Before a comment is posted
again after an unclear result (a timeout), the ticket's comments are searched
for that marker, so the comment is never posted twice.

Before every GitHub call the pause file and the shared quota wait are read
again. Either one stops the run with no call; the entries stay as they are.

Exit 0 queue empty, 1 an entry is still pending (logged), 2 bad input or queue
file, 3 deferred by the pause file or a quota wait, 4 GraphQL quota (see
github_quota.py; REST calls rarely hit it).
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
from pathlib import Path
from typing import Any, Callable

import github_quota
import next_action
from github_quota import QuotaExhausted, run_gh, stop_on_quota
from next_action import EPIC, REPO, issue_links, issue_url, uncovered_leaves

STATE_DIR = Path(
    os.environ.get("EPIC_STATE_DIR", Path.home() / ".local" / "state" / "epic-loop")
)
QUEUE_FILE = "close-queue.json"
DONE_KEEP = 500
TESTS_MAX = 600
VALIDATION = re.compile(r"^Validation:[ \t]*(.*)$", re.MULTILINE)
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
DEFERRED = 3

Fetch = Callable[[list[str]], Any]


class BadQueue(Exception):
    pass


class Deferred(Exception):
    """The pause file or a stored quota wait: no GitHub call now."""


def github_blocked() -> str | None:
    """Why no GitHub call may run now, or None. Read again before each call."""
    if next_action.PAUSE_FILE.exists():
        return f"pause file {next_action.PAUSE_FILE} exists"
    try:
        result, retry_at = github_quota.check(github_quota.STATE_DIR, time.time())
    except (OSError, ValueError) as error:
        return f"quota wait file unreadable: {error}"
    if result == github_quota.DEFERRED:
        return f"GitHub quota wait until {retry_at}"
    return None


def gh_json(args: list[str]) -> Any:
    out = run_gh(args)
    return json.loads(out) if out.strip() else None


def pr_url(number: int) -> str:
    return f"https://github.com/{REPO}/pull/{number}"


# --- queue file ---------------------------------------------------------------


def load(state_dir: Path) -> dict[str, Any]:
    path = state_dir / QUEUE_FILE
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {"pending": [], "done": []}
    except (OSError, ValueError) as error:
        raise BadQueue(f"{path}: {error}") from error
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("pending"), list)
        or not isinstance(data.get("done"), list)
        or not all(
            isinstance(e, dict) and type(e.get("pr")) is int for e in data["pending"]
        )
        or not all(
            isinstance(e, dict) and type(e.get("pr")) is int for e in data["done"]
        )
    ):
        raise BadQueue(f"{path}: not a close queue")
    return data


def save(state_dir: Path, data: dict[str, Any]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    data["done"] = data["done"][-DONE_KEEP:]
    fd, tmp = tempfile.mkstemp(dir=state_dir, prefix=".close-queue.")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, state_dir / QUEUE_FILE)


def enqueue(data: dict[str, Any], pr: int) -> bool:
    """Add PR `pr` unless it is pending or was handled before."""
    if any(e["pr"] == pr for e in data["pending"] + data["done"]):
        return False
    data["pending"].append({"pr": pr, "commented": False, "attempts": 0})
    return True


# --- decision -----------------------------------------------------------------


def tests_text(pr_body: str) -> str:
    """The PR's `Validation:` line (template field), or a pointer to the PR."""
    m = VALIDATION.search(HTML_COMMENT.sub("", pr_body or ""))
    text = " ".join((m.group(1) if m else "").split())
    if not text:
        return "see the PR"
    return text if len(text) <= TESTS_MAX else text[: TESTS_MAX - 1] + "…"


def has_sub_issues(number: int, issue: dict[str, Any], fetch: Fetch) -> bool:
    summary = issue.get("sub_issues_summary")
    if isinstance(summary, dict) and isinstance(summary.get("total"), int):
        return summary["total"] > 0
    rows = fetch(["api", f"repos/{REPO}/issues/{number}/sub_issues?per_page=1"])
    return bool(rows)


def decide(pr: dict[str, Any], fetch: Fetch) -> tuple[str, int | None, str]:
    """(kind, ticket number, reason) for one PR, from GitHub as it is now."""
    number = pr["number"]
    if not pr.get("merged_at"):
        return "unmerged", None, f"PR {number} is not merged"
    body = pr.get("body") or ""
    links = issue_links(body)
    if len(links) != 1:
        return "skip", None, f"PR {number} has {len(links)} Issue: lines, not one"
    ticket, partial = links[0]
    if ticket == EPIC:
        return "skip", ticket, f"PR {number} names the epic"
    if partial:
        return "skip", ticket, f"PR {number} is partial for issue {ticket}"
    issue = fetch(["api", f"repos/{REPO}/issues/{ticket}"])
    if issue.get("pull_request"):
        return "skip", ticket, f"{ticket} is a PR, not an issue"
    if has_sub_issues(ticket, issue, fetch):
        return "skip", ticket, f"issue {ticket} has sub-issues"
    if issue.get("state") == "closed":
        return "done", ticket, f"issue {ticket} is already closed"
    open_leaves = uncovered_leaves(issue.get("body") or "", body)
    if open_leaves:
        return "note", ticket, ", ".join(open_leaves)
    return "close", ticket, f"issue {ticket} closed"


def marker(kind: str, pr: dict[str, Any]) -> str:
    """Stable per merge, so a retry can find a comment that already landed."""
    sha = pr.get("merge_commit_sha") or "unknown"
    return f"<!-- epic-close {kind} pr={pr['number']} merge={sha} -->"


def close_comment(pr: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"Closed after merge of {pr_url(pr['number'])}.",
            "",
            f"- Merge commit: {pr.get('merge_commit_sha') or 'unknown'}",
            f"- Target: {(pr.get('base') or {}).get('ref') or 'unknown'}",
            f"- Tests: {tests_text(pr.get('body') or '')}",
            "",
            "A merged PR is not activation evidence. Reopen for follow-up work.",
            "",
            marker("close", pr),
        ]
    )


def note_comment(pr: dict[str, Any], leaves: str) -> str:
    return (
        f"Not closed: {pr_url(pr['number'])} merged, but these leaves are "
        f"unchecked and not in its `Leaf IDs:`: {leaves}.\n\n{marker('note', pr)}"
    )


# --- GitHub writes ------------------------------------------------------------


def comment(ticket: int, text: str) -> None:
    run_gh(["api", f"repos/{REPO}/issues/{ticket}/comments", "-f", f"body={text}"])


def has_comment(ticket: int, mark: str) -> bool:
    """True when a comment on the ticket contains `mark`."""
    out = run_gh(
        [
            "api",
            "--paginate",
            f"repos/{REPO}/issues/{ticket}/comments?per_page=100",
            "--jq",
            f".[] | select(.body | contains({json.dumps(mark)})) | .id",
        ]
    )
    return bool(out.strip())


def close(ticket: int) -> None:
    run_gh(
        [
            "api",
            "--method",
            "PATCH",
            f"repos/{REPO}/issues/{ticket}",
            "-f",
            "state=closed",
            "-f",
            "state_reason=completed",
        ]
    )


def post_once(
    entry: dict[str, Any],
    ticket: int,
    text: str,
    mark: str,
    write_comment: Callable[[int, str], None],
    find_comment: Callable[[int, str], bool],
    persist: Callable[[], None],
) -> None:
    """Post `text` unless this entry posted it before. `posting` is saved before
    the request: after an unclear result the next run first looks for `mark`."""
    if entry.get("commented"):
        return
    if entry.get("posting") != mark or not find_comment(ticket, mark):
        entry["posting"] = mark
        persist()
        write_comment(ticket, text)
    entry["commented"] = True
    entry.pop("posting", None)
    persist()


def handle(
    entry: dict[str, Any],
    fetch: Fetch,
    write_comment: Callable[[int, str], None],
    write_close: Callable[[int], None],
    find_comment: Callable[[int, str], bool],
    persist: Callable[[], None],
) -> tuple[str, int | None, str]:
    """Run one queue entry to its end. GitHub errors propagate; progress is
    persisted around each comment."""
    pr = fetch(["api", f"repos/{REPO}/pulls/{entry['pr']}"])
    kind, ticket, reason = decide(pr, fetch)
    if kind == "close" and ticket is not None:
        post_once(
            entry,
            ticket,
            close_comment(pr),
            marker("close", pr),
            write_comment,
            find_comment,
            persist,
        )
        write_close(ticket)
    elif kind == "note" and ticket is not None:
        post_once(
            entry,
            ticket,
            note_comment(pr, reason),
            marker("note", pr),
            write_comment,
            find_comment,
            persist,
        )
        reason = f"issue {ticket} kept open: leaves {reason} not covered"
    return kind, ticket, reason


def guarded(call: Callable[..., Any], blocked: Callable[[], str | None]) -> Any:
    """`call`, but first raise Deferred when `blocked()` gives a reason."""

    def run(*args: Any) -> Any:
        reason = blocked()
        if reason:
            raise Deferred(reason)
        return call(*args)

    return run


def run_queue(
    state_dir: Path,
    fetch: Fetch = gh_json,
    write_comment: Callable[[int, str], None] = comment,
    write_close: Callable[[int], None] = close,
    find_comment: Callable[[int, str], bool] = has_comment,
    now: Callable[[], float] = time.time,
    blocked: Callable[[], str | None] = github_blocked,
) -> int:
    data = load(state_dir)
    fetch, write_comment, write_close, find_comment = (
        guarded(call, blocked)
        for call in (fetch, write_comment, write_close, find_comment)
    )
    failed = 0
    for entry in list(data["pending"]):
        try:
            kind, ticket, reason = handle(
                entry,
                fetch,
                write_comment,
                write_close,
                find_comment,
                lambda: save(state_dir, data),
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            stderr = getattr(error, "stderr", None)
            text = stderr if isinstance(stderr, str) and stderr.strip() else str(error)
            entry["attempts"] = entry.get("attempts", 0) + 1
            entry["last_error"] = text.strip()[:300]
            save(state_dir, data)
            failed += 1
            print(
                f"close: PR {entry['pr']} failed (attempt {entry['attempts']}): "
                f"{entry['last_error']}; retry next tick",
                file=sys.stderr,
            )
            continue
        except (QuotaExhausted, Deferred):
            save(state_dir, data)
            raise
        data["pending"].remove(entry)
        if kind == "unmerged":
            # Not handled: a later merge of this PR may queue it again.
            save(state_dir, data)
            print(f"close: PR {entry['pr']} dropped: {reason}")
            continue
        data["done"].append(
            {
                "pr": entry["pr"],
                "issue": ticket,
                "result": kind,
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now())),
            }
        )
        save(state_dir, data)
        print(f"close: PR {entry['pr']} -> {kind}: {reason}")
    return 1 if failed else 0


# --- verification (tick_verify.py) ----------------------------------------------


def verify(pr_number: int, fetch: Fetch) -> tuple[bool, str]:
    """After a merge: the ticket the PR completes is closed, or needs no close."""
    pr = fetch(["api", f"repos/{REPO}/pulls/{pr_number}"])
    kind, ticket, reason = decide(pr, fetch)
    if kind == "close" and ticket is not None:
        return False, f"{issue_url(ticket)} is still open; close is pending"
    if kind == "note":
        return True, f"issue {ticket} kept open: leaves {reason} not covered"
    return True, reason


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, default=STATE_DIR)
    sub = parser.add_subparsers(dest="command", required=True)
    que = sub.add_parser("queue", help="queue a PR, no GitHub call")
    que.add_argument("--pr", type=int, required=True)
    rec = sub.add_parser("record", help="queue a merged PR, then run the queue")
    rec.add_argument("--pr", type=int, required=True)
    sub.add_parser("retry", help="run the queue")
    args = parser.parse_args()
    try:
        if args.command in ("queue", "record"):
            data = load(args.state_dir)
            if enqueue(data, args.pr):
                save(args.state_dir, data)
            else:
                print(f"close: PR {args.pr} was queued before; not again")
        if args.command == "queue":
            return 0
        return run_queue(args.state_dir)
    except BadQueue as error:
        print(f"close: bad queue file: {error}", file=sys.stderr)
        return 2
    except Deferred as error:
        print(f"close: deferred, no GitHub call: {error}", file=sys.stderr)
        return DEFERRED
    except QuotaExhausted as error:
        return stop_on_quota(error)


if __name__ == "__main__":
    sys.exit(main())
