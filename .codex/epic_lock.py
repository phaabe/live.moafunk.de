"""Acquire the tick directory lock, recovering only an expired, dead owner."""

from __future__ import annotations

import fcntl
from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import time
from typing import Iterator, TYPE_CHECKING

if TYPE_CHECKING:
    from leases import ProcessEvidence, ProcessIdentity, StopReport


# Import the lease/runtime modules only on the opt-in provider path. Existing
# callers copy this file alone and must not need a lease store or new modules.
PROVIDER_NAME = "epic-process-v1"


def shared_modules():
    shared = str(Path(__file__).resolve().parents[1] / "scripts" / "epic")
    if shared not in sys.path:
        sys.path.insert(0, shared)
    import leases
    import runtime

    return leases, runtime


class ProcessProvider:
    """Opt-in evidence journal; missing or incomplete evidence fails closed.

    A process-table scan cannot track double-forked/reparented descendants.
    Until a lossless containment backend exists, releasing arbitrary code
    permanently makes that session insufficient for takeover. Do not confuse
    a successful launch, a free target lock, or ESRCH for a leader with proof
    that all of its children have stopped.
    """

    name = PROVIDER_NAME
    live = True

    def __init__(self, root: Path) -> None:
        self.root = root

    def compatibility(self) -> dict[str, object]:
        return {
            "provider": self.name,
            "schema": 1,
            "interface": "ProcessEvidence/StopReport",
            "start_identity": "runtime.process_table",
            "registered_launch": True,
            "lossless_descendants": False,
            "activation_ready": False,
            "reason": "process snapshots cannot prove absence of escaped descendants",
        }

    @contextmanager
    def locked(self, *, create: bool = False) -> Iterator[None]:
        if create:
            self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        # Never unlink this inode, including on failure or retirement.
        with (self.root / "admission.lock").open("a") as lock:
            deadline = time.monotonic() + 10
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("process admission lock unavailable")
                    time.sleep(0.02)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def path(self, owner: str) -> Path:
        leases, _ = shared_modules()
        if not isinstance(owner, str) or not leases.ID.fullmatch(owner):
            raise ValueError("invalid process owner")
        return self.root / f"{owner}.json"

    def read(self, owner: str) -> dict:
        data = json.loads(self.path(owner).read_text())
        leases, _ = shared_modules()
        if not isinstance(data, dict) or set(data) != {
            "schema",
            "blocked",
            "pending",
            "released",
            "evidence",
        }:
            raise ValueError("incomplete process journal")
        if (
            type(data["schema"]) is not int
            or data["schema"] != 1
            or any(
                type(data[key]) is not bool
                for key in ("blocked", "pending", "released")
            )
        ):
            raise ValueError("invalid process journal")
        evidence = leases.ProcessEvidence.from_json(data["evidence"])
        if evidence.owner != owner or evidence.provider != self.name:
            raise ValueError("process journal identity mismatch")
        return data

    def write(self, owner: str, data: dict) -> None:
        destination = self.path(owner)
        fd, name = tempfile.mkstemp(prefix=".process-", dir=self.root)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(data, stream)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, destination)
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(name).unlink(missing_ok=True)

    def identity(self, pid: int) -> ProcessIdentity:
        leases, runtime = shared_modules()
        table = runtime.process_table()
        if pid not in table:
            raise ValueError(f"cannot read start identity for {pid}")
        return leases.ProcessIdentity.from_json({"pid": pid, "start": table[pid][1]})

    def register(self, owner: str, wrapper: int) -> ProcessEvidence:
        """Before wrapper admission; an existing journal requires handoff.

        Installing/loading this helper does not call register or init leases.
        """
        leases, _ = shared_modules()
        self.path(owner)  # validate before any filesystem side effect
        with self.locked(create=True):
            if self.path(owner).exists():
                raise ValueError("owner already registered; explicit handoff required")
            evidence = leases.ProcessEvidence(
                owner, self.name, self.identity(wrapper), (), (), time.time()
            )
            self.write(
                owner,
                {
                    "schema": 1,
                    "blocked": False,
                    "pending": False,
                    "released": False,
                    "evidence": evidence.to_json(),
                },
            )
            return evidence

    def block(self, owner: str) -> None:
        """Persist a manual admission block; no automatic unblock operation."""
        with self.locked():
            data = self.read(owner)
            data["blocked"] = True
            self.write(owner, data)

    def admission_blocked(self, owner: str) -> bool:
        try:
            with self.locked():
                return self.read(owner)["blocked"]
        except (OSError, ValueError, TimeoutError):
            return False

    def launch(self, owner: str, argv: list[str]) -> subprocess.Popen:
        """Record a new session before releasing its exec gate.

        The caller is the registered wrapper. A durable pending marker precedes
        spawn; wrapper death before registration leaves uncertain evidence.
        EOF on the pipe exits the child without executing the payload.
        """
        if not argv or not all(isinstance(arg, str) for arg in argv):
            raise ValueError("launch needs a command")
        with self.locked():
            data = self.read(owner)
            if data["blocked"] or data["pending"]:
                raise ValueError("process admission is blocked or incomplete")
            if data["evidence"]["wrapper"] != self.identity(os.getpid()).to_json():
                raise ValueError("only the recorded wrapper may launch")
            data["pending"] = True
            self.write(owner, data)
            read_fd, write_fd = os.pipe()
            child = None
            admitted = False
            try:
                child = subprocess.Popen(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--process-child",
                        str(read_fd),
                        *argv,
                    ],
                    pass_fds=(read_fd,),
                    start_new_session=True,
                )
                ident = self.identity(child.pid)
                if os.getpgid(child.pid) != child.pid:
                    raise ValueError("child did not establish its process group")
                data["evidence"]["groups"].append(ident.to_json())
                data["evidence"]["recorded_at"] = time.time()
                data["pending"] = False
                # Persist uncertainty BEFORE arbitrary code can fork/escape.
                data["released"] = True
                self.write(owner, data)
                os.write(write_fd, b"1")
                admitted = True
                return child
            finally:
                os.close(read_fd)
                os.close(write_fd)
                # On an exception the gate closes. Do not signal by a PID that
                # may have been reused; retain the journal for manual recovery.
                if child is not None and not admitted:
                    child.wait(timeout=10)

    def probe(self, ident: ProcessIdentity, *, group: bool = False) -> str:
        """Only a kernel ESRCH is gone; absence in a snapshot proves nothing."""
        try:
            if group:
                os.killpg(ident.pid, 0)
            else:
                os.kill(ident.pid, 0)
        except ProcessLookupError:
            return "gone"
        except PermissionError:
            return "eperm"
        except OSError:
            return "error"
        try:
            current = self.identity(ident.pid)
        except (OSError, ValueError, subprocess.SubprocessError):
            # A live group without its original leader is uncertain.
            return "error"
        return "alive" if current == ident else "reused"

    def stop(self, evidence: ProcessEvidence) -> StopReport:
        """Refuse unsafe signaling and return conservative shutdown evidence.

        No PID-based terminating signal is sent: snapshots cannot fence PID
        reuse between identity read and signal, or contain escaped children.
        A later backend must supply both guarantees before activation.
        """
        leases, _ = shared_modules()
        refused = leases.StopReport(
            "error",
            {p.pid: "error" for p in evidence.groups},
            {p.pid: "error" for p in evidence.descendants},
        )
        try:
            with self.locked():
                data = self.read(evidence.owner)
                if (
                    not data["blocked"]
                    or data["pending"]
                    or data["released"]
                    or data["evidence"] != evidence.to_json()
                ):
                    return refused
                # An empty journal is not evidence that an arbitrary wrapper
                # never forked. Until containment exists, even ESRCH cannot
                # turn this snapshot-only provider into takeover authority.
                wrapper = self.probe(evidence.wrapper)
                groups = {p.pid: self.probe(p, group=True) for p in evidence.groups}
                descendants = {p.pid: self.probe(p) for p in evidence.descendants}
                return leases.StopReport(
                    "error" if wrapper == "gone" else wrapper, groups, descendants
                )
        except (OSError, ValueError, TimeoutError, subprocess.SubprocessError):
            return refused


def provider() -> ProcessProvider:
    """Lease loader entry point; constructing it has no filesystem effects."""
    shared_modules()
    from target_lock import lock_dir

    return ProcessProvider(lock_dir().parent / "process-evidence" / "v1")


def process_child(fd: int, argv: list[str]) -> int:
    """Private exec gate; never run the payload after parent death/EOF."""
    try:
        admitted = os.read(fd, 1) == b"1"
    finally:
        os.close(fd)
    if not admitted or not argv:
        return 75
    os.execvp(argv[0], argv)
    return 75


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
    if len(sys.argv) > 1 and sys.argv[1] == "--process-child":
        sys.exit(process_child(int(sys.argv[2]), sys.argv[3:]))
    sys.exit(
        0 if acquire(Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])) else 75
    )
