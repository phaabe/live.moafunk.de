"""Check on GitHub that a tick's action really landed.

A model session can exit 0 without its work reaching GitHub, for example when
a push or merge was denied. claude-tick.sh runs this after the session:

  merge                         the PR is merged
  review                        an unedited "Review: ... by <Agent> at <selected sha>"
                                comment was created during the tick
  fix-checks, resolve-conflict  the PR head moved (a push arrived)
  fix                           the PR head moved, or a comment whose first line
                                is "Reply-only fix by <Agent> at <selected sha>"
                                was created during the tick
  adopt                         the PR body has the owner lines for this agent
                                at line start and still holds the original body

`continue`, `claim`, `escalate`, `idle` and `stop` are not checked yet. An
unknown action is bad input: it never passes. Exit 0 when the action landed or
is not checked, 1 when it did not land, 2 on bad input, 4 on a GraphQL quota error
(wait stored, see github_quota.py). GitHub read errors exit 1:
an unverified tick is not reported as done.

With --worktree (the runner worktree of the tick), a moved head counts only when
it is this worktree's HEAD and no rebase is left in it. Another writer's push
also moves the head; a refused lease push or an unfinished conflict then stays
a failed tick, and the repeat gate suppresses it.

Any other comment (progress, a blocker, a bot) does not count as a fix. Both
agents share one GitHub account, so the marker names the agent.

Usage:
  tick_verify.py --agent claude --action-file action.json --since 2026-09-28T12:00:00Z
      [--worktree DIR]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from github_quota import QuotaExhausted, run_gh, stop_on_quota
from next_action import EPIC, REPO, body_digest, issue_url, other

PUSHES = {"fix-checks", "resolve-conflict"}
CHECKED = {"merge", "review", "fix", "adopt", *PUSHES}
UNCHECKED = {"continue", "claim", "escalate", "idle", "stop"}

Fetch = Callable[[list[str]], Any]


def gh_json(args: list[str]) -> Any:
    return json.loads(run_gh(args))


def unpublished(worktree: str, head: str) -> str | None:
    """Why `head` is not this worktree's finished work, or None."""

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", worktree, *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout.strip()

    try:
        for state in ("rebase-merge", "rebase-apply"):
            if Path(
                git("rev-parse", "--path-format=absolute", "--git-path", state)
            ).exists():
                return f"a rebase is unfinished in {worktree}"
        local = git("rev-parse", "HEAD")
    except (OSError, subprocess.SubprocessError) as error:
        return f"cannot read {worktree}: {error}"
    if local != head:
        return f"the new head {head[:7]} is not the runner's work ({local[:7]})"
    return None


def landed(
    agent: str,
    action: dict[str, Any],
    since: str,
    fetch: Fetch = gh_json,
    worktree: str | None = None,
) -> tuple[bool, str]:
    """(landed, reason) for one selected action."""
    kind = action.get("action")
    if kind in UNCHECKED:
        return True, f"{kind} is not checked"
    if kind not in CHECKED:
        raise ValueError(f"unknown action {kind!r}")
    number, sha = action.get("pr"), action.get("sha")
    if not isinstance(number, int) or not isinstance(sha, str):
        raise ValueError(f"{kind} action needs pr and sha")
    if kind == "adopt":
        return adopted(agent, action, fetch)
    pr = fetch(
        ["pr", "view", str(number), "--repo", REPO, "--json", "state,headRefOid"]
    )
    if kind == "merge":
        return pr["state"] == "MERGED", f"PR {number} state is {pr['state']}"
    moved = pr["headRefOid"] != sha
    if moved and worktree:
        reason = unpublished(worktree, pr["headRefOid"])
        if reason:
            return False, f"PR {number} head moved, but {reason}"
    if kind in PUSHES:
        return moved, f"PR {number} head {'moved' if moved else 'did not move'}"
    comments = fetch(
        [
            "api",
            f"repos/{REPO}/issues/{number}/comments?since={since}&per_page=100",
        ]
    )
    new = [c for c in comments if c.get("created_at", "") >= since]
    name = agent.capitalize()
    if kind == "review":
        verdict = re.compile(
            rf"^Review: (APPROVED|CHANGES REQUESTED) by {name} at {sha}$"
        )
        found = any(
            verdict.match((c.get("body") or "").strip()) and not edited(c, fetch)
            for c in new
        )
        return found, f"verdict for {sha[:7]} {'posted' if found else 'missing'}"
    if moved:
        return True, "head moved"
    marker = f"Reply-only fix by {name} at {sha}"
    if any((c.get("body") or "").strip().splitlines()[:1] == [marker] for c in new):
        return True, "reply-only fix posted"
    return False, f"PR {number} head did not move and no reply-only fix was posted"


def owner_lines(agent: str, lane: str | None) -> dict[str, re.Pattern[str]]:
    """The line-start owner lines `adopt` must add, by key."""
    name = agent.capitalize()
    issue = re.escape(issue_url(0)[:-1]) + r"\d+"
    lane_value = re.escape(lane) if lane else r"\S+"
    return {
        "Epic": re.compile(
            rf"^Epic:[ \t]*{re.escape(issue_url(EPIC))}[ \t]*$", re.MULTILINE
        ),
        "Executor": re.compile(rf"^Executor:[ \t]*{name}[ \t]*$", re.MULTILINE),
        "Lane": re.compile(rf"^Lane:[ \t]*{lane_value}[ \t]*$", re.MULTILINE),
        "Reviewer": re.compile(rf"^Reviewer:[ \t]*{other(name)}[ \t]*$", re.MULTILINE),
        "Leaf IDs": re.compile(r"^Leaf IDs:[ \t]*\S.*$", re.MULTILINE),
        "Issue": re.compile(rf"^Issue:[ \t]*{issue}[ \t]*$", re.MULTILINE),
    }


def adopted(agent: str, action: dict[str, Any], fetch: Fetch) -> tuple[bool, str]:
    """The PR body names this agent as owner and keeps the original text."""
    number = action["pr"]
    expected = action.get("body_sha")
    if not isinstance(expected, str):
        raise ValueError("adopt action needs body_sha")
    body = fetch(["api", f"repos/{REPO}/pulls/{number}"]).get("body") or ""
    missing = [
        key
        for key, line in owner_lines(agent, action.get("lane")).items()
        if not line.search(body)
    ]
    if missing:
        return False, f"PR {number} body lacks {', '.join(missing)}"
    if body_digest(body) != expected:
        return False, f"PR {number} body lost or changed its original text"
    return True, f"PR {number} adopted by {agent.capitalize()}"


def edited(comment: dict[str, Any], fetch: Fetch) -> bool:
    """True when GitHub records a body edit. An edited verdict counts as none."""
    query = "query($id:ID!){node(id:$id){... on IssueComment{lastEditedAt}}}"
    node = fetch(
        ["api", "graphql", "-f", f"query={query}", "-f", f"id={comment['node_id']}"]
    )
    return node["data"]["node"]["lastEditedAt"] is not None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", choices=("claude", "codex"), required=True)
    parser.add_argument("--action-file", required=True)
    parser.add_argument("--since", required=True, help="tick start, UTC ISO 8601")
    parser.add_argument("--worktree", help="the tick's runner worktree")
    args = parser.parse_args()
    try:
        with open(args.action_file) as f:
            action = json.load(f)
        ok, reason = landed(
            args.agent, action, args.since, worktree=args.worktree or None
        )
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"tick: cannot verify: {error}", file=sys.stderr)
        return 2
    except QuotaExhausted as error:
        return stop_on_quota(error)
    except subprocess.CalledProcessError:
        print("tick: cannot verify: GitHub read failed", file=sys.stderr)
        return 1
    if not ok:
        print(f"tick: {action.get('action')} did not land: {reason}", file=sys.stderr)
        return 1
    print(f"tick: verified: {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
