"""Shared rebase policy for `resolve-conflict` (both runners).

A rebase cancels the peer's verdict, because the verdict names the head SHA.
This module makes a rebase cheap to re-review and safe to publish:

  test proof     `prove` runs the suites the PR's paths require on the rebased
                 commit and writes a proof: commit, tree, target tip, commands,
                 results. The worktree and index must be clean before and after
                 the tests. The permission gate (git_gate.py) refuses the lease
                 push unless the proof matches the current HEAD and the tree is
                 still clean. Failed or skipped suites give no valid proof.
  rebase record  after the push, the owner's runner posts one comment in the
                 exact format of record_body() (`publish`). It holds no verdict.
  review scope   the peer's runner computes the review scope (`scope`): focused
                 only when the record is valid and matches the peer's last
                 verdict, else full.
  attempt limit  one store for both runners, keyed by PR, head and pinned target
                 tip (`attempt-check`, `attempt-start`, `attempt-finish`).
                 `refine` runs share it under the key next_action.py gives
                 (`refine:<issue>:<revision>:<reset>`, see refinement.py).

Values of one attempt (docs/implementation/epic-rules.md, section 8):

  old head         the PR head before the rebase. It is also the lease value:
                   `--force-with-lease=refs/heads/<branch>:<old head>`.
  target tip       the base tip pinned for this attempt: the rebase destination
                   and part of the attempt key. Never the lease value.
  old series base  `git merge-base <old head> <target tip>`: the base the
                   reviewed patch series was built on.

Required suites (SUITES, by path prefix of the files the PR changes against
the target tip). EPIC_REBASE_SUITES may name a JSON file with another list of
the same shape; the gate and verify read it from the runner's environment.

Attempt limit: EPIC_REBASE_ATTEMPT_LIMIT, default 2. Each started attempt counts
once, also when it fails or times out; only a landed one does not count. An
attempt whose model reported the GitHub quota is void. Pause, quota and read
waits and lock skips never start one. Cooldown expiry and comments do not reset
the count; a new head or a new target tip is a new key. At the limit no model
starts for the key and the owner's runner adds the label `needs-anton`. When
that post fails, the key stays suppressed and only the label post is retried.
For `refine` the limit is refinement.MAX_FAILED_RUNS and the label goes on the
issue; a new proposal revision or a reset by Anton is a new key.

Usage:
  rebase_policy.py prove --worktree W --pr N --base BASE [--state-dir D]
  rebase_policy.py attempt-check  --state-dir D --agent A --action-file F --out ATTEMPT
  rebase_policy.py attempt-start  --state-dir D --attempt-file ATTEMPT --id TICK
  rebase_policy.py attempt-finish --state-dir D --attempt-file ATTEMPT --id TICK
                                  --outcome succeeded|failed|void
  rebase_policy.py attempt-status --state-dir D
  rebase_policy.py publish --agent A --attempt-file ATTEMPT --worktree W
                           --rebases-file R [--state-dir D]
  rebase_policy.py scope --agent A --action-file F --repo-dir D --out SCOPE

Exit codes: 0 done (prove: valid proof; check/start: run), 1 not done (prove:
a suite failed or was skipped; publish: nothing to publish), 2 bad input or
state, 3 skip (attempt limit reached), 4 GitHub quota (wait stored), 5 GitHub
read failed. `scope` always writes a scope; a failed read gives a full review.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Generator

from github_quota import QuotaExhausted, run_gh, stop_on_quota

REPO = "phaabe/live.moafunk.de"
ESCALATION_LABEL = "needs-anton"
SHA = re.compile(r"[0-9a-f]{40}")
ISSUE_URL = re.compile(rf"https://github\.com/{re.escape(REPO)}/issues/(\d+)")
REFINE_KEY = re.compile(r"refine:(\d+):(\d+):(none|\d+)")
VERDICT = re.compile(
    r"^Review: (APPROVED|CHANGES REQUESTED) by (Claude|Codex) at ([0-9a-f]{40})$"
)
PROOF_VERSION = 1
PROOF_DIR = "rebase-proofs"
ATTEMPTS = "rebase-attempts.json"
LIMIT_ENV = "EPIC_REBASE_ATTEMPT_LIMIT"
DEFAULT_LIMIT = 2
SUITES_ENV = "EPIC_REBASE_SUITES"
SUITE_TIMEOUT = 1800
# Keep in sync with the table in docs/implementation/epic-rules.md, section 8.
SUITES: tuple[dict[str, Any], ...] = (
    {
        "name": "epic",
        "paths": ["scripts/epic/"],
        "cwd": ".",
        "command": ["python3", "scripts/epic/run_tests.py", "scripts/epic"],
    },
    {
        "name": "codex",
        "paths": [".codex/"],
        "cwd": ".",
        "command": ["python3", "scripts/epic/run_tests.py", ".codex/tests"],
    },
    {
        "name": "epic-guard",
        "paths": ["scripts/epic_guard/", ".claude/hooks/"],
        "cwd": ".",
        "command": [
            "python3",
            "-m",
            "unittest",
            "discover",
            "-s",
            "scripts/epic_guard",
        ],
    },
    {
        "name": "gh-checks",
        "paths": ["scripts/gh_checks/"],
        "cwd": ".",
        "command": ["python3", "-m", "unittest", "discover", "-s", "scripts/gh_checks"],
    },
    {
        "name": "backend",
        "paths": ["backend/"],
        "cwd": "backend",
        "command": ["cargo", "test", "--locked"],
    },
    {
        "name": "frontend",
        "paths": ["frontend/"],
        "cwd": "frontend",
        "command": ["npm", "test", "--", "--run"],
    },
)
# Operations that must not be in progress when a proof is taken or published.
OPERATIONS = (
    "rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD",
    "REVERT_HEAD", "BISECT_LOG", "sequencer",
)  # fmt: skip
RECORD_FIELDS = (
    "Repository",
    "PR",
    "Old head",
    "New head",
    "Old series base",
    "Target tip",
    "Conflicted files",
    "Proof",
)
# Limits for the scope file the review prompt carries.
MAX_DIFF = 60_000

RUN, NOT_DONE, BAD, SKIP, QUOTA, READ_FAILED = 0, 1, 2, 3, 4, 5


class Problem(Exception):
    """The operation cannot go on; the message says why."""


class ReadFailed(Exception):
    """A GitHub read failed."""


# --- git --------------------------------------------------------------------


def git(top: Path, *args: str, check: bool = True) -> str:
    out = subprocess.run(
        ["git", "-C", str(top), *args], capture_output=True, text=True, timeout=120
    )
    if check and out.returncode != 0:
        raise Problem(f"git {' '.join(args)} failed: {out.stderr.strip()}")
    return out.stdout.strip()


def has_commit(top: Path, sha: str) -> bool:
    return (
        bool(SHA.fullmatch(sha or ""))
        and subprocess.run(
            ["git", "-C", str(top), "cat-file", "-e", f"{sha}^{{commit}}"],
            capture_output=True,
            timeout=30,
        ).returncode
        == 0
    )


def is_ancestor(top: Path, old: str, new: str) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(top), "merge-base", "--is-ancestor", old, new],
            capture_output=True,
            timeout=60,
        ).returncode
        == 0
    )


def unclean(top: Path) -> str | None:
    """Why the worktree is not clean, or None. Staged, unstaged and untracked
    changes all count; a tree match alone would not see unstaged edits."""
    for name in OPERATIONS:
        path = git(top, "rev-parse", "--path-format=absolute", "--git-path", name)
        if Path(path).exists():
            return f"an operation is in progress in {top} ({name})"
    status = git(top, "status", "--porcelain", "--untracked-files=all")
    if status:
        first = status.splitlines()[0]
        return f"{top} has uncommitted or untracked changes ({first.strip()} ...)"
    return None


def changed_files(top: Path, *revisions: str) -> list[str]:
    """`git diff --name-only` as real paths: NUL-separated, so Git does not
    quote non-ASCII names (renames count twice)."""
    out = subprocess.run(
        ["git", "-C", str(top), "diff", "--name-only", "-z", "--no-renames", *revisions],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if out.returncode != 0:
        raise Problem(f"git diff --name-only failed: {out.stderr.strip()}")
    return sorted({name for name in out.stdout.split("\0") if name})


def touched(top: Path, onto: str, head: str) -> list[str]:
    """Files the PR changes on top of the target tip."""
    return changed_files(top, f"{onto}...{head}")


# --- suites and proof -------------------------------------------------------


def suites() -> list[dict[str, Any]]:
    """The suite table: SUITES, or the JSON file named by EPIC_REBASE_SUITES."""
    path = os.environ.get(SUITES_ENV)
    if not path:
        return [dict(s) for s in SUITES]
    data = json.loads(Path(path).read_text())
    if not isinstance(data, list) or not all(valid_suite(s) for s in data):
        raise Problem(f"{SUITES_ENV} file has no valid suite list")
    return data


def valid_suite(suite: Any) -> bool:
    return (
        isinstance(suite, dict)
        and isinstance(suite.get("name"), str)
        and isinstance(suite.get("cwd"), str)
        and isinstance(suite.get("paths"), list)
        and all(isinstance(p, str) and p for p in suite["paths"])
        and isinstance(suite.get("command"), list)
        and bool(suite["command"])
        and all(isinstance(c, str) for c in suite["command"])
    )


def required(paths: list[str], table: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Suites whose path prefixes match any changed file, in table order."""
    return [s for s in table if any(p.startswith(tuple(s["paths"])) for p in paths)]


def proof_path(state_dir: Path, pr: int, commit: str) -> Path:
    return state_dir / PROOF_DIR / f"pr-{pr}-{commit}.json"


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, prefix=f".{path.name}-", delete=False
    ) as out:
        json.dump(data, out, indent=1, sort_keys=True)
        out.write("\n")
    os.replace(out.name, path)


def suite_env() -> dict[str, str]:
    """Tests never see runner settings or live state."""
    return {k: v for k, v in os.environ.items() if not k.startswith("EPIC_")}


def run_suite(top: Path, suite: dict[str, Any]) -> dict[str, Any]:
    entry = {k: suite[k] for k in ("name", "cwd", "command")}
    try:
        out = subprocess.run(
            suite["command"],
            cwd=top / suite["cwd"],
            env=suite_env(),
            capture_output=True,
            text=True,
            timeout=SUITE_TIMEOUT,
        )
    except FileNotFoundError as error:
        return {**entry, "exit": None, "result": "skipped", "tail": str(error)}
    except subprocess.TimeoutExpired:
        return {**entry, "exit": None, "result": "failed", "tail": "timed out"}
    # 127: the shell or a wrapper found no such command.
    result = {0: "passed", 127: "skipped"}.get(out.returncode, "failed")
    tail = (out.stdout + out.stderr)[-2000:]
    return {**entry, "exit": out.returncode, "result": result, "tail": tail}


def prove(top: Path, pr: int, base: str, state_dir: Path) -> dict[str, Any]:
    """Run the required suites on HEAD and write the proof. Raises Problem when
    the tree is not clean before or after the tests."""
    problem = unclean(top)
    if problem:
        raise Problem(f"no proof: {problem}")
    head = git(top, "rev-parse", "--verify", "HEAD^{commit}")
    tree = git(top, "rev-parse", "--verify", "HEAD^{tree}")
    onto = git(top, "merge-base", "HEAD", f"refs/remotes/origin/{base}")
    table = required(touched(top, onto, head), suites())
    results = [run_suite(top, suite) for suite in table]
    problem = unclean(top)
    if problem:
        raise Problem(f"no proof: the tests left changes: {problem}")
    if git(top, "rev-parse", "HEAD") != head:
        raise Problem("no proof: HEAD moved while the tests ran")
    proof = {
        "version": PROOF_VERSION,
        "repository": REPO,
        "pr": pr,
        "commit": head,
        "tree": tree,
        "onto": onto,
        "suites": results,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    write_json(proof_path(state_dir, pr, head), proof)
    return proof


def load_proof(state_dir: Path, pr: int, commit: str) -> dict[str, Any] | None:
    try:
        data = json.loads(proof_path(state_dir, pr, commit).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def proof_problem(
    proof: dict[str, Any] | None, top: Path, pr: int, commit: str, onto: str
) -> str | None:
    """Why the proof does not cover `commit` rebased onto `onto`, or None."""
    if proof is None:
        return f"no test proof for {commit[:7]}"
    if proof.get("version") != PROOF_VERSION or proof.get("repository") != REPO:
        return "the test proof has another version or repository"
    if proof.get("pr") != pr or proof.get("commit") != commit:
        return f"the test proof is not for PR {pr} at {commit[:7]}"
    if proof.get("tree") != git(top, "rev-parse", "--verify", f"{commit}^{{tree}}"):
        return "the test proof's tree does not match the commit"
    if proof.get("onto") != onto:
        return f"the test proof is for target tip {str(proof.get('onto'))[:7]}, not {onto[:7]}"
    ran = proof.get("suites")
    if not isinstance(ran, list):
        return "the test proof lists no suites"
    for entry in ran:
        if not isinstance(entry, dict) or entry.get("result") != "passed":
            name = entry.get("name") if isinstance(entry, dict) else "?"
            result = entry.get("result") if isinstance(entry, dict) else "?"
            return f"suite {name} {result}"
        if entry.get("exit") != 0:
            return f"suite {entry.get('name')} exited {entry.get('exit')}"
    have = {(e["name"], e.get("cwd"), tuple(e.get("command") or ())) for e in ran}
    for suite in required(touched(top, onto, commit), suites()):
        if (suite["name"], suite["cwd"], tuple(suite["command"])) not in have:
            return f"required suite {suite['name']} did not run"
    return None


def proof_summary(proof: dict[str, Any]) -> str:
    names = ", ".join(f"{s['name']}={s['result']}" for s in proof["suites"])
    return f"{proof['commit']} tree {proof['tree']}; {names or 'no suites required'}"


# --- rebase record ----------------------------------------------------------


def record_body(
    owner: str,
    pr: int,
    old_head: str,
    new_head: str,
    old_base: str,
    target_tip: str,
    conflicted: list[str],
    proof: dict[str, Any],
) -> str:
    """The exact record comment. No trailing newline."""
    values = (
        REPO,
        str(pr),
        old_head,
        new_head,
        old_base,
        target_tip,
        ", ".join(sorted(set(conflicted))) or "none",
        proof_summary(proof),
    )
    lines = [f"Rebase record by {owner}"]
    lines += [f"{key}: {value}" for key, value in zip(RECORD_FIELDS, values)]
    return "\n".join(lines)


def parse_record(body: str, owner: str) -> dict[str, str] | None:
    """The fields of an exact record by `owner`, or None."""
    lines = body.split("\n")
    if len(lines) != len(RECORD_FIELDS) + 1 or lines[0] != f"Rebase record by {owner}":
        return None
    fields: dict[str, str] = {}
    for key, line in zip(RECORD_FIELDS, lines[1:]):
        prefix = f"{key}: "
        if not line.startswith(prefix) or not line[len(prefix) :]:
            return None
        fields[key] = line[len(prefix) :]
    for key in ("Old head", "New head", "Old series base", "Target tip"):
        if not SHA.fullmatch(fields[key]):
            return None
    if not fields["PR"].isdigit():
        return None
    return fields


def conflicted_list(fields: dict[str, str]) -> list[str]:
    value = fields["Conflicted files"]
    return [] if value == "none" else [p for p in value.split(", ") if p]


def record_problem(
    fields: dict[str, str], top: Path, pr: int, old_head: str, new_head: str
) -> str | None:
    """Local checks of a record: repository, PR, SHAs, objects and ancestry."""
    if fields["Repository"] != REPO:
        return f"the record is for {fields['Repository']}"
    if int(fields["PR"]) != pr:
        return f"the record is for PR {fields['PR']}"
    if fields["Old head"] != old_head:
        return f"the record's old head is not {old_head[:7]}"
    if fields["New head"] != new_head:
        return f"the record's new head is not {new_head[:7]}"
    tip, old_base = fields["Target tip"], fields["Old series base"]
    for sha in (old_head, new_head, tip, old_base):
        if not has_commit(top, sha):
            return f"commit {sha[:7]} is missing locally"
    found = git(top, "merge-base", old_head, tip, check=False)
    if found != old_base:
        return (
            "the old series base is not the merge-base of the old head and target tip"
        )
    if not is_ancestor(top, tip, new_head):
        return "the new head is not built on the target tip"
    return None


# --- GitHub -----------------------------------------------------------------


def read(args: list[str]) -> str:
    try:
        return run_gh(args, timeout=60)
    except (subprocess.SubprocessError, OSError) as error:
        raise ReadFailed(f"gh {' '.join(args[:2])} failed") from error


def read_pr(pr: int) -> dict[str, Any]:
    data = json.loads(read(["api", f"repos/{REPO}/pulls/{pr}"]))
    if not isinstance(data, dict):
        raise ReadFailed(f"PR {pr} is not an object")
    return data


def base_ref(pr: int) -> str:
    """The PR's base branch (the same read as tick_cooldown.base_sha)."""
    ref = read(["api", f"repos/{REPO}/pulls/{pr}", "--jq", ".base.ref"]).strip()
    if not ref or "\n" in ref:
        raise ReadFailed(f"PR {pr} has no base branch")
    return ref


def base_tip(ref: str) -> str:
    sha = read(
        ["api", f"repos/{REPO}/git/ref/heads/{ref}", "--jq", ".object.sha"]
    ).strip()
    if not SHA.fullmatch(sha):
        raise ReadFailed(f"base {ref} has no commit SHA")
    return sha


def comments(pr: int) -> list[dict[str, Any]]:
    """All issue comments of the PR, oldest first."""
    rows = json.loads(
        read(
            [
                "api",
                "--paginate",
                "--slurp",
                f"repos/{REPO}/issues/{pr}/comments?per_page=100",
            ]
        )
    )
    if not isinstance(rows, list):
        raise ReadFailed("comments are not a list")
    flat = [c for page in rows for c in (page if isinstance(page, list) else [page])]
    return sorted(flat, key=lambda c: (c.get("created_at") or "", c.get("id") or 0))


def unedited(comment: dict[str, Any]) -> bool:
    return comment.get("updated_at") == comment.get("created_at")


def post_label(pr: int) -> None:
    run_gh(
        [
            "api",
            "--method",
            "POST",
            f"repos/{REPO}/issues/{pr}/labels",
            "-f",
            f"labels[]={ESCALATION_LABEL}",
        ],
        timeout=60,
    )


def post_comment(pr: int, body: str) -> None:
    run_gh(
        [
            "api",
            "--method",
            "POST",
            f"repos/{REPO}/issues/{pr}/comments",
            "-f",
            f"body={body}",
        ],
        timeout=60,
    )


# --- attempts ---------------------------------------------------------------


def limit(key: str = "") -> int:
    if key.startswith("refine:"):
        # Imported here: the Codex runner tests copy this file without it.
        from refinement import MAX_FAILED_RUNS

        return MAX_FAILED_RUNS
    value = os.environ.get(LIMIT_ENV, str(DEFAULT_LIMIT))
    if not value.isdigit() or int(value) <= 0:
        raise ValueError(f"{LIMIT_ENV} must be a positive integer")
    return int(value)


def attempt_key(pr: int, head: str, tip: str) -> str:
    return f"pr:{pr}:head:{head}:base:{tip}"


def counted(entry: dict[str, Any]) -> int:
    """Attempts that count as failed: every started one that did not land.
    A `started` one without outcome is a crash or a kill: it counts too."""
    return sum(
        1
        for a in entry.get("attempts", [])
        if a.get("outcome") not in ("succeeded", "void")
    )


@contextmanager
def locked(state_dir: Path) -> Generator[dict[str, Any]]:
    """The store, locked for one writer; saved when the block ends."""
    state_dir.mkdir(parents=True, exist_ok=True)
    with open(state_dir / "rebase-attempts.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = state_dir / ATTEMPTS
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            data = {}
        if not isinstance(data, dict) or not all(
            isinstance(v, dict) and isinstance(v.get("attempts"), list)
            for v in data.values()
        ):
            raise ValueError(f"invalid {ATTEMPTS}")
        try:
            yield data
        finally:
            write_json(path, data)


def escalate(entry: dict[str, Any], pr: int, post: Callable[[int], None]) -> None:
    """Post the label once. A failure keeps the key suppressed; the next check
    retries only the post."""
    if entry.get("escalation") == "posted":
        return
    try:
        post(pr)
    except QuotaExhausted:
        entry["escalation"] = "pending"
        raise
    except (subprocess.SubprocessError, OSError) as error:
        entry["escalation"] = "pending"
        entry["escalation_error"] = str(error)[:200]
        print(f"attempts: label post failed for PR {pr}: {error}", file=sys.stderr)
        return
    entry["escalation"] = "posted"
    entry.pop("escalation_error", None)
    print(f"attempts: PR {pr} labeled {ESCALATION_LABEL}", file=sys.stderr)


def attempt_check(
    state_dir: Path,
    agent: str,
    action: dict[str, Any],
    out: Path,
    post: Callable[[int], None] = post_label,
) -> int:
    """SKIP at the limit (and post the label); else pin the attempt in `out`."""
    out.unlink(missing_ok=True)
    if action.get("action") == "refine":
        return refine_check(state_dir, agent, action, out, post)
    pr, head = action.get("pr"), action.get("sha")
    if action.get("action") != "resolve-conflict" or type(pr) is not int:
        raise ValueError("attempts count only resolve-conflict on a PR")
    if not isinstance(head, str) or not SHA.fullmatch(head):
        raise ValueError("invalid PR head")
    ref = base_ref(pr)
    tip = base_tip(ref)
    key = attempt_key(pr, head, tip)
    if pin(state_dir, key, pr, post) == SKIP:
        return SKIP
    write_json(
        out,
        {"key": key, "agent": agent, "pr": pr, "head": head, "base": ref, "tip": tip},
    )
    return RUN


def refine_check(
    state_dir: Path,
    agent: str,
    action: dict[str, Any],
    out: Path,
    post: Callable[[int], None],
) -> int:
    """attempt_check for `refine`: the key comes from the action, and it must
    name the action's issue. `pr` in the attempt file is the issue number."""
    m = ISSUE_URL.fullmatch(action.get("issue") or "")
    key = action.get("attempt_key")
    k = REFINE_KEY.fullmatch(key) if isinstance(key, str) else None
    if not m or not k or k.group(1) != m.group(1):
        raise ValueError("refine needs its issue URL and a matching attempt_key")
    issue = int(m.group(1))
    if pin(state_dir, k.group(0), issue, post) == SKIP:
        return SKIP
    write_json(out, {"key": k.group(0), "agent": agent, "pr": issue, "issue": issue})
    return RUN


def pin(state_dir: Path, key: str, number: int, post: Callable[[int], None]) -> int:
    """SKIP when `key` is at its limit (the label is posted on `number`)."""
    with locked(state_dir) as data:
        entry = data.setdefault(key, {"attempts": []})
        failed = counted(entry)
        if failed >= limit(key):
            escalate(entry, number, post)
            print(
                f"attempts: {key} failed {failed} times; no model "
                f"(escalation {entry.get('escalation')})",
                file=sys.stderr,
            )
            return SKIP
        if not entry["attempts"]:
            data.pop(key)
    return RUN


def load_attempt(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or not isinstance(data.get("key"), str):
        raise ValueError("invalid attempt file")
    return data


def attempt_start(state_dir: Path, attempt: dict[str, Any], tick: str) -> int:
    """Count the attempt before the model starts. SKIP when the limit was
    reached meanwhile. A second start with the same id counts once."""
    with locked(state_dir) as data:
        entry = data.setdefault(attempt["key"], {"attempts": []})
        if any(a.get("id") == tick for a in entry["attempts"]):
            return RUN
        if counted(entry) >= limit(attempt["key"]):
            return SKIP
        entry["attempts"].append(
            {
                "id": tick,
                "agent": attempt.get("agent"),
                "at": time.time(),
                "outcome": "started",
            }
        )
    return RUN


def attempt_finish(
    state_dir: Path,
    attempt: dict[str, Any],
    tick: str,
    outcome: str,
    post: Callable[[int], None] = post_label,
) -> int:
    """Record the outcome once; the first recorded outcome wins. Reaching the
    limit posts the label right away (retried by the next check if it fails)."""
    if outcome not in ("succeeded", "failed", "void"):
        raise ValueError(f"unknown outcome {outcome}")
    with locked(state_dir) as data:
        entry = data.get(attempt["key"])
        mine = [a for a in (entry or {}).get("attempts", []) if a.get("id") == tick]
        if not mine:
            raise ValueError(f"no started attempt {tick} for {attempt['key']}")
        if mine[0].get("outcome") != "started":
            print(
                f"attempts: {tick} already recorded as {mine[0]['outcome']}",
                file=sys.stderr,
            )
            return RUN
        mine[0]["outcome"] = outcome
        assert entry is not None
        if outcome == "failed" and counted(entry) >= limit(attempt["key"]):
            try:
                escalate(entry, attempt["pr"], post)
            except QuotaExhausted:
                print(
                    "attempts: label post hit the quota; retried later", file=sys.stderr
                )
    return RUN


def attempt_lines(state_dir: Path) -> list[str]:
    """Keys with counted attempts, for --status."""
    try:
        data = json.loads((state_dir / ATTEMPTS).read_text())
        cap = limit()
    except FileNotFoundError:
        return ["  none"]
    except (OSError, ValueError) as error:
        return [f"  cannot read {ATTEMPTS}: {error}"]
    lines = []
    for key, entry in sorted(data.items()):
        failed = counted(entry) if isinstance(entry, dict) else 0
        if failed:
            cap = limit(key)
            state = "suppressed" if failed >= cap else "open"
            esc = entry.get("escalation") or "-"
            lines.append(f"  {key}  failed {failed}/{cap} {state} escalation={esc}")
    return lines or ["  none"]


# --- publish (owner) --------------------------------------------------------


def publish(
    agent: str,
    attempt: dict[str, Any],
    top: Path,
    rebases: Path,
    state_dir: Path,
) -> tuple[int, str]:
    """Post the record for a finished, proven push. Idempotent per new head."""
    pr, old_head, tip = attempt["pr"], attempt["head"], attempt["tip"]
    pull = read_pr(pr)
    new_head = str((pull.get("head") or {}).get("sha") or "")
    branch = str((pull.get("head") or {}).get("ref") or "")
    if new_head == old_head:
        return NOT_DONE, "the PR head did not move; no record"
    if git(top, "rev-parse", "HEAD") != new_head:
        return NOT_DONE, f"the new head {new_head[:7]} is not the worktree's HEAD"
    problem = unclean(top)
    if problem:
        return NOT_DONE, problem
    problem = proof_problem(load_proof(state_dir, pr, new_head), top, pr, new_head, tip)
    if problem:
        return NOT_DONE, problem
    try:
        gate = json.loads(rebases.read_text()).get(branch)
    except (OSError, ValueError, AttributeError):
        gate = None
    if (
        not isinstance(gate, dict)
        or gate.get("pr") != pr
        or gate.get("sha") != old_head
        or gate.get("onto") != tip
    ):
        return NOT_DONE, "no approved rebase of this attempt is recorded"
    old_base = git(top, "merge-base", old_head, tip)
    owner = agent.capitalize()
    for c in comments(pr):
        fields = parse_record(c.get("body") or "", owner)
        if fields and fields["New head"] == new_head and unedited(c):
            return RUN, f"record for {new_head[:7]} already posted"
    proof = load_proof(state_dir, pr, new_head)
    assert proof is not None
    body = record_body(
        owner,
        pr,
        old_head,
        new_head,
        old_base,
        tip,
        gate.get("conflicted") or [],
        proof,
    )
    post_comment(pr, body)
    return RUN, f"record for {new_head[:7]} posted"


def record_comment(
    rows: list[dict[str, Any]], owner: str, new_head: str, since: str | None = None
) -> dict[str, str] | None:
    """The latest unedited record by `owner` for `new_head`."""
    found = None
    for c in rows:
        if since and (c.get("created_at") or "") < since:
            continue
        fields = parse_record(c.get("body") or "", owner)
        if fields and fields["New head"] == new_head and unedited(c):
            found = fields
    return found


def verify_resolution(
    agent: str,
    attempt: dict[str, Any],
    new_head: str,
    top: Path,
    state_dir: Path,
    rows: list[dict[str, Any]],
    since: str,
) -> str | None:
    """Why a moved head is not a proven, recorded resolution, or None."""
    pr, old_head, tip = attempt["pr"], attempt["head"], attempt["tip"]
    proof = load_proof(state_dir, pr, new_head)
    problem = proof_problem(proof, top, pr, new_head, tip)
    if problem:
        return problem
    fields = record_comment(rows, agent.capitalize(), new_head, since)
    if fields is None:
        return f"no rebase record for {new_head[:7]} was posted"
    problem = record_problem(fields, top, pr, old_head, new_head)
    if problem:
        return problem
    if fields["Target tip"] != tip:
        return "the record's target tip is not the pinned one"
    assert proof is not None
    if fields["Proof"] != proof_summary(proof):
        return "the record's proof summary does not match the proof"
    return None


# --- review scope (peer) ----------------------------------------------------


def verdicts(rows: list[dict[str, Any]], by: str) -> list[dict[str, Any]]:
    """Unedited exact verdicts by `by`, oldest first."""
    found = []
    for c in rows:
        match = VERDICT.fullmatch(c.get("body") or "")
        if match and match.group(2) == by and unedited(c):
            found.append(
                {
                    "state": match.group(1),
                    "sha": match.group(3),
                    "at": c.get("created_at") or "",
                    "url": c.get("html_url") or "",
                }
            )
    return found


def open_findings(rows: list[dict[str, Any]], mine: list[dict[str, Any]]) -> list[str]:
    """Finding URLs of every review since the reviewer's last approval: a later
    changes-requested review need not repeat a finding for it to stay open.
    Nothing is open when the last verdict approved."""
    if not mine or mine[-1]["state"] != "CHANGES REQUESTED":
        return []
    approved = [v["at"] for v in mine if v["state"] == "APPROVED"]
    since = approved[-1] if approved else ""
    verdict_urls = {v["url"] for v in mine}
    return [
        c.get("html_url") or ""
        for c in rows
        if since < (c.get("created_at") or "") <= mine[-1]["at"]
        and (c.get("html_url") or "") not in verdict_urls
        and not (c.get("body") or "").startswith("Rebase record by ")
    ]


def stem(path: str) -> str:
    return Path(path).stem


def symbols(diff: str) -> set[str]:
    """Names defined on changed lines (Python, Rust, TypeScript, shell)."""
    names = set()
    pattern = re.compile(
        r"^[+-]\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:def|fn|function|class|struct|enum|const|let|type|interface)\s+([A-Za-z_][A-Za-z0-9_]*)"
    )
    for line in diff.splitlines():
        if line.startswith(("+++", "---")):
            continue
        match = pattern.match(line)
        if match and len(match.group(1)) > 2:
            names.add(match.group(1))
    return names


def related(
    top: Path,
    changed: list[str],
    pr_files: list[str],
    old_base: str,
    tip: str,
    head: str,
) -> list[str]:
    """Base changes that touch the PR's files, or that a PR file names (by
    module stem or a changed symbol), or that name a PR file."""
    texts = {}
    for f in pr_files:
        texts[f] = git(top, "show", f"{head}:{f}", check=False)
    keep = []
    for f in changed:
        if f in pr_files:
            keep.append(f)
            continue
        diff = git(top, "diff", old_base, tip, "--", f, check=False)
        names = symbols(diff) | ({stem(f)} if len(stem(f)) > 2 else set())
        body = git(top, "show", f"{tip}:{f}", check=False)
        uses = any(
            re.search(rf"\b{re.escape(n)}\b", text)
            for n in names
            for text in texts.values()
        )
        used = any(
            len(stem(p)) > 2 and re.search(rf"\b{re.escape(stem(p))}\b", body)
            for p in pr_files
        )
        if uses or used:
            keep.append(f)
    return keep


def full(reason: str) -> dict[str, Any]:
    return {"mode": "full", "reason": reason}


def scope(
    reviewer: str,
    pr: int,
    head: str,
    top: Path,
    rows: list[dict[str, Any]],
    current_tip: str,
) -> dict[str, Any]:
    """The review scope for the reviewer at `head`. Full unless every check
    of the ticket holds."""
    owner = "Codex" if reviewer.capitalize() == "Claude" else "Claude"
    mine = verdicts(rows, reviewer.capitalize())
    if not mine:
        return full(f"no earlier verdict by {reviewer.capitalize()}")
    last = mine[-1]
    if last["sha"] == head:
        return full("the last verdict is already for this head")
    fields = record_comment(rows, owner, head)
    if fields is None:
        return full(f"no valid rebase record by {owner} for {head[:7]}")
    try:
        problem = record_problem(fields, top, pr, last["sha"], head)
    except Problem as error:
        problem = str(error)
    if problem:
        return full(f"record check failed: {problem}")
    tip, old_base = fields["Target tip"], fields["Old series base"]
    if current_tip != tip:
        return full(
            f"the base advanced after the rebase ({tip[:7]} -> {current_tip[:7]})"
        )
    try:
        range_diff = git(
            top,
            "range-diff",
            "--no-color",
            f"{old_base}..{last['sha']}",
            f"{tip}..{head}",
        )
        pr_files = touched(top, tip, head)
        changed = changed_files(top, old_base, tip)
        keep = related(top, changed, pr_files, old_base, tip, head)
        base_diff = git(top, "diff", old_base, tip, "--", *keep) if keep else ""
    except Problem as error:
        return full(f"cannot compute the focused scope: {error}")
    return {
        "mode": "focused",
        "reason": f"valid rebase record from {last['sha'][:7]} to {head[:7]}",
        "old_head": last["sha"],
        "new_head": head,
        "old_series_base": old_base,
        "target_tip": tip,
        "last_verdict": last["state"],
        "range_diff": range_diff[:MAX_DIFF],
        "base_changes": f"{old_base}..{tip}",
        "base_changed_files": changed,
        "base_changes_in_scope": keep,
        "base_diff": base_diff[:MAX_DIFF],
        "truncated": len(range_diff) > MAX_DIFF or len(base_diff) > MAX_DIFF,
        "conflicted_files": conflicted_list(fields),
        "open_findings": open_findings(rows, mine),
    }


def review_scope(reviewer: str, action: dict[str, Any], top: Path) -> dict[str, Any]:
    """scope() with the GitHub reads; a failed read gives a full review."""
    pr, head = action.get("pr"), action.get("sha")
    if action.get("action") != "review" or type(pr) is not int:
        raise ValueError("a scope needs a review action on a PR")
    if not isinstance(head, str) or not SHA.fullmatch(head):
        raise ValueError("invalid PR head")
    try:
        tip = base_tip(base_ref(pr))
        rows = comments(pr)
    except (ReadFailed, ValueError, KeyError, TypeError, AttributeError) as error:
        return full(f"GitHub read failed: {error}")
    return scope(reviewer, pr, head, top, rows, tip)


# --- CLI --------------------------------------------------------------------


def state_arg(value: Path | None) -> Path:
    if value is not None:
        return value
    env = os.environ.get("EPIC_STATE_DIR")
    if not env:
        raise ValueError("--state-dir or EPIC_STATE_DIR is required")
    return Path(env)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "command",
        choices=[
            "prove",
            "attempt-check",
            "attempt-start",
            "attempt-finish",
            "attempt-status",
            "publish",
            "scope",
        ],
    )
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--worktree", type=Path)
    parser.add_argument("--repo-dir", type=Path)
    parser.add_argument("--pr", type=int)
    parser.add_argument("--base")
    parser.add_argument("--agent", choices=("claude", "codex"))
    parser.add_argument("--action-file", type=Path)
    parser.add_argument("--attempt-file", type=Path)
    parser.add_argument("--rebases-file", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--id")
    parser.add_argument("--outcome")
    args = parser.parse_args()
    quota_dir = args.state_dir
    try:
        if args.command == "attempt-status":
            print("\n".join(attempt_lines(state_arg(args.state_dir))))
            return RUN
        if args.command == "prove":
            if args.worktree is None or args.pr is None or not args.base:
                parser.error("prove needs --worktree, --pr and --base")
            state = state_arg(args.state_dir)
            proof = prove(args.worktree.resolve(), args.pr, args.base, state)
            bad = [s for s in proof["suites"] if s["result"] != "passed"]
            for s in proof["suites"]:
                print(f"proof: {s['name']} {s['result']}: {' '.join(s['command'])}")
            print(f"proof: {proof_path(state, args.pr, proof['commit'])}")
            if bad:
                print(
                    "proof: NOT valid; fix the failures and prove again",
                    file=sys.stderr,
                )
                return NOT_DONE
            print(f"proof: valid for {proof['commit']}")
            return RUN
        if args.command == "scope":
            if None in (args.agent, args.action_file, args.repo_dir, args.out):
                parser.error("scope needs --agent, --action-file, --repo-dir, --out")
            action = json.loads(args.action_file.read_text())
            result = review_scope(args.agent, action, args.repo_dir.resolve())
            write_json(args.out, result)
            print(f"scope: {result['mode']}: {result['reason']}")
            return RUN
        state = state_arg(args.state_dir)
        quota_dir = state
        if args.command == "attempt-check":
            if None in (args.agent, args.action_file, args.out):
                parser.error("attempt-check needs --agent, --action-file, --out")
            action = json.loads(args.action_file.read_text())
            return attempt_check(state, args.agent, action, args.out)
        if args.attempt_file is None:
            parser.error(f"{args.command} needs --attempt-file")
        attempt = load_attempt(args.attempt_file)
        if args.command == "attempt-start":
            if not args.id:
                parser.error("attempt-start needs --id")
            return attempt_start(state, attempt, args.id)
        if args.command == "attempt-finish":
            if not args.id or not args.outcome:
                parser.error("attempt-finish needs --id and --outcome")
            return attempt_finish(state, attempt, args.id, args.outcome)
        if None in (args.agent, args.worktree, args.rebases_file):
            parser.error("publish needs --agent, --worktree, --rebases-file")
        code, reason = publish(
            args.agent, attempt, args.worktree.resolve(), args.rebases_file, state
        )
        print(f"record: {reason}", file=sys.stderr if code else sys.stdout)
        return code
    except QuotaExhausted as error:
        # One GitHub quota for all agents: EPIC_QUOTA_DIR wins over the
        # state dir, which is the agent's own folder for registered agents.
        shared = os.environ.get("EPIC_QUOTA_DIR")
        if shared:
            quota_dir = Path(shared)
        return stop_on_quota(error, quota_dir) if quota_dir else stop_on_quota(error)
    except ReadFailed as error:
        print(f"rebase: GitHub read failed: {error}", file=sys.stderr)
        return READ_FAILED
    except subprocess.CalledProcessError as error:
        print(f"rebase: GitHub write failed: {error.stderr or error}", file=sys.stderr)
        return READ_FAILED
    except Problem as error:
        print(f"rebase: {error}", file=sys.stderr)
        return NOT_DONE
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"rebase: {error}", file=sys.stderr)
        return BAD


if __name__ == "__main__":
    sys.exit(main())
