"""Acquire the tick directory lock, recovering only an expired, dead owner."""

from __future__ import annotations

import fcntl
import json
import logging
import os
from pathlib import Path
import sys
import time


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def acquire(lock: Path, pid: int, max_age: int) -> bool:
    # Keep this guard file: unlinking it would let contenders lock different inodes.
    # flock is released by the OS if this short acquisition process dies.
    with lock.with_suffix(".guard").open("a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        if lock.is_symlink():
            raise ValueError("tick: refusing a symlink lock")
        now = int(time.time())
        try:
            lock.mkdir()
        except FileExistsError:
            try:
                owner = json.loads((lock / "owner.json").read_text())
                if not isinstance(owner, dict) or any(
                    type(owner.get(key)) is not int or owner[key] <= 0
                    for key in ("pid", "started_at", "max_age")
                ):
                    raise ValueError("invalid lock metadata")
            except (OSError, ValueError):
                logging.warning("tick: locked; owner metadata unavailable or invalid")
                return False
            if alive(owner["pid"]) or now - owner["started_at"] <= max(
                max_age, owner["max_age"]
            ):
                logging.info(
                    "tick: locked; owner pid %s is live or within its timeout",
                    owner["pid"],
                )
                return False
            expected = {
                "owner.json",
                "action.json",
                "assignment.json",
                "prompt.txt",
                "worktree.txt",
                "review-context.json",
            }
            if any(
                entry.name not in expected or entry.is_dir() for entry in lock.iterdir()
            ):
                logging.warning(
                    "tick: locked; unexpected lock contents need manual review"
                )
                return False
            for name in expected:
                (lock / name).unlink(missing_ok=True)
            lock.rmdir()
            logging.info("tick: reclaimed stale lock from pid %s", owner["pid"])
            lock.mkdir()
        (lock / "owner.json").write_text(
            json.dumps({"pid": pid, "started_at": now, "max_age": max_age}) + "\n"
        )
        return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.exit(
        0 if acquire(Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])) else 75
    )
