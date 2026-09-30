"""Per-target lock tests. Run: python3 -m unittest discover -s scripts/epic"""

from __future__ import annotations

import isolated_env  # noqa: F401  (first: hides live runner state)

import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from target_lock import BUSY, paths, targets

HELPER = Path(__file__).resolve().parent / "target_lock.py"
R = "https://github.com/phaabe/live.moafunk.de/issues"


def try_lock(path: Path) -> int:
    """Exit code of one acquire on a fresh descriptor, as a runner does it."""
    return subprocess.run(
        [
            "/bin/bash",
            "-c",
            f'exec 8>> "$1"; python3 "{HELPER}" acquire --fd 8',
            "_",
            str(path),
        ],
        capture_output=True,
        check=False,
    ).returncode


class TargetsTest(unittest.TestCase):
    def test_pr_and_issue_in_number_order(self) -> None:
        self.assertEqual(targets({"pr": 500, "issue": f"{R}/488"}), [488, 500])
        self.assertEqual(targets({"issue": f"{R}/488/"}), [488])
        self.assertEqual(targets({"action": "idle"}), [])

    def test_paths_live_in_the_shared_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "locks"
            got = paths({"pr": 9, "issue": f"{R}/3"}, root)
            self.assertEqual(got, [root / "3.lock", root / "9.lock"])
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)


class LockTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.lock = self.dir / "484.lock"

    def holder(self) -> subprocess.Popen[bytes]:
        """A tick that took the lock and runs a model child in the background."""
        script = (
            'exec 8>> "$1"\n'
            f'python3 "{HELPER}" acquire --fd 8 || exit $?\n'
            "sleep 60 &\n"
            'echo $! > "$2"\n'
            "wait\n"
        )
        proc = subprocess.Popen(
            ["/bin/bash", "-c", script, "_", str(self.lock), str(self.dir / "child")],
            start_new_session=True,
        )
        self.addCleanup(self.kill_group, proc.pid)
        for _ in range(100):
            if (self.dir / "child").exists() and (
                self.dir / "child"
            ).read_text().strip():
                return proc
            time.sleep(0.05)
        self.fail("holder did not start")

    @staticmethod
    def kill_group(pid: int) -> None:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def test_two_runners_one_wins(self) -> None:
        self.holder()
        self.assertEqual(try_lock(self.lock), BUSY)

    def test_free_lock_is_taken(self) -> None:
        self.assertEqual(try_lock(self.lock), 0)
        self.assertEqual(try_lock(self.lock), 0)  # released when the shell exited

    def test_sigkill_of_the_tick_keeps_the_lock_while_the_child_runs(self) -> None:
        proc = self.holder()
        child = int((self.dir / "child").read_text())
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)
        self.assertEqual(try_lock(self.lock), BUSY)
        os.kill(child, signal.SIGKILL)
        for _ in range(100):
            if try_lock(self.lock) == 0:
                break
            time.sleep(0.05)
        else:
            self.fail("lock not released after the child died")
        self.assertTrue(self.lock.exists())  # never deleted

    def test_second_target_busy_frees_the_first(self) -> None:
        self.holder()
        first = self.dir / "100.lock"
        script = f'exec 8>> "$1" 9>> "$2"\npython3 "{HELPER}" acquire --fd 8 --fd 9\n'
        result = subprocess.run(
            ["/bin/bash", "-c", script, "_", str(first), str(self.lock)],
            check=False,
        )
        self.assertEqual(result.returncode, BUSY)
        self.assertEqual(try_lock(first), 0)


if __name__ == "__main__":
    unittest.main()
