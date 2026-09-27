"""Pick the next action for one agent on the architecture epic.

Read-only: it reads GitHub through `gh` and prints one JSON action. The agent
does the action; this script never writes to GitHub. All state comes from
GitHub (verdict lines keyed by head SHA, project Status and Executor), so a
restarted agent picks up where it stopped.

Priority (first match wins), see docs/implementation/epic-rules.md section 8:
  stop      pause file exists
  escalate  my PR reached MAX_ROUNDS changes-requested verdicts
  merge     my PR is approved for its head, checks green, no conflict
  fix       my PR has a changes-requested verdict for its head
  fix-checks  my PR has failing checks on its head
  resolve-conflict  my PR conflicts with its base
  review    the other agent's ready PR has no verdict from me for its head
  continue  my draft PR, or my In progress leaf without a PR
  claim     a Ready leaf with Executor = me
  idle      nothing to do

Usage:
  next_action.py --agent claude|codex        one JSON action
  next_action.py --status                    all pending actions, both agents
  next_action.py ... --state-file state.json use saved state (tests, dry runs)
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

REPO = "phaabe/live.moafunk.de"
PROJECT_OWNER = "anneoneone"
PROJECT_NUMBER = "2"
BASES = ("dev/312-interim", "dev/streaming-architecture")
AGENTS = ("Claude", "Codex")
MAX_ROUNDS = 3
MAX_OPEN_PRS = 2
PAUSE_FILE = Path.home() / ".epic-pause"
ESCALATION_LABEL = "needs-anton"

VERDICT = re.compile(
    r"^Review: (APPROVED|CHANGES REQUESTED) by (Claude|Codex) at ([0-9a-f]{40})$"
)
AUTHOR_LINE = re.compile(r"\bAuthor:\s*(Claude|Codex)\b")
REVIEWER_LINE = re.compile(r"\bReviewer:\s*(Claude|Codex)\b")
ISSUE_URL = re.compile(rf"https://github\.com/{re.escape(REPO)}/issues/(\d+)")
GREEN = {"SUCCESS", "NEUTRAL", "SKIPPED"}
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

    def to_json(self) -> str:
        return json.dumps(
            {k: v for k, v in asdict(self).items() if v not in (None, [])}
        )


def other(agent: str) -> str:
    return AGENTS[1] if agent == AGENTS[0] else AGENTS[0]


def pr_author(pr: dict[str, Any]) -> str | None:
    """Both agents share one GitHub account, so the PR body names the author."""
    body = pr.get("body") or ""
    m = AUTHOR_LINE.search(body)
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
    """'green', 'failed' or 'pending' for the PR head."""
    states = []
    for c in pr.get("statusCheckRollup") or []:
        states.append(
            (c.get("conclusion") or c.get("state") or c.get("status") or "").upper()
        )
    if any(s in FAILED for s in states):
        return "failed"
    if all(s in GREEN for s in states):
        return "green"
    return "pending"


def labels(item: dict[str, Any]) -> set[str]:
    names = set()
    for lbl in item.get("labels") or []:
        name = lbl.get("name") if isinstance(lbl, dict) else lbl
        if isinstance(name, str):
            names.add(name)
    return names


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
    return {int(n) for n in ISSUE_URL.findall(text or "")}


def decide(agent: str, state: dict[str, Any], paused: bool = False) -> list[Action]:
    """All actions for the agent, highest priority first. Never empty."""
    if paused:
        return [Action("stop", f"pause file {PAUSE_FILE} exists")]
    peer = other(agent)
    prs = [
        p
        for p in state.get("prs", [])
        if p.get("baseRefName") in BASES and ESCALATION_LABEL not in labels(p)
    ]
    mine = [p for p in prs if pr_author(p) == agent]
    theirs = [p for p in prs if pr_author(p) == peer]
    ranked: dict[str, list[Action]] = {}

    def add(kind: str, act: Action) -> None:
        ranked.setdefault(kind, []).append(act)

    for p in mine:
        n, head = p["number"], p["headRefOid"]
        if p.get("isDraft"):
            add("continue", Action("continue", "my draft PR", pr=n, sha=head))
            continue
        theirs_v = verdicts(p, peer)
        latest = theirs_v[-1] if theirs_v else None
        rounds = len({v["sha"] for v in theirs_v if v["state"] == "CHANGES REQUESTED"})
        if latest and latest["sha"] == head and latest["state"] == "CHANGES REQUESTED":
            if rounds >= MAX_ROUNDS:
                add(
                    "escalate",
                    Action(
                        "escalate",
                        f"{rounds} changes-requested rounds; add label {ESCALATION_LABEL} and ask Anton",
                        pr=n,
                        sha=head,
                        comments=[latest["url"]] if latest.get("url") else [],
                    ),
                )
                continue
            previous = theirs_v[-2]["at"] if len(theirs_v) > 1 else None
            add(
                "fix",
                Action(
                    "fix",
                    f"{peer} requested changes for the current head",
                    pr=n,
                    sha=head,
                    comments=findings(p, latest, previous),
                ),
            )
            continue
        checks = checks_state(p)
        if p.get("mergeable") == "CONFLICTING":
            add(
                "resolve-conflict",
                Action(
                    "resolve-conflict", "PR conflicts with its base", pr=n, sha=head
                ),
            )
            continue
        if checks == "failed":
            add(
                "fix-checks",
                Action(
                    "fix-checks", "checks failed on the current head", pr=n, sha=head
                ),
            )
            continue
        if (
            latest
            and latest["sha"] == head
            and latest["state"] == "APPROVED"
            and checks == "green"
        ):
            add(
                "merge",
                Action(
                    "merge",
                    f"{peer} approved the current head; checks green",
                    pr=n,
                    sha=head,
                ),
            )

    for p in theirs:
        if p.get("isDraft"):
            continue
        n, head = p["number"], p["headRefOid"]
        if not any(v["sha"] == head for v in verdicts(p, agent)):
            add(
                "review",
                Action(
                    "review",
                    f"{peer}'s PR has no verdict from {agent} for its head",
                    pr=n,
                    sha=head,
                ),
            )

    linked = set()
    for p in prs:
        linked |= issue_numbers(p.get("body") or "")
    open_mine = len(mine)
    items = [
        i
        for i in state.get("items", [])
        if i.get("executor") == agent
        and (i.get("content") or {}).get("type") == "Issue"
        and ESCALATION_LABEL not in labels(i)
    ]
    items.sort(key=lambda i: (str(i.get("wave") or "9"), i["content"]["number"]))
    for i in items:
        number, url = i["content"]["number"], i["content"]["url"]
        if number in linked:
            continue
        if i.get("status") == "In progress":
            add(
                "continue",
                Action("continue", "my In progress leaf has no PR yet", issue=url),
            )
        elif i.get("status") == "Ready" and open_mine < MAX_OPEN_PRS:
            add("claim", Action("claim", "Ready leaf assigned to me", issue=url))

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
    actions = [a for kind in order for a in ranked.get(kind, [])]
    if any(a.action == "continue" for a in actions):
        actions = [
            a for a in actions if a.action != "claim"
        ]  # one implementation at a time
    return actions or [Action("idle", "nothing to do")]


def gh_json(args: list[str]) -> Any:
    out = subprocess.run(
        ["gh", *args], check=True, capture_output=True, text=True, timeout=120
    )
    return json.loads(out.stdout)


def fetch_state() -> dict[str, Any]:
    fields = (
        "number,title,body,baseRefName,headRefName,headRefOid,isDraft,labels,"
        "mergeable,statusCheckRollup,comments"
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
    items = gh_json(
        [
            "project",
            "item-list",
            PROJECT_NUMBER,
            "--owner",
            PROJECT_OWNER,
            "--format",
            "json",
            "--limit",
            "500",
        ]
    )["items"]
    return {"prs": prs, "items": items}


def status(state: dict[str, Any], paused: bool) -> str:
    lines = [f"Paused: {'yes' if paused else 'no'}"]
    for agent in AGENTS:
        lines.append(f"\n{agent}:")
        for a in decide(agent, state, paused):
            target = f"PR {a.pr}" if a.pr else (a.issue or "")
            lines.append(f"  {a.action:<17} {target:<55} {a.reason}")
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
    args = parser.parse_args()

    paused = PAUSE_FILE.exists()
    state = (
        json.loads(args.state_file.read_text()) if args.state_file else fetch_state()
    )
    if args.dump_state:
        args.dump_state.write_text(json.dumps(state, indent=1))
    if args.status:
        print(status(state, paused))
    else:
        print(decide(args.agent.capitalize(), state, paused)[0].to_json())
    return 0


if __name__ == "__main__":
    sys.exit(main())
