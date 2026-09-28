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
# Python line for a stub: the other runner stores a quota wait now.
WAIT_WRITE = (
    "open(os.path.join(os.environ['EPIC_STATE_DIR'], 'github-quota-wait.json'), 'w')"
    '.write(\'{"retry_at": "2099-01-01T00:00:00Z"}\')'
)


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
            "scripts/epic/github_quota.py",
            "scripts/epic/agents.py",
            ".codex/epic_lock.py",
            ".claude/commands/epic/epic-tick.md",
        ):
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / rel, self.repo / rel)
        (self.repo / "scripts/epic/next_action.py").write_text(
            "import json, os, sys\n"
            "with open(os.environ['TEST_CALLS'], 'a') as f:\n"
            "    f.write(json.dumps(['select']) + '\\n')\n"
            "if os.environ.get('TEST_SELECT_WAIT'):\n"
            f"    {WAIT_WRITE}\n"
            "code = int(os.environ.get('TEST_SELECT_EXIT', '0'))\n"
            f"print({json.dumps(json.dumps(ACTION))}) if code == 0 else sys.exit(code)\n"
        )
        (self.repo / "scripts/epic/tick_gate.py").write_text(
            "import json, os, sys\n"
            "with open(os.environ['TEST_CALLS'], 'a') as f:\n"
            "    f.write(json.dumps(['gate', sys.argv[1]]) + '\\n')\n"
            "if sys.argv[1] == 'check' and os.environ.get('TEST_GATE_WAIT'):\n"
            f"    {WAIT_WRITE}\n"
            "if sys.argv[1] == 'check':\n"
            "    sys.exit(int(os.environ.get('TEST_GATE_EXIT', '0')))\n"
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
            # Another runner stores a quota wait while this session runs.
            'if [[ -n "${TEST_MODEL_WAIT:-}" ]]; then\n'
            '    printf \'{"retry_at": "2099-01-01T00:00:00Z"}\' > "$EPIC_STATE_DIR/github-quota-wait.json"\n'
            "fi\n"
            'if [[ -n "${TEST_MODEL_SLEEP:-}" ]]; then exec sleep "$TEST_MODEL_SLEEP"; fi\n'
        )
        (bin_dir / "gh").write_text(
            "#!/bin/bash\n"
            'printf \'["gh", "%s"]\\n\' "$*" >> "$TEST_CALLS"\n'
            'if [[ -n "${TEST_GH_OUT:-}" ]]; then echo "$TEST_GH_OUT"; exit 0; fi\n'
            "exit 1\n"
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
            [calls[0], calls[1], calls[2], calls[4], calls[5]],
            [
                ["git", "pull -q --ff-only"],
                ["select"],
                ["gate", "check"],
                ["verify", "--since"],
                ["gate", "record"],
            ],
        )
        self.assertEqual(calls[3][0], "claude")
        self.assertTrue(
            calls[3][1].startswith(
                "-p --model opus --effort high --permission-mode auto"
            )
        )
        self.assertFalse((self.state / "claude.lock").exists())

    def test_model_prompts_go_to_the_permission_gate(self) -> None:
        # claude -p cannot prompt; without the gate, pushes and merges stop.
        self.assertEqual(self.run_tick().wait(timeout=30), 0)
        args = self.calls_made()[3][1]
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
            self.calls_made()[-2:], [["verify", "--since"], ["gate", "record"]]
        )
        self.assertIn("tick: finished exit=1", (self.state / "claude.log").read_text())
        self.assertFalse((self.state / "claude.lock").exists())

    def test_failed_pull_stops_the_tick(self) -> None:
        self.assertEqual(self.run_tick(TEST_GIT_EXIT="1").wait(timeout=30), 1)
        self.assertEqual(self.calls_made(), [["git", "pull -q --ff-only"]])
        self.assertFalse((self.state / "claude.lock").exists())

    def store_wait(self, retry_at: str = "2099-01-01T00:00:00Z") -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "github-quota-wait.json").write_text(
            json.dumps({"retry_at": retry_at})
        )

    def test_stored_quota_wait_makes_no_call_and_starts_no_model(self) -> None:
        self.store_wait()
        self.assertEqual(self.run_tick().wait(timeout=30), 0)
        self.assertFalse(self.calls.exists())  # no git, gh, selector or model
        self.assertIn("retry at 2099-01-01", (self.state / "claude.log").read_text())
        self.assertFalse((self.state / "claude.lock").exists())

    def test_expired_quota_wait_runs_normally(self) -> None:
        self.store_wait("2000-01-01T00:00:00Z")
        self.assertEqual(self.run_tick().wait(timeout=30), 0)
        self.assertEqual(self.calls_made()[3][0], "claude")

    def test_bad_quota_wait_file_stops_the_tick(self) -> None:
        self.state.mkdir(parents=True)
        (self.state / "github-quota-wait.json").write_text("not json")
        self.assertEqual(self.run_tick().wait(timeout=30), 2)
        self.assertFalse(self.calls.exists())

    def test_selector_quota_error_stops_before_gate_and_model(self) -> None:
        self.assertEqual(self.run_tick(TEST_SELECT_EXIT="4").wait(timeout=30), 75)
        self.assertEqual(self.calls_made(), [["git", "pull -q --ff-only"], ["select"]])
        self.assertFalse((self.state / "claude.lock").exists())

    def test_selector_deferred_ends_quietly(self) -> None:
        self.assertEqual(self.run_tick(TEST_SELECT_EXIT="3").wait(timeout=30), 0)
        self.assertEqual(self.calls_made(), [["git", "pull -q --ff-only"], ["select"]])

    def test_gate_quota_error_starts_no_model(self) -> None:
        self.assertEqual(self.run_tick(TEST_GATE_EXIT="4").wait(timeout=30), 75)
        self.assertEqual(self.calls_made()[-1], ["gate", "check"])
        self.assertNotIn("claude", [c[0] for c in self.calls_made()])

    def test_verify_quota_error_fails_the_tick(self) -> None:
        self.assertEqual(self.run_tick(TEST_VERIFY_EXIT="4").wait(timeout=30), 75)
        self.assertEqual(self.calls_made()[-1], ["verify", "--since"])

    def test_wait_stored_during_the_session_skips_verify(self) -> None:
        self.assertEqual(self.run_tick(TEST_MODEL_WAIT="1").wait(timeout=30), 75)
        calls = self.calls_made()
        self.assertEqual(calls[-1][0], "claude")
        self.assertNotIn(["verify", "--since"], calls)
        self.assertNotIn(["gate", "record"], calls)

    def use_real_gate(self) -> None:
        shutil.copyfile(
            ROOT / "scripts/epic/tick_gate.py", self.repo / "scripts/epic/tick_gate.py"
        )

    def test_quota_stop_after_the_session_keeps_the_repeat_gate(self) -> None:
        # Codex review on PR 440: a quota stop recorded the gate, so the
        # unverified action was skipped as a repeat after the reset.
        old = {"fingerprint": "old", "updated_at": "x", "at": 1.0, "action": {}}
        for name, env in (
            ("wait during session", {"TEST_MODEL_WAIT": "1"}),
            ("verify quota error", {"TEST_VERIFY_EXIT": "4"}),
        ):
            for existing in (None, old):
                with self.subTest(name, existing=existing is not None):
                    self.setUp()
                    self.use_real_gate()
                    gate_file = self.state / "claude-gate.json"
                    if existing is not None:
                        self.state.mkdir(parents=True)
                        gate_file.write_text(json.dumps(existing))
                    runner = self.run_tick(TEST_GH_OUT="2026-09-28T20:00:00Z", **env)
                    self.assertEqual(runner.wait(timeout=30), 75)
                    self.assertIn("claude", [c[0] for c in self.calls_made()])
                    if existing is None:
                        self.assertFalse(gate_file.exists())
                    else:
                        self.assertEqual(json.loads(gate_file.read_text()), existing)

    def test_ordinary_verify_failure_still_records_the_real_gate(self) -> None:
        self.use_real_gate()
        runner = self.run_tick(TEST_GH_OUT="2026-09-28T20:00:00Z", TEST_VERIFY_EXIT="1")
        self.assertEqual(runner.wait(timeout=30), 1)
        record = json.loads((self.state / "claude-gate.json").read_text())
        self.assertEqual(record["action"], ACTION)

    def test_wait_stored_during_selection_makes_no_further_call(self) -> None:
        # Codex review on PR 440: the gate still read GitHub and a model started.
        self.assertEqual(self.run_tick(TEST_SELECT_WAIT="1").wait(timeout=30), 0)
        self.assertEqual(self.calls_made(), [["git", "pull -q --ff-only"], ["select"]])
        self.assertFalse((self.state / "claude.lock").exists())

    def test_wait_stored_during_the_gate_check_starts_no_model(self) -> None:
        self.assertEqual(self.run_tick(TEST_GATE_WAIT="1").wait(timeout=30), 0)
        self.assertEqual(
            self.calls_made(),
            [["git", "pull -q --ff-only"], ["select"], ["gate", "check"]],
        )
    def test_unset_state_dir_uses_the_home_default(self) -> None:
        env = {k: v for k, v in self.env.items() if k != "EPIC_STATE_DIR"}
        runner = subprocess.Popen(
            ["/bin/bash", str(self.repo / "scripts/epic/claude-tick.sh")], env=env
        )
        self.assertEqual(runner.wait(timeout=30), 0)
        default = Path(env["HOME"]) / ".local/state/epic-loop"
        self.assertIn("tick: finished exit=0", (default / "claude.log").read_text())
        self.assertFalse(self.state.exists())

    def test_registered_agent_uses_its_own_folder(self) -> None:
        env = {"EPIC_AGENT_ID": "claude-2", "EPIC_AGENT_LABEL": "docs"}
        self.assertEqual(self.run_tick(**env).wait(timeout=30), 0)
        home = self.state / "agents/claude-2"
        agent = json.loads((home / "agent.json").read_text())
        self.assertEqual(
            (agent["kind"], agent["label"], agent["interval_seconds"]),
            ("claude", "docs", 600),
        )
        self.assertEqual(agent["budget_seconds"], 1930)
        self.assertIn("tick: finished exit=0", (home / "claude.log").read_text())
        self.assertFalse((self.state / "claude.log").exists())
        self.assertFalse((home / "claude.lock").exists())
        config = self.calls_made()[2][1].split("--mcp-config ", 1)[1].split(" --", 1)[0]
        server = json.loads(config)["mcpServers"]["epic-gate"]
        self.assertEqual(server["env"], {"EPIC_STATE_DIR": str(home)})

    def test_agent_id_of_another_kind_is_refused(self) -> None:
        runner = subprocess.Popen(
            ["/bin/bash", str(self.repo / "scripts/epic/claude-tick.sh")],
            env={**self.env, "EPIC_AGENT_ID": "codex-2"},
            stderr=subprocess.PIPE,
        )
        _, error = runner.communicate(timeout=30)
        self.assertEqual(runner.returncode, 2)
        self.assertIn(b"EPIC_AGENT_ID must look like claude", error)
        self.assertFalse(self.state.exists())
        self.assertFalse(self.calls.exists())

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
