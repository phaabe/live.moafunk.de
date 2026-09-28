"""Run the real Bash wrapper with isolated state and fake agent/API boundaries."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


class TickTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="epic-tick-", dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "checkout with spaces"
        self.home = self.root / "home"
        self.home.mkdir()
        self.state = self.home / ".local/state/epic-loop"
        self.lock = self.state / "codex.lock"
        self.calls = self.root / "codex-calls.jsonl"
        self.selections = self.root / "selector-calls"
        self.runner = self.repo / ".codex/codex-tick.sh"
        self.runner.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / "codex-tick.sh", self.runner)
        shutil.copyfile(ROOT / "epic-tick.md", self.runner.parent / "epic-tick.md")
        shutil.copyfile(ROOT / "epic_lock.py", self.runner.parent / "epic_lock.py")
        selector = self.repo / "scripts/epic/next_action.py"
        selector.parent.mkdir(parents=True)
        selector.write_text(
            "import os, pathlib, sys\n"
            "assert sys.argv[1:] == ['--agent', 'codex']\n"
            "with open(os.environ['TEST_SELECTIONS'], 'a') as f: f.write('call\\n')\n"
            "if os.environ.get('TEST_SELECTOR_EXIT'): sys.exit(23)\n"
            "if os.environ.get('TEST_PAUSE_AFTER_SELECT'):\n"
            "    (pathlib.Path.home() / '.epic-pause').touch()\n"
            "print(os.environ['TEST_DECISION'])\n"
        )
        self.bin = self.root / "bin"
        self.bin.mkdir()
        codex = self.bin / "codex"
        codex.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, socket, sys\n"
            "with open(os.environ['TEST_CALLS'], 'a') as f:\n"
            "    f.write(json.dumps({'args': sys.argv[1:], 'prompt': sys.stdin.read()}) + '\\n')\n"
            "print('fake Codex stdout', flush=True)\n"
            "print('fake Codex stderr', file=sys.stderr, flush=True)\n"
            "if os.environ.get('TEST_SOCKET'):\n"
            "    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:\n"
            "        s.connect(os.environ['TEST_SOCKET'])\n"
            "        s.sendall(b'ready')\n"
            "        s.recv(1)\n"
            "sys.exit(int(os.environ.get('TEST_CODEX_EXIT', '0')))\n"
        )
        codex.chmod(0o755)
        self.env = {
            **os.environ,
            "HOME": str(self.home),
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "TEST_CALLS": str(self.calls),
            "TEST_SELECTIONS": str(self.selections),
            "TEST_DECISION": json.dumps(
                {"action": "review", "pr": 406, "sha": "a" * 40}
            ),
            "EPIC_TICK_TIMEOUT_SECONDS": "10",
            "EPIC_SELECT_TIMEOUT_SECONDS": "10",
        }

    def run_tick(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/bash", str(self.runner)],
            env=self.env,
            cwd=self.home,
            capture_output=True,
            text=True,
            timeout=20,
        )

    def test_pause_never_calls_selector_or_codex(self) -> None:
        (self.home / ".epic-pause").touch()
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertFalse(self.selections.exists())
        self.assertFalse(self.calls.exists())
        self.assertFalse(self.lock.exists())

    def test_existing_lock_is_not_removed(self) -> None:
        self.lock.mkdir(parents=True)
        owner = self.lock / "owner"
        owner.write_text("another tick")
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertEqual(owner.read_text(), "another tick")
        self.assertFalse(self.selections.exists())
        self.assertFalse(self.calls.exists())

    def test_idle_and_stop_do_not_start_codex(self) -> None:
        for action in ("idle", "stop"):
            with self.subTest(action=action):
                self.env["TEST_DECISION"] = json.dumps({"action": action})
                self.assertEqual(self.run_tick().returncode, 0)
                self.assertFalse(self.calls.exists())
                self.assertFalse(self.lock.exists())
        self.assertEqual(self.selections.read_text().splitlines(), ["call", "call"])

    def seed_lock(self, pid: int, age: int, max_age: int = 30) -> None:
        self.lock.mkdir(parents=True)
        (self.lock / "owner.json").write_text(
            json.dumps(
                {"pid": pid, "started_at": int(time.time()) - age, "max_age": max_age}
            )
        )

    @staticmethod
    def dead_pid() -> int:
        process = subprocess.Popen(["/usr/bin/true"])
        process.wait(timeout=5)
        return process.pid

    def test_dead_expired_lock_is_reclaimed_and_logged(self) -> None:
        pid = self.dead_pid()
        self.seed_lock(pid, age=1000)
        (self.lock / "action.json").write_text("old action")
        (self.lock / "prompt.txt").write_text("old prompt")
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertTrue(self.calls.exists())
        self.assertFalse(self.lock.exists())
        self.assertIn(
            f"reclaimed stale lock from pid {pid}",
            (self.state / "codex.log").read_text(),
        )

    def test_live_pid_keeps_even_an_old_lock(self) -> None:
        self.seed_lock(os.getpid(), age=1000)
        owner = (self.lock / "owner.json").read_text()
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertEqual((self.lock / "owner.json").read_text(), owner)
        self.assertFalse(self.selections.exists())

    def test_dead_pid_keeps_a_young_lock(self) -> None:
        self.seed_lock(self.dead_pid(), age=0)
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertTrue(self.lock.is_dir())
        self.assertFalse(self.selections.exists())

    def test_original_timeout_budget_prevents_early_reclaim(self) -> None:
        self.seed_lock(self.dead_pid(), age=100, max_age=2000)
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertTrue(self.lock.is_dir())
        self.assertFalse(self.selections.exists())

    def test_one_fresh_session_receives_prompt_and_selected_action(self) -> None:
        self.assertEqual(self.run_tick().returncode, 0)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0]["args"],
            [
                "exec",
                "--cd",
                str(self.repo.resolve()),
                "--sandbox",
                "workspace-write",
                "-c",
                "sandbox_workspace_write.network_access=true",
                "--color",
                "never",
                "-",
            ],
        )
        self.assertIn("# Codex epic tick", calls[0]["prompt"])
        self.assertIn(self.env["TEST_DECISION"], calls[0]["prompt"])
        self.assertEqual(self.selections.read_text(), "call\n")
        self.assertFalse(self.lock.exists())
        log = (self.state / "codex.log").read_text()
        self.assertIn("fake Codex stdout", log)
        self.assertIn("fake Codex stderr", log)

    def test_pause_created_during_selection_prevents_session(self) -> None:
        self.env["TEST_PAUSE_AFTER_SELECT"] = "1"
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertFalse(self.calls.exists())
        self.assertFalse(self.lock.exists())

    def test_selector_errors_and_invalid_json_fail_closed(self) -> None:
        for value in ("broken json", '{"action": "unknown"}', "[]"):
            with self.subTest(value=value):
                self.env["TEST_DECISION"] = value
                self.assertNotEqual(self.run_tick().returncode, 0)
                self.assertFalse(self.calls.exists())
                self.assertFalse(self.lock.exists())
        self.env["TEST_SELECTOR_EXIT"] = "1"
        self.assertEqual(self.run_tick().returncode, 23)
        self.assertFalse(self.lock.exists())

    def test_codex_failure_is_logged_and_unlocks(self) -> None:
        self.env["TEST_CODEX_EXIT"] = "17"
        self.assertEqual(self.run_tick().returncode, 17)
        self.assertFalse(self.lock.exists())
        self.assertIn("exit=17", (self.state / "codex.log").read_text())

    def test_invalid_timeout_never_starts_work(self) -> None:
        for value in ("0", "-1", "1.5", "never"):
            with self.subTest(value=value):
                self.env["EPIC_TICK_TIMEOUT_SECONDS"] = value
                self.assertEqual(self.run_tick().returncode, 2)
                self.assertFalse(self.selections.exists())
                self.assertFalse(self.calls.exists())
                self.assertFalse(self.lock.exists())

    def blocked_tick(self) -> tuple[subprocess.Popen[bytes], socket.socket]:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        address = str(self.root / "ready.sock")
        listener.bind(address)
        listener.listen(1)
        listener.settimeout(10)
        self.env["TEST_SOCKET"] = address
        process = subprocess.Popen(["/bin/bash", str(self.runner)], env=self.env)
        self.addCleanup(self.stop_process, process)
        connection, _ = listener.accept()
        self.addCleanup(connection.close)
        connection.settimeout(10)
        self.assertEqual(connection.recv(5), b"ready")
        return process, connection

    @staticmethod
    def stop_process(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=15)

    def test_concurrent_invocation_cannot_start_a_second_session(self) -> None:
        process, connection = self.blocked_tick()
        self.assertTrue(self.lock.is_dir())
        owner = json.loads((self.lock / "owner.json").read_text())
        self.assertEqual(owner["pid"], process.pid)
        self.assertGreater(owner["started_at"], 0)
        self.assertEqual(owner["max_age"], 30)
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)
        self.assertTrue(self.lock.is_dir())
        connection.sendall(b"x")
        self.assertEqual(process.wait(timeout=10), 0)
        self.assertFalse(self.lock.exists())

    def test_concurrent_recovery_cannot_replace_a_new_owner(self) -> None:
        self.seed_lock(self.dead_pid(), age=1000)
        process, connection = self.blocked_tick()
        owner = (self.lock / "owner.json").read_text()
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertEqual((self.lock / "owner.json").read_text(), owner)
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)
        connection.sendall(b"x")
        self.assertEqual(process.wait(timeout=10), 0)
        self.assertFalse(self.lock.exists())

    def test_timeout_stops_child_and_releases_lock(self) -> None:
        self.env["EPIC_TICK_TIMEOUT_SECONDS"] = "1"
        process, connection = self.blocked_tick()
        self.assertEqual(process.wait(timeout=15), 124)
        self.assertEqual(connection.recv(1), b"")
        self.assertFalse(self.lock.exists())

    def test_term_stops_child_before_releasing_lock(self) -> None:
        process, connection = self.blocked_tick()
        process.send_signal(signal.SIGTERM)
        self.assertEqual(process.wait(timeout=15), 143)
        self.assertEqual(connection.recv(1), b"")
        self.assertFalse(self.lock.exists())


if __name__ == "__main__":
    unittest.main()
