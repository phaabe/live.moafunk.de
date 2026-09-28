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

Other actions are not checked yet. Exit 0 when the action landed or is not
checked, 1 when it did not land, 2 on bad input. GitHub read errors exit 1:
an unverified tick is not reported as done.

Any other comment (progress, a blocker, a bot) does not count as a fix. Both
agents share one GitHub account, so the marker names the agent.

Usage:
  tick_verify.py --agent claude --action-file action.json --since 2026-09-28T12:00:00Z
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from typing import Any, Callable

REPO = "phaabe/live.moafunk.de"
PUSHES = {"fix-checks", "resolve-conflict"}
CHECKED = {"merge", "review", "fix", *PUSHES}

Fetch = Callable[[list[str]], Any]


def gh_json(args: list[str]) -> Any:
    out = subprocess.run(["gh", *args], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def landed(
    agent: str, action: dict[str, Any], since: str, fetch: Fetch = gh_json
) -> tuple[bool, str]:
    """(landed, reason) for one selected action."""
    kind = action.get("action")
    if kind not in CHECKED:
        return True, f"{kind} is not checked"
    number, sha = action.get("pr"), action.get("sha")
    if not isinstance(number, int) or not isinstance(sha, str):
        raise ValueError(f"{kind} action needs pr and sha")
    pr = fetch(
        ["pr", "view", str(number), "--repo", REPO, "--json", "state,headRefOid"]
    )
    if kind == "merge":
        return pr["state"] == "MERGED", f"PR {number} state is {pr['state']}"
    moved = pr["headRefOid"] != sha
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
    args = parser.parse_args()
    try:
        with open(args.action_file) as f:
            action = json.load(f)
        ok, reason = landed(args.agent, action, args.since)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"tick: cannot verify: {error}", file=sys.stderr)
        return 2
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
