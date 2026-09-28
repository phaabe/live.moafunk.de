"""Run the real claude-tick.sh with isolated state and fake git/selector/gate/model."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ACTION = {"action": "fix", "reason": "test", "pr": 1, "sha": "a" * 40}


class ClaudeTickTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="claude-tick-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "checkout"
        self.state = self.root / "state"
        self.model_pid = self.root / "model.pid"
        self.calls = self.root / "calls.jsonl"
        for rel in (
            "scripts/epic/claude-tick.sh",
            ".codex/epic_lock.py",
            ".claude/commands/epic/epic-tick.md",
        ):
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / rel, self.repo / rel)
        (self.repo / "scripts/epic/next_action.py").write_text(
            f"print({json.dumps(json.dumps(ACTION))})\n"
        )
        (self.repo / "scripts/epic/tick_gate.py").write_text(
            "import json, os, sys\n"
            "with open(os.environ['TEST_CALLS'], 'a') as f:\n"
            "    f.write(json.dumps(['gate', sys.argv[1]]) + '\\n')\n"
        )
        (self.repo / "scripts/epic/tick_verify.py").write_text(
            "import json, os, sys\n"
            "with open(os.environ['TEST_CALLS'], 'a') as f:\n"
            "    f.write(json.dumps(['verify', sys.argv[-2]]) + '\\n')\n"
            "sys.exit(int(os.environ.get('TEST_VERIFY_EXIT', '0')))\n"
        )
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        (bin_dir / "git").write_text(
            "#!/bin/bash\n"
            'printf \'["git", "%s"]\\n\' "$*" >> "$TEST_CALLS"\n'
            'exit "${TEST_GIT_EXIT:-0}"\n'
        )
        (bin_dir / "claude").write_text(
            "#!/bin/bash\n"
            # JSON-encode: the arguments include the gate's JSON config.
            'python3 -c \'import json, sys; print(json.dumps(["claude", " ".join(sys.argv[1:])]))\' "$@" >> "$TEST_CALLS"\n'
            'echo $$ > "$TEST_MODEL_PID"\n'
            'if [[ -n "${TEST_MODEL_SLEEP:-}" ]]; then exec sleep "$TEST_MODEL_SLEEP"; fi\n'
        )
        for stub in bin_dir.iterdir():
            stub.chmod(0o755)
        home = self.root / "home"
        home.mkdir()
        self.env = {
            **os.environ,
            "HOME": str(home),
            "EPIC_STATE_DIR": str(self.state),
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "TEST_CALLS": str(self.calls),
            "TEST_MODEL_PID": str(self.model_pid),
        }

    def run_tick(self, **env: str) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            ["/bin/bash", str(self.repo / "scripts/epic/claude-tick.sh")],
            env={**self.env, **env},
        )

    def calls_made(self) -> list[list[str]]:
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def test_fix_runs_opus_and_records_the_gate(self) -> None:
        self.assertEqual(self.run_tick().wait(timeout=30), 0)
        calls = self.calls_made()
        self.assertEqual(
            [calls[0], calls[1], calls[3], calls[4]],
            [
                ["git", "pull -q --ff-only"],
                ["gate", "check"],
                ["gate", "record"],
                ["verify", "--since"],
            ],
        )
        self.assertEqual(calls[2][0], "claude")
        self.assertTrue(
            calls[2][1].startswith(
                "-p --model opus --effort high --permission-mode auto"
            )
        )
        self.assertFalse((self.state / "claude.lock").exists())

    def test_model_prompts_go_to_the_permission_gate(self) -> None:
        # claude -p cannot prompt; without the gate, pushes and merges stop.
        self.assertEqual(self.run_tick().wait(timeout=30), 0)
        args = self.calls_made()[2][1]
        self.assertIn("--permission-prompt-tool mcp__epic-gate__approve", args)
        config = json.loads(args.split("--mcp-config ", 1)[1].split(" --", 1)[0])
        server = config["mcpServers"]["epic-gate"]
        self.assertEqual(
            server["args"],
            [str(self.repo.resolve() / "scripts/epic/permission_gate.py")],
        )
        self.assertEqual(server["env"], {"EPIC_STATE_DIR": str(self.state)})

    def test_action_that_did_not_land_fails_the_tick_after_recording(self) -> None:
        # A denied push or merge exited 0 before; now the tick reports it.
        self.assertEqual(self.run_tick(TEST_VERIFY_EXIT="1").wait(timeout=30), 1)
        self.assertEqual(
            self.calls_made()[-2:], [["gate", "record"], ["verify", "--since"]]
        )
        self.assertIn("tick: finished exit=1", (self.state / "claude.log").read_text())
        self.assertFalse((self.state / "claude.lock").exists())

    def test_failed_pull_stops_the_tick(self) -> None:
        self.assertEqual(self.run_tick(TEST_GIT_EXIT="1").wait(timeout=30), 1)
        self.assertEqual(self.calls_made(), [["git", "pull -q --ff-only"]])
        self.assertFalse((self.state / "claude.lock").exists())

    def test_term_stops_the_model_before_the_lock_is_released(self) -> None:
        # Codex review on PR 412: TERM removed the lock but left the model running.
        runner = self.run_tick(TEST_MODEL_SLEEP="60")
        deadline = time.time() + 20
        while not self.model_pid.exists() and time.time() < deadline:
            time.sleep(0.1)
        pid = int(self.model_pid.read_text())
        runner.send_signal(signal.SIGTERM)
        self.assertEqual(runner.wait(timeout=30), 143)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertFalse((self.state / "claude.lock").exists())
        self.assertNotIn(["gate", "record"], self.calls_made())


if __name__ == "__main__":
    unittest.main()
