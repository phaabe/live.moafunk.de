"""Git commands the headless Claude runner may run after a permission prompt.

The runner settings (claude-runner-settings.json) send plain `git push`,
`git rebase` and every `git -<option> ...` form to permission_gate.py, which
hands git commands to decide() here. G below is `git -C <abs>` or
`git -C<abs>`: exactly one -C, no other global option (no `-c`).

  G push [-u] [-q] origin B                    owned worktree, B its branch
  G push [-q] --force-with-lease=refs/heads/B:S origin HEAD:refs/heads/B
                                               S pinned by the approved rebase
  G push origin --delete B                     merge tick, PR merged, B its head
  G rebase [-q] origin/BASE                    clean tree, BASE the PR's base
  G rebase --continue | --abort                the recorded rebase only
  G add -- <paths...> / G commit --file <file> / G fetch [-q] origin
                                               owned worktree
  G status|log|diff|show|rev-parse|ls-files|merge-base with the read-only
                                               flags and revisions below

Optional flags occur at most once, in the order shown. Plain `git push` and
`git rebase` are refused: the gate cannot see the Bash tool's directory.

"Owned worktree": the runner context file (EPIC_CONTEXT_FILE, written by
runner_worktree.py for this action) names it, its real path is the real
`<EPIC_WORKTREE_DIR>/<B>` (no symlink below the fixed dir), Git lists it as a
worktree of the runner checkout (EPIC_TRUSTED_ROOT), and every origin fetch
and push URL (after insteadOf rewrites) is this GitHub repository: the full
URL with host, not only a matching path. `add` and `commit` also need B checked out;
during the runner's recorded rebase only `add` works in the detached HEAD.
Read-only commands may also run in the runner checkout or another worktree
under EPIC_WORKTREE_DIR.

Rebase and lease push read the PR fresh from GitHub: open, head branch in this
repository equals B, base is an epic base, `Executor: Claude`. The initial
rebase also needs the head to equal the action's SHA, which it pins as S in
`claude-rebases.json` in the state dir, with the onto commit and the original
local head. Continue and abort need Git's rebase state to match all three; an
approved abort retires the record. The lease push needs a finished rebase onto
the recorded commit and the PR head still at S; the gate never renews S.
Missing or stale context refuses the command.

Promotion barrier (runtime.py): while a runtime promotion marker exists,
every command except the read-only ones is refused unless the caller runs
inside a tick admitted before the promotion started.

Rebase policy (rebase_policy.py): a `resolve-conflict` rebase must go onto the
target tip the runner pinned for this attempt (EPIC_ATTEMPT_FILE). Paths added
during the recorded rebase are its conflicted files; the runner puts them into
the rebase record. Every lease push needs a valid test proof for the current
HEAD on the recorded onto commit, and a clean worktree and index.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import rebase_policy
import runtime
from github_quota import QuotaExhausted, run_gh
from next_action import BASES, REPO, pr_author
from runner_worktree import feature_branch, rebasing

SHA = re.compile(r"^[0-9a-f]{40}$")
# The whole destination: GitHub over https or ssh, this repository. Tests
# replace this with their local bare repository; production has no override.
TRUSTED = re.compile(
    r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    + re.escape(REPO)
    + r"(?:\.git)?/?"
)
# Words the shell would expand before git sees them.
EXPANDS = re.compile(r"[*?\[\]{}~!#]")
REBASE_ACTIONS = {"fix", "fix-checks", "resolve-conflict", "continue"}
READ_VERBS = {"status", "log", "diff", "show", "rev-parse", "ls-files", "merge-base"}
READ_FLAGS = {
    "--short", "--porcelain", "--oneline", "--stat", "--name-only",
    "--name-status", "--no-color", "--abbrev-ref", "--show-toplevel",
}  # fmt: skip
FETCH_REFSPEC = "+refs/heads/*:refs/remotes/origin/*"
# Operations a new rebase must not start on top of (paths in the git dir).
OPERATIONS = (
    "rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD",
    "REVERT_HEAD", "BISECT_LOG", "sequencer",
)  # fmt: skip
RECORDS = "claude-rebases.json"


class Refused(Exception):
    """The command is not approved; the message says why."""


def decide(words: list[str]) -> tuple[bool, str]:
    """(allowed, reason) for one git command, split into words."""
    try:
        return True, check(words)
    except Refused as refused:
        return False, str(refused)


def check(words: list[str]) -> str:
    for word in words:
        if EXPANDS.search(word):
            raise Refused(f"{word!r} has characters the shell would expand")
    path, rest = split_global(words[1:])
    if path is None:
        if rest[:1] in (["push"], ["rebase"]):
            raise Refused(
                f"plain `git {rest[0]}` is not approved: the gate cannot see the "
                f"shell directory; use `git -C <runner worktree> {rest[0]} ...`"
            )
        raise Refused(f"`git {rest[0]}` is not approved")
    verb, args = rest[0], rest[1:]
    if verb in READ_VERBS:
        top = readable(path)
        read_only(args, top)
        return f"read-only git {verb} in {top}"
    blocked = runtime.write_barrier()
    if blocked:
        raise Refused(blocked)
    if verb == "push":
        return push(path, args)
    if verb == "rebase":
        return rebase(path, args)
    if verb in ("add", "commit", "fetch"):
        return local_write(path, verb, args)
    raise Refused(f"`git -C <path> {verb}` is not approved")


def split_global(args: list[str]) -> tuple[Path | None, list[str]]:
    """(the -C path or None, the verb and its arguments)."""
    if not args:
        raise Refused("git needs a command")
    first = args[0]
    if first == "-C":
        if len(args) < 2:
            raise Refused("-C needs a path")
        value, rest = args[1], args[2:]
    elif first.startswith("-C"):
        value, rest = first[2:], args[1:]
    elif first.startswith("-"):
        raise Refused(f"git option {first} is not approved; only one -C <path>")
    else:
        return None, args
    if not rest:
        raise Refused("git needs a command")
    if rest[0].startswith("-"):
        raise Refused(f"git option {rest[0]} is not approved; only one -C <path>")
    if not os.path.isabs(value):
        raise Refused(f"-C {value} is not an absolute path")
    return Path(value), rest


# --- environment and context ------------------------------------------------


def env_path(name: str) -> Path:
    value = os.environ.get(name) or ""
    if not os.path.isabs(value):
        raise Refused(f"the runner did not set {name}")
    return Path(os.path.realpath(value))


def state_dir() -> Path:
    return Path(
        os.environ.get("EPIC_STATE_DIR") or Path.home() / ".local/state/epic-loop"
    )


def load_json(name: str) -> dict[str, Any]:
    path = os.environ.get(name) or ""
    try:
        data = json.loads(Path(path).read_text()) if path else None
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        raise Refused(f"missing runner {name}")
    return data


def action() -> dict[str, Any]:
    return load_json("EPIC_ACTION_FILE")


def context() -> dict[str, Any]:
    """The runner context of this action (runner_worktree.py), or refuse."""
    ctx = load_json("EPIC_CONTEXT_FILE")
    if ctx.get("action") != action():
        raise Refused("the runner context is stale: it is for another action")
    if not isinstance(ctx.get("branch"), str) or not isinstance(
        ctx.get("worktree"), str
    ):
        raise Refused("the runner context has no branch or worktree")
    return ctx


def git(top: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(top), *args], capture_output=True, text=True, timeout=60
    )


def git_out(top: Path, *args: str) -> str:
    out = git(top, *args)
    if out.returncode != 0:
        raise Refused(f"git {' '.join(args)} failed: {out.stderr.strip()}")
    return out.stdout.strip()


def registered(root: Path) -> set[Path]:
    """Real paths of all worktrees of the runner checkout."""
    lines = git_out(root, "worktree", "list", "--porcelain").splitlines()
    return {
        Path(os.path.realpath(line[len("worktree ") :]))
        for line in lines
        if line.startswith("worktree ")
    }


def readable(path: Path) -> Path:
    """The runner checkout or a worktree of it under the fixed directory."""
    root = env_path("EPIC_TRUSTED_ROOT")
    real = Path(os.path.realpath(path))
    if real == root:
        return real
    if real.is_relative_to(env_path("EPIC_WORKTREE_DIR")) and real in registered(root):
        return real
    raise Refused(f"{path} is not the runner checkout or a runner worktree")


def owned(path: Path, branch: str) -> Path:
    """`<fixed dir>/<branch>`, a registered worktree with this repository as origin."""
    root = env_path("EPIC_TRUSTED_ROOT")
    real = Path(os.path.realpath(path))
    # The real fixed dir plus the branch, compared without resolving it again:
    # a symlink below the fixed dir that points elsewhere does not match.
    expected = env_path("EPIC_WORKTREE_DIR") / branch
    if real != expected:
        raise Refused(f"{path} is not the runner worktree of {branch} ({expected})")
    if real not in registered(root):
        raise Refused(f"{real} is not a worktree of the runner checkout")
    # Git fetches from and pushes to every configured URL: check them all.
    for args in (("--all",), ("--push", "--all")):
        urls = git_out(real, "remote", "get-url", *args, "origin").splitlines()
        for url in urls or [""]:
            if not TRUSTED.fullmatch(url):
                kind = "push URL" if "--push" in args else "URL"
                raise Refused(f"origin {kind} {url or '(none)'} is not {REPO}")
    return real


def context_worktree(path: Path) -> tuple[dict[str, Any], Path]:
    ctx = context()
    if not branch_ok(ctx["branch"]):
        raise Refused(f"{ctx['branch']} is not a feature branch")
    top = owned(path, ctx["branch"])
    if top != Path(os.path.realpath(ctx["worktree"])):
        raise Refused(f"{path} is not this action's worktree {ctx['worktree']}")
    return ctx, top


def branch_ok(name: str) -> bool:
    """A feature branch with a valid Git name and no `..`."""
    if not feature_branch(name) or ".." in name:
        return False
    out = subprocess.run(
        ["git", "check-ref-format", "--branch", name],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return out.returncode == 0 and out.stdout.strip() == name


def current_branch(top: Path) -> str:
    return git(top, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()


def git_path(top: Path, name: str) -> Path:
    return Path(git_out(top, "rev-parse", "--path-format=absolute", "--git-path", name))


# --- fresh PR ---------------------------------------------------------------


def read_pr(number: int) -> dict[str, Any]:
    """The PR from GitHub REST. Tests replace this function."""
    return json.loads(run_gh(["api", f"repos/{REPO}/pulls/{number}"], timeout=60))


def fresh_pr(number: int, branch: str) -> dict[str, Any]:
    try:
        pr = read_pr(number)
    except QuotaExhausted:
        raise Refused("GitHub quota exhausted; the PR cannot be checked") from None
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise Refused(f"fresh read of PR {number} failed: {error}") from None
    head = pr.get("head") or {}
    if (head.get("repo") or {}).get("full_name") != REPO:
        raise Refused(f"PR {number} head is not a branch of {REPO}")
    if head.get("ref") != branch:
        raise Refused(f"PR {number} head is {head.get('ref')}, not {branch}")
    return pr


def open_pr(number: int, branch: str) -> dict[str, Any]:
    pr = fresh_pr(number, branch)
    if pr.get("state") != "open":
        raise Refused(f"PR {number} is not open")
    base = (pr.get("base") or {}).get("ref")
    if base not in BASES:
        raise Refused(f"PR {number} base {base} is not an epic base")
    if (pr_author({"body": pr.get("body")}) or "").lower() != "claude":
        raise Refused(f"PR {number} is not assigned to Claude")
    return pr


def pr_context(path: Path) -> tuple[dict[str, Any], Path, int]:
    """Context, worktree and PR number of a PR action that may rebase."""
    ctx, top = context_worktree(path)
    act = action()
    number = act.get("pr")
    if act.get("action") not in REBASE_ACTIONS or not isinstance(number, int):
        raise Refused(f"a {act.get('action')} tick does not rebase or force-push")
    if ctx.get("pr") != number:
        raise Refused("the runner context is for another PR")
    return ctx, top, number


# --- rebase records ---------------------------------------------------------


def records() -> dict[str, Any]:
    path = state_dir() / RECORDS
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise Refused(f"rebase records unreadable: {error}") from None
    return data if isinstance(data, dict) else {}


def save_record(branch: str, record: dict[str, Any]) -> None:
    data = records()
    data[branch] = record
    path = state_dir() / RECORDS
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=1))
    temp.replace(path)


def recorded(branch: str, top: Path, number: int) -> dict[str, Any]:
    record = records().get(branch)
    if (
        not isinstance(record, dict)
        or record.get("worktree") != str(top)
        or record.get("pr") != number
    ):
        raise Refused(f"no rebase of {branch} recorded by the runner")
    return record


# --- verbs ------------------------------------------------------------------


def rebase(path: Path, args: list[str]) -> str:
    if args in (["--continue"], ["--abort"]):
        return rebase_step(path, args[0])
    if "--skip" in args:
        raise Refused(
            "rebase --skip can drop a commit; it needs a separate explicit decision"
        )
    rest = args[1:] if args[:1] == ["-q"] else args
    if len(rest) != 1 or not rest[0].startswith("origin/"):
        raise Refused(
            "rebase must be `rebase [-q] origin/<base>`, `--continue` or `--abort`"
        )
    base = rest[0][len("origin/") :]
    ctx, top, number = pr_context(path)
    branch = ctx["branch"]
    pr = open_pr(number, branch)
    if (pr.get("base") or {}).get("ref") != base:
        raise Refused(f"PR {number} base is {pr['base']['ref']}, not {base}")
    pinned = str((pr.get("head") or {}).get("sha") or "")
    if not SHA.match(pinned) or pinned != action().get("sha"):
        raise Refused(f"PR {number} head moved since the action was selected")
    if current_branch(top) != branch:
        raise Refused(f"{top} is not on {branch}")
    for name in OPERATIONS:
        if git_path(top, name).exists():
            raise Refused(f"{top} has an unfinished operation ({name})")
    if git_out(top, "status", "--porcelain"):
        raise Refused(f"{top} has uncommitted or untracked changes")
    if git(top, "merge-base", "--is-ancestor", pinned, "HEAD").returncode != 0:
        raise Refused(f"local {branch} does not contain the PR head {pinned[:7]}")
    onto = git_out(
        top, "rev-parse", "--verify", f"refs/remotes/origin/{base}^{{commit}}"
    )
    if action().get("action") == "resolve-conflict":
        tip = pinned_tip(number, pinned, base)
        if onto != tip:
            raise Refused(
                f"origin/{base} is {onto[:7]}, not the target tip {tip[:7]} pinned "
                "for this attempt; fetch origin, or stop if the base moved on"
            )
    orig = git_out(top, "rev-parse", "--verify", "HEAD")
    save_record(
        branch,
        {
            "pr": number,
            "worktree": str(top),
            "base": base,
            "sha": pinned,
            "onto": onto,
            "orig": orig,
            "conflicted": [],
        },
    )
    return f"rebase of {branch} onto origin/{base}, lease pinned at {pinned[:7]}"


def this_attempt(record: dict[str, Any], number: int) -> None:
    """A resolve-conflict tick may only finish or push a rebase onto its own
    pinned target tip. A rebase left unfinished by an earlier tick onto an
    older tip must be aborted and started again."""
    if action().get("action") != "resolve-conflict":
        return
    tip = pinned_tip(number, str(record.get("sha")), str(record.get("base")))
    if record.get("onto") != tip:
        raise Refused(
            f"the recorded rebase is onto {str(record.get('onto'))[:7]}, not this "
            f"attempt's target tip {tip[:7]}; abort it and rebase again"
        )


def pinned_tip(number: int, head: str, base: str) -> str:
    """The target tip the runner pinned for this resolve-conflict attempt."""
    attempt = load_json("EPIC_ATTEMPT_FILE")
    if (
        attempt.get("pr") != number
        or attempt.get("head") != head
        or attempt.get("base") != base
        or not SHA.match(str(attempt.get("tip") or ""))
    ):
        raise Refused("the runner's attempt pin is for another PR, head or base")
    return str(attempt["tip"])


def rebase_state(top: Path, name: str) -> str:
    """One file of Git's rebase state (onto, orig-head), empty when missing."""
    for folder in ("rebase-merge", "rebase-apply"):
        file = git_path(top, f"{folder}/{name}")
        if file.is_file():
            return file.read_text().strip()
    return ""


def active_rebase(top: Path, branch: str, number: int) -> dict[str, Any]:
    """The runner's record, when the rebase in progress is that rebase.

    Identity: branch, worktree, PR, onto commit and the original head. A later
    rebase onto the same base from another head does not match. A record
    whose abort was approved approves nothing more: a rebase started again by
    hand looks the same, so even a retried abort is left to a human."""
    record = recorded(branch, top, number)
    if rebasing(top) != branch:
        raise Refused(f"{top} is not rebasing {branch}")
    if (
        record.get("aborted")
        or rebase_state(top, "onto") != record.get("onto")
        or rebase_state(top, "orig-head") != record.get("orig")
    ):
        raise Refused("the rebase in progress is not the one the runner recorded")
    return record


def rebase_step(path: Path, step: str) -> str:
    ctx, top, number = pr_context(path)
    branch = ctx["branch"]
    record = active_rebase(top, branch, number)
    if step == "--continue":
        pr = open_pr(number, branch)
        if (pr.get("base") or {}).get("ref") != record.get("base"):
            raise Refused(f"PR {number} base changed during the rebase")
        this_attempt(record, number)
    else:
        # Retired: after this approval the record approves no further
        # continue, abort, add or lease push, until a new approved rebase
        # replaces it.
        save_record(branch, {**record, "aborted": True})
    return f"rebase {step} of {branch}"


def push(path: Path, args: list[str]) -> str:
    if args[:2] == ["origin", "--delete"]:
        if len(args) != 3:
            raise Refused("delete exactly one branch")
        return delete(path, args[2])
    rest = list(args)
    upstream = rest[:1] == ["-u"]
    if upstream:
        rest = rest[1:]
    if rest[:1] == ["-q"]:
        rest = rest[1:]
    if rest[:1] and rest[0].startswith("--force-with-lease"):
        if upstream:
            raise Refused("the lease push takes no -u")
        return lease_push(path, rest)
    if len(rest) != 2 or rest[0] != "origin" or rest[1].startswith(("-", "+")):
        raise Refused(
            "push must be `push [-u] [-q] origin <branch>` or the lease form "
            "`push [-q] --force-with-lease=refs/heads/<branch>:<sha> origin "
            "HEAD:refs/heads/<branch>`"
        )
    branch = rest[1]
    ctx, top = context_worktree(path)
    if branch != ctx["branch"]:
        raise Refused(f"{branch} is not this action's branch {ctx['branch']}")
    if current_branch(top) != branch:
        raise Refused(f"{top} is not on {branch}")
    return f"push of {branch}"


def lease_push(path: Path, rest: list[str]) -> str:
    lease = re.fullmatch(
        r"--force-with-lease=refs/heads/([^:]+):([0-9a-f]{40})", rest[0]
    )
    if not lease:
        raise Refused(
            "the lease must be --force-with-lease=refs/heads/<branch>:<40-char sha>"
        )
    branch, pinned = lease.groups()
    if rest[1:] != ["origin", f"HEAD:refs/heads/{branch}"]:
        raise Refused(f"the lease push must be `origin HEAD:refs/heads/{branch}`")
    ctx, top, number = pr_context(path)
    if branch != ctx["branch"]:
        raise Refused(f"{branch} is not this action's branch {ctx['branch']}")
    record = recorded(branch, top, number)
    if record.get("aborted"):
        raise Refused(f"the recorded rebase of {branch} was aborted")
    if pinned != record.get("sha"):
        raise Refused(
            f"the lease must pin {record.get('sha')}, the head before the rebase"
        )
    this_attempt(record, number)
    if rebasing(top) is not None or current_branch(top) != branch:
        raise Refused(f"{top} is not on {branch} with a finished rebase")
    if git(top, "merge-base", "--is-ancestor", record["onto"], "HEAD").returncode:
        raise Refused(f"{branch} is not rebased onto the recorded base commit")
    pr = open_pr(number, branch)
    if (pr.get("base") or {}).get("ref") != record.get("base"):
        raise Refused(f"PR {number} base changed since the rebase")
    if (pr.get("head") or {}).get("sha") != pinned:
        raise Refused(
            f"PR {number} head moved since the rebase; the lease is not renewed"
        )
    proven(top, number, str(record["onto"]))
    return f"lease push of {branch} over {pinned[:7]}, tests proven"


def proven(top: Path, number: int, onto: str) -> None:
    """A clean tree and a valid test proof for HEAD (rebase_policy.py)."""
    try:
        problem = rebase_policy.unclean(top)
        head = git_out(top, "rev-parse", "--verify", "HEAD")
        problem = problem or rebase_policy.proof_problem(
            rebase_policy.load_proof(state_dir(), number, head), top, number, head, onto
        )
    except (rebase_policy.Problem, OSError, ValueError) as error:
        problem = str(error)
    if problem:
        raise Refused(
            f"no valid test proof: {problem}; run rebase_policy.py prove in the "
            "clean worktree first"
        )


def delete(path: Path, branch: str) -> str:
    act = action()
    number = act.get("pr")
    if act.get("action") != "merge" or not isinstance(number, int):
        raise Refused("only a merge tick deletes its PR branch")
    if not branch_ok(branch):
        raise Refused(f"{branch} is not a feature branch")
    owned(path, branch)
    if not fresh_pr(number, branch).get("merged_at"):
        raise Refused(f"PR {number} is not merged; its branch stays")
    return f"delete of merged {branch}"


def on_branch(ctx: dict[str, Any], top: Path, verb: str) -> None:
    """add/commit change only the context branch. During a conflict, `add`
    also works in the detached HEAD of the runner's recorded rebase."""
    branch = ctx["branch"]
    if current_branch(top) == branch and rebasing(top) is None:
        return
    number = action().get("pr")
    if verb == "add" and isinstance(number, int) and ctx.get("pr") == number:
        active_rebase(top, branch, number)
        return
    raise Refused(f"{top} is not on {branch}")


def local_write(path: Path, verb: str, args: list[str]) -> str:
    ctx, top = context_worktree(path)
    if verb in ("add", "commit"):
        on_branch(ctx, top, verb)
    if verb == "add":
        if args[:1] != ["--"] or len(args) < 2:
            raise Refused("add must be `add -- <paths...>`")
        for name in args[1:]:
            inside(top, name)
        if rebasing(top) is not None:
            resolved(ctx["branch"], top, args[1:])
        return f"add in {top}"
    if verb == "commit":
        if len(args) != 2 or args[0] != "--file":
            raise Refused("commit must be `commit --file <message-file>`")
        message = Path(args[1]) if os.path.isabs(args[1]) else top / args[1]
        if message.is_symlink() or not message.is_file():
            raise Refused(f"{args[1]} is not a regular file")
        return f"commit in {top}"
    if args not in (["origin"], ["-q", "origin"]):
        raise Refused("fetch must be `fetch [-q] origin`")
    refspecs = git_out(top, "config", "--get-all", "remote.origin.fetch").splitlines()
    if refspecs != [FETCH_REFSPEC]:
        raise Refused("origin has non-standard fetch refspecs")
    return f"fetch in {top}"


def resolved(branch: str, top: Path, names: list[str]) -> None:
    """Paths added during the recorded rebase: its conflicted files."""
    record = records().get(branch) or {}
    paths = set(record.get("conflicted") or [])
    for name in names:
        real = Path(os.path.realpath(top / name))
        paths.add(str(real.relative_to(top)))
    save_record(branch, {**record, "conflicted": sorted(paths)})


def inside(top: Path, name: str) -> None:
    if name.startswith((":", "-")):
        raise Refused(f"path {name} is not a plain path")
    real = Path(os.path.realpath(top / name))
    if not real.is_relative_to(top):
        raise Refused(f"path {name} is outside {top}")


def revision(word: str) -> bool:
    for sep in ("...", ".."):
        if sep in word:
            left, _, right = word.partition(sep)
            return single_revision(left) and single_revision(right)
    return single_revision(word)


def single_revision(word: str) -> bool:
    if word == "HEAD" or SHA.match(word):
        return True
    ref = word.removeprefix("origin/")
    return word.startswith("origin/") and (ref in BASES or feature_branch(ref))


def read_only(args: list[str], top: Path) -> None:
    i = 0
    while i < len(args):
        word = args[i]
        if word == "--":
            for name in args[i + 1 :]:
                inside(top, name)
            return
        if word == "-n":
            if i + 1 >= len(args) or not args[i + 1].isdigit():
                raise Refused("-n needs a number")
            i += 1
        elif not (
            word in READ_FLAGS or re.fullmatch(r"-[0-9]+", word) or revision(word)
        ):
            raise Refused(f"{word} is not an approved read-only argument")
        i += 1
