"""Ticket data for the Tickets dashboard (epic-tickets); the runners never read it.

The current board Status, plus the status history the collector records
(ticket_history.py) for time in status, agents and flow times.

Sources, each with its own health (`epic_ticket_source_ok`):
  board   the selector snapshot's board items (status, fields, readiness)
  github  the selector snapshot's open and merged PRs (linked PR, review)
  deps    REST `issues/{n}/dependencies/blocked_by`, Ready tickets only
  review  REST issue comments of Refinement tickets (body reviews)
  ledger  the status history files (time in status, flow times)
  local   runner ticks from the local loop (agent names)
  labels  REST issue events of every shown ticket: status label changes
          (set_status.py), merged into the ledger as recorded change times

deps, review and labels are read in a child process with conditional REST requests
(ETags under `runtime/ticket-cache`). A failed source makes its check and
notes unknown; it never turns into "no problem".
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from typing import Any

import next_action as epic
from ticket_history import CODES, LABEL_MARKER, LABEL_STATUSES, Summary
from ticks import Sink

Json = dict[str, Any]
REPO_URL = f"https://github.com/{epic.REPO}"
# Fixed order; zeros are published so a missing status never reads as 0.
STATUSES = ("Backlog", "Refinement", "Ready", "In progress", "In review", "Done")
LEVELS = ("Task", "Subtask")
EXECUTORS = ("Claude", "Codex", "Anton", "Unassigned")
MAX_OPEN = 100
MAX_DONE = 50
DONE_DAYS = 7
# done_sort of a Done ticket whose issue is still open: sorts before every
# closed one when the table sorts descending.
OPEN_DONE_SORT = 9_999_999_999
SOURCES = ("board", "github", "deps", "review", "ledger", "local", "labels")
# Which not-Done tickets fill MAX_OPEN first.
CAP_ORDER = ("In review", "In progress", "Ready", "Refinement", "Unknown", "Backlog")


HOUR = 3600


@dataclass(frozen=True)
class Check:
    id: str
    title: str
    # Severity when the check has members: 1 neutral, 2 amber, 3 red.
    severity: int
    source: str
    # (seconds, severity): a member longer than that in its status (strictly
    # more) raises the severity. Needs the ledger; without it, no raise.
    steps: tuple[tuple[int, int], ...] = ()
    # Time-based membership: status and minimum time in it (strictly more).
    status: str = ""
    longer: int = 0


CHECKS = (
    Check("ready_undeclared", "Ready · dependency not declared", 3, "deps"),
    Check("ready_claimable", "Ready · may be claimed", 1, "claims", ((24 * HOUR, 2),)),
    Check(
        "in_progress_long",
        "In progress > 1 day",
        2,
        "ledger",
        ((48 * HOUR, 3),),
        "In progress",
        24 * HOUR,
    ),
    Check(
        "in_review_long", "In review > 1 day", 3, "ledger", (), "In review", 24 * HOUR
    ),
    Check(
        "refinement_unreviewed",
        "Refinement · body not reviewed",
        1,
        "review",
        ((72 * HOUR, 2),),
    ),
    Check("done_open", "Board Done · issue open", 2, "board"),
    Check("label_out_of_sync", "Status label out of sync", 2, "board"),
)

# A blocked-by issue, in this repository or another one.
DEPENDENCY_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/issues/[1-9][0-9]*")


class Malformed(ValueError):
    """A REST row without the fields a check needs."""


# The whole comment body, exactly one line (no trailing newline).
BODY_REVIEW = re.compile(r"Body review: (APPROVED|CHANGES REQUESTED) ([0-9a-f]{12})")


def body_digest(body: str) -> str:
    """First 12 hex characters of SHA-256 of the raw issue body.

    No normalization: any edit of the body needs a new review. Not
    next_action.body_digest(), which normalizes for adopt.
    """
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]


def body_review(rows: list[Json]) -> Json | None:
    """The newest valid body review in REST comment rows, or None.

    Valid: unedited (updated_at == created_at) and the whole body matches.
    A row without an id, a body or both timestamps raises Malformed: a
    newer comment that cannot be read must never let an older approval count.
    """
    found = []
    for row in rows:
        body, created, updated = (
            row.get(k) for k in ("body", "created_at", "updated_at")
        )
        if (
            type(row.get("id")) is not int
            or not isinstance(body, str)
            or parse_time(created) is None
            or parse_time(updated) is None
        ):
            raise Malformed("comment without id, body or timestamps")
        match = BODY_REVIEW.fullmatch(body)
        if match and updated == created:
            found.append((parse_time(created), row["id"], match))
    if not found:
        return None
    *_, match = max(found, key=lambda entry: entry[:2])
    return {"state": match[1], "digest": match[2]}


def parse_time(text: object) -> float | None:
    if not isinstance(text, str):
        return None
    try:
        at = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return at.timestamp() if at.tzinfo else None


@dataclass
class Ticket:
    number: int
    item: Json
    status: str
    open: bool
    closed_at: float | None

    @property
    def url(self) -> str:
        return epic.issue_url(self.number)


@dataclass
class Population:
    tickets: list[Ticket] = field(default_factory=list)
    dropped: int = 0
    # Every Task and Subtask on the board by status, before the caps.
    counts: dict[str, int] = field(default_factory=dict)


def board_tickets(state: Json, now: float) -> Population:
    """Task and Subtask issues of this repository on the board.

    Not Done: most advanced status first (CAP_ORDER), then lowest number, at
    most MAX_OPEN; so a long Backlog never pushes out active work. Done: issue still
    open or closed in the last DONE_DAYS, open ones first, then the newest
    closed, at most MAX_DONE.

    A ticket without the fields its checks need raises Malformed, so the
    board source is unknown and nothing is published from a guess: the issue
    state, the closure time of a closed Done ticket, and the body of a
    Refinement ticket (REST null is a confirmed empty body; no key is not).
    """
    counts: dict[str, int] = dict.fromkeys((*STATUSES, "Unknown"), 0)
    active: list[Ticket] = []
    done: list[Ticket] = []
    since = now - DONE_DAYS * 86400
    for number, item in sorted(epic.board_issues(state).items()):
        if item.get("level") not in LEVELS:
            continue
        raw = item.get("status")
        status = raw if isinstance(raw, str) and raw in STATUSES else "Unknown"
        counts[status] += 1
        content = item.get("content") or {}
        if content.get("state") not in ("open", "closed"):
            raise Malformed(f"issue {number} has no state")
        is_open = content["state"] == "open"
        closed_at = parse_time(content.get("closed_at"))
        if status == "Done" and not is_open and closed_at is None:
            raise Malformed(f"closed Done issue {number} has no closed_at")
        if status == "Refinement" and not (
            "body" in content and isinstance(content["body"], str | None)
        ):
            raise Malformed(f"Refinement issue {number} has no body field")
        ticket = Ticket(number, item, status, is_open, closed_at)
        if status != "Done":
            active.append(ticket)
        elif ticket.open or (
            ticket.closed_at is not None and ticket.closed_at >= since
        ):
            done.append(ticket)
    active.sort(key=lambda t: (CAP_ORDER.index(t.status), t.number))
    done.sort(key=lambda t: (not t.open, -(t.closed_at or 0), t.number))
    return Population(
        active[:MAX_OPEN] + done[:MAX_DONE],
        max(len(active) - MAX_OPEN, 0) + max(len(done) - MAX_DONE, 0),
        counts,
    )


def claimable(state: Json, focus_file: Path = epic.FOCUS_FILE) -> set[int] | None:
    """Issues the selector would offer as a claim to either runner kind.

    Uses the runners' focus file, as they do. A pause is ignored: the check
    shows what a running loop would claim. None when the focus file cannot
    be read.
    """
    try:
        focus = epic.read_focus(focus_file)
    except (OSError, UnicodeError):
        return None
    found: set[int] = set()
    for agent in epic.AGENTS:
        for action in epic.decide(
            agent,
            state,
            include_waiting=True,
            focus=focus,
            completed_tickets=bool(state.get("completed_tickets")),
            free_claims=epic.shared_reader(),
        ):
            match = epic.ISSUE_URL.fullmatch(action.issue or "")
            if action.action == "claim" and match:
                found.add(int(match[1]))
    return found


def linked_prs(state: Json) -> dict[int, tuple[int, str, Json]]:
    """Per issue: (PR number, "open" | "draft" | "merged", PR) of the PR that
    names it in its `Issue:` line. An open PR wins over a merged one; then
    the newest."""
    found: dict[int, tuple[int, str, Json]] = {}
    rows = [
        *(("merged", pr) for pr in state.get("merged_prs") or []),
        *(
            ("draft" if pr.get("isDraft") else "open", pr)
            for pr in state.get("prs") or []
        ),
    ]
    for kind, pr in rows:
        number = pr.get("number")
        if type(number) is not int:
            continue
        for issue in epic.issue_numbers(pr.get("body") or ""):
            old = found.get(issue)
            rank = (kind != "merged", number)
            if old is None or rank > (old[1] != "merged", old[0]):
                found[issue] = (number, kind, pr)
    return found


def reviewer_kind(pr: Json) -> str:
    line = epic.REVIEWER_LINE.search(pr.get("body") or "")
    if line:
        return line[1].lower()
    author = epic.pr_author(pr)
    return epic.other(author).lower() if author else "unknown"


def dependency_label(dep: str) -> str:
    match = epic.ISSUE_URL.fullmatch(dep)
    return match[1] if match else dep


@dataclass
class Extra:
    """Ticket-only reads; None when the source failed."""

    # Ready issue -> URLs of its open blocked-by issues.
    deps: dict[int, list[str]] | None
    # Refinement issue -> newest valid body review (or None).
    reviews: dict[int, Json | None] | None
    # Shown issue -> its status label events (status_events rows).
    events: dict[int, list[Json]] | None = None


def ticket_metrics(
    metrics: Sink,
    state: Json,
    extra: Extra,
    claims: set[int] | None,
    now: float,
    history: Summary | None = None,
) -> None:
    """One info row per ticket, check members and counts, status counts.

    `history` is None when the ledger cannot be read: the time-based checks
    are unknown then and no severity is raised by time.
    """
    population = board_tickets(state, now)
    metrics.add("ticket_snapshot_timestamp_seconds", now)
    for status, count in population.counts.items():
        metrics.add("tickets_by_status", count, status=status)
    metrics.add("tickets_dropped", population.dropped)
    prs = linked_prs(state)
    members: dict[str, list[int]] = {check.id: [] for check in CHECKS}
    ages: dict[int, float] = {}
    for ticket in population.tickets:
        note, checks = ticket_note(ticket, extra, claims, prs.get(ticket.number))
        timed = history.tickets.get(ticket.number) if history else None
        if timed is not None and timed.status == ticket.status:
            # Since the entry into the status; a lower bound when seeded.
            ages[ticket.number] = max(0.0, now - timed.entered)
        for check in CHECKS:
            if (
                check.status == ticket.status
                and ages.get(ticket.number, 0) > check.longer
            ):
                checks.append(check.id)
        for check in checks:
            members[check].append(ticket.number)
        metrics.add(
            "ticket_status_code", CODES[ticket.status], issue=str(ticket.number)
        )
        item = ticket.item
        executor = item.get("executor") or "Unassigned"
        pr = prs.get(ticket.number)
        done_sort = ""
        if ticket.status == "Done":
            done_sort = str(
                OPEN_DONE_SORT if ticket.open else int(ticket.closed_at or 0)
            )
        metrics.add(
            "ticket_info",
            1,
            issue=str(ticket.number),
            title=str((item.get("content") or {}).get("title") or ""),
            url=ticket.url,
            area=str(item.get("area") or ""),
            level=str(item.get("level") or ""),
            executor=executor if executor in EXECUTORS else "Unknown",
            status=ticket.status,
            note=note,
            pr=str(pr[0]) if pr else "",
            done_sort=done_sort,
            last_agent=timed.agent if timed else "",
            ready_entered=str(timed.ready_entered or "") if timed else "",
        )
    healthy = {
        "board": True,
        "claims": claims is not None,
        "deps": extra.deps is not None,
        "review": extra.reviews is not None,
        "ledger": history is not None,
    }
    for check in CHECKS:
        if not healthy[check.source]:
            continue  # unknown: no rows, so the tile shows grey, never 0
        for number in members[check.id]:
            metrics.add("ticket_check_member", 1, issue=str(number), check=check.id)
        count = len(members[check.id])
        severity = check.severity if count else 0
        worst = max((ages.get(n, 0) for n in members[check.id]), default=0)
        for seconds, raised in check.steps:
            if count and worst > seconds:
                severity = max(severity, raised)
        metrics.add("ticket_check_count", count, check=check.id)
        metrics.add("ticket_check_severity", severity, check=check.id)


def declared_dependencies(item: Json) -> set[str]:
    """What the readiness comments' "Start after" lines name: the runner's
    leaves and ticket URLs, plus issues in other repositories. Blocked-by
    issues can live in other repositories; the runner rules stay as they are.
    """
    foreign = {
        url
        for clause in epic.START_AFTER.findall(item.get("readiness") or "")
        for url in DEPENDENCY_URL.findall(clause)
    }
    return epic.start_after(item) | foreign


def ticket_note(
    ticket: Ticket,
    extra: Extra,
    claims: set[int] | None,
    pr: tuple[int, str, Json] | None,
) -> tuple[str, list[str]]:
    """The note column and the checks this ticket is in.

    Notes are built only from validated fields; never comment or runner text.
    """
    status, number = ticket.status, ticket.number
    checks: list[str] = []
    note = ""
    if status == "Ready":
        declared = declared_dependencies(ticket.item)
        open_deps = None if extra.deps is None else extra.deps.get(number)
        hidden = sorted(dep for dep in open_deps or [] if dep not in declared)
        if hidden:
            checks.append("ready_undeclared")
            more = f" (+{len(hidden) - 1} more)" if len(hidden) > 1 else ""
            note = f"Needs {hidden[0]} (open) · not declared{more}"
        if claims is not None and number in claims:
            checks.append("ready_claimable")
            note = note or "May be claimed"
        if not note and declared:
            note = "Start after " + ", ".join(
                sorted(dependency_label(dep) for dep in declared)
            )
        if not note and open_deps is None:
            note = "Dependencies unknown"
    elif status == "Refinement":
        if extra.reviews is None:
            note = "Body review unknown"
        else:
            review = extra.reviews.get(number)
            body = (ticket.item.get("content") or {}).get("body") or ""
            if review is None or review["state"] != "APPROVED":
                note = "Body not reviewed"
                checks.append("refinement_unreviewed")
            elif review["digest"] != body_digest(body):
                note = "Body changed since review"
                checks.append("refinement_unreviewed")
            else:
                note = "Body reviewed"
    elif status == "In progress":
        if pr is None or pr[1] == "merged":
            note = "No PR yet"
        elif pr[1] == "draft":
            note = f"PR {pr[0]} (draft)"
        else:
            note = f"PR {pr[0]} · review by {reviewer_kind(pr[2])}"
    elif status == "In review":
        if pr is None:
            note = "No PR yet"
        elif pr[1] == "merged":
            note = f"PR {pr[0]} · merged"
        elif pr[1] == "draft":
            note = f"PR {pr[0]} (draft)"
        else:
            note = f"PR {pr[0]} · review by {reviewer_kind(pr[2])}"
    elif status == "Done":
        if ticket.open:
            note = "Board Done · issue open"
            checks.append("done_open")
        elif pr is not None and pr[1] == "merged":
            note = "Merged"
    label = label_note(ticket)
    if label:
        checks.append("label_out_of_sync")
        note = f"{note} · {label}" if note else label
    return note, checks


def label_note(ticket: Ticket) -> str:
    """Why the status labels do not match the board Status; "" when they do.

    Read from the same board snapshot as the Status (the board's Labels
    field). A ticket without a known Status or without the labels field is
    not judged.
    """
    if ticket.status not in STATUSES or not isinstance(ticket.item.get("labels"), list):
        return ""
    names = epic.labels(ticket.item)
    if LABEL_MARKER in names:
        return "Sync not finished"
    found = sorted(LABEL_STATUSES[name] for name in names if name in LABEL_STATUSES)
    if not found:
        return "No status label"
    if len(found) > 1:
        return "Labels " + ", ".join(found)
    if found[0] != ticket.status:
        return f"Label {found[0]}, board {ticket.status}"
    return ""


def source_metrics(metrics: Sink, ok: dict[str, bool], now: float) -> None:
    metrics.add("ticket_attempt_timestamp_seconds", now)
    for source in SOURCES:
        metrics.add("ticket_source_ok", int(ok.get(source, False)), source=source)


def wanted(state: Json, now: float) -> Json:
    """Issue numbers the child process reads: Ready and Refinement tickets,
    and the label events of every shown ticket."""
    tickets = board_tickets(state, now).tickets
    return {
        "ready": [t.number for t in tickets if t.status == "Ready"],
        "refinement": [t.number for t in tickets if t.status == "Refinement"],
        "events": [t.number for t in tickets],
    }


EVENT_KINDS = ("labeled", "unlabeled")


def status_event(row: Json) -> Json | None:
    """The fields of a status label event, None for any other event.

    A label event without an id or a readable time raises Malformed: it
    could be a change, so the whole source is unknown.
    """
    if row.get("event") not in EVENT_KINDS:
        return None
    name = (row.get("label") or {}).get("name")
    if not isinstance(name, str) or not name.startswith("status::"):
        return None
    if type(row.get("id")) is not int or parse_time(row.get("created_at")) is None:
        raise Malformed("status label event without id or time")
    return {
        "id": row["id"],
        "event": row["event"],
        "label": {"name": name},
        "created_at": row["created_at"],
    }


def open_dependencies(rows: list[Json]) -> list[str]:
    """URLs of the open blocked-by issues. A row without a known state or a
    valid issue URL raises Malformed: the source is unknown, never empty."""
    found = []
    for row in rows:
        state, url = row.get("state"), row.get("html_url")
        if state not in ("open", "closed") or not (
            isinstance(url, str) and DEPENDENCY_URL.fullmatch(url)
        ):
            raise Malformed("blocked-by row without state or issue URL")
        if state == "open":
            found.append(url)
    return sorted(found)


def fetch_extra(
    request: Json,
    pages: Callable[[str], list[Json]],
    blocked: tuple[type[BaseException], ...],
) -> Json:
    """Read blocked-by issues and body reviews; each source fails alone.

    `pages` returns every row of a REST list or raises one of `blocked`.
    """
    deps: dict[str, list[str]] | None
    try:
        deps = {
            str(number): open_dependencies(
                pages(
                    f"repos/{epic.REPO}/issues/{number}"
                    "/dependencies/blocked_by?per_page=100"
                )
            )
            for number in request["ready"]
        }
    except (*blocked, Malformed):
        deps = None
    reviews: dict[str, Json | None] | None
    try:
        reviews = {
            str(number): body_review(
                pages(f"repos/{epic.REPO}/issues/{number}/comments?per_page=100")
            )
            for number in request["refinement"]
        }
    except (*blocked, Malformed):
        reviews = None
    events: dict[str, list[Json]] | None
    try:
        # All pages of every issue, or nothing: a partial read adds nothing.
        events = {
            str(number): [
                found
                for row in pages(
                    f"repos/{epic.REPO}/issues/{number}/events?per_page=100"
                )
                if (found := status_event(row)) is not None
            ]
            for number in request.get("events", [])
        }
    except (*blocked, Malformed):
        events = None
    return {"deps": deps, "reviews": reviews, "events": events}


def valid_extra(data: object, request: Json) -> Extra:
    """Check the child's answer; a malformed source becomes unknown."""
    if not isinstance(data, dict):
        return Extra(None, None)
    deps = data.get("deps")
    good_deps: dict[int, list[str]] | None = None
    if isinstance(deps, dict) and set(deps) == {str(n) for n in request["ready"]}:
        if all(
            isinstance(urls, list)
            and all(isinstance(u, str) and DEPENDENCY_URL.fullmatch(u) for u in urls)
            for urls in deps.values()
        ):
            good_deps = {int(n): urls for n, urls in deps.items()}
    reviews = data.get("reviews")
    good_reviews: dict[int, Json | None] | None = None
    if isinstance(reviews, dict) and set(reviews) == {
        str(n) for n in request["refinement"]
    }:
        if all(
            review is None
            or (
                isinstance(review, dict)
                and review.get("state") in ("APPROVED", "CHANGES REQUESTED")
                and isinstance(review.get("digest"), str)
                and re.fullmatch(r"[0-9a-f]{12}", review["digest"])
            )
            for review in reviews.values()
        ):
            good_reviews = {int(n): review for n, review in reviews.items()}
    events = data.get("events")
    good_events: dict[int, list[Json]] | None = None
    if isinstance(events, dict) and set(events) == {
        str(n) for n in request.get("events", [])
    }:
        try:
            good_events = {
                int(n): [valid_event(row) for row in rows] for n, rows in events.items()
            }
        except (Malformed, AttributeError, TypeError):
            good_events = None
    return Extra(good_deps, good_reviews, good_events)


def valid_event(row: object) -> Json:
    if not isinstance(row, dict):
        raise Malformed("event row is not an object")
    found = status_event(row)
    if found is None or found != row:
        raise Malformed("not a status label event row")
    return found


def main_fetch(cache: Path, request: Json, seconds: float) -> Json:
    """Run in a child process by the monitor, with a timeout."""
    import github_state

    ns = github_state.Namespace(cache, {"purpose": "tickets", "repo": epic.REPO})
    client = github_state.Client(ns, "tickets", seconds, writable=True)
    return fetch_extra(
        request,
        client.pages,
        (github_state.ReadBlocked, github_state.Gone),
    )


def numbers(values: object) -> list[int]:
    if not isinstance(values, list) or any(
        type(value) is not int or value <= 0 for value in values
    ):
        raise ValueError("issue numbers must be a list of positive integers")
    return values


def read_request(text: str) -> Json:
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("request must be an object")
    return {
        key: numbers(data.get(key) or []) for key in ("ready", "refinement", "events")
    }
