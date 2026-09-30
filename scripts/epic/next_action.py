"""Pick the next action for one agent on the architecture epic.

Read-only: it reads GitHub through `gh` and prints one JSON action. The agent
does the action; this script never writes to GitHub. All state comes from
GitHub (verdict lines keyed by head SHA, project Status and Executor), so a
restarted agent picks up where it stopped.

Priority (first match wins), see docs/implementation/epic-rules.md section 8:
  stop      pause file exists

Focus: when ~/.epic-focus lists labels (one per line, e.g. project::Stream),
only issues with one of them, and PRs whose own labels or `Issue:` ticket have
one, get actions. Everything else is frozen. No file or an empty file: all.
  escalate  my PR reached MAX_ROUNDS changes-requested verdicts
  merge     my PR is approved for its head, checks green, no conflict
  fix       my PR has a changes-requested verdict for its head
  fix-checks  my PR has failing checks on its head
  resolve-conflict  my PR conflicts with its base
  review    the other agent's ready PR has no verdict from me for its head
  continue  my draft PR, or my In progress leaf without a PR
  adopt     a focus PR with no owner line whose files route to me (routing.py);
            only when EPIC_FOCUS_ACTIONS lists `adopt`
  claim     a Ready leaf with Executor = me, after its "Start after" leaves
            or tickets and the leaves before it in the epic's batch order
  idle      nothing to do

Inside each action the order is priority, then age: `priority::high`, then
`priority::medium` (also no label), then `priority::low`; then the lowest PR or
issue number (claims: lowest wave before the number). A PR takes the highest
priority of its own labels and its `Issue:` tickets. Priority only orders; it
never makes a PR or issue eligible.

Usage:
  next_action.py --agent claude|codex        one JSON action
  next_action.py --agent ... --candidates    all actions, one JSON per line
  next_action.py --status                    all pending actions, both agents
  next_action.py ... --state-file state.json use saved state (tests, dry runs)
  next_action.py ... --focus project::Stream  override ~/.epic-focus (repeatable)

  next_action.py --agent ... --recheck action.json
                                             read the action's target fresh
                                             and check the selector still
                                             picks it (shared reader only)

Completed tickets (EPIC_REQUIRE_COMPLETED_TICKETS, off by default): with 1, a
ticket named in "Start after" counts as done only while its issue is closed as
completed. A merged PR or board Status Done is not enough. Closed issues get
no claim or continue action. Unset or 0 keeps the old rule; other values are
bad settings (exit 2).

Shared reader (github_state.py): with EPIC_SHARED_READER=1, fetch_state()
reads the shared REST snapshot instead of calling GitHub itself. Off by default
until both runners handle exit codes 5 and 6.

Exit codes: 0 action printed (or --recheck: still valid), 2 bad settings,
3 a GraphQL quota wait is stored (no GitHub call), 4 a read hit the GraphQL
quota (wait stored, no action), 5 the read is blocked (lock or refresh timeout,
failed or partial read, REST rate limit, suspected access loss; nothing on
stdout), 6 --recheck found the action stale. Only the runners translate 5 and 6.
See github_quota.py and github_state.py.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import quote

from github_quota import (
    DEFERRED,
    STATE_DIR,
    QuotaExhausted,
    check as quota_check,
    run_gh,
    stop_on_quota,
)

if TYPE_CHECKING:
    from routing import Route

REPO = "phaabe/live.moafunk.de"
PROJECT_OWNER = "anneoneone"
PROJECT_NUMBER = "2"
PROJECT_API = f"users/{PROJECT_OWNER}/projectsV2/{PROJECT_NUMBER}"
# Board fields that fetch_state() callers read (selector, monitor, delivery).
PROJECT_FIELDS = ("Status", "Wave", "Executor", "Labels", "Area", "Level")
BASES = ("dev/312-interim", "dev/streaming-architecture")
AGENTS = ("Claude", "Codex")
MAX_ROUNDS = 3
MAX_OPEN_PRS = 2
PAUSE_FILE = Path.home() / ".epic-pause"
FOCUS_FILE = Path.home() / ".epic-focus"
ESCALATION_LABEL = "needs-anton"
# New actions are emitted only when listed in EPIC_FOCUS_ACTIONS (comma list).
# One is added in both runners after both its Claude and Codex leaves merged.
NEW_ACTIONS = frozenset({"adopt"})
ACTIONS_ENV = "EPIC_FOCUS_ACTIONS"
# Ticket dependencies need a closed-as-completed issue (see completed_tickets()).
COMPLETED_TICKETS_ENV = "EPIC_REQUIRE_COMPLETED_TICKETS"
# A prerequisite read with this status blocks only its successors.
MISSING_STATUSES = frozenset({404, 410})
HTTP_STATUS = re.compile(r"\(HTTP (\d{3})\)")
# PR body lines `adopt` adds, at line start.
OWNER_KEYS = ("Epic", "Executor", "Lane", "Reviewer", "Leaf IDs", "Issue")
OWNER_KEY_LINE = re.compile(rf"^(?:{'|'.join(OWNER_KEYS)}):")
# Any owner line, with any value ("Executor: Anton", "Reviewer: TBD", "Executor: REPLACE").
ANY_OWNER_LINE = re.compile(r"^[ \t]*(?:Executor|Author|Reviewer):", re.MULTILINE)
PR_URL = re.compile(rf"https://github\.com/{re.escape(REPO)}/pull/(\d+)")
# Rank per priority label, lower first. No label counts as medium.
PRIORITIES = {"priority::high": 0, "priority::medium": 1, "priority::low": 2}
DEFAULT_PRIORITY = PRIORITIES["priority::medium"]
PRIORITY_NAMES = {rank: name.split("::")[1] for name, rank in PRIORITIES.items()}

VERDICT = re.compile(
    r"^Review: (APPROVED|CHANGES REQUESTED) by (Claude|Codex) at ([0-9a-f]{40})$"
)
EXECUTOR_LINE = re.compile(
    r"^(?:Executor|Author):[ \t]*(Claude|Codex)[ \t]*$", re.MULTILINE
)
REVIEWER_LINE = re.compile(r"\bReviewer:\s*(Claude|Codex)\b")
ISSUE_LINE = re.compile(
    rf"^Issue:[ \t]*https://github\.com/{re.escape(REPO)}/issues/(\d+)[ \t]*$",
    re.MULTILINE,
)
LEAF = re.compile(r"\b[A-Z][0-9]+\.[0-9]+\.[0-9]+\b")
# Readiness comments may chain leaves: "Start after B1.1.6 (same backend editor)."
# They may also name tickets by URL: "Start after https://github.com/.../issues/432."
# The clause ends at a sentence end (". " or end of line) or an opening "(".
START_AFTER = re.compile(r"Start after (.*?)(?:\.(?=\s|$)|\(|$)", re.MULTILINE)
LEAF_IDS_LINE = re.compile(r"^Leaf IDs:[ \t]*(.+)$", re.MULTILINE)
CHECKED_LEAF = re.compile(r"- \[[xX]\] \*\*([A-Z][0-9]+\.[0-9]+\.[0-9]+)\*\*")
# Batch tables on the epic: "| Codex | O1.2.4 (<url>) first; then P1 (<url>) ... |".
# A row is split into stages at "then" or an arrow; later stages wait for earlier leaves.
BATCH_ROW = re.compile(r"^\|[ \t]*(Claude|Codex)[ \t]*\|(.*)\|[ \t]*$", re.MULTILINE)
STAGE_SPLIT = re.compile(r"\bthen\b|→|->")
ISSUE_URL = re.compile(rf"https://github\.com/{re.escape(REPO)}/issues/(\d+)")
EPIC = 312
GREEN = {"SUCCESS", "NEUTRAL", "SKIPPED"}
# Merge gate: its success is required for "green". It reports waiting (draft,
# missing verdict, running checks) as pending and only real breaks as failure.
GUARD_CHECKS = {"epic-guard"}
# The guard's publisher job fails when any open PR is not ready, so it says
# nothing about this PR. The `epic-guard` status is the gate.
IGNORED_CHECKS = {"epic-guard-runner"}
FAILED = {
    "FAILURE",
    "ERROR",
    "CANCELLED",
    "TIMED_OUT",
    "ACTION_REQUIRED",
    "STARTUP_FAILURE",
}


@dataclass
class Action:
    action: str
    reason: str
    pr: int | None = None
    sha: str | None = None
    issue: str | None = None
    comments: list[str] = field(default_factory=list)
    # The target's `updated_at` at selection. The runner skips the action when
    # it changed before the target lock was taken (tick_gate.py check).
    updated_at: str | None = None
    # adopt only: the lane to write, and the hash of the body it must keep.
    lane: str | None = None
    body_sha: str | None = None
    # For --status only. Kept out of the JSON so runner fingerprints stay stable.
    priority: int = DEFAULT_PRIORITY
    # Board/issue mismatches of the tickets a claim waits for. Never a blocker.
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(
            {
                k: v
                for k, v in asdict(self).items()
                if v not in (None, []) and k not in ("priority", "warnings")
            }
        )


def other(agent: str) -> str:
    return AGENTS[1] if agent == AGENTS[0] else AGENTS[0]


def pr_author(pr: dict[str, Any]) -> str | None:
    """Both agents share one GitHub account, so the PR body names the author.

    Uses the `Executor:` line from the PR template (`Author:` is accepted too);
    falls back to the opposite of `Reviewer:`.
    """
    body = pr.get("body") or ""
    m = EXECUTOR_LINE.search(body)
    if m:
        return m.group(1)
    m = REVIEWER_LINE.search(body)
    return other(m.group(1)) if m else None


def body_digest(body: str) -> str:
    """SHA-256 of a PR body without the `adopt` lines, blank lines and line ends.

    `adopt` must keep the original body: this digest is equal before and after.
    """
    kept = [
        line.rstrip()
        for line in (body or "").splitlines()
        if line.strip() and not OWNER_KEY_LINE.match(line)
    ]
    return hashlib.sha256("\n".join(kept).encode()).hexdigest()


class SettingError(ValueError):
    """A bad environment setting; the script exits 2 before any GitHub read."""


def completed_tickets(env: dict[str, str] | None = None) -> bool:
    """EPIC_REQUIRE_COMPLETED_TICKETS: unset or 0 off, 1 on, anything else an error."""
    value = (os.environ if env is None else env).get(COMPLETED_TICKETS_ENV)
    if value is None or value == "0":
        return False
    if value == "1":
        return True
    raise SettingError(f"{COMPLETED_TICKETS_ENV} must be 0 or 1, not {value!r}")


def read_actions(value: str | None) -> frozenset[str]:
    """New actions enabled by EPIC_FOCUS_ACTIONS; unknown names are ignored."""
    names = {part.strip() for part in (value or "").split(",")}
    return frozenset(names & NEW_ACTIONS)


def verdicts(pr: dict[str, Any], by: str) -> list[dict[str, Any]]:
    """Valid verdicts from one agent, oldest first. Edited comments do not count."""
    found = []
    for c in pr.get("comments") or []:
        if c.get("includesCreatedEdit"):
            continue
        m = VERDICT.match((c.get("body") or "").strip())
        if m and m.group(2) == by:
            found.append(
                {
                    "state": m.group(1),
                    "sha": m.group(3),
                    "at": c["createdAt"],
                    "url": c.get("url"),
                }
            )
    return sorted(found, key=lambda v: v["at"])


def checks_state(pr: dict[str, Any]) -> str:
    """'green', 'failed' or 'pending' for the PR head.

    The epic guard reports waiting as pending, so a failed guard is a real
    break (lane, metadata, a failed check) and counts as failed. No merge until
    the guard is green. A missing guard status counts as pending: the publisher
    may not have run yet.
    """
    states = []
    guard_green = False
    for c in pr.get("statusCheckRollup") or []:
        name = c.get("name") or c.get("context")
        if name in IGNORED_CHECKS:
            continue
        state = (c.get("conclusion") or c.get("state") or c.get("status") or "").upper()
        if name in GUARD_CHECKS:
            guard_green = guard_green or state in GREEN
        states.append(state)
    if any(s in FAILED for s in states):
        return "failed"
    if guard_green and all(s in GREEN for s in states):
        return "green"
    return "pending"


def labels(item: dict[str, Any]) -> set[str]:
    names = set()
    for lbl in item.get("labels") or []:
        name = lbl.get("name") if isinstance(lbl, dict) else lbl
        if isinstance(name, str):
            names.add(name)
    return names


def priority_rank(names: set[str]) -> int:
    """Rank of the highest priority label; no label is medium."""
    return min(
        (PRIORITIES[n] for n in names if n in PRIORITIES), default=DEFAULT_PRIORITY
    )


def findings(
    pr: dict[str, Any], verdict: dict[str, Any], previous_at: str | None
) -> list[str]:
    """Comment URLs posted after the previous verdict, up to this one."""
    urls = []
    for c in pr.get("comments") or []:
        at = c["createdAt"]
        if (previous_at is None or at > previous_at) and at <= verdict["at"]:
            if c.get("url") and c.get("url") != verdict["url"]:
                urls.append(c["url"])
    return urls


def issue_numbers(text: str) -> set[int]:
    """Issues a PR implements: only line-start `Issue:` lines, not other links."""
    return {int(n) for n in ISSUE_LINE.findall(text or "")}


def issue_url(number: int) -> str:
    return f"https://github.com/{REPO}/issues/{number}"


def done_leaves(state: dict[str, Any], tickets: bool = True) -> set[str]:
    """Leaves listed by a merged PR's `Leaf IDs:` line or ticked in an issue body,
    plus (with `tickets`, the old rule) the URLs of tickets a merged PR names in
    its `Issue:` line."""
    done: set[str] = set()
    for pr in state.get("merged_prs", []):
        for line in LEAF_IDS_LINE.findall(pr.get("body") or ""):
            done |= set(LEAF.findall(line))
        if tickets:
            done |= {issue_url(n) for n in issue_numbers(pr.get("body") or "")}
    for item in state.get("items", []):
        done |= set(CHECKED_LEAF.findall((item.get("content") or {}).get("body") or ""))
    return done


def start_after(item: dict[str, Any]) -> set[str]:
    """Leaves and tickets a Ready issue must wait for, from its readiness comments."""
    wanted: set[str] = set()
    for clause in START_AFTER.findall(item.get("readiness") or ""):
        wanted |= set(LEAF.findall(clause))
        wanted |= {issue_url(int(n)) for n in ISSUE_URL.findall(clause)}
    return wanted


def is_closed(item: dict[str, Any]) -> bool:
    return (item.get("content") or {}).get("state") == "closed"


def ticket_from_issue(number: int, issue: dict[str, Any]) -> dict[str, Any]:
    """Completion fields of a REST issue read for ticket `number` of REPO.

    `gh api` follows a moved issue's redirect and returns 200 from the new
    repository. That issue is not this ticket: its state never counts.
    """
    source = str(issue.get("repository_url") or "").rsplit("/repos/", 1)[-1]
    if source.lower() != REPO.lower() or issue.get("number") != number:
        moved = f"{source}#{issue.get('number')}" if source else "unknown"
        return {"problem": f"moved to {moved}; update the dependency URL"}
    return {"state": issue.get("state"), "state_reason": issue.get("state_reason")}


def missing_ticket(status: int) -> dict[str, Any]:
    return {"problem": f"not readable (HTTP {status}); fix the dependency"}


def board_issues(state: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """This repository's issues on the board, by number."""
    return {
        i["content"]["number"]: i
        for i in state.get("items", [])
        if (i.get("content") or {}).get("type") == "Issue"
        and ISSUE_URL.fullmatch(i["content"].get("url") or "")
    }


def ticket_prerequisites(items: list[dict[str, Any]]) -> set[int]:
    """Tickets named in the "Start after" lines of Ready board issues."""
    return {
        int(ISSUE_URL.fullmatch(url).group(1))  # type: ignore[union-attr]
        for item in items
        if item.get("status") == "Ready"
        for url in start_after(item)
        if ISSUE_URL.fullmatch(url)
    }


def read_tickets(
    items: list[dict[str, Any]],
    read: Callable[[int], dict[str, Any]],
    extra: set[int] = frozenset(),  # type: ignore[assignment]
) -> dict[str, dict[str, Any]]:
    """Completion fields of each prerequisite ticket, keyed by number.

    `extra` adds tickets outside "Start after" lines (Waiting comments).
    Board data is reused when it has the issue state; any other ticket (off the
    board, archived, moved) is read with `read`, once per snapshot.
    """
    board = board_issues({"items": items})
    found: dict[str, dict[str, Any]] = {}
    for n in sorted(ticket_prerequisites(items) | set(extra)):
        content = (board.get(n) or {}).get("content") or {}
        if content.get("state") in ("open", "closed"):
            found[str(n)] = {
                "state": content["state"],
                "state_reason": content.get("state_reason"),
            }
        else:
            found[str(n)] = read(n)
    return found


def ticket_problem(state: dict[str, Any], number: int) -> str | None:
    """None when the ticket's issue is closed as completed, else why not.

    Only the issue state counts: no merged PR, board Status or history.
    """
    ticket = (state.get("tickets") or {}).get(str(number))
    if not isinstance(ticket, dict):
        return "not read"
    if ticket.get("problem"):
        return str(ticket["problem"])
    if ticket.get("state") == "open":
        return "open"
    if ticket.get("state") != "closed":
        return "state unknown"
    if ticket.get("state_reason") == "completed":
        return None
    return f"closed as {ticket.get('state_reason') or 'unknown reason'}"


def ticket_warnings(state: dict[str, Any], numbers: set[int]) -> list[str]:
    """Board Status that disagrees with the issue state. A warning, not a gate."""
    board = board_issues(state)
    found = []
    for n in sorted(numbers):
        item = board.get(n)
        ticket = (state.get("tickets") or {}).get(str(n)) or {}
        if item is None or ticket.get("problem"):
            continue
        status = item.get("status") or "not set"
        if ticket_problem(state, n) is None and status != "Done":
            found.append(
                f"{issue_url(n)} is closed as completed; board Status is {status}"
            )
        elif ticket.get("state") == "open" and status == "Done":
            found.append(f"{issue_url(n)} is open; board Status is Done")
    return found


def batch_blockers(state: dict[str, Any], agent: str) -> dict[int, set[str]]:
    """Leaves each issue must wait for, from the batch order tables on the epic.

    An issue first named in stage k waits for all leaves named in stages 0..k-1.
    """
    blockers: dict[int, set[str]] = {}
    for text in state.get("batch_order", []):
        if "Scope, in order" not in text:
            continue
        for who, scope in BATCH_ROW.findall(text):
            if who != agent:
                continue
            earlier: set[str] = set()
            seen: set[int] = set()
            for stage in STAGE_SPLIT.split(scope):
                for n in (int(x) for x in ISSUE_URL.findall(stage)):
                    if n not in seen:
                        seen.add(n)
                        blockers.setdefault(n, set()).update(earlier)
                earlier |= set(LEAF.findall(stage))
    return blockers


WAITING_LABEL = "waiting"
WAITING_ACTORS = (*AGENTS, "Anton")
WAITING_LINE = re.compile(r"Waiting:[ \t]*(\S+)")
REASON_LINE = re.compile(r"Reason:[ \t]*(\S.*)")
RESUME_AFTER_LINE = re.compile(r"Resume after:[ \t]*(\S.*)")
RESUME_ANTON = "Resume: Anton"


def rows_as_comments(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """REST comment rows in the decide() comment shape (no count check)."""
    return [
        {
            "body": r.get("body") or "",
            "createdAt": r["created_at"],
            "url": r.get("html_url"),
            "includesCreatedEdit": r.get("updated_at") != r["created_at"]
            or bool(r.get("last_edited_at")),
        }
        for r in rows
    ]


def newest_waiting(comments: list[dict[str, Any]]) -> dict[str, Any]:
    """The stored record of the newest comment that starts with `Waiting:`.

    A newer malformed or edited record is kept, never skipped for an older one.
    """
    found = [
        c for c in comments if (c.get("body") or "").lstrip().startswith("Waiting:")
    ]
    if not found:
        return {"found": False}
    c = sorted(found, key=lambda c: c["createdAt"])[-1]
    return {
        "found": True,
        "body": c.get("body") or "",
        "url": c.get("url"),
        "edited": bool(c.get("includesCreatedEdit")),
    }


def parse_waiting(body: str) -> tuple[dict[str, Any] | None, str | None]:
    """(record, None) or (None, problem) for a `Waiting:` comment.

    Exactly three lines: `Waiting: <actor>`, `Reason: <text>`, and either
    `Resume after: <issue URL>, ...` or `Resume: Anton`.
    """
    lines = [line.strip() for line in body.strip().splitlines() if line.strip()]
    if len(lines) != 3:
        return None, "needs exactly three lines"
    actor = WAITING_LINE.fullmatch(lines[0])
    if not actor or actor.group(1) not in WAITING_ACTORS:
        return None, "first line must be Waiting: Claude, Codex or Anton"
    reason = REASON_LINE.fullmatch(lines[1])
    if not reason:
        return None, "second line must be Reason: <text>"
    record: dict[str, Any] = {"actor": actor.group(1), "reason": reason.group(1)}
    if lines[2] == RESUME_ANTON:
        record["resume"] = "Anton"
        return record, None
    after = RESUME_AFTER_LINE.fullmatch(lines[2])
    if not after:
        return None, "third line must be Resume after: <issue URLs> or Resume: Anton"
    numbers = []
    for part in after.group(1).split(","):
        m = ISSUE_URL.fullmatch(part.strip())
        if not m:
            return None, f"not a full issue URL of {REPO}: {part.strip()!r}"
        numbers.append(int(m.group(1)))
    record["resume"] = sorted(set(numbers))
    return record, None


def all_issue_labels(state: dict[str, Any]) -> dict[int, set[str]]:
    """Labels of this repository's issues: board items, then linked off-board ones."""
    found = {
        int(n): set(names) for n, names in (state.get("linked_labels") or {}).items()
    }
    # Project boards may hold issues from other repositories with the same number.
    found.update({n: labels(i) for n, i in board_issues(state).items()})
    return found


def pr_wait_sources(
    pr: dict[str, Any], issue_labels: dict[int, set[str]]
) -> list[tuple[int, str]]:
    """(number, URL) of the PR and its `Issue:` tickets that carry the label."""
    found = []
    if WAITING_LABEL in labels(pr):
        found.append((pr["number"], f"https://github.com/{REPO}/pull/{pr['number']}"))
    for n in sorted(issue_numbers(pr.get("body") or "")):
        if WAITING_LABEL in issue_labels.get(n, set()):
            found.append((n, issue_url(n)))
    return found


def waiting_sources(state: dict[str, Any]) -> set[int]:
    """Issues and PRs whose Waiting comment the selector needs: labeled draft
    PRs and their labeled `Issue:` tickets, and labeled In progress issues."""
    issue_labels = all_issue_labels(state)
    found: set[int] = set()
    for p in state.get("prs", []):
        if p.get("baseRefName") in BASES and p.get("isDraft"):
            found |= {n for n, _ in pr_wait_sources(p, issue_labels)}
    for n, i in board_issues(state).items():
        if i.get("status") == "In progress" and WAITING_LABEL in labels(i):
            found.add(n)
    return found


def read_waiting(
    state: dict[str, Any],
    read_comments: Callable[[int], list[dict[str, Any]]],
    pr_comments: bool,
) -> dict[str, dict[str, Any]]:
    """The newest Waiting record of each source, keyed by number.

    `pr_comments`: the PRs in `state` carry their full comment history, so a
    PR source needs no new read. A failed read raises; no partial result.
    """
    prs = {p["number"]: p for p in state.get("prs", [])}
    found = {}
    for n in sorted(waiting_sources(state)):
        pr = prs.get(n)
        comments = (
            pr["comments"]
            if pr_comments and pr is not None and isinstance(pr.get("comments"), list)
            else read_comments(n)
        )
        found[str(n)] = newest_waiting(comments)
    return found


def resume_tickets(state: dict[str, Any]) -> set[int]:
    """Tickets named in valid `Resume after:` lines of the read records."""
    found: set[int] = set()
    for record in (state.get("waiting") or {}).values():
        if isinstance(record, dict) and record.get("found"):
            parsed, _ = parse_waiting(record.get("body") or "")
            if parsed and isinstance(parsed["resume"], list):
                found |= set(parsed["resume"])
    return found


@dataclass
class Waiting:
    """Why a draft PR or In progress issue must not continue."""

    blocking: list[str]
    satisfied: list[str]
    # A record was not read: claims stay held, as without waiting support.
    unread: bool


def waiting_of(
    agent: str,
    sources: list[tuple[int, str]],
    state: dict[str, Any],
    done: set[str],
    completed_tickets: bool,
) -> Waiting | None:
    """None without a waiting label, else the state of every source's record.

    A dependency wait resolves with the active "Start after" completion rule.
    An operator wait (`Resume: Anton`) holds until Anton removes the label.
    """
    if not sources:
        return None
    records = state.get("waiting") or {}
    result = Waiting([], [], False)
    for n, url in sources:
        record = records.get(str(n))
        if not isinstance(record, dict):
            result.unread = True
            result.blocking.append(f"{url}: Waiting comment not read")
            continue
        if not record.get("found"):
            result.blocking.append(
                f"{url}: label {WAITING_LABEL} without a Waiting comment"
            )
            continue
        where = record.get("url") or url
        if record.get("edited"):
            result.blocking.append(
                f"{where}: Waiting comment was edited; post a new one"
            )
            continue
        parsed, problem = parse_waiting(record.get("body") or "")
        if parsed is None:
            result.blocking.append(f"{where}: invalid Waiting comment ({problem})")
            continue
        if parsed["actor"] not in (agent, "Anton"):
            result.blocking.append(
                f"{where}: Waiting comment by {parsed['actor']}, not {agent} or Anton"
            )
            continue
        if parsed["resume"] == "Anton":
            result.blocking.append(
                f"{url}: {parsed['reason']}; resumes when Anton removes the label"
            )
            continue
        still = []
        for dep in parsed["resume"]:
            if completed_tickets:
                problem = ticket_problem(state, dep)
                if problem:
                    still.append(f"{issue_url(dep)} ({problem})")
            elif issue_url(dep) not in done:
                still.append(issue_url(dep))
        if still:
            result.blocking.append(
                f"{url}: {parsed['reason']}; resume after {', '.join(still)}"
            )
        else:
            result.satisfied.append(
                f"{url}: Waiting comment is satisfied; label {WAITING_LABEL} still present"
            )
    return result


def read_focus(path: Path) -> frozenset[str]:
    """Labels in the focus file; empty when the file is missing or empty."""
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return frozenset()
    stripped = (line.strip() for line in lines)
    return frozenset(line for line in stripped if line and not line.startswith("#"))


def pr_labels(pr: dict[str, Any], issue_labels: dict[int, set[str]]) -> set[str]:
    """A PR's own labels plus the labels of its `Issue:` tickets."""
    found = set(labels(pr))
    for n in issue_numbers(pr.get("body") or ""):
        found |= issue_labels.get(n, set())
    return found


def in_focus(
    focus: frozenset[str], pr: dict[str, Any], issue_labels: dict[int, set[str]]
) -> bool:
    """A PR is in focus through its own labels or its `Issue:` tickets' labels."""
    return bool(pr_labels(pr, issue_labels) & focus)


def ownerless(pr: dict[str, Any]) -> bool:
    """True when the PR body has no owner line at all, known agent or not.

    Only such a PR may be adopted: `adopt` must never overwrite an owner.
    """
    return pr_author(pr) is None and not ANY_OWNER_LINE.search(pr.get("body") or "")


def board_executors(state: dict[str, Any]) -> dict[int, str]:
    """Executor project field of this repository's PRs on the board, by number.

    The board also holds PRs of other repositories with the same numbers.
    """
    return {
        i["content"]["number"]: i["executor"]
        for i in state.get("items", [])
        if (i.get("content") or {}).get("type") == "PullRequest"
        and PR_URL.fullmatch(i["content"].get("url") or "")
        and i.get("executor") in AGENTS
    }


def pr_route(
    pr: dict[str, Any], state: dict[str, Any], rules: list[dict[str, Any]] | None
) -> Route:
    """Routing for a PR without an owner line: board Executor, else file owners."""
    # Imported here: the Codex runner tests copy this file without routing.py.
    from routing import load_rules, route

    executor = board_executors(state).get(pr["number"])
    files = pr.get("files")
    if executor or not files:
        return route(executor, files, [], "adopt")
    return route(None, files, load_rules() if rules is None else rules, "adopt")


def decide(
    agent: str,
    state: dict[str, Any],
    paused: bool = False,
    include_waiting: bool = False,
    focus: frozenset[str] = frozenset(),
    enabled: frozenset[str] = frozenset(),
    rules: list[dict[str, Any]] | None = None,
    completed_tickets: bool = False,
    free_claims: bool = False,
) -> list[Action]:
    """All actions for the agent, highest priority first. Never empty.

    `include_waiting` adds `wait` entries (Ready leaves blocked by "Start after")
    for --status; they are never returned as the action to do.
    `focus` limits actions to issues and PRs with one of these labels.
    `enabled` lists the new actions (NEW_ACTIONS) that may be emitted.
    `rules` are the lane map's file rules (default: .github/epic-lanes.yml).
    `completed_tickets` (EPIC_REQUIRE_COMPLETED_TICKETS): a ticket dependency
    needs its issue closed as completed, and closed issues get no action.
    A draft PR or In progress issue with an open `waiting` label becomes a
    status-only `wait`. `free_claims` (only where a fresh recheck runs before
    the model) lets such waits leave claims free; otherwise they hold claims.
    """
    if paused:
        return [Action("stop", f"pause file {PAUSE_FILE} exists")]
    peer = other(agent)
    # Linked tickets that are not on the board; fetch_state() reads their labels.
    issue_labels = all_issue_labels(state)
    done = done_leaves(state, tickets=not completed_tickets)
    # A wait whose record was not read, or any wait without a fresh recheck.
    hold_claims = False
    held = "" if free_claims else "; claims stay held without a fresh recheck"
    all_prs = [p for p in state.get("prs", []) if p.get("baseRefName") in BASES]
    # Escalated PRs get no PR action, but still link their issue and count as open.
    # So do PRs outside the focus: they are frozen, not forgotten.
    prs = [
        p
        for p in all_prs
        if ESCALATION_LABEL not in labels(p)
        and (not focus or in_focus(focus, p, issue_labels))
    ]
    mine = [p for p in prs if pr_author(p) == agent]
    theirs = [p for p in prs if pr_author(p) == peer]
    # Each kind's list is sorted by this key: priority, then age (number).
    ranked: dict[str, list[tuple[tuple[Any, ...], Action]]] = {}

    def add(kind: str, act: Action, *order: Any) -> None:
        ranked.setdefault(kind, []).append(((act.priority, *order), act))

    def pr_action(p: dict[str, Any], kind: str, reason: str, **kw: Any) -> None:
        rank = priority_rank(pr_labels(p, issue_labels))
        n = p["number"]
        if not p.get("isDraft"):
            kw.setdefault(
                "warnings",
                [
                    f"{url} has label {WAITING_LABEL}; a ready PR keeps its actions"
                    for _, url in pr_wait_sources(p, issue_labels)
                ],
            )
        add(
            kind,
            Action(
                kind,
                reason,
                pr=n,
                sha=p["headRefOid"],
                priority=rank,
                updated_at=p.get("updatedAt"),
                **kw,
            ),
            # Waits share one list with issue waits: (wave, number).
            *(("", n) if kind == "wait" else (n,)),
        )

    for p in mine:
        head = p["headRefOid"]
        if p.get("isDraft"):
            wait = waiting_of(
                agent,
                pr_wait_sources(p, issue_labels),
                state,
                done,
                completed_tickets,
            )
            if wait and wait.blocking:
                hold_claims |= wait.unread or not free_claims
                reason = "waiting: " + "; ".join(wait.blocking)
                pr_action(p, "wait", reason + held)
            else:
                satisfied = wait.satisfied if wait else []
                pr_action(p, "continue", "my draft PR", warnings=satisfied)
            continue
        theirs_v = verdicts(p, peer)
        latest = theirs_v[-1] if theirs_v else None
        rounds = len({v["sha"] for v in theirs_v if v["state"] == "CHANGES REQUESTED"})
        if latest and latest["sha"] == head and latest["state"] == "CHANGES REQUESTED":
            if rounds >= MAX_ROUNDS:
                pr_action(
                    p,
                    "escalate",
                    f"{rounds} changes-requested rounds; add label {ESCALATION_LABEL} and ask Anton",
                    comments=[latest["url"]] if latest.get("url") else [],
                )
                continue
            previous = theirs_v[-2]["at"] if len(theirs_v) > 1 else None
            pr_action(
                p,
                "fix",
                f"{peer} requested changes for the current head",
                comments=findings(p, latest, previous),
            )
            continue
        checks = checks_state(p)
        if p.get("mergeable") == "CONFLICTING":
            pr_action(p, "resolve-conflict", "PR conflicts with its base")
            continue
        if checks == "failed":
            pr_action(p, "fix-checks", "checks failed on the current head")
            continue
        if (
            latest
            and latest["sha"] == head
            and latest["state"] == "APPROVED"
            and checks == "green"
        ):
            pr_action(p, "merge", f"{peer} approved the current head; checks green")

    for p in theirs:
        if p.get("isDraft"):
            continue
        head = p["headRefOid"]
        if not any(v["sha"] == head for v in verdicts(p, agent)):
            pr_action(
                p, "review", f"{peer}'s PR has no verdict from {agent} for its head"
            )

    # A focus PR without an owner line: only the routed agent adopts it.
    if focus and "adopt" in enabled:
        for p in prs:
            if not ownerless(p):
                continue
            routed = pr_route(p, state, rules)
            if routed.agent == agent:
                pr_action(
                    p,
                    "adopt",
                    f"no owner line; {routed.reason}",
                    lane=routed.lane,
                    body_sha=body_digest(p.get("body") or ""),
                )

    linked = set()
    for p in all_prs:
        linked |= issue_numbers(p.get("body") or "")
    open_mine = len([p for p in all_prs if pr_author(p) == agent])
    items = [
        i
        for i in state.get("items", [])
        if i.get("executor") == agent
        and (i.get("content") or {}).get("type") == "Issue"
        and ESCALATION_LABEL not in labels(i)
        and (not focus or labels(i) & focus)
        # A closed issue left Ready or In progress is no work and must not
        # suppress claims through a `continue`.
        and not (completed_tickets and is_closed(i))
    ]
    batch = batch_blockers(state, agent)
    for i in items:
        number, url = i["content"]["number"], i["content"]["url"]
        if number in linked:
            continue
        rank = priority_rank(labels(i))
        wave = str(i.get("wave") or "9")
        if i.get("status") == "In progress":
            sources = [(number, url)] if WAITING_LABEL in labels(i) else []
            wait = waiting_of(agent, sources, state, done, completed_tickets)
            if wait and wait.blocking:
                hold_claims |= wait.unread or not free_claims
                add(
                    "wait",
                    Action(
                        "wait",
                        "waiting: " + "; ".join(wait.blocking) + held,
                        issue=url,
                        priority=rank,
                    ),
                    wave,
                    number,
                )
                continue
            # Same queue and key as draft PRs: priority, then number.
            add(
                "continue",
                Action(
                    "continue",
                    "my In progress leaf has no PR yet",
                    issue=url,
                    priority=rank,
                    updated_at=i["content"].get("updated_at"),
                    warnings=wait.satisfied if wait else [],
                ),
                number,
            )
        elif i.get("status") == "Ready" and open_mine < MAX_OPEN_PRS:
            wanted = start_after(i) | batch.get(number, set())
            warnings: list[str] = []
            if completed_tickets:
                tickets = {int(n) for n in ISSUE_URL.findall(" ".join(wanted))}
                blockers = sorted(
                    w for w in wanted - done if not ISSUE_URL.fullmatch(w)
                )
                for n in sorted(tickets):
                    problem = ticket_problem(state, n)
                    if problem:
                        blockers.append(f"{issue_url(n)} ({problem})")
                warnings = ticket_warnings(state, tickets)
            else:
                blockers = sorted(wanted - done)
            if blockers:
                add(
                    "wait",
                    Action(
                        "wait",
                        f"starts after {', '.join(blockers)}",
                        issue=url,
                        priority=rank,
                        warnings=warnings,
                    ),
                    wave,
                    number,
                )
            else:
                add(
                    "claim",
                    Action(
                        "claim",
                        "Ready leaf assigned to me",
                        issue=url,
                        priority=rank,
                        updated_at=i["content"].get("updated_at"),
                        warnings=warnings,
                    ),
                    wave,
                    number,
                )

    order = [
        "escalate",
        "merge",
        "fix",
        "fix-checks",
        "resolve-conflict",
        "review",
        "continue",
        "adopt",
        "claim",
    ]
    if include_waiting:
        order = [*order, "wait"]
    actions = [
        a
        for kind in order
        for _, a in sorted(ranked.get(kind, []), key=lambda entry: entry[0])
    ]
    if hold_claims or any(a.action == "continue" for a in actions):
        actions = [
            a for a in actions if a.action != "claim"
        ]  # one implementation at a time
    if not actions and focus:
        return [Action("idle", f"nothing to do in focus {', '.join(sorted(focus))}")]
    return actions or [Action("idle", "nothing to do")]


def comments_from_rest(
    rows: list[dict[str, Any]], expected: int
) -> list[dict[str, Any]]:
    """Map REST issue comments to the shape decide() reads. Refuse partial history.

    An edited comment has updated_at != created_at; decide() ignores edited verdicts.
    """
    if len(rows) != expected or len({r.get("id") for r in rows}) != len(rows):
        raise ValueError(
            f"comment history incomplete: got {len(rows)} of {expected}; retry"
        )
    return [
        {
            "body": r.get("body") or "",
            "createdAt": r["created_at"],
            "url": r.get("html_url"),
            "includesCreatedEdit": r.get("updated_at") != r["created_at"],
        }
        for r in rows
    ]


def gh_json(args: list[str]) -> Any:
    return json.loads(run_gh(args))


def rest_rows(endpoint: str) -> list[dict[str, Any]]:
    """All rows of a paginated REST list. A failed page raises; no partial list."""
    pages = gh_json(["api", "--paginate", "--slurp", endpoint])
    return [row for page in pages for row in page]


def item_from_rest(row: dict[str, Any]) -> dict[str, Any]:
    """Map a Projects REST item to the `gh project item-list --format json` shape.

    Single-select values stay strings (wave "0" must not become 0); unset is None.
    """
    values = {f.get("name"): f.get("value") for f in row.get("fields") or []}

    def single(name: str) -> str | None:
        value = values.get(name)
        return None if value is None else str(value["name"]["raw"])

    source = row.get("content") or {}
    content: dict[str, Any] = {"type": row.get("content_type")}
    # state and state_reason: ticket completion (completed_tickets()).
    for key in ("number", "title", "body", "updated_at", "state", "state_reason"):
        if key in source:
            content[key] = source[key]
    # REST `url` is the API address; monitor.py matches the web address.
    if source.get("html_url"):
        content["url"] = source["html_url"]
    return {
        "id": row.get("node_id"),
        "title": source.get("title"),
        "content": content,
        "status": single("Status"),
        "wave": single("Wave"),
        "executor": single("Executor"),
        "area": single("Area"),
        "level": single("Level"),
        "labels": [lbl["name"] for lbl in values.get("Labels") or []],
    }


def project_items() -> list[dict[str, Any]]:
    """Board items via REST. `gh project item-list` cost 203 GraphQL points a tick."""
    fields = {
        f.get("name"): f.get("id")
        for f in rest_rows(f"{PROJECT_API}/fields?per_page=100")
    }
    missing = [name for name in PROJECT_FIELDS if fields.get(name) is None]
    if missing:
        raise ValueError(f"project board lacks fields: {', '.join(missing)}")
    query = "&".join(f"fields[]={fields[name]}" for name in PROJECT_FIELDS)
    return [
        item_from_rest(row)
        for row in rest_rows(f"{PROJECT_API}/items?per_page=100&{query}")
    ]


class QuotaWait(Exception):
    """The other runner stored a GraphQL quota wait during this read."""


def search_issues(
    label: str, fetch: Callable[[list[str]], Any] | None = None
) -> list[dict[str, Any]]:
    """Open issues with this label, all pages. Partial results raise."""
    fetch = fetch or gh_json
    query = quote(f'repo:{REPO} is:issue is:open label:"{label}"')
    pages = fetch(
        ["api", "--paginate", "--slurp", f"search/issues?q={query}&per_page=100"]
    )
    if any(page.get("incomplete_results") for page in pages):
        raise ValueError(f"search for label {label} is incomplete; retry")
    return [row for page in pages for row in page.get("items") or []]


def focus_issues(
    focus: frozenset[str],
    fetch: Callable[[list[str]], Any] | None = None,
    now: Callable[[], float] = time.time,
) -> list[dict[str, Any]]:
    """Open issues with a focus label, on the board or not. One entry per issue.

    Checks the shared quota wait before each search: the other runner can
    store one at any time.
    """
    found: dict[int, dict[str, Any]] = {}
    for label in sorted(focus):
        wait, retry_at = quota_check(STATE_DIR, now())
        if wait == DEFERRED:
            raise QuotaWait(retry_at)
        for row in search_issues(label, fetch):
            if "pull_request" in row or row["number"] in found:
                continue
            found[row["number"]] = {
                "number": row["number"],
                "url": row.get("html_url") or issue_url(row["number"]),
                "title": row.get("title"),
                "labels": sorted(labels(row)),
                "updated_at": row.get("updated_at"),
            }
    return [found[n] for n in sorted(found)]


def changed_paths(rows: list[dict[str, Any]]) -> list[str]:
    """Paths a PR touches. A rename counts with both paths, as in epic-guard."""
    found = set()
    for row in rows:
        found.add(row["filename"])
        if row.get("status") == "renamed" and row.get("previous_filename"):
            found.add(row["previous_filename"])
    return sorted(found)


SHARED_READER_ENV = "EPIC_SHARED_READER"
READ_BLOCKED = 5
STALE = 6


def shared_reader() -> bool:
    return os.environ.get(SHARED_READER_ENV) == "1"


def rest_ticket(number: int) -> dict[str, Any]:
    """One prerequisite ticket via REST. 404 and 410 block only its successors;
    every other failure raises and stops the tick."""
    try:
        issue = gh_json(["api", f"repos/{REPO}/issues/{number}"])
    except subprocess.CalledProcessError as error:
        found = HTTP_STATUS.search(error.stderr or "")
        if found and int(found.group(1)) in MISSING_STATUSES:
            return missing_ticket(int(found.group(1)))
        raise
    return ticket_from_issue(number, issue)


def fetch_state(focus: frozenset[str] = frozenset()) -> dict[str, Any]:
    """The state decide() reads. EPIC_REQUIRE_COMPLETED_TICKETS=1 adds the
    prerequisite tickets' issue state; off, no ticket is read."""
    mode = completed_tickets()
    if shared_reader():
        # Imported here: the Codex runner tests copy this file alone.
        import github_state

        return github_state.read_snapshot(focus, completed_tickets=mode).state
    fields = (
        "number,title,body,baseRefName,headRefName,headRefOid,isDraft,labels,"
        "mergeable,statusCheckRollup,updatedAt"
    )
    prs: list[dict[str, Any]] = []
    for base in BASES:
        prs += gh_json(
            [
                "pr",
                "list",
                "--repo",
                REPO,
                "--state",
                "open",
                "--base",
                base,
                "--json",
                fields,
                "--limit",
                "100",
            ]
        )
    for pr in prs:
        # `gh pr list` returns only the first 100 comments; read them all.
        n = pr["number"]
        pages = gh_json(
            [
                "api",
                "--paginate",
                "--slurp",
                f"repos/{REPO}/issues/{n}/comments?per_page=100",
            ]
        )
        count = gh_json(["api", f"repos/{REPO}/issues/{n}", "--jq", "{comments}"])[
            "comments"
        ]
        pr["comments"] = comments_from_rest(
            [row for page in pages for row in page], count
        )
        # Routing of a PR without an owner line needs its files.
        if pr.get("baseRefName") in BASES and ownerless(pr):
            rows = rest_rows(f"repos/{REPO}/pulls/{n}/files?per_page=100")
            pr["files"] = changed_paths(rows)
    merged: list[dict[str, Any]] = []
    for base in BASES:
        merged += gh_json(
            ["pr", "list", "--repo", REPO, "--state", "merged", "--base", base]
            + ["--json", "number,body", "--limit", "300"]
        )
    items = project_items()
    # Priority and focus read the labels of a PR's `Issue:` tickets. Read the
    # ones that are not on the board.
    on_board = {
        (item.get("content") or {}).get("number")
        for item in items
        if ISSUE_URL.fullmatch((item.get("content") or {}).get("url") or "")
    }
    linked_labels: dict[str, list[str]] = {}
    for n in sorted({n for pr in prs for n in issue_numbers(pr.get("body") or "")}):
        if n not in on_board:
            issue = gh_json(["api", f"repos/{REPO}/issues/{n}"])
            linked_labels[str(n)] = sorted(labels(issue))
    for item in items:
        # Only Ready issues need their readiness comments ("Start after ...").
        content = item.get("content") or {}
        if item.get("status") == "Ready" and content.get("type") == "Issue":
            pages = gh_json(
                ["api", "--paginate", "--slurp"]
                + [f"repos/{REPO}/issues/{content['number']}/comments?per_page=100"]
            )
            item["readiness"] = "\n".join(
                row.get("body") or "" for page in pages for row in page
            )
    # Batch order tables ("Scope, in order") live in comments on the epic.
    pages = gh_json(
        ["api", "--paginate", "--slurp"]
        + [f"repos/{REPO}/issues/{EPIC}/comments?per_page=100"]
    )
    batch_order = [
        row.get("body") or ""
        for page in pages
        for row in page
        if "Scope, in order" in (row.get("body") or "")
    ]
    state: dict[str, Any] = {
        "prs": prs,
        "items": items,
        "linked_labels": linked_labels,
        "merged_prs": merged,
        "batch_order": batch_order,
        # Focus issues also off the board, so --status can name them.
        "focus_issues": focus_issues(focus) if focus else [],
    }
    # Waiting records: PR comments are read above; issues are read here.
    state["waiting"] = read_waiting(
        state,
        lambda n: rows_as_comments(
            rest_rows(f"repos/{REPO}/issues/{n}/comments?per_page=100")
        ),
        pr_comments=True,
    )
    if mode:
        state["completed_tickets"] = True
        state["tickets"] = read_tickets(items, rest_ticket, resume_tickets(state))
    return state


def pr_reason(
    agent: str,
    p: dict[str, Any],
    state: dict[str, Any],
    enabled: frozenset[str],
    rules: list[dict[str, Any]] | None,
) -> str:
    """Why a focus PR gets no action from this agent."""
    if ESCALATION_LABEL in labels(p):
        return ESCALATION_LABEL
    author = pr_author(p)
    if author is None and not ownerless(p):
        return "owner line names no known agent; fix it by hand"
    if author is None:
        routed = pr_route(p, state, rules)
        if routed.agent is None:
            return f"no owner; {routed.reason}"
        if routed.agent != agent:
            return f"no owner; routed to {routed.agent}"
        if "adopt" not in enabled:
            return f"no owner; adopt is not in {ACTIONS_ENV}"
        return "no owner"
    if p.get("isDraft"):
        return f"draft of {author}"
    if author == agent:
        return f"waiting for {other(agent)}'s review or checks"
    return f"reviewed by {agent} for the current head"


def issue_reason(
    agent: str,
    number: int,
    item: dict[str, Any] | None,
    linked: set[int],
    completed_tickets: bool = False,
) -> str:
    """Why a focus issue gets no action from this agent."""
    if item is None:
        return "not on board"
    if completed_tickets and is_closed(item):
        return f"closed; board Status is {item.get('status') or 'not set'}"
    if ESCALATION_LABEL in labels(item):
        return ESCALATION_LABEL
    if number in linked:
        return "has an open PR"
    if item.get("executor") != agent:
        return f"Executor is {item.get('executor') or 'not set'}"
    if item.get("status") not in ("Ready", "In progress"):
        return f"blocked: status {item.get('status') or 'not set'}"
    return "blocked: other work to continue or two open PRs"


def no_action(
    agent: str,
    state: dict[str, Any],
    focus: frozenset[str],
    enabled: frozenset[str],
    rules: list[dict[str, Any]] | None,
    acted: list[Action],
    completed_tickets: bool = False,
) -> list[tuple[str, str]]:
    """(target, reason) for each focus item this agent has no action for."""
    issue_labels = {
        int(n): set(names) for n, names in (state.get("linked_labels") or {}).items()
    }
    board = {
        i["content"]["number"]: i
        for i in state.get("items", [])
        if (i.get("content") or {}).get("type") == "Issue"
        and ISSUE_URL.fullmatch(i["content"].get("url") or "")
    }
    issue_labels.update({n: labels(i) for n, i in board.items()})
    prs = [p for p in state.get("prs", []) if p.get("baseRefName") in BASES]
    linked = {n for p in prs for n in issue_numbers(p.get("body") or "")}
    acted_prs = {a.pr for a in acted if a.pr}
    acted_issues = {a.issue for a in acted if a.issue}
    found = []
    for p in sorted(prs, key=lambda p: p["number"]):
        if p["number"] not in acted_prs and in_focus(focus, p, issue_labels):
            found.append(
                (f"PR {p['number']}", pr_reason(agent, p, state, enabled, rules))
            )
    issues = {n: issue_url(n) for n, i in board.items() if labels(i) & focus}
    issues.update({i["number"]: i["url"] for i in state.get("focus_issues") or []})
    for n in sorted(issues):
        if issues[n] not in acted_issues:
            reason = issue_reason(agent, n, board.get(n), linked, completed_tickets)
            found.append((issues[n], reason))
    return found


def status(
    state: dict[str, Any],
    paused: bool,
    focus: frozenset[str] = frozenset(),
    enabled: frozenset[str] = frozenset(),
    rules: list[dict[str, Any]] | None = None,
    completed_tickets: bool = False,
    free_claims: bool = False,
) -> str:
    lines = [
        f"Paused: {'yes' if paused else 'no'}",
        f"Focus: {', '.join(sorted(focus)) if focus else 'all'}",
        f"New actions: {', '.join(sorted(enabled)) or 'none'}",
        f"Completed tickets rule ({COMPLETED_TICKETS_ENV}): "
        + ("on" if completed_tickets else "off"),
    ]
    for agent in AGENTS:
        lines.append(f"\n{agent}:")
        acted = decide(
            agent,
            state,
            paused,
            include_waiting=True,
            focus=focus,
            enabled=enabled,
            rules=rules,
            completed_tickets=completed_tickets,
            free_claims=free_claims,
        )
        for a in acted:
            target = f"PR {a.pr}" if a.pr else (a.issue or "")
            level = PRIORITY_NAMES[a.priority] if target else ""
            lines.append(f"  {a.action:<17} {level:<6} {target:<55} {a.reason}")
            for warning in a.warnings:
                lines.append(f"  {'warning':<17} {'':<6} {target:<55} {warning}")
        if focus and not paused:
            for target, reason in no_action(
                agent, state, focus, enabled, rules, acted, completed_tickets
            ):
                lines.append(f"  {'no action':<17} {'':<6} {target:<55} {reason}")
    unknown = [
        p["number"]
        for p in state.get("prs", [])
        if p.get("baseRefName") in BASES and not pr_author(p)
    ]
    if unknown and not focus:
        lines.append(f"\nPRs without an owner line (adopt needs a focus): {unknown}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--agent", choices=[a.lower() for a in AGENTS])
    group.add_argument("--status", action="store_true")
    parser.add_argument(
        "--candidates",
        action="store_true",
        help="print every action, one JSON per line (the runner tries them in order)",
    )
    parser.add_argument("--state-file", type=Path)
    parser.add_argument(
        "--dump-state", type=Path, help="save the fetched state as JSON"
    )
    parser.add_argument(
        "--focus", action="append", help=f"label to work on; overrides {FOCUS_FILE}"
    )
    parser.add_argument(
        "--recheck",
        type=Path,
        help="action JSON to check fresh against GitHub before the model starts",
    )
    args = parser.parse_args()
    if args.recheck and not args.agent:
        parser.error("--recheck needs --agent")

    paused = PAUSE_FILE.exists()
    focus = frozenset(args.focus) if args.focus else read_focus(FOCUS_FILE)
    enabled = read_actions(os.environ.get(ACTIONS_ENV))
    try:
        mode = completed_tickets()
    except SettingError as error:
        print(f"config: {error}", file=sys.stderr)
        return 2
    if args.recheck:
        return run_recheck(args.agent, args.recheck, focus, enabled, paused, mode)
    # The shared reader's own errors; empty (catches nothing) when it is off.
    reader_errors: tuple[type[Exception], ...] = ()
    if shared_reader() and not args.state_file:
        import github_state

        reader_errors = (github_state.ConfigError, github_state.ReadBlocked)
        try:
            github_state.settings()
        except github_state.ConfigError as error:
            return reader_exit(error)
    if not args.state_file:
        # A stored GraphQL quota wait means zero GitHub calls until it expires.
        wait, retry_at = quota_check(STATE_DIR, time.time())
        if wait == DEFERRED:
            print(
                f"quota: GitHub GraphQL quota wait, retry at {retry_at}",
                file=sys.stderr,
            )
            return DEFERRED
    try:
        state = (
            json.loads(args.state_file.read_text())
            if args.state_file
            else fetch_state(focus)
        )
    except QuotaWait as wait:
        print(f"quota: GitHub GraphQL quota wait, retry at {wait}", file=sys.stderr)
        return DEFERRED
    except QuotaExhausted as error:
        # No action from a partial read: the tick stops here.
        return stop_on_quota(error)
    except reader_errors as error:
        return reader_exit(error)
    if args.dump_state:
        args.dump_state.write_text(json.dumps(state, indent=1))
    if args.status:
        print(
            status(
                state,
                paused,
                focus,
                enabled,
                completed_tickets=mode,
                free_claims=shared_reader(),
            )
        )
        # Local runner state, no GitHub read. Imported here: only --status needs it.
        from tick_cooldown import status_lines

        print("\nClaude cooldowns (tick_cooldown.py):")
        print("\n".join(status_lines(STATE_DIR, time.time())))
        return 0
    actions = decide(
        args.agent.capitalize(),
        state,
        paused,
        focus=focus,
        enabled=enabled,
        completed_tickets=mode,
        # Waits free claims only where the runners recheck before the model.
        free_claims=shared_reader(),
    )
    for a in actions if args.candidates else actions[:1]:
        print(a.to_json())
    return 0


def reader_exit(error: Exception) -> int:
    """2 for bad settings, 5 for a blocked read; the reason goes to stderr."""
    import github_state

    if isinstance(error, github_state.ConfigError):
        print(f"config: {error}", file=sys.stderr)
        return 2
    print(f"read blocked: {error}", file=sys.stderr)
    return READ_BLOCKED


def run_recheck(
    agent: str,
    action_file: Path,
    focus: frozenset[str],
    enabled: frozenset[str],
    paused: bool,
    completed_tickets: bool = False,
) -> int:
    """Fresh check of one selected action: 0 still valid, 6 stale, 5 blocked.

    Bounded by EPIC_RECHECK_TIMEOUT_SECONDS; the runner's own timeout around
    this command is a little longer, so a slow read ends as 5 here.
    """
    import github_state

    try:
        action = json.loads(action_file.read_text())
        if not isinstance(action, dict):
            raise ValueError("action is not an object")
    except (OSError, ValueError) as error:
        print(f"recheck: bad action file: {error}", file=sys.stderr)
        return 2
    try:
        seconds = max(1, github_state.settings().recheck - 5)
        reader = github_state.FreshReader("recheck", seconds)
        reason = github_state.recheck(
            agent.capitalize(),
            action,
            focus,
            enabled,
            paused,
            reader,
            completed_tickets=completed_tickets,
        )
    except QuotaExhausted as error:
        return stop_on_quota(error)
    except (github_state.ConfigError, github_state.ReadBlocked) as error:
        return reader_exit(error)
    except ValueError as error:
        print(f"recheck: {error}", file=sys.stderr)
        return 2
    if reason:
        print(f"recheck: stale, {reason}", file=sys.stderr)
        return STALE
    print("recheck: still valid", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
