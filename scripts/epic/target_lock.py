"""Per-target lock: one `flock` per issue or PR number, shared by all runners.

The runner shell opens each lock file on a fixed descriptor (8, then 9) and
calls `acquire` with those descriptors. `flock` belongs to the open file, not
to this process, so the lock stays held after `acquire` exits: until the shell
and every child that inherited the descriptor (the model session) exited. A
SIGKILL of the shell therefore never frees a target while its model still runs,
and the OS frees it when the last holder dies: no stale lock, no age reclaim.

Lock files are never deleted: unlinking one would let two runners lock
different inodes of the same name. An action with a PR and an issue locks both,
in number order, so two runners never wait on each other in a cycle.

Directory: EPIC_LOCK_DIR, default ~/.local/state/epic-loop/target-locks. It
does not follow EPIC_STATE_DIR, so every Claude and Codex runner on this machine
uses the same one.

Usage:
  target_lock.py paths --action-file action.json   lock file per target, in order
  target_lock.py acquire --fd 8 [--fd 9]           0 locked, 75 busy
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

BUSY = 75
ISSUE_TAIL = re.compile(r"/issues/(\d+)/?$")


def lock_dir() -> Path:
    return Path(
        os.environ.get("EPIC_LOCK_DIR")
        or Path.home() / ".local" / "state" / "epic-loop" / "target-locks"
    )


def targets(action: dict[str, Any]) -> list[int]:
    """Issue and PR numbers the action changes, lowest first."""
    found = set()
    if isinstance(action.get("pr"), int):
        found.add(action["pr"])
    match = ISSUE_TAIL.search(str(action.get("issue") or ""))
    if match:
        found.add(int(match.group(1)))
    return sorted(found)


def paths(action: dict[str, Any], root: Path) -> list[Path]:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return [root / f"{n}.lock" for n in targets(action)]


def acquire(fds: list[int]) -> bool:
    """Lock each descriptor in order without waiting. False when one is busy.

    On False the caller closes all descriptors, which frees any lock taken here.
    """
    for fd in fds:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("paths").add_argument("--action-file", required=True, type=Path)
    sub.add_parser("acquire").add_argument(
        "--fd", action="append", type=int, default=[]
    )
    args = parser.parse_args()
    if args.command == "paths":
        action = json.loads(args.action_file.read_text())
        for path in paths(action, lock_dir()):
            print(path)
        return 0
    if not acquire(args.fd):
        print("tick: target is locked by another runner", file=sys.stderr)
        return BUSY
    return 0


if __name__ == "__main__":
    sys.exit(main())
