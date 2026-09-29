"""Run gitnexus_noise.py against real Git checkouts."""

from __future__ import annotations

import contextlib
import io
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().with_name("gitnexus_noise.py")
sys.path.insert(0, str(SCRIPT.parent))
import gitnexus_noise  # noqa: E402

DOC = (
    "# Guide\n\nHand-written text.\n\n"
    "<!-- gitnexus:start -->\n"
    "# GitNexus\n\n"
    "Indexed (10 symbols, 20 relationships).\n\n"
    "> Index stale? Run analyze.\n"
    "<!-- gitnexus:end -->\n\n"
    "Footer.\n"
)


class NoiseTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="gitnexus-noise-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.repo = self.root / "runner"
        self.repo.mkdir()
        self.git("init", "-q")
        for name in ("AGENTS.md", "CLAUDE.md"):
            (self.repo / name).write_text(DOC)
        (self.repo / "tick.sh").write_text("echo tick\n")
        (self.repo / ".gitnexus").mkdir()
        (self.repo / ".gitignore").write_text(".gitnexus/\n")
        self.git("add", "-A")
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        self.home = self.root / "home"
        self.home.mkdir()

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def run_helper(self, wait: str = "5") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--repo", str(self.repo), "--wait", wait],
            capture_output=True,
            text=True,
            env={**os.environ, "HOME": str(self.home)},
            timeout=30,
        )

    @staticmethod
    def reap(process: subprocess.Popen[bytes]) -> None:
        process.kill()
        process.wait()

    def edit(self, name: str, old: str, new: str) -> None:
        path = self.repo / name
        text = path.read_text()
        self.assertIn(old, text)
        path.write_text(text.replace(old, new))

    def noise(self, name: str = "AGENTS.md") -> None:
        self.edit(name, "10 symbols, 20 relationships", "11 symbols, 22 relationships")

    def status(self) -> str:
        return self.git("status", "--porcelain", "--untracked-files=no")

    def assert_stopped(self, reason: str) -> None:
        before = self.status()
        result = self.run_helper()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(reason, result.stderr)
        self.assertEqual(self.status(), before)

    def test_clean_tree(self) -> None:
        result = self.run_helper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_noise_only_is_restored(self) -> None:
        self.noise("AGENTS.md")
        self.noise("CLAUDE.md")
        self.edit("CLAUDE.md", "> Index stale? Run analyze.\n", "")
        result = self.run_helper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "restored GitNexus-only changes in AGENTS.md CLAUDE.md", result.stdout
        )
        self.assertEqual(self.status(), "")
        self.assertFalse((self.repo / ".gitnexus/.analyze.lock").exists())

    def test_insert_inside_block_is_restored(self) -> None:
        self.edit("AGENTS.md", "# GitNexus\n", "# GitNexus\nNew line.\n")
        self.assertEqual(self.run_helper().returncode, 0)
        self.assertEqual(self.status(), "")

    def test_untracked_files_are_left_alone(self) -> None:
        (self.repo / "scratch.txt").write_text("keep\n")
        self.noise()
        self.assertEqual(self.run_helper().returncode, 0)
        self.assertEqual((self.repo / "scratch.txt").read_text(), "keep\n")

    def test_noise_plus_change_outside_block_stops(self) -> None:
        self.noise()
        self.edit("AGENTS.md", "Hand-written text.", "Edited text.")
        self.assert_stopped("change outside the GitNexus block")

    def test_change_outside_block_stops(self) -> None:
        self.edit("CLAUDE.md", "Footer.", "New footer.")
        self.assert_stopped("change outside the GitNexus block")

    def test_insert_right_after_end_marker_stops(self) -> None:
        self.edit(
            "AGENTS.md", "<!-- gitnexus:end -->\n", "<!-- gitnexus:end -->\nextra\n"
        )
        self.assert_stopped("change outside the GitNexus block")

    def test_noise_plus_other_file_stops(self) -> None:
        self.noise()
        (self.repo / "tick.sh").write_text("echo changed\n")
        self.assert_stopped("tracked change in tick.sh")

    def test_staged_noise_stops(self) -> None:
        self.noise()
        self.git("add", "AGENTS.md")
        self.assert_stopped("staged, conflict or other")

    def test_broken_markers_stop(self) -> None:
        self.edit("AGENTS.md", "<!-- gitnexus:end -->", "")
        self.assert_stopped("GitNexus markers missing")

    def test_mode_change_stops(self) -> None:
        self.noise()
        (self.repo / "AGENTS.md").chmod(0o755)
        self.assert_stopped("mode or type changed")

    def test_file_replaced_by_symlink_stops(self) -> None:
        (self.repo / "AGENTS.md").unlink()
        (self.repo / "AGENTS.md").symlink_to("CLAUDE.md")
        self.assert_stopped("status 'T'")

    def test_running_analyze_stops_without_restore(self) -> None:
        self.noise()
        sleeper = subprocess.Popen(["sleep", "30"])
        self.addCleanup(self.reap, sleeper)
        (self.repo / ".gitnexus/.analyze.lock").write_text(f"{sleeper.pid}\n")
        before = self.status()
        result = self.run_helper(wait="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("analyze still running", result.stderr)
        self.assertEqual(self.status(), before)

    def test_running_global_analyze_stops_without_restore(self) -> None:
        self.noise()
        sleeper = subprocess.Popen(["sleep", "30"])
        self.addCleanup(self.reap, sleeper)
        logs = self.home / ".claude/logs/gitnexus"
        logs.mkdir(parents=True)
        (logs / "runner.lock").write_text(f"{sleeper.pid}\n")
        result = self.run_helper(wait="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("analyze still running", result.stderr)

    def test_stale_lock_is_ignored(self) -> None:
        self.noise()
        dead = subprocess.Popen(["true"])
        dead.wait()
        (self.repo / ".gitnexus/.analyze.lock").write_text(f"{dead.pid}\n")
        self.assertEqual(self.run_helper().returncode, 0)
        self.assertEqual(self.status(), "")

    # In-process runs below: an analyzer starts at an exact point of the run.

    def start_analyzer(self, lock: Path, seconds: str = "2") -> int:
        # Detached, so init reaps it: a zombie child still looks alive to kill 0.
        pid = int(
            subprocess.run(
                ["sh", "-c", f"sleep {seconds} >/dev/null 2>&1 & echo $!"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )
        self.addCleanup(self.kill_quietly, pid)
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(f"{pid}\n")
        return pid

    @staticmethod
    def kill_quietly(pid: int) -> None:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def run_in_process(self, wait: float = 10) -> int:
        with (
            mock.patch.dict(os.environ, {"HOME": str(self.home)}),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            return gitnexus_noise.run(self.repo, wait)

    def test_analyzer_starting_during_check_keeps_its_lock(self) -> None:
        self.noise()
        lock = self.repo / ".gitnexus/.analyze.lock"
        real_check = gitnexus_noise.check
        calls: list[int] = []

        def check(repo: Path) -> list[str]:
            if not calls:
                calls.append(self.start_analyzer(lock))
            else:
                calls.append(0)
            return real_check(repo)

        with mock.patch.object(gitnexus_noise, "check", check):
            self.assertEqual(self.run_in_process(), 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(lock.read_text(), f"{calls[0]}\n")
        self.assertEqual(self.status(), "")

    def test_global_refresh_during_restore_is_restored_again(self) -> None:
        self.noise()
        lock = self.home / ".claude/logs/gitnexus/runner.lock"
        real_git = gitnexus_noise.git
        pids: list[int] = []

        def git(repo: Path, *args: str) -> str:
            out = real_git(repo, *args)
            if args[0] == "restore" and not pids:
                pids.append(self.start_analyzer(lock))
                self.noise("CLAUDE.md")  # the refresh rewrites the block
            return out

        with mock.patch.object(gitnexus_noise, "git", git):
            self.assertEqual(self.run_in_process(), 0)
        self.assertEqual(lock.read_text(), f"{pids[0]}\n")
        self.assertEqual(self.status(), "")

    def test_analyzer_that_keeps_starting_stops_the_tick(self) -> None:
        self.noise()
        lock = self.repo / ".gitnexus/.analyze.lock"
        pid = self.start_analyzer(lock, "30")
        with (
            mock.patch.object(gitnexus_noise, "wait_for_analyze", lambda *_: None),
            self.assertRaises(gitnexus_noise.Stop),
        ):
            self.run_in_process()
        self.assertEqual(lock.read_text(), f"{pid}\n")


if __name__ == "__main__":
    unittest.main()
