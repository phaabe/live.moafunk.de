"""Close tickets whose implementation merged. No stored state.

The Claude runner runs this once per tick, before the selector
(scripts/epic/claude-tick.sh). It also works by hand; `--dry-run` only prints
what it would do.

  1. List PRs merged into an epic base in the last WINDOW_DAYS (REST).
  2. Take each PR's one `Issue:` line. Skip `(partial)`, the epic and PRs
     without exactly one `Issue:` line.
  3. For each named ticket that is still open, skip it when it has
     sub-issues (umbrella), an open PR names it, or it was reopened after
     the newest of its merges. A ticket with unchecked leaves
     (`- [ ] **X1.2.3**`) that no merged PR (of any age, for any ticket)
     lists in `Leaf IDs:` stays open and gets one note.
  4. Otherwise post one evidence comment and close it as completed.

Every comment ends with a hidden marker naming the PR. The ticket's comments
are searched for it first, so a rerun never posts twice; a run that posted
the comment but failed to close simply closes on the next tick. A reopened
ticket stays open until a newer PR for it merges.

The reopen check, marker read and writes for a ticket run under its target
lock (target_lock.py), shared by all runners on this machine. Two runs at once
therefore never both read "no marker" and post twice. A busy lock skips the
ticket; the next tick tries again.

The pause file and a stored quota wait are read before every GitHub call;
either one ends the run with no call.

Exit 0 done, 1 a GitHub call failed (logged; the next tick tries again),
2 bad usage, 3 deferred by the pause file or a quota wait.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import Any

import github_quota
import next_action
import target_lock
from github_quota import QuotaExhausted, parse_iso, run_gh
from next_action import BASES, EPIC, LEAF, REPO, issue_url

WINDOW_DAYS = 14
DEFERRED = 3
ANY_ISSUE_LINE = re.compile(r"^Issue:[ \t]*(.*?)[ \t]*$", re.MULTILINE)
ISSUE_TARGET = re.compile(rf"https://github\.com/{re.escape(REPO)}/issues/(\d+)")
UNCHECKED_LEAF = re.compile(r"- \[ \] \*\*([A-Z][0-9]+\.[0-9]+\.[0-9]+)\*\*")
VALIDATION = re.compile(r"^Validation:[ \t]*(.+)$", re.MULTILINE)
MARKER = "<!-- epic-close-merged {kind} pr={pr} -->"
MARKERS = re.compile(r"<!-- epic-close-merged (close|note) pr=(\d+) -->")
TESTS_MAX = 600
PER_PAGE = 100
# gh --jq filters. Each prints one JSON value per line (strings via tojson).
# Every closed PR, merged or not, so a short page shows the list ended.
CLOSED_JQ = (
    ".[] | {number, body, merged_at, updated_at, merge_commit_sha, base: .base.ref}"
)
LEAVES_JQ = (
    '.[] | select(.merged_at != null) | .body // ""'
    ' | [scan("(?m)^Leaf IDs:.*$")] | .[] | tojson'
)
OPEN_PR_JQ = '.[] | .body // "" | [scan("(?m)^Issue:.*$")] | .[] | tojson'
OPEN_ISSUES_JQ = (
    ".[] | select(.pull_request == null) | {number, body,"
    " sub_issues: (.sub_issues_summary.total // 0)}"
)
REOPENED_JQ = '.[] | select(.event == "reopened") | .created_at | tojson'
MARKERS_JQ = (
    '.[] | .body // "" | [scan("<!-- epic-close-merged (?:close|note) pr=[0-9]+ -->")]'
    " | .[] | tojson"
)

Gh = Callable[[list[str]], str]


class Deferred(Exception):
    """The pause file or a stored quota wait: no GitHub call now."""


def blocked() -> str | None:
    """Why no GitHub call may run now, or None."""
    if next_action.PAUSE_FILE.exists():
        return f"pause file {next_action.PAUSE_FILE} exists"
    try:
        result, retry_at = github_quota.check(github_quota.STATE_DIR, time.time())
    except (OSError, ValueError) as error:
        return f"quota wait file unreadable: {error}"
    if result == github_quota.DEFERRED:
        return f"GitHub quota wait until {retry_at}"
    return None


def guarded(gh: Gh) -> Gh:
    def call(args: list[str]) -> str:
        reason = blocked()
        if reason:
            raise Deferred(reason)
        return gh(args)

    return call


def lines(gh: Gh, args: list[str]) -> list[Any]:
    """`gh api --paginate --jq` output: one JSON value per line."""
    return [json.loads(line) for line in gh(args).splitlines() if line.strip()]


def pr_url(number: int) -> str:
    return f"https://github.com/{REPO}/pull/{number}"


def ticket_of(body: str) -> int | None:
    """The one ticket a PR implements, or None (partial, epic, none, several)."""
    found = ANY_ISSUE_LINE.findall(body or "")
    if len(found) != 1:
        return None
    match = ISSUE_TARGET.fullmatch(found[0])
    if match is None or int(match.group(1)) == EPIC:
        return None
    return int(match.group(1))


def merged_prs(gh: Gh, now: float) -> list[dict[str, Any]]:
    """PRs merged into an epic base within the window, newest merge first.

    Pages are sorted by update time, newest first, and a merge updates the
    PR. Paging stops at a short page or at a page that reaches an update
    before the window: every later PR was last updated, so merged, before it.
    """
    since = now - WINDOW_DAYS * 86400
    prs = []
    for base in BASES:
        page = 1
        while True:
            found = lines(
                gh,
                [
                    "api",
                    f"repos/{REPO}/pulls?state=closed&base={base}"
                    f"&sort=updated&direction=desc&per_page={PER_PAGE}&page={page}",
                    "--jq",
                    CLOSED_JQ,
                ],
            )
            prs += [p for p in found if p["merged_at"]]
            if len(found) < PER_PAGE or parse_iso(found[-1]["updated_at"]) < since:
                break
            page += 1
    recent = [p for p in prs if parse_iso(p["merged_at"]) >= since]
    return sorted(recent, key=lambda p: p["merged_at"], reverse=True)


def listed_leaves(gh: Gh) -> set[str]:
    """Leaves any merged epic PR lists in `Leaf IDs:`, however old.

    A leaf is often listed by a PR that names another ticket (or the epic).
    """
    listed: set[str] = set()
    for base in BASES:
        for line in lines(
            gh,
            [
                "api",
                "--paginate",
                f"repos/{REPO}/pulls?state=closed&base={base}&per_page=100",
                "--jq",
                LEAVES_JQ,
            ],
        ):
            listed |= set(LEAF.findall(line))
    return listed


def open_pr_tickets(gh: Gh) -> set[int]:
    """Tickets an open PR names in an `Issue:` line, partial or not."""
    found = lines(
        gh,
        [
            "api",
            "--paginate",
            f"repos/{REPO}/pulls?state=open&per_page=100",
            "--jq",
            OPEN_PR_JQ,
        ],
    )
    return {int(m.group(1)) for line in found if (m := ISSUE_TARGET.search(line))}


def open_tickets(gh: Gh) -> dict[int, dict[str, Any]]:
    found = lines(
        gh,
        [
            "api",
            "--paginate",
            f"repos/{REPO}/issues?state=open&per_page=100",
            "--jq",
            OPEN_ISSUES_JQ,
        ],
    )
    return {t["number"]: t for t in found}


def reopened_since(gh: Gh, number: int, merged_at: str) -> bool:
    times = lines(
        gh,
        [
            "api",
            "--paginate",
            f"repos/{REPO}/issues/{number}/events?per_page=100",
            "--jq",
            REOPENED_JQ,
        ],
    )
    merged = parse_iso(merged_at)
    return any(parse_iso(t) > merged for t in times)


def markers(gh: Gh, number: int) -> set[tuple[str, int]]:
    found = lines(
        gh,
        [
            "api",
            "--paginate",
            f"repos/{REPO}/issues/{number}/comments?per_page=100",
            "--jq",
            MARKERS_JQ,
        ],
    )
    return {(m.group(1), int(m.group(2))) for t in found if (m := MARKERS.search(t))}


@contextmanager
def ticket_lock(number: int) -> Generator[bool]:
    """Hold the ticket's target lock (target_lock.py) without waiting.

    Yields False when another runner holds it. Closing the file frees it.
    """
    root = target_lock.lock_dir()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(root / f"{number}.lock", "a") as handle:
        yield target_lock.acquire([handle.fileno()])


def uncovered(ticket_body: str, listed: set[str]) -> list[str]:
    """Unchecked leaves of the ticket that no merged PR lists in `Leaf IDs:`."""
    return sorted(set(UNCHECKED_LEAF.findall(ticket_body or "")) - listed)


def tests_text(body: str) -> str:
    match = VALIDATION.search(body or "")
    text = " ".join(match.group(1).split()) if match else ""
    if not text:
        return "see the PR"
    return text if len(text) <= TESTS_MAX else text[: TESTS_MAX - 1] + "…"


def close_comment(pr: dict[str, Any]) -> str:
    return (
        "Closed: the implementation is merged.\n\n"
        f"- PR: {pr_url(pr['number'])}\n"
        f"- Merge commit: `{pr['merge_commit_sha']}` into `{pr['base']}`\n"
        f"- Tests: {tests_text(pr.get('body') or '')}\n\n"
        + MARKER.format(kind="close", pr=pr["number"])
    )


def note_comment(pr: dict[str, Any], leaves: list[str]) -> str:
    return (
        f"Kept open: {pr_url(pr['number'])} merged, but no merged PR lists these"
        f" leaves in `Leaf IDs:`: {', '.join(leaves)}.\n\n"
        + MARKER.format(kind="note", pr=pr["number"])
    )


def comment(gh: Gh, number: int, body: str) -> None:
    gh(
        [
            "api",
            "-X",
            "POST",
            f"repos/{REPO}/issues/{number}/comments",
            "-f",
            f"body={body}",
        ]
    )


def close(gh: Gh, number: int) -> None:
    gh(
        [
            "api",
            "-X",
            "PATCH",
            f"repos/{REPO}/issues/{number}",
            "-f",
            "state=closed",
            "-f",
            "state_reason=completed",
        ]
    )


def settle(
    gh: Gh,
    ticket: dict[str, Any],
    newest: dict[str, Any],
    listed: set[str],
    dry_run: bool,
    log: Callable[[str], None],
) -> None:
    """Note or close one ticket. The caller holds the ticket's lock."""
    number = ticket["number"]
    url = issue_url(number)
    if reopened_since(gh, number, newest["merged_at"]):
        log(
            f"close: {url} was reopened after {pr_url(newest['number'])} merged; left open"
        )
        return
    leaves = uncovered(ticket.get("body") or "", listed)
    done = markers(gh, number)
    if leaves:
        if ("note", newest["number"]) in done:
            return
        log(f"close: {url} keeps open leaves {', '.join(leaves)}; noting it")
        if not dry_run:
            comment(gh, number, note_comment(newest, leaves))
        return
    log(f"close: {url} done by {pr_url(newest['number'])}; closing")
    if dry_run:
        return
    if ("close", newest["number"]) not in done:
        comment(gh, number, close_comment(newest))
    close(gh, number)


def run(
    gh: Gh, now: float, dry_run: bool = False, log: Callable[[str], None] = print
) -> int:
    """Raises Deferred when the pause file or a quota wait stops the run."""
    prs = merged_prs(gh, now)
    by_ticket: dict[int, list[dict[str, Any]]] = {}
    for pr in prs:
        ticket = ticket_of(pr.get("body") or "")
        if ticket is not None:
            by_ticket.setdefault(ticket, []).append(pr)
    if not by_ticket:
        return 0
    tickets = open_tickets(gh)
    if not tickets.keys() & by_ticket.keys():
        return 0
    in_review = open_pr_tickets(gh)
    listed: set[str] | None = None
    failed = False
    for number, its_prs in sorted(by_ticket.items()):
        ticket = tickets.get(number)
        if ticket is None:
            continue  # closed already
        newest = its_prs[0]
        url = issue_url(number)
        try:
            if ticket["sub_issues"]:
                log(f"close: {url} has sub-issues; left open")
                continue
            if number in in_review:
                continue  # an open PR still works on it
            if UNCHECKED_LEAF.search(ticket.get("body") or "") and listed is None:
                listed = listed_leaves(gh)
            with ticket_lock(number) as locked:
                if not locked:
                    log(f"close: {url} is locked by another runner; next tick")
                    continue
                settle(gh, ticket, newest, listed or set(), dry_run, log)
        except subprocess.SubprocessError as error:
            failed = True
            detail = getattr(error, "stderr", "") or error
            log(f"close: {url}: GitHub call failed: {str(detail).strip()}")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        return run(guarded(run_gh), time.time(), args.dry_run)
    except Deferred as reason:
        print(f"close: deferred: {reason}")
        return DEFERRED
    except QuotaExhausted as error:
        print(f"close: GitHub quota: {error}")
        return DEFERRED
    except (subprocess.SubprocessError, OSError, ValueError, KeyError) as error:
        detail = getattr(error, "stderr", "") or error
        print(f"close: failed: {str(detail).strip()}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
