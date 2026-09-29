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
  next_action.py --status                    all pending actions, both agents
  next_action.py ... --state-file state.json use saved state (tests, dry runs)
  next_action.py ... --focus project::Stream  override ~/.epic-focus (repeatable)

Exit codes: 0 action printed, 3 a GraphQL quota wait is stored (no GitHub call),
4 a read hit the GraphQL quota (wait stored, no action). See github_quota.py.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from github_quota import (
    DEFERRED,
    STATE_DIR,
    QuotaExhausted,
    check as quota_check,
    run_gh,
    stop_on_quota,
)

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
# Merge gate, not a code check (".github/epic-lanes.yml" lists it as ignored).
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
    # For --status only. Kept out of the JSON so runner fingerprints stay stable.
    priority: int = DEFAULT_PRIORITY

    def to_json(self) -> str:
        return json.dumps(
            {
                k: v
                for k, v in asdict(self).items()
                if v not in (None, []) and k != "priority"
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

    The epic guard fails on purpose until the reviewer's verdict for the head
    exists. That is no code failure to fix, so a failed guard counts as
    pending: no `fix-checks`, and no merge until it is green. A missing guard
    status also counts as pending: the publisher may not have run yet.
    """
    states = []
    guard_green = False
    for c in pr.get("statusCheckRollup") or []:
        name = c.get("name") or c.get("context")
        if name in IGNORED_CHECKS:
            continue
        state = (c.get("conclusion") or c.get("state") or c.get("status") or "").upper()
        if name in GUARD_CHECKS:
            if state in FAILED:
                state = "PENDING"
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


def done_leaves(state: dict[str, Any]) -> set[str]:
    """Leaves listed by a merged PR's `Leaf IDs:` line or ticked in an issue body,
    plus the URLs of tickets a merged PR names in its `Issue:` line."""
    done: set[str] = set()
    for pr in state.get("merged_prs", []):
        for line in LEAF_IDS_LINE.findall(pr.get("body") or ""):
            done |= set(LEAF.findall(line))
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


def decide(
    agent: str,
    state: dict[str, Any],
    paused: bool = False,
    include_waiting: bool = False,
    focus: frozenset[str] = frozenset(),
) -> list[Action]:
    """All actions for the agent, highest priority first. Never empty.

    `include_waiting` adds `wait` entries (Ready leaves blocked by "Start after")
    for --status; they are never returned as the action to do.
    `focus` limits actions to issues and PRs with one of these labels.
    """
    if paused:
        return [Action("stop", f"pause file {PAUSE_FILE} exists")]
    peer = other(agent)
    # Linked tickets that are not on the board; fetch_state() reads their labels.
    issue_labels = {
        int(n): set(names) for n, names in (state.get("linked_labels") or {}).items()
    }
    # Project boards may hold issues from other repositories with the same number.
    issue_labels.update(
        {
            i["content"]["number"]: labels(i)
            for i in state.get("items", [])
            if (i.get("content") or {}).get("type") == "Issue"
            and ISSUE_URL.fullmatch(i["content"].get("url") or "")
        }
    )
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
        add(
            kind,
            Action(kind, reason, pr=n, sha=p["headRefOid"], priority=rank, **kw),
            n,
        )

    for p in mine:
        head = p["headRefOid"]
        if p.get("isDraft"):
            pr_action(p, "continue", "my draft PR")
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
    ]
    done = done_leaves(state)
    batch = batch_blockers(state, agent)
    for i in items:
        number, url = i["content"]["number"], i["content"]["url"]
        if number in linked:
            continue
        rank = priority_rank(labels(i))
        wave = str(i.get("wave") or "9")
        if i.get("status") == "In progress":
            # Same queue and key as draft PRs: priority, then number.
            add(
                "continue",
                Action(
                    "continue",
                    "my In progress leaf has no PR yet",
                    issue=url,
                    priority=rank,
                ),
                number,
            )
        elif i.get("status") == "Ready" and open_mine < MAX_OPEN_PRS:
            blockers = sorted((start_after(i) | batch.get(number, set())) - done)
            if blockers:
                add(
                    "wait",
                    Action(
                        "wait",
                        f"starts after {', '.join(blockers)}",
                        issue=url,
                        priority=rank,
                    ),
                    wave,
                    number,
                )
            else:
                add(
                    "claim",
                    Action(
                        "claim", "Ready leaf assigned to me", issue=url, priority=rank
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
        "claim",
    ]
    if include_waiting:
        order = [*order, "wait"]
    actions = [
        a
        for kind in order
        for _, a in sorted(ranked.get(kind, []), key=lambda entry: entry[0])
    ]
    if any(a.action == "continue" for a in actions):
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
    for key in ("number", "title", "body"):
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


def fetch_state() -> dict[str, Any]:
    fields = (
        "number,title,body,baseRefName,headRefName,headRefOid,isDraft,labels,"
        "mergeable,statusCheckRollup"
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
    return {
        "prs": prs,
        "items": items,
        "linked_labels": linked_labels,
        "merged_prs": merged,
        "batch_order": batch_order,
    }


def status(
    state: dict[str, Any], paused: bool, focus: frozenset[str] = frozenset()
) -> str:
    lines = [
        f"Paused: {'yes' if paused else 'no'}",
        f"Focus: {', '.join(sorted(focus)) if focus else 'all'}",
    ]
    for agent in AGENTS:
        lines.append(f"\n{agent}:")
        for a in decide(agent, state, paused, include_waiting=True, focus=focus):
            target = f"PR {a.pr}" if a.pr else (a.issue or "")
            level = PRIORITY_NAMES[a.priority] if target else ""
            lines.append(f"  {a.action:<17} {level:<6} {target:<55} {a.reason}")
    unknown = [
        p["number"]
        for p in state.get("prs", [])
        if p.get("baseRefName") in BASES and not pr_author(p)
    ]
    if unknown:
        lines.append(f"\nPRs without an Author or Reviewer line (ignored): {unknown}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--agent", choices=[a.lower() for a in AGENTS])
    group.add_argument("--status", action="store_true")
    parser.add_argument("--state-file", type=Path)
    parser.add_argument(
        "--dump-state", type=Path, help="save the fetched state as JSON"
    )
    parser.add_argument(
        "--focus", action="append", help=f"label to work on; overrides {FOCUS_FILE}"
    )
    args = parser.parse_args()

    paused = PAUSE_FILE.exists()
    focus = frozenset(args.focus) if args.focus else read_focus(FOCUS_FILE)
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
            else fetch_state()
        )
    except QuotaExhausted as error:
        # No action from a partial read: the tick stops here.
        return stop_on_quota(error)
    if args.dump_state:
        args.dump_state.write_text(json.dumps(state, indent=1))
    if args.status:
        print(status(state, paused, focus))
    else:
        print(decide(args.agent.capitalize(), state, paused, focus=focus)[0].to_json())
    return 0


if __name__ == "__main__":
    sys.exit(main())
