"""Prepare the runner's feature worktree before a model session starts.

The headless runner edits feature branches only inside one fixed directory,
never in a checkout a human session holds. For the actions that edit a branch
(claim, continue, fix, fix-checks, resolve-conflict), claude-tick.sh calls
`prepare` after the gate check and before the model starts:

  - It checks the repository (origin is phaabe/live.moafunk.de) and the branch.
    For a PR: the head is a branch of this repository, it is a feature branch,
    the base is an epic base, and the PR's `Executor:` is this agent. For an
    issue: the one existing feature branch `<type>/<issue>-...`, or a new
    `feat/<issue>-<slug>` from origin/dev/312-interim.
  - It resumes `<dir>/<branch>` when that checkout already holds the branch, and
    changes nothing in it, so unfinished work stays. Otherwise it runs
    `git worktree add`.
  - Git refuses a branch that is checked out in another worktree. Then no model
    starts: it reports `handoff needed: <branch> in <path>` and exits 3. The
    human releases their checkout; a later tick tries again.

Never `--force`, `--ignore-other-worktrees`, or any change to another checkout.

Repeat suppression: a stop (handoff or another blocked state) is stored per
target in `<agent>-handoff.json` in the state dir. The same stop for the same
action (tick_gate.py fingerprint) within the repeat TTL exits 3 with one short
line, not a new handoff report. Git is asked again on every tick, so a released
branch is picked up at once; a ready worktree clears the record.

Runner context: with --context-file, a ready worktree also writes the action,
branch, base (PR only), PR, issue and worktree path there. The permission gate
(git_gate.py) approves git writes only for that context. Any other result
removes the file.

Usage:
  runner_worktree.py prepare --agent claude --action-file action.json --dir DIR
      [--context-file context.json]

Prints the worktree path (empty for actions without one). Exit codes: 0 ready,
2 bad settings, 3 stop without a model (handoff or blocked), 4 a read hit the
GraphQL quota (wait stored), 1 other errors.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from github_quota import QuotaExhausted, run_gh, stop_on_quota
from next_action import BASES, REPO, pr_author
from permission_gate import BRANCH
from tick_gate import DEFAULT_TTL, fingerprint, target_key, target_number

STATE_DIR = Path(
    os.environ.get("EPIC_STATE_DIR", Path.home() / ".local" / "state" / "epic-loop")
)
EDITING = {"claim", "continue", "fix", "fix-checks", "resolve-conflict"}
NEW_BASE = "dev/312-interim"
ORIGIN = re.compile(rf"[:/]{re.escape(REPO)}(\.git)?/?$")
# git 2.x: "... is already checked out at '<path>'" or
# "... is already used by worktree at '<path>'".
HELD = re.compile(r"already (?:checked out|used by worktree) at '([^']+)'")
SKIP = 3


class Stop(Exception):
    """No model this tick. The message is the report (handoff or blocked)."""


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=120
    )


def git_out(repo: Path, *args: str) -> str:
    out = git(repo, *args)
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {out.stderr.strip()}")
    return out.stdout


def check_repo(repo: Path) -> None:
    url = git_out(repo, "remote", "get-url", "origin").strip()
    if not ORIGIN.search(url):
        raise RuntimeError(f"origin is {url}, not {REPO}")


def rebasing(path: Path) -> str | None:
    """The branch a detached worktree is rebasing, from Git's rebase state."""
    for state in ("rebase-merge/head-name", "rebase-apply/head-name"):
        out = git(path, "rev-parse", "--path-format=absolute", "--git-path", state)
        head_name = Path(out.stdout.strip())
        if out.returncode == 0 and head_name.is_file():
            ref = head_name.read_text().strip()
            if ref.startswith("refs/heads/"):
                return ref[len("refs/heads/") :]
    return None


def worktrees(repo: Path) -> dict[str, Path]:
    """Branch name -> path of the worktree that has it checked out.

    A worktree in an unfinished rebase is detached, but still holds its branch
    (Git refuses to check it out elsewhere too)."""
    held: dict[str, Path] = {}
    path: Path | None = None
    for line in git_out(repo, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = Path(line[len("worktree ") :])
        elif line.startswith("branch refs/heads/") and path is not None:
            held[line[len("branch refs/heads/") :]] = path
        elif line == "detached" and path is not None and path.is_dir():
            branch = rebasing(path)
            if branch:
                held[branch] = path
    return held


def feature_branch(name: str) -> bool:
    return bool(BRANCH.match(name)) and ".." not in name.split("/")


def slug(title: str) -> str:
    words = re.sub(r"[^a-z0-9]+", " ", title.lower()).split()[:6]
    return "-".join(words)[:40].strip("-") or "work"


def pr_branch(
    agent: str, number: int, read: Callable[[str], Any], info: dict[str, Any]
) -> str:
    pr = read(f"repos/{REPO}/pulls/{number}")
    if pr.get("state") != "open":
        raise Stop(f"PR {number} is not open")
    head = pr.get("head") or {}
    if ((head.get("repo") or {}).get("full_name")) != REPO:
        raise Stop(f"PR {number} head is not a branch of {REPO}")
    base = (pr.get("base") or {}).get("ref")
    if base not in BASES:
        raise Stop(f"PR {number} base {base} is not an epic base")
    if (pr_author({"body": pr.get("body")}) or "").lower() != agent:
        raise Stop(f"PR {number} is not assigned to {agent}")
    branch = str(head.get("ref") or "")
    if not feature_branch(branch):
        raise Stop(f"PR {number} head {branch} is not a feature branch")
    info["base"] = base
    return branch


def issue_branch(
    repo: Path, number: int, read: Callable[[str], Any]
) -> tuple[str, bool]:
    """(branch, new). The one feature branch for the issue, local or on origin."""
    refs = git_out(
        repo,
        "for-each-ref",
        "--format=%(refname)",
        "refs/heads",
        "refs/remotes/origin",
    ).split()
    names = {
        ref.removeprefix("refs/heads/").removeprefix("refs/remotes/origin/")
        for ref in refs
    }
    prefix = re.compile(rf"^[a-z]+/{number}-")
    found = sorted(n for n in names if prefix.match(n) and feature_branch(n))
    if len(found) > 1:
        raise Stop(f"issue {number} has several branches: {', '.join(found)}")
    if found:
        return found[0], False
    title = str(read(f"repos/{REPO}/issues/{number}").get("title") or "")
    return f"feat/{number}-{slug(title)}", True


def add(repo: Path, branch: str, path: Path, new: bool) -> None:
    """`git worktree add` without force. Git refuses a branch held elsewhere."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if new:
        args = [
            "worktree",
            "add",
            "--no-track",
            "-b",
            branch,
            str(path),
            f"origin/{NEW_BASE}",
        ]
    elif (
        git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}").returncode
        == 0
    ):
        args = ["worktree", "add", str(path), branch]
    else:
        args = [
            "worktree",
            "add",
            "--track",
            "-b",
            branch,
            str(path),
            f"origin/{branch}",
        ]
    out = git(repo, *args)
    if out.returncode == 0:
        return
    held = HELD.search(out.stderr)
    where = held.group(1) if held else worktrees(repo).get(branch)
    if where:
        raise Stop(f"handoff needed: {branch} in {where}")
    raise RuntimeError(f"git worktree add failed: {out.stderr.strip()}")


def prepare(
    agent: str,
    action: dict[str, Any],
    repo: Path,
    root: Path,
    read: Callable[[str], Any],
    info: dict[str, Any] | None = None,
) -> Path | None:
    """The ready worktree for the action, or None when it edits no branch.

    `info` receives the branch, base, PR and issue of a ready worktree."""
    info = {} if info is None else info
    if action.get("action") not in EDITING:
        return None
    check_repo(repo)
    number = target_number(action)
    if number is None:
        raise Stop("the action has no PR or issue")
    if action.get("pr"):
        branch, new = pr_branch(agent, number, read, info), False
    else:
        branch, new = issue_branch(repo, number, read)
        info["base"] = None
    info.update(branch=branch, pr=action.get("pr"), issue=action.get("issue"))
    path = root / branch
    # The real checkout must sit in the real fixed dir: a symlink at the
    # branch path (or below the dir) may point at a human's checkout.
    expected = root.resolve() / branch
    if path.resolve() != expected:
        raise Stop(f"{path} resolves outside {root}; handoff needed")
    held = worktrees(repo)
    where = held.get(branch)
    if where is not None and where.resolve() == expected:
        if not path.is_dir():
            # Pruning is left to a human: `git worktree prune` acts on all checkouts.
            raise Stop(f"{path} is registered for {branch} but missing")
        return path  # resume: the checkout and its unfinished work stay as they are
    if path.exists():
        raise Stop(f"{path} exists but does not hold {branch}")
    if where is not None:
        # Git would refuse it too; say where, and leave that checkout alone.
        raise Stop(f"handoff needed: {branch} in {where}")
    add(repo, branch, path, new)
    return path


def remember(
    agent: str,
    action: dict[str, Any],
    message: str | None,
    state_dir: Path,
    now: float,
    ttl: int,
) -> bool:
    """Store or clear the stop for this target. True when it is a repeat."""
    record_file = state_dir / f"{agent}-handoff.json"
    records = json.loads(record_file.read_text()) if record_file.exists() else {}
    key = target_key(action)
    old = records.get(key)
    records = {
        k: v
        for k, v in records.items()
        if isinstance(v, dict) and now - float(v.get("at", 0)) < ttl
    }
    if message is None:
        if key not in records:
            return False
        del records[key]
        repeat = False
    else:
        repeat = (
            isinstance(old, dict)
            and old.get("message") == message
            and old.get("fingerprint") == fingerprint(action)
            and now - float(old.get("at", 0)) < ttl
        )
        if not repeat:
            records[key] = {
                "message": message,
                "fingerprint": fingerprint(action),
                "at": now,
            }
    state_dir.mkdir(parents=True, exist_ok=True)
    record_file.write_text(json.dumps(records, indent=1))
    return repeat


def read_rest(endpoint: str) -> Any:
    return json.loads(run_gh(["api", endpoint], timeout=60))


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=["prepare"])
    parser.add_argument("--agent", required=True, choices=["claude", "codex"])
    parser.add_argument("--action-file", required=True, type=Path)
    parser.add_argument("--dir", required=True, type=Path)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--context-file", type=Path)
    args = parser.parse_args()

    repo = args.repo.resolve()
    root = args.dir
    if not root.is_absolute():
        print("worktree: --dir must be absolute", file=sys.stderr)
        return 2
    if root.resolve().is_relative_to(repo):
        print("worktree: --dir must be outside the runner checkout", file=sys.stderr)
        return 2
    action = json.loads(args.action_file.read_text())
    ttl = int(os.environ.get("EPIC_REPEAT_TTL_SECONDS", DEFAULT_TTL))
    if args.context_file:
        args.context_file.unlink(missing_ok=True)
    info: dict[str, Any] = {}
    try:
        path = prepare(args.agent, action, repo, root, read_rest, info)
    except QuotaExhausted as error:
        return stop_on_quota(error)
    except Stop as stop:
        message = str(stop)
        if remember(args.agent, action, message, STATE_DIR, time.time(), ttl):
            print(
                f"worktree: target {target_key(action)} unchanged since the last "
                "report, skip",
                file=sys.stderr,
            )
        else:
            print(f"worktree: {message}", file=sys.stderr)
        return SKIP
    if path is not None:
        remember(args.agent, action, None, STATE_DIR, time.time(), ttl)
        if args.context_file:
            context = {"action": action, **info, "worktree": str(path)}
            args.context_file.write_text(json.dumps(context, indent=1))
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
