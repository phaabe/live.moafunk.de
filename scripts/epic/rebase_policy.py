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
`attempt-check` pins the table in the attempt (`proof_suites`); proof, gate,
publish and verify then use the pinned table (attempt_suites()). A suite with
`"mandatory": true` is required whatever the paths.

Upstream test recovery (next_action.recovery_of(), opt-in `upstream-recovery`
in EPIC_FOCUS_ACTIONS): the PR owner's exact request makes a `resolve-conflict`
action with a `recovery` object for a PR that does not conflict. attempt-check
reads the PR, its comments with edit evidence, the base tip and the ancestry
fresh, runs the selector's rule again and refuses a changed request (exit 6,
no attempt counted). The target tip is the request's base tip, never a newer
one. The pinned table makes every requested suite mandatory, so a proof needs
the normal path suites plus the suites that failed on the old head.
recovery_worktree_problem() keeps uncommitted or unpublished work: the model
starts only from a clean worktree at the PR head or from this attempt's
recorded rebase.

Attempt limit: EPIC_REBASE_ATTEMPT_LIMIT, default 2. Each started attempt counts
once, also when it fails or times out; only a landed one does not count. An
attempt whose model reported the GitHub quota is void. Pause, quota and read
waits and lock skips never start one. Cooldown expiry and comments do not reset
the count; a new head or a new target tip is a new key. At the limit no model
starts for the key and the owner's runner adds the label `needs-anton`. When
that post fails, the key stays suppressed and only the label post is retried.

Usage:
  rebase_policy.py prove --worktree W --pr N --base BASE [--state-dir D]
                         [--attempt-file ATTEMPT]
  rebase_policy.py attempt-check  --state-dir D --agent A --action-file F --out ATTEMPT
  rebase_policy.py attempt-start  --state-dir D --attempt-file ATTEMPT --id TICK
  rebase_policy.py attempt-finish --state-dir D --attempt-file ATTEMPT --id TICK
                                  --outcome succeeded|failed|void
  rebase_policy.py attempt-status --state-dir D
  rebase_policy.py publish --agent A --attempt-file ATTEMPT --worktree W
                           --rebases-file R [--state-dir D]
  rebase_policy.py scope --agent A --action-file F --repo-dir D --out SCOPE
  rebase_policy.py export --sha SHA

`export` (upstream test recovery evidence) writes a read-only copy of one
commit for tests: the selected PR head (the `sha` of EPIC_ACTION_FILE) or the
current tip of the PR's base (read fresh). It runs one fixed `git archive` in
the runner worktree (EPIC_WORKTREE), extracts it safely into a new private
directory under TMPDIR and prints `export: <sha> <path>`. It writes no ref,
index or worktree file and runs no test.

Exit codes: 0 done (prove: valid proof; check/start: run), 1 not done (prove:
a suite failed or was skipped; publish: nothing to publish), 2 bad input or
state, 3 skip (attempt limit reached), 4 GitHub quota (wait stored), 5 GitHub
read failed, 6 stale (attempt-check: the recovery request, head, base or tip
changed after selection; no attempt). `scope` always writes a scope; a failed
read gives a full review.
"""

from __future__ import annotations

import argparse
import fcntl
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Generator

from github_quota import QuotaExhausted, run_gh, stop_on_quota

REPO = "phaabe/live.moafunk.de"
ESCALATION_LABEL = "needs-anton"
SHA = re.compile(r"[0-9a-f]{40}")
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
# run_tests.SLOTS_DIR_ENV and SLOTS_ENV (docs/implementation/shared-test-slots.md)
SLOT_SETTINGS = ("EPIC_TEST_SLOTS_DIR", "EPIC_TEST_SLOTS")
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

RUN, NOT_DONE, BAD, SKIP, QUOTA, READ_FAILED, STALE = 0, 1, 2, 3, 4, 5, 6
# GitHub answers a compare of an unknown commit with these.
UNKNOWN_COMMIT = frozenset({404, 422})
HTTP_STATUS = re.compile(r"\(HTTP (\d{3})\)")


class Problem(Exception):
    """The operation cannot go on; the message says why."""


class ReadFailed(Exception):
    """A GitHub read failed."""


class Stale(Exception):
    """A recovery action no longer matches GitHub; no attempt starts."""


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
        [
            "git",
            "-C",
            str(top),
            "diff",
            "--name-only",
            "-z",
            "--no-renames",
            *revisions,
        ],
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


def rebase_file(top: Path, name: str) -> str:
    """One file of Git's rebase state (head-name, onto, orig-head), or ""."""
    for folder in ("rebase-merge", "rebase-apply"):
        path = git(
            top, "rev-parse", "--path-format=absolute", "--git-path", f"{folder}/{name}"
        )
        if Path(path).is_file():
            return Path(path).read_text().strip()
    return ""


def recovery_worktree_problem(
    top: Path, branch: str, head: str, tip: str, record: dict[str, Any] | None
) -> str | None:
    """Why an upstream recovery model must not start in `top`, or None.

    It starts from a clean worktree on `branch` at the PR head, or from the
    runner's recorded rebase of this head (`record`: worktree, sha, onto,
    orig, aborted): one in progress (onto this attempt's tip it resumes; onto
    an older tip the model aborts it with the gate and starts again), or one
    finished onto this attempt's tip and not pushed yet. A finished, unpushed
    rebase onto an older tip cannot be aborted: it stays for Anton. Anything
    else (uncommitted, untracked or unpublished work, another operation)
    stays as it is too: no stash, reset or checkpoint commit."""
    rec = record if isinstance(record, dict) else {}
    ours = (
        not rec.get("aborted")
        and rec.get("worktree") == str(top)
        and rec.get("sha") == head
        and rec.get("orig") == head
    )
    if rebase_file(top, "head-name"):
        if (
            ours
            and rebase_file(top, "head-name") == f"refs/heads/{branch}"
            and rebase_file(top, "onto") == rec.get("onto")
            and rebase_file(top, "orig-head") == head
        ):
            return None
        return "a rebase is in progress that the runner did not record for this head; it stays"
    problem = unclean(top)
    if problem:
        return f"{problem}; they stay, publish or park them first"
    if git(top, "symbolic-ref", "-q", "HEAD", check=False) != f"refs/heads/{branch}":
        return f"{top} is not on {branch}"
    local = git(top, "rev-parse", "--verify", "HEAD^{commit}")
    if local == head:
        return None
    onto = str(rec.get("onto"))
    if ours and is_ancestor(top, onto, local):
        if onto == tip:
            return None
        return (
            f"a finished rebase onto the older tip {onto[:7]} is not pushed; "
            "it stays for Anton (push it by hand after review, or drop it)"
        )
    return (
        f"HEAD {local[:7]} is not the PR head {head[:7]}; unpublished or "
        "older commits stay, publish them first"
    )


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
        and isinstance(suite.get("mandatory", False), bool)
    )


def required(paths: list[str], table: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Suites whose path prefixes match any changed file, plus every mandatory
    one (the failed suites of an upstream recovery), in table order."""
    return [
        s
        for s in table
        if s.get("mandatory") is True
        or any(p.startswith(tuple(s["paths"])) for p in paths)
    ]


def without_flag(suite: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in suite.items() if k != "mandatory"}


def pinned_table(mandatory: list[str]) -> list[dict[str, Any]]:
    """The table an attempt pins: suites(), with each named suite mandatory."""
    table = suites()
    unknown = sorted(set(mandatory) - {s["name"] for s in table})
    if unknown:
        raise Problem(f"unknown suites: {', '.join(unknown)}")
    return [{**s, "mandatory": True} if s["name"] in mandatory else s for s in table]


def attempt_suites(attempt: dict[str, Any]) -> list[dict[str, Any]]:
    """The proof table pinned in the attempt; suites() for an older attempt
    without one. A pin may make suites mandatory, never drop or change one."""
    if "proof_suites" not in attempt:
        return suites()
    table = attempt["proof_suites"]
    if not isinstance(table, list) or not all(valid_suite(s) for s in table):
        raise Problem("the attempt pins no valid proof suites")
    normal = suites()
    if [without_flag(s) for s in table] != [without_flag(s) for s in normal] or any(
        s.get("mandatory") is True and pin.get("mandatory") is not True
        for s, pin in zip(normal, table)
    ):
        raise Problem("the attempt's proof suites differ from the runner's suite table")
    return table


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
    """Tests never see runner settings or live state. Only the shared test-slot
    settings reach the suite's runner (run_tests.py), so a proof run shares the
    machine-wide slots; isolated_env.py removes them from every test."""
    return {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("EPIC_") or k in SLOT_SETTINGS
    }


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


def prove(
    top: Path,
    pr: int,
    base: str,
    state_dir: Path,
    table: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run the required suites on HEAD and write the proof. Raises Problem when
    the tree is not clean before or after the tests. `table`: the attempt's
    pinned table (attempt_suites()); None means suites()."""
    problem = unclean(top)
    if problem:
        raise Problem(f"no proof: {problem}")
    head = git(top, "rev-parse", "--verify", "HEAD^{commit}")
    tree = git(top, "rev-parse", "--verify", "HEAD^{tree}")
    onto = git(top, "merge-base", "HEAD", f"refs/remotes/origin/{base}")
    table = required(touched(top, onto, head), suites() if table is None else table)
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
    proof: dict[str, Any] | None,
    top: Path,
    pr: int,
    commit: str,
    onto: str,
    table: list[dict[str, Any]] | None = None,
) -> str | None:
    """Why the proof does not cover `commit` rebased onto `onto`, or None.
    `table`: the attempt's pinned table; None means suites()."""
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
    table = suites() if table is None else table
    for suite in required(touched(top, onto, commit), table):
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


# --- export (test evidence) ------------------------------------------------

ORIGIN = re.compile(rf"[:/]{re.escape(REPO)}(?:\.git)?/?$")


def extract(data: bytes, out: Path) -> None:
    """Unpack a `git archive` tar into `out`: regular files, directories and
    links that stay inside only. Anything else raises Problem."""
    # Without tarfile's data filter (older Python) chained links could
    # leave the copy: no links at all then.
    filtered = getattr(tarfile, "data_filter", None) is not None
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
            members = tar.getmembers()
            for m in members:
                if os.path.isabs(m.name) or ".." in Path(m.name).parts:
                    raise Problem(f"unsafe path in the archive: {m.name}")
                if not (m.isfile() or m.isdir() or m.issym()):
                    raise Problem(f"unsupported entry in the archive: {m.name}")
                if m.issym() and not filtered:
                    raise Problem(f"links need tarfile's data filter: {m.name}")
                if m.issym():
                    target = os.path.normpath(
                        os.path.join(os.path.dirname(m.name), m.linkname)
                    )
                    if os.path.isabs(m.linkname) or target.split(os.sep)[0] == "..":
                        raise Problem(f"unsafe link in the archive: {m.name}")
            if filtered:
                tar.extractall(out, members=members, filter="data")
            else:  # the checks above are the filter
                tar.extractall(out, members=members)
    except tarfile.TarError as error:
        raise Problem(f"cannot unpack the archive: {error}") from error


def export(sha: str, action: dict[str, Any], worktree: Path) -> Path:
    """A private copy of commit `sha` for tests. `sha` must be the selected
    PR head or the current tip of the PR's base; the commit must be in the
    runner worktree's repository (fetch first). Raises ValueError for a
    commit or worktree that is not allowed, Problem for a failed export."""
    pr, head = action.get("pr"), action.get("sha")
    if type(pr) is not int or not isinstance(head, str) or not SHA.fullmatch(head):
        raise ValueError("the selected action names no PR head")
    if not SHA.fullmatch(sha):
        raise ValueError("--sha needs a 40-char commit SHA")
    if sha != head:
        tip = base_tip(base_ref(pr))
        if sha != tip:
            raise ValueError(
                f"{sha[:7]} is neither the PR head {head[:7]} nor the base tip {tip[:7]}"
            )
    real = Path(os.path.realpath(worktree))
    if (
        not worktree.is_absolute()
        or Path(git(real, "rev-parse", "--show-toplevel")) != real
    ):
        raise ValueError(f"{worktree} is not a checkout")
    if not ORIGIN.search(git(real, "remote", "get-url", "origin")):
        raise ValueError(f"{worktree} is not a checkout of {REPO}")
    if not has_commit(real, sha):
        raise Problem(f"commit {sha[:7]} is not in the repository; fetch origin first")
    archive = subprocess.run(
        ["git", "-C", str(real), "archive", "--format=tar", sha],
        capture_output=True,
        timeout=300,
    )
    if archive.returncode != 0:
        raise Problem(
            f"git archive failed: {archive.stderr.decode(errors='replace').strip()}"
        )
    out = Path(
        tempfile.mkdtemp(
            prefix=f"epic-export-{sha[:12]}-", dir=os.environ.get("TMPDIR") or None
        )
    )
    extract(archive.stdout, out)
    return out


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


def read_json(endpoint: str, missing: frozenset[int] = frozenset()) -> Any:
    """One REST read; None when GitHub answers a status in `missing`."""
    try:
        return json.loads(run_gh(["api", endpoint], timeout=60))
    except subprocess.CalledProcessError as error:
        found = HTTP_STATUS.search(error.stderr or "")
        if found and int(found.group(1)) in missing:
            return None
        raise ReadFailed(f"gh api {endpoint.split('?')[0]} failed") from error
    except (subprocess.SubprocessError, OSError, ValueError) as error:
        raise ReadFailed(f"gh api {endpoint.split('?')[0]} failed") from error


def fresh_pr(pr: int) -> dict[str, Any]:
    """The PR in the selector's shape, read fresh: state, head, base, body,
    draft, mergeable, all comments (with GraphQL edit evidence when a
    recovery request candidate exists) and, for a request naming its head,
    the base tip and ancestry (next_action.recovery_read()). Any failed read
    raises ReadFailed."""
    # Imported here: the Codex helper copies this file without the selector.
    import github_state
    import next_action as na

    pull = read_pr(pr)
    shaped = {
        "number": pr,
        "state": pull.get("state"),
        "body": pull.get("body") or "",
        "baseRefName": (pull.get("base") or {}).get("ref"),
        "headRefOid": (pull.get("head") or {}).get("sha"),
        "isDraft": bool(pull.get("draft")),
        # null: GitHub is still computing it.
        "mergeable": "MERGEABLE"
        if pull.get("mergeable") is True
        else "CONFLICTING"
        if pull.get("mergeable") is False
        else "UNKNOWN",
    }
    count = pull.get("comments")
    if not isinstance(count, int):
        raise ReadFailed(f"PR {pr} has no comment count")
    rows = comments(pr)
    try:
        if na.recovery_rows(rows):
            na.legacy_edit_evidence(pr, rows)
        shaped["comments"] = na.comments_from_rest(rows, count)
        na.recovery_read(shaped, read_json)
    except (
        subprocess.SubprocessError,
        OSError,
        ValueError,
        github_state.ConfigError,
    ) as error:
        raise ReadFailed(f"recovery evidence of PR {pr}: {error}") from error
    return shaped


def recovery_admission(agent: str, action: dict[str, Any]) -> dict[str, Any]:
    """The upstream recovery evidence the selector's own rule gives now, from
    fresh reads (fresh_pr()). Raises Stale when it is not the action's
    evidence any more, ReadFailed when a read failed: the runner then starts
    no model and counts no attempt."""
    import next_action as na

    pr, head = action["pr"], action["sha"]
    shaped = fresh_pr(pr)
    if shaped["state"] != "open":
        raise Stale(f"PR {pr} is {shaped['state']}")
    if shaped["headRefOid"] != head:
        raise Stale(f"the head of PR {pr} moved after selection")
    known = frozenset(s["name"] for s in suites())
    found, problem = na.recovery_of(shaped, agent.capitalize(), known)
    if found is None or found.state != "valid" or found.evidence is None:
        why = problem or (found.problem if found else None)
        raise Stale(why or f"PR {pr} has no upstream test recovery request")
    evidence = found.evidence
    if evidence != action.get("recovery"):
        raise Stale(f"the recovery request of PR {pr} changed after selection")
    return evidence


def recovery_hold(pr: int) -> str | None:
    """Why a merge or a verdict on PR `pr` must wait, from fresh reads: its
    owner's request for the current head is pending, needs new evidence or
    is admitted (next_action.recovery_of()). An ignored request (edited,
    foreign, malformed, older head) holds nothing. None: no hold."""
    import next_action as na

    shaped = fresh_pr(pr)
    found, _ = na.recovery_of(shaped, str(na.pr_author(shaped)), na.recovery_suites())
    if found is None:
        return None
    newest = na.newest_recovery(shaped) or {}
    return (
        f"PR {pr}: the owner's upstream test recovery request "
        f"{newest.get('url')} ({found.state}) holds merge and review of "
        f"{str(shaped['headRefOid'])[:7]}"
    )


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


def limit() -> int:
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
    """SKIP at the limit (and post the label); else pin the attempt in `out`.

    An upstream recovery (`recovery` in the action) is admitted fresh first
    (recovery_admission(): Stale or ReadFailed before the store is touched).
    Its target tip is the request's base tip. The pin holds the proof table:
    suites(), with the recovery's suites mandatory."""
    out.unlink(missing_ok=True)
    pr, head = action.get("pr"), action.get("sha")
    if action.get("action") != "resolve-conflict" or type(pr) is not int:
        raise ValueError("attempts count only resolve-conflict on a PR")
    if not isinstance(head, str) or not SHA.fullmatch(head):
        raise ValueError("invalid PR head")
    recovery = None
    if action.get("recovery") is not None:
        recovery = recovery_admission(agent, action)
        ref, tip = recovery["base"], recovery["tip"]
    else:
        ref = base_ref(pr)
        tip = base_tip(ref)
    table = pinned_table(recovery["suites"] if recovery else [])
    key = attempt_key(pr, head, tip)
    with locked(state_dir) as data:
        entry = data.setdefault(key, {"attempts": []})
        failed = counted(entry)
        if failed >= limit():
            escalate(entry, pr, post)
            print(
                f"attempts: {key} failed {failed} times; no model "
                f"(escalation {entry.get('escalation')})",
                file=sys.stderr,
            )
            return SKIP
        if not entry["attempts"]:
            data.pop(key)
    pin = {"key": key, "agent": agent, "pr": pr, "head": head, "base": ref, "tip": tip}
    pin["proof_suites"] = table
    if recovery:
        pin["recovery"] = recovery
    write_json(out, pin)
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
        if counted(entry) >= limit():
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
        if outcome == "failed" and counted(entry) >= limit():
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
    problem = proof_problem(
        load_proof(state_dir, pr, new_head),
        top,
        pr,
        new_head,
        tip,
        attempt_suites(attempt),
    )
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
    problem = proof_problem(proof, top, pr, new_head, tip, attempt_suites(attempt))
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
            "export",
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
    parser.add_argument("--sha")
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
            table = None
            if args.attempt_file is not None:
                # The runner's pin: its proof table, for this PR and base only.
                pin = load_attempt(args.attempt_file)
                if pin.get("pr") != args.pr or pin.get("base") != args.base:
                    raise ValueError("the attempt pin is for another PR or base")
                table = attempt_suites(pin)
            proof = prove(args.worktree.resolve(), args.pr, args.base, state, table)
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
        if args.command == "export":
            if not args.sha:
                parser.error("export needs --sha")
            action = json.loads(
                Path(os.environ.get("EPIC_ACTION_FILE") or "").read_text()
            )
            worktree = Path(os.environ.get("EPIC_WORKTREE") or "")
            if not isinstance(action, dict) or not str(worktree).startswith("/"):
                raise ValueError(
                    "export needs the runner's EPIC_ACTION_FILE and EPIC_WORKTREE"
                )
            path = export(args.sha, action, worktree)
            print(f"export: {args.sha} {path}")
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
    except Stale as error:
        print(f"rebase: stale upstream recovery: {error}", file=sys.stderr)
        return STALE
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
