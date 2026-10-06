"""wait_cmd.sh runs one command and returns only when it ends or times out
(https://github.com/phaabe/live.moafunk.de/issues/426)."""

from __future__ import annotations

import isolated_env  # noqa: F401  (first: hides live runner state)

import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "wait_cmd.sh"


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class WaitCmdTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.state = self.tmp / "state"
        self.env = dict(os.environ)
        self.env["EPIC_STATE_DIR"] = str(self.state)
        self.env["WAIT_CMD_KILL_GRACE"] = "1"
        self.log = self.state / "claude-cmd.log"
        self.pids: list[int] = []

    def tearDown(self) -> None:
        for pid in self.pids:
            if alive(pid):
                os.kill(pid, signal.SIGKILL)
        subprocess.run(["rm", "-rf", str(self.tmp)], check=True)

    def run_cmd(
        self, *args: str, timeout: float = 30
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(SCRIPT), *args],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def wait_for(self, path: Path, limit: float = 10) -> str:
        end = time.monotonic() + limit
        while time.monotonic() < end:
            if path.exists() and path.read_text().strip():
                return path.read_text().strip()
            time.sleep(0.05)
        self.fail(f"{path} never appeared")

    def assert_gone(self, pid: int, limit: float = 5) -> None:
        end = time.monotonic() + limit
        while time.monotonic() < end:
            if not alive(pid):
                return
            time.sleep(0.05)
        self.fail(f"process {pid} is still running")

    def background_sleeper(self) -> tuple[str, Path]:
        """A shell command that starts a grandchild sleep and records its pid."""
        pid_file = self.tmp / "grandchild.pid"
        return f"sleep 60 & echo $! > {pid_file}; wait", pid_file

    def test_success_prints_exit_and_output(self) -> None:
        result = self.run_cmd(
            "--agent", "claude", "--", "sh", "-c", "echo hello; echo oops >&2"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("wait_cmd: exit=0 duration=", result.stdout)
        self.assertIn(f"wait_cmd: full output in {self.log}", result.stdout)
        self.assertIn("hello\noops\n", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_failure_keeps_the_exit_code(self) -> None:
        result = self.run_cmd("--agent", "claude", "--", "sh", "-c", "echo bad; exit 3")
        self.assertEqual(result.returncode, 3)
        self.assertIn("wait_cmd: exit=3 ", result.stdout)
        self.assertIn("bad", result.stdout)

    def test_timeout_exits_124_and_stops_the_group(self) -> None:
        command, pid_file = self.background_sleeper()
        started = time.monotonic()
        result = self.run_cmd(
            "--agent", "claude", "--timeout", "1", "--", "sh", "-c", command
        )
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(result.returncode, 124)
        self.assertIn("(timeout after 1s)", result.stdout)
        grandchild = int(pid_file.read_text())
        self.pids.append(grandchild)
        self.assert_gone(grandchild)
        self.assertNotIn("Terminated", result.stderr)

    def test_timeout_kills_a_child_that_ignores_term(self) -> None:
        pid_file = self.tmp / "stubborn.pid"
        command = f"trap '' TERM; echo $$ > {pid_file}; while :; do sleep 1; done"
        result = self.run_cmd(
            "--agent", "claude", "--timeout", "1", "--", "sh", "-c", command
        )
        self.assertEqual(result.returncode, 124)
        stubborn = int(pid_file.read_text())
        self.pids.append(stubborn)
        self.assert_gone(stubborn)

    def signal_test(self, sig: signal.Signals, code: int) -> None:
        command, pid_file = self.background_sleeper()
        proc = subprocess.Popen(
            [str(SCRIPT), "--agent", "claude", "--", "sh", "-c", command],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.pids.append(proc.pid)
        grandchild = int(self.wait_for(pid_file))
        self.pids.append(grandchild)
        proc.send_signal(sig)
        stdout, _ = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, code)
        self.assertIn(f"wait_cmd: exit={code} ", stdout)
        self.assertIn("(stopped by signal)", stdout)
        self.assert_gone(grandchild)

    def test_sigterm_stops_the_child(self) -> None:
        self.signal_test(signal.SIGTERM, 143)

    def test_sigint_stops_the_child(self) -> None:
        self.signal_test(signal.SIGINT, 130)

    def test_log_is_appended_with_start_and_end_lines(self) -> None:
        self.env["SECRET_FOR_TEST"] = "do-not-log-me"
        self.run_cmd("--agent", "claude", "--", "echo", "first run")
        self.run_cmd("--agent", "claude", "--", "sh", "-c", "echo second; exit 4")
        text = self.log.read_text()
        lines = text.splitlines()
        self.assertEqual(len(lines), 6, text)
        self.assertRegex(
            lines[0],
            r"^=== \S+Z start: cwd=\S+ timeout=900s command: echo first\\ run$",
        )
        self.assertEqual(lines[1], "first run")
        self.assertRegex(lines[2], r"^=== \S+Z end: exit=0 duration=\d+s$")
        self.assertIn("start:", lines[3])
        self.assertEqual(lines[4], "second")
        self.assertRegex(lines[5], r"end: exit=4 ")
        self.assertNotIn("do-not-log-me", text)

    def test_stdout_is_short(self) -> None:
        result = self.run_cmd(
            "--agent",
            "claude",
            "--",
            "sh",
            "-c",
            "i=1; while [ $i -le 100 ]; do echo line$i; i=$((i+1)); done",
        )
        self.assertEqual(result.returncode, 0)
        out = result.stdout.splitlines()
        self.assertEqual(len(out), 3 + 40)
        self.assertEqual(out[3], "line61")
        self.assertEqual(out[-1], "line100")
        self.assertEqual(len(self.log.read_text().splitlines()), 102)

    def test_agent_comes_from_the_agent_id(self) -> None:
        self.env["EPIC_AGENT_ID"] = "codex-2"
        result = self.run_cmd("--", "true")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.state / "codex-cmd.log").exists())

    def test_usage_errors_exit_2(self) -> None:
        for args in (
            ["--agent", "claude", "--"],
            ["--agent", "claude", "true"],
            ["--agent", "claude", "--timeout", "0", "--", "true"],
            ["--agent", "claude", "--timeout", "1.5", "--", "true"],
            ["--", "true"],
            ["--agent", "../x", "--", "true"],
        ):
            with self.subTest(args=args):
                result = self.run_cmd(*args)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
        self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
