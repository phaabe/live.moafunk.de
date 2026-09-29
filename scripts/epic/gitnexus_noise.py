"""Restore GitNexus-only changes in a runner checkout before the tick's pull.

A bare `gitnexus analyze` rewrites the block between the GitNexus markers in
AGENTS.md and CLAUDE.md. If the checkout keeps that change, the tick's
`git pull --ff-only` fails as soon as a merged PR also changes one of them.

  clean tree                       exit 0, nothing to do
  only GitNexus block changes      restore those files, log one line, exit 0
  anything else                    print the reason, restore nothing, exit 1

"Only GitNexus block changes" means: unstaged changes to AGENTS.md or
CLAUDE.md only, same file mode, both markers still present once and in order,
and every changed line strictly between the markers in HEAD. Staged changes,
conflicts, mode or type changes and any other tracked change stop the tick.
Untracked files are ignored and never touched.

It waits for a running analyze (the project `.gitnexus/.analyze.lock` and the
machine-wide `~/.claude/logs/gitnexus/<checkout>.lock`) before the check. It
never writes or removes those locks: the analyzer hooks do not share an atomic
lock protocol, so taking one could clobber a live owner. Instead, if an analyze
started during check and restore, it waits and checks again (up to ROUNDS
times). An analyze that starts after the last check can still rewrite the
block before the pull; that pull fails once and the next tick restores it.

Usage:
  gitnexus_noise.py [--repo DIR] [--wait SECONDS]
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

FILES = ("AGENTS.md", "CLAUDE.md")
START = "<!-- gitnexus:start -->"
END = "<!-- gitnexus:end -->"
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")
ROUNDS = 3


class Stop(Exception):
    """The checkout has a change this helper must not restore."""


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise Stop(f"git {' '.join(args[:2])} failed: {result.stderr.strip()}")
    return result.stdout


def changed(repo: Path) -> list[tuple[str, str]]:
    """Return (XY status, path) for every tracked change."""
    out = git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=no")
    entries = out.split("\0")
    result: list[tuple[str, str]] = []
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if not entry:
            continue
        status, path = entry[:2], entry[3:]
        if status[0] in "RC":
            i += 1  # skip the rename/copy source
        result.append((status, path))
    return result


def markers(lines: list[str], where: str) -> tuple[int, int]:
    """Return the 1-based lines of the start and end markers."""
    starts = [n for n, line in enumerate(lines, 1) if line.strip() == START]
    ends = [n for n, line in enumerate(lines, 1) if line.strip() == END]
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise Stop(f"{where}: GitNexus markers missing, repeated or out of order")
    return starts[0], ends[0]


def check_file(repo: Path, path: str) -> None:
    raw = git(repo, "diff", "--raw", "--no-renames", "HEAD", "--", path).split()
    if len(raw) < 5 or raw[0].lstrip(":") != raw[1] or raw[4] != "M":
        raise Stop(f"{path}: mode or type changed")
    head = git(repo, "show", f"HEAD:{path}").splitlines()
    start, end = markers(head, f"{path} in HEAD")
    try:
        work = (repo / path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as err:
        raise Stop(f"{path}: cannot read ({err})") from err
    markers(work, path)
    diff = git(
        repo,
        "diff",
        "-U0",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        "HEAD",
        "--",
        path,
    )
    hunks = [HUNK.match(line) for line in diff.splitlines() if line.startswith("@@")]
    if not hunks or any(h is None for h in hunks):
        raise Stop(f"{path}: change is not a plain text diff")
    for hunk in hunks:
        assert hunk is not None
        first = int(hunk.group(1))
        count = int(hunk.group(2) if hunk.group(2) is not None else 1)
        # count 0 is a pure insert after line `first`.
        inside = (
            start <= first < end
            if count == 0
            else start < first and first + count - 1 < end
        )
        if not inside:
            raise Stop(f"{path}: change outside the GitNexus block")


def check(repo: Path) -> list[str]:
    """Return the files to restore, or raise Stop."""
    files: list[str] = []
    for status, path in changed(repo):
        if path not in FILES:
            raise Stop(f"tracked change in {path}")
        if status != " M":
            raise Stop(f"{path}: status {status.strip()!r} (staged, conflict or other)")
        check_file(repo, path)
        files.append(path)
    return files


def pid_alive(lock: Path) -> bool:
    try:
        pid = int(lock.read_text().strip())
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return pid != os.getpid()


def wait_for_analyze(locks: list[Path], wait: float) -> None:
    deadline = time.monotonic() + wait
    while any(pid_alive(lock) for lock in locks):
        if time.monotonic() >= deadline:
            raise Stop("GitNexus analyze still running; retry next tick")
        time.sleep(1)


def run(repo: Path, wait: float) -> int:
    repo = Path(git(repo, "rev-parse", "--show-toplevel").strip())
    if not changed(repo):
        return 0
    project_lock = repo / ".gitnexus" / ".analyze.lock"
    global_lock = Path.home() / ".claude/logs/gitnexus" / f"{repo.name}.lock"
    locks = [project_lock, global_lock]
    restored: set[str] = set()
    for _ in range(ROUNDS):
        wait_for_analyze(locks, wait)
        files = check(repo)
        if files:
            git(repo, "restore", "--worktree", "--", *files)
            restored.update(files)
        # An analyze that started meanwhile may write again: check once more.
        if any(pid_alive(lock) for lock in locks) or changed(repo):
            continue
        if restored:
            names = " ".join(sorted(restored))
            print(f"tick: restored GitNexus-only changes in {names}")
        return 0
    raise Stop("GitNexus analyze kept changing the checkout; retry next tick")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Restore GitNexus-only changes before the pull."
    )
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--wait", type=float, default=120)
    args = parser.parse_args(argv)
    try:
        return run(args.repo, args.wait)
    except (Stop, OSError) as err:
        print(
            f"tick: runner checkout not clean: {err}; fix it by hand", file=sys.stderr
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
