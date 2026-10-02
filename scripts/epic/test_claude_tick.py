"""Run the real claude-tick.sh with isolated state and fake git/selector/gate/model."""

from __future__ import annotations

import isolated_env  # noqa: F401  (first: hides live runner state)

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
TRANSCRIPT = ROOT / "scripts/epic/fixtures/claude-transcript.jsonl"
# Usage of that sanitized transcript (test_tick_events.py checks the counting).
TRANSCRIPT_USAGE = {
    "input": 6,
    "output": 943,
    "cache_read": 109053,
    "cache_write": 38698,
}
# Stub model lines: store TEST_MODEL_TRANSCRIPT where the CLI keeps the
# transcript of its --session-id, named after the launch folder.
TRANSCRIPT_STUB = (
    'session=""; previous=""\n'
    'for arg in "$@"; do\n'
    '    if [[ "$previous" == --session-id ]]; then session=$arg; fi\n'
    "    previous=$arg\n"
    "done\n"
    'if [[ -n "${TEST_MODEL_TRANSCRIPT:-}" ]]; then\n'
    "    folder=\"$CLAUDE_CONFIG_DIR/projects/$(pwd -P | sed 's/[^A-Za-z0-9]/-/g')\"\n"
    '    mkdir -p "$folder"\n'
    '    cp "$TEST_MODEL_TRANSCRIPT" "$folder/$session.jsonl"\n'
    "fi\n"
)
ACTION = {"action": "fix", "reason": "test", "pr": 1, "sha": "a" * 40}
# Python line for a stub: the other runner stores a quota wait now.
WAIT_WRITE = (
    "open(os.path.join(os.environ['EPIC_STATE_DIR'], 'github-quota-wait.json'), 'w')"
    '.write(\'{"retry_at": "2099-01-01T00:00:00Z"}\')'
)


class RunnerHarness(unittest.TestCase):
    """Stubs and helpers; test_claude_cooldown.py reuses them."""

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
            "scripts/epic/tick_events.py",
            "scripts/epic/target_lock.py",
            "scripts/epic/tick_cooldown.py",
            "scripts/epic/rebase_policy.py",
            "scripts/epic/claude-result-schema.json",
            # Passed to the model only; the stub model ignores them.
            "scripts/epic/claude-runner-settings.json",
            "scripts/epic/claude-mcp-config.json",
            "scripts/epic/permission_gate.py",
            "scripts/epic/runtime.py",
            "scripts/epic/lockhold",
            ".codex/epic_lock.py",
            ".claude/commands/epic/epic-tick.md",
        ):
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / rel, self.repo / rel)
            if os.access(ROOT / rel, os.X_OK):  # the lockhold prefix runs directly
                (self.repo / rel).chmod(0o755)
        # --recheck: exits from TEST_RECHECK_EXITS in order (the last repeats).
        (self.repo / "scripts/epic/next_action.py").write_text(
            "import json, os, sys, time\n"
            "if '--recheck' in sys.argv:\n"
            "    calls = os.environ['TEST_CALLS']\n"
            "    pr = json.load(open(sys.argv[-1])).get('pr')\n"
            "    with open(calls, 'a') as f:\n"
            "        f.write(json.dumps(['recheck', pr]) + '\\n')\n"
            "    time.sleep(float(os.environ.get('TEST_RECHECK_SLEEP', '0')))\n"
            "    codes = os.environ.get('TEST_RECHECK_EXITS', '0').split(',')\n"
            "    n = sum(1 for line in open(calls) if line.startswith('[\"recheck\"'))\n"
            "    sys.exit(int(codes[min(n, len(codes)) - 1]))\n"
            "with open(os.environ['TEST_CALLS'], 'a') as f:\n"
            "    f.write(json.dumps(['select']) + '\\n')\n"
            "if os.environ.get('TEST_SELECT_WAIT'):\n"
            f"    {WAIT_WRITE}\n"
            "code = int(os.environ.get('TEST_SELECT_EXIT', '0'))\n"
            f"out = os.environ.get('TEST_CANDIDATES') or {json.dumps(json.dumps(ACTION))}\n"
            "print(out) if code == 0 else sys.exit(code)\n"
        )
        (self.repo / "scripts/epic/tick_gate.py").write_text(
            "import json, os, sys\n"
            "with open(os.environ['TEST_CALLS'], 'a') as f:\n"
            "    f.write(json.dumps(['gate', sys.argv[1]]) + '\\n')\n"
            "if sys.argv[1] == 'check' and os.environ.get('TEST_GATE_WAIT'):\n"
            f"    {WAIT_WRITE}\n"
            "if sys.argv[1] == 'check':\n"
            "    seen = os.path.join(os.environ['EPIC_STATE_DIR'], 'claude-gate-seen.json')\n"
            "    open(seen, 'w').write('{}')\n"
            "    sys.exit(int(os.environ.get('TEST_GATE_EXIT', '0')))\n"
            "with open(os.environ['TEST_CALLS'] + '.env', 'a') as f:\n"
            "    f.write(os.environ.get('EPIC_STATE_DIR', '') + '\\n')\n"
        )
        # Records how many calls came before it, to prove it runs before the pull.
        (self.repo / "scripts/epic/gitnexus_noise.py").write_text(
            "import os, sys\n"
            "calls = os.environ['TEST_CALLS']\n"
            "n = len(open(calls).read().splitlines()) if os.path.exists(calls) else 0\n"
            "with open(calls + '.noise', 'a') as f:\n"
            "    f.write(f'{n}\\n')\n"
            "sys.exit(int(os.environ.get('TEST_NOISE_EXIT', '0')))\n"
        )
        # Worktree step: its own call file, so the call indexes above stay.
        (self.repo / "scripts/epic/runner_worktree.py").write_text(
            "import json, os, sys\n"
            "with open(os.environ['TEST_CALLS'] + '.worktree', 'a') as f:\n"
            "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "if os.environ.get('TEST_WORKTREE_OUT'):\n"
            "    print(os.environ['TEST_WORKTREE_OUT'])\n"
            "code = int(os.environ.get('TEST_WORKTREE_EXIT', '0'))\n"
            "only = os.environ.get('TEST_WORKTREE_PR')\n"
            "action = json.load(open(sys.argv[sys.argv.index('--action-file') + 1]))\n"
            "sys.exit(0 if only and str(action.get('pr')) != only else code)\n"
        )
        (self.repo / "scripts/epic/tick_verify.py").write_text(
            "import json, os, sys\n"
            "with open(os.environ['TEST_CALLS'], 'a') as f:\n"
            "    f.write(json.dumps(['verify', sys.argv[-2]]) + '\\n')\n"
            "with open(os.environ['TEST_CALLS'] + '.verify-argv', 'w') as f:\n"
            "    f.write(json.dumps(sys.argv[1:]))\n"
            "with open(os.environ['TEST_CALLS'] + '.since', 'w') as f:\n"
            "    f.write(sys.argv[-1])\n"
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
            'printf \'%s\\n%s\\n\' "${EPIC_ACTION_FILE:-}" "${EPIC_TRUSTED_ROOT:-}" > "$TEST_CALLS.modelenv"\n'
            'printf \'%s\' "${EPIC_WORKTREE:-}" > "$TEST_CALLS.worktree-env"\n'
            'printf \'%s\' "${GIT_EDITOR:-}" > "$TEST_CALLS.editor"\n'
            'printf \'%s\' "${EPIC_BODY_DIR:-}" > "$TEST_CALLS.body-dir"\n'
            'printf \'%s\' "${EPIC_BODY_DIR_ID:-}" > "$TEST_CALLS.body-id"\n'
            'if [[ -n "${EPIC_BODY_DIR:-}" ]]; then\n'
            "    python3 -c 'import os, sys; print(os.stat(sys.argv[1]).st_mode & 0o777)' "
            '"$EPIC_BODY_DIR" > "$TEST_CALLS.body-mode"\n'
            "    python3 -c 'import os, sys; s = os.stat(sys.argv[1]); "
            'print(f"{s.st_dev}:{s.st_ino}")\' '
            '"$EPIC_BODY_DIR" > "$TEST_CALLS.body-inode"\n'
            "fi\n"
            'cat > "$TEST_CALLS.prompt"\n'
            # The session's runtime environment: prefix, updater, python3.
            "printf '%s\\n%s\\n%s\\n' \"${CLAUDE_CODE_SHELL_PREFIX:-}\" "
            '"${DISABLE_AUTOUPDATER:-}" '
            # The binary `python3` really runs, resolved while the session runs.
            "\"$(python3 -c 'import os, sys; print(os.path.realpath(sys.executable))')\" "
            '> "$TEST_CALLS.session-env"\n'
            # A tool child started through the prefix, left running.
            'if [[ -n "${TEST_PREFIX_CHILD:-}" ]]; then\n'
            '    "$CLAUDE_CODE_SHELL_PREFIX" "sleep $TEST_PREFIX_CHILD" > /dev/null 2>&1 &\n'
            '    echo $! > "$TEST_CALLS.child"\n'
            "fi\n"
            + TRANSCRIPT_STUB
            + 'if [[ -n "${TEST_MODEL_RESULT:-}" ]]; then printf \'%s\' "$TEST_MODEL_RESULT"; fi\n'
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
            "CLAUDE_CONFIG_DIR": str(self.root / "claude-config"),
            "EPIC_STATE_DIR": str(self.state),
            "EPIC_LOCK_DIR": str(self.root / "locks"),
            # Legacy mode, explicit; pinned mode has its own tests.
            "EPIC_RUNTIME_LEGACY": "1",
            "EPIC_RUNTIME_HOME": str(self.root / "runtime-home"),
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

    def model_targets(self) -> list[str]:
        """The selected action of each model session, from its prompt's last line."""
        return [c[1] for c in self.calls_made() if c[0] == "claude"]


class ClaudeTickTest(RunnerHarness):
    def test_locked_target_falls_through_to_the_next_candidate(self) -> None:
        second = {"action": "review", "reason": "t", "pr": 2, "sha": "b" * 40}
        locks = self.root / "locks"
        locks.mkdir()
        holder = subprocess.Popen(
            [
                "/bin/bash",
                "-c",
                'exec 8>> "$1"; python3 "$2" acquire --fd 8 && exec sleep 30',
                "_",
                str(locks / "1.lock"),
                str(ROOT / "scripts/epic/target_lock.py"),
            ]
        )
        self.addCleanup(holder.kill)
        time.sleep(0.5)
        candidates = "\n".join(json.dumps(a) for a in (ACTION, second))
        self.assertEqual(self.run_tick(TEST_CANDIDATES=candidates).wait(timeout=30), 0)
        gates = [c for c in self.calls_made() if c[0] == "gate"]
        self.assertEqual(gates, [["gate", "check"], ["gate", "record"]])
        self.assertEqual(len(self.model_targets()), 1)
        self.assertIn(
            "locked by another runner", (self.state / "claude.log").read_text()
        )

    def test_suppressed_repeat_does_not_starve_the_next_target(self) -> None:
        second = {"action": "review", "reason": "t", "pr": 2, "sha": "b" * 40}
        candidates = "\n".join(json.dumps(a) for a in (ACTION, second))
        # The stub gate skips only the first check; the second candidate runs.
        (self.repo / "scripts/epic/tick_gate.py").write_text(
            "import json, os, sys\n"
            "calls = os.environ['TEST_CALLS']\n"
            "with open(calls, 'a') as f:\n"
            "    f.write(json.dumps(['gate', sys.argv[1], json.load(open(sys.argv[-1]))['pr']]) + '\\n')\n"
            "n = sum('\"check\"' in line for line in open(calls))\n"
            "sys.exit(3 if sys.argv[1] == 'check' and n == 1 else 0)\n"
        )
        self.assertEqual(self.run_tick(TEST_CANDIDATES=candidates).wait(timeout=30), 0)
        gates = [c for c in self.calls_made() if c[0] == "gate"]
        self.assertEqual(
            gates, [["gate", "check", 1], ["gate", "check", 2], ["gate", "record", 2]]
        )
        self.assertEqual(len(self.model_targets()), 1)

    def test_gate_reading_stdin_does_not_eat_the_next_candidate(self) -> None:
        # Candidates are read from fd 3, so a command in the loop that reads
        # stdin (here the stub gate) cannot consume them.
        second = {"action": "review", "reason": "t", "pr": 2, "sha": "b" * 40}
        candidates = "\n".join(json.dumps(a) for a in (ACTION, second))
        (self.repo / "scripts/epic/tick_gate.py").write_text(
            "import json, os, sys\n"
            "sys.stdin.read()\n"
            "calls = os.environ['TEST_CALLS']\n"
            "with open(calls, 'a') as f:\n"
            "    f.write(json.dumps(['gate', sys.argv[1], json.load(open(sys.argv[-1]))['pr']]) + '\\n')\n"
            "n = sum('\"check\"' in line for line in open(calls))\n"
            "sys.exit(3 if sys.argv[1] == 'check' and n == 1 else 0)\n"
        )
        # stdin is /dev/null so the new loop's gate never waits on an open
        # pipe; the old loop replaced stdin with the candidates anyway.
        tick = subprocess.Popen(
            ["/bin/bash", str(self.repo / "scripts/epic/claude-tick.sh")],
            env={**self.env, "TEST_CANDIDATES": candidates},
            stdin=subprocess.DEVNULL,
        )
        self.assertEqual(tick.wait(timeout=30), 0)
        gates = [c for c in self.calls_made() if c[0] == "gate"]
        self.assertEqual(
            gates, [["gate", "check", 1], ["gate", "check", 2], ["gate", "record", 2]]
        )

    def test_candidate_after_many_suppressed_ones_still_runs(self) -> None:
        # Codex review on https://github.com/phaabe/live.moafunk.de/issues/488:
        # a cap of ten starved candidate eleven while the first ten stayed blocked.
        blocked = [
            {"action": "review", "reason": "t", "pr": n, "sha": "b" * 40}
            for n in range(1, 12)
        ]
        last = {"action": "review", "reason": "t", "pr": 12, "sha": "c" * 40}
        candidates = "\n".join(json.dumps(a) for a in (*blocked, last))
        (self.repo / "scripts/epic/tick_gate.py").write_text(
            "import json, os, sys\n"
            "pr = json.load(open(sys.argv[-1]))['pr']\n"
            "with open(os.environ['TEST_CALLS'], 'a') as f:\n"
            "    f.write(json.dumps(['gate', sys.argv[1], pr]) + '\\n')\n"
            "sys.exit(3 if sys.argv[1] == 'check' and pr != 12 else 0)\n"
        )
        self.assertEqual(self.run_tick(TEST_CANDIDATES=candidates).wait(timeout=60), 0)
        gates = [c for c in self.calls_made() if c[0] == "gate"]
        self.assertEqual(
            gates,
            [["gate", "check", n] for n in range(1, 13)] + [["gate", "record", 12]],
        )
        self.assertEqual(len(self.model_targets()), 1)

    def test_quota_wait_from_a_skipped_gate_stops_the_candidate_loop(self) -> None:
        # Codex review on PR 497: the next candidate's gate read GitHub anyway.
        second = {"action": "review", "reason": "t", "pr": 2, "sha": "b" * 40}
        candidates = "\n".join(json.dumps(a) for a in (ACTION, second))
        runner = self.run_tick(
            TEST_CANDIDATES=candidates, TEST_GATE_WAIT="1", TEST_GATE_EXIT="3"
        )
        self.assertEqual(runner.wait(timeout=30), 0)
        gates = [c for c in self.calls_made() if c[0] == "gate"]
        self.assertEqual(gates, [["gate", "check"]])
        self.assertEqual(self.model_targets(), [])

    def test_two_runners_on_one_target_start_one_model(self) -> None:
        first = self.run_tick(TEST_MODEL_SLEEP="3")
        for _ in range(100):
            if self.model_pid.exists():
                break
            time.sleep(0.05)
        other_state = self.root / "state-2"
        second = self.run_tick(EPIC_STATE_DIR=str(other_state))
        self.assertEqual(second.wait(timeout=30), 0)
        self.assertEqual(first.wait(timeout=30), 0)
        self.assertEqual(len(self.model_targets()), 1)
        self.assertIn("no candidate to run", (other_state / "claude.log").read_text())

    def test_unknown_candidate_action_stops_the_tick(self) -> None:
        bad = json.dumps({"action": "refine", "reason": "t", "issue": "x"})
        self.assertEqual(self.run_tick(TEST_CANDIDATES=bad).wait(timeout=30), 1)
        self.assertEqual(self.model_targets(), [])

    def test_adopt_runs_sonnet(self) -> None:
        adopt = {"action": "adopt", "reason": "t", "pr": 1, "sha": "a" * 40}
        self.assertEqual(
            self.run_tick(TEST_CANDIDATES=json.dumps(adopt)).wait(timeout=30), 0
        )
        (session,) = self.model_targets()
        self.assertIn("--model sonnet --effort medium", session)

    def test_adopt_gets_a_private_body_dir_removed_after_the_tick(self) -> None:
        adopt = {"action": "adopt", "reason": "t", "pr": 1, "sha": "a" * 40}
        self.assertEqual(
            self.run_tick(TEST_CANDIDATES=json.dumps(adopt)).wait(timeout=30), 0
        )
        body_dir = Path(str(self.calls) + ".body-dir").read_text()
        self.assertTrue(Path(body_dir).name.startswith("epic-adopt-claude."))
        self.assertEqual(Path(str(self.calls) + ".body-mode").read_text(), "448\n")
        (session,) = self.model_targets()
        self.assertIn(f"--add-dir {body_dir}", session)
        config = json.loads(session.split("--mcp-config ", 1)[1].split(" --", 1)[0])
        gate_env = config["mcpServers"]["epic-gate"]["env"]
        self.assertEqual(gate_env["EPIC_BODY_DIR"], body_dir)
        # The gate anchors to the directory the runner created.
        anchor = Path(str(self.calls) + ".body-inode").read_text().strip()
        self.assertEqual(gate_env["EPIC_BODY_DIR_ID"], anchor)
        self.assertEqual(Path(str(self.calls) + ".body-id").read_text(), anchor)
        self.assertIn(
            f"PR body directory (write the adopt body file only here): {body_dir}",
            Path(str(self.calls) + ".prompt").read_text(),
        )
        self.assertFalse(Path(body_dir).exists())

    def test_adopt_body_dir_is_under_tmpdir(self) -> None:
        # The rebase proof sandbox allows writes only under its TMPDIR.
        temporary = self.root / "adopt temp"
        temporary.mkdir()
        adopt = {"action": "adopt", "reason": "t", "pr": 1, "sha": "a" * 40}
        self.assertEqual(
            self.run_tick(
                TEST_CANDIDATES=json.dumps(adopt), TMPDIR=str(temporary)
            ).wait(timeout=30),
            0,
        )
        body_dir = Path(Path(str(self.calls) + ".body-dir").read_text())
        self.assertEqual(body_dir.parent, temporary)
        self.assertTrue(body_dir.name.startswith("epic-adopt-claude."))
        self.assertFalse(body_dir.exists())

    def test_other_actions_get_no_body_dir(self) -> None:
        # An inherited value never reaches the model or the gate.
        inherited = str(self.root)
        self.assertEqual(
            self.run_tick(EPIC_BODY_DIR=inherited, EPIC_BODY_DIR_ID="1:2").wait(
                timeout=30
            ),
            0,
        )
        self.assertEqual(Path(str(self.calls) + ".body-dir").read_text(), "")
        self.assertEqual(Path(str(self.calls) + ".body-id").read_text(), "")
        self.assertTrue(Path(inherited).is_dir())
        (session,) = self.model_targets()
        self.assertNotIn("EPIC_BODY_DIR", session)
        self.assertNotIn("epic-adopt-claude.", session)

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
        env = server["env"]
        lock = self.state / "claude.lock"
        self.assertEqual(env["EPIC_STATE_DIR"], str(self.state))
        self.assertEqual(env["EPIC_ACTION_FILE"], str(lock / "action.json"))
        # The git contract needs the runner context and paths in every tick.
        self.assertEqual(env["EPIC_CONTEXT_FILE"], str(lock / "context.json"))
        self.assertEqual(env["EPIC_TRUSTED_ROOT"], str(self.repo.resolve()))
        self.assertEqual(
            env["EPIC_WORKTREE_DIR"],
            str(self.repo.resolve().parent / "live.moafunk.de-claude-wt"),
        )
        self.assertIn("HOME", env)
        self.assertNotIn("EPIC_SHARED_READER", env)

    def test_runner_settings_and_editor_reach_the_model(self) -> None:
        self.assertEqual(self.run_tick().wait(timeout=30), 0)
        args = self.model_targets()[0]
        # Legacy keeps today's session: project settings plus the runner's
        # ask rules, inline (the file holds the pinned settings now).
        runner_ask = ["Bash(git push:*)", "Bash(git rebase:*)", "Bash(git -*)"]
        self.assertIn(
            "--settings " + json.dumps({"permissions": {"ask": runner_ask}}), args
        )
        self.assertNotIn("--setting-sources", args)
        # Legacy also locks every child (prefix); its claude may update itself.
        prefix, updater, _ = (
            Path(str(self.calls) + ".session-env").read_text().splitlines()
        )
        self.assertEqual(prefix, str(self.repo.resolve() / "scripts/epic/lockhold"))
        self.assertEqual(updater, "")
        self.assertEqual(Path(str(self.calls) + ".editor").read_text(), "true")
        # The pinned file asks the same first, and routes every git global
        # option to the gate; the shared settings stay as they are.
        pinned = json.loads(
            (ROOT / "scripts/epic/claude-runner-settings.json").read_text()
        )
        self.assertEqual(pinned["permissions"]["ask"][:3], runner_ask)
        shared = json.loads((ROOT / ".claude/settings.json").read_text())
        self.assertNotIn("Bash(git -*)", shared["permissions"]["ask"])

    def test_verify_checks_the_runner_worktree(self) -> None:
        wt = "/runner-wt/feat/1-x"
        self.assertEqual(self.run_tick(TEST_WORKTREE_OUT=wt).wait(timeout=30), 0)
        argv = json.loads(Path(str(self.calls) + ".verify-argv").read_text())
        self.assertEqual(argv[argv.index("--worktree") + 1], wt)

    def test_worktree_step_writes_the_runner_context(self) -> None:
        self.assertEqual(self.run_tick().wait(timeout=30), 0)
        (call,) = self.worktree_calls()
        self.assertEqual(
            call[call.index("--context-file") + 1],
            str(self.state / "claude.lock/context.json"),
        )

    def test_action_that_did_not_land_fails_the_tick_after_recording(self) -> None:
        # A denied push or merge exited 0 before; now the tick reports it.
        # The cooldown reads the PR; a failed read is no evidence: gate record.
        self.assertEqual(self.run_tick(TEST_VERIFY_EXIT="1").wait(timeout=30), 1)
        self.assertEqual(
            self.calls_made()[-3:],
            [
                ["verify", "--since"],
                [
                    "gh",
                    "api repos/phaabe/live.moafunk.de/pulls/1 --jq .state, .head.sha",
                ],
                ["gate", "record"],
            ],
        )
        self.assertFalse((self.state / "claude-cooldown.json").exists())
        self.assertIn("tick: finished exit=1", (self.state / "claude.log").read_text())
        self.assertFalse((self.state / "claude.lock").exists())

    def test_failed_pull_stops_the_tick(self) -> None:
        self.assertEqual(self.run_tick(TEST_GIT_EXIT="1").wait(timeout=30), 1)
        self.assertEqual(self.calls_made(), [["git", "pull -q --ff-only"]])
        self.assertFalse((self.state / "claude.lock").exists())

    def test_noise_check_runs_before_the_pull(self) -> None:
        self.assertEqual(self.run_tick(TEST_SELECT_EXIT="3").wait(timeout=30), 0)
        noise = Path(str(self.calls) + ".noise").read_text().splitlines()
        self.assertEqual(noise, ["0"])
        self.assertEqual(self.calls_made()[0], ["git", "pull -q --ff-only"])

    def test_unclean_checkout_stops_before_the_pull(self) -> None:
        self.assertEqual(self.run_tick(TEST_NOISE_EXIT="1").wait(timeout=30), 1)
        self.assertFalse(self.calls.exists())
        self.assertIn("tick: finished exit=1", (self.state / "claude.log").read_text())
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

    def test_relative_state_dir_is_resolved_before_changing_directory(self) -> None:
        runner = subprocess.Popen(
            ["/bin/bash", str(self.repo / "scripts/epic/claude-tick.sh")],
            env={**self.env, "EPIC_STATE_DIR": "relative state"},
            cwd=self.root,
        )
        self.assertEqual(runner.wait(timeout=30), 0)
        custom = self.root / "relative state"
        self.assertIn("tick: finished exit=0", (custom / "claude.log").read_text())
        self.assertFalse((custom / "claude.lock").exists())
        self.assertFalse((self.repo / "relative state").exists())
        # Codex review round 4: the gate helper runs after the cd.
        gate_dirs = set(Path(f"{self.calls}.env").read_text().split("\n")) - {""}
        self.assertEqual(
            {str(Path(d).resolve()) for d in gate_dirs}, {str(custom.resolve())}
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
        model = next(c for c in self.calls_made() if c[0] == "claude")
        config = model[1].split("--mcp-config ", 1)[1].split(" --", 1)[0]
        server = json.loads(config)["mcpServers"]["epic-gate"]
        self.assertEqual(server["env"]["EPIC_STATE_DIR"], str(home))
        self.assertEqual(
            server["env"]["EPIC_ACTION_FILE"], str(home / "claude.lock/action.json")
        )
        self.assertEqual(
            server["env"]["EPIC_CONTEXT_FILE"], str(home / "claude.lock/context.json")
        )
        self.assertEqual(
            server["env"]["EPIC_WORKTREE_DIR"],
            str(self.repo.resolve().parent / "live.moafunk.de-claude-2-wt"),
        )

    def test_registered_agent_obeys_the_shared_quota_wait(self) -> None:
        # The GraphQL quota belongs to the GitHub user, not to one agent.
        self.store_wait()
        env = {"EPIC_AGENT_ID": "claude-2"}
        self.assertEqual(self.run_tick(**env).wait(timeout=30), 0)
        self.assertFalse(self.calls.exists())  # no git, gh, selector or model
        log = (self.state / "agents/claude-2/claude.log").read_text()
        self.assertIn("retry at 2099-01-01", log)

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

    def tick_events(self, state: Path | None = None) -> list[dict[str, object]]:
        path = (state or self.state) / "claude-ticks.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()]

    def finish(self) -> tuple[object, ...]:
        start, finish = self.tick_events()
        self.assertEqual((start["event"], finish["event"]), ("start", "finish"))
        self.assertEqual(start["tick"], finish["tick"])
        return finish["exit"], finish["outcome"], finish["phase"]

    def test_events_name_each_exit_path(self) -> None:
        for env, expected in (
            ({}, (0, "ok", "record")),
            ({"TEST_VERIFY_EXIT": "1"}, (1, "error", "verify")),
            ({"TEST_SELECT_EXIT": "4"}, (75, "blocked", "quota")),
            ({"TEST_SELECT_EXIT": "3"}, (0, "ok", "select")),
            ({"TEST_SELECT_EXIT": "9"}, (9, "error", "select")),
            ({"TEST_GATE_EXIT": "4"}, (75, "blocked", "quota")),
            ({"TEST_GIT_EXIT": "1"}, (1, "error", "refresh")),
        ):
            with self.subTest(env=env):
                shutil.rmtree(self.state, ignore_errors=True)
                self.run_tick(**env).wait(timeout=30)
                self.assertEqual(self.finish(), expected)

    def test_finish_event_carries_the_selected_action(self) -> None:
        self.assertEqual(self.run_tick().wait(timeout=30), 0)
        finish = self.tick_events()[-1]
        self.assertEqual((finish["action"], finish["pr"]), ("fix", 1))
        self.assertIsNone(finish["tokens"])

    def session_arg(self) -> str:
        [args] = [c[1] for c in self.calls_made() if c[0] == "claude"]
        words = args.split()
        return words[words.index("--session-id") + 1]

    def test_finish_event_records_the_session_and_its_usage(self) -> None:
        output = '{"type":"result","structured_output":{"status":"completed"}}'
        code = self.run_tick(
            TEST_MODEL_TRANSCRIPT=str(TRANSCRIPT), TEST_MODEL_RESULT=output
        ).wait(timeout=30)
        self.assertEqual(code, 0)
        finish = self.tick_events()[-1]
        self.assertRegex(finish["session_id"], r"^[0-9a-f-]{36}$")
        self.assertEqual(finish["session_id"], self.session_arg())
        self.assertEqual(
            finish["usage"], {**TRANSCRIPT_USAGE, "complete": True, "reason": None}
        )
        self.assertEqual((finish["tokens"], finish["outcome"]), (None, "ok"))
        # Stdout, the log lines, the permission route and verify are unchanged.
        [args] = [c[1] for c in self.calls_made() if c[0] == "claude"]
        self.assertIn("--output-format json", args)
        self.assertIn("--permission-prompt-tool mcp__epic-gate__approve", args)
        log = (self.state / "claude.log").read_text()
        self.assertIn(f"{output}\ntick: model exit=0\n", log)
        self.assertIn("tick: fix with model=opus effort=high\n", log)
        self.assertIn("tick: finished exit=0\n", log)
        self.assertIn(["verify", "--since"], self.calls_made())
        self.assertIn(["gate", "record"], self.calls_made())

    def test_usage_is_kept_whatever_the_tick_result(self) -> None:
        transcript = {"TEST_MODEL_TRANSCRIPT": str(TRANSCRIPT)}
        for env, result, usage in (
            (
                {
                    **transcript,
                    "EPIC_TICK_TIMEOUT_SECONDS": "2",
                    "TEST_MODEL_SLEEP": "30",
                },
                (124, "timeout", "model"),
                (943, False, "interrupted"),
            ),
            (
                {**transcript, "TEST_VERIFY_EXIT": "1"},
                (1, "error", "verify"),
                (943, True, None),
            ),
            ({}, (0, "ok", "record"), (None, False, "no-transcript")),
        ):
            with self.subTest(env=env):
                shutil.rmtree(self.state, ignore_errors=True)
                self.calls.unlink(missing_ok=True)
                self.run_tick(**env).wait(timeout=40)
                self.assertEqual(self.finish(), result)
                finish = self.tick_events()[-1]
                self.assertEqual(
                    tuple(finish["usage"][k] for k in ("output", "complete", "reason")),
                    usage,
                )
                self.assertEqual(finish["session_id"], self.session_arg())

    def test_no_model_session_means_no_usage(self) -> None:
        for env in ({"TEST_SELECT_EXIT": "3"}, {"TEST_GATE_EXIT": "4"}):
            with self.subTest(env=env):
                shutil.rmtree(self.state, ignore_errors=True)
                self.run_tick(**env).wait(timeout=30)
                finish = self.tick_events()[-1]
                self.assertEqual((finish["session_id"], finish["usage"]), (None, None))

    def test_stopped_runner_keeps_the_partial_usage(self) -> None:
        runner = self.run_tick(
            TEST_MODEL_SLEEP="60", TEST_MODEL_TRANSCRIPT=str(TRANSCRIPT)
        )
        deadline = time.time() + 20
        while not self.model_pid.exists() and time.time() < deadline:
            time.sleep(0.1)
        time.sleep(0.3)  # the stub stores the transcript before it sleeps
        runner.send_signal(signal.SIGTERM)
        self.assertEqual(runner.wait(timeout=30), 143)
        self.assertEqual(self.finish(), (143, "killed", "model"))
        usage = self.tick_events()[-1]["usage"]
        self.assertEqual(
            (usage["output"], usage["complete"], usage["reason"]),
            (943, False, "interrupted"),
        )

    def test_term_writes_a_killed_event_before_the_lock_is_released(self) -> None:
        runner = self.run_tick(TEST_MODEL_SLEEP="60")
        deadline = time.time() + 20
        while not self.model_pid.exists() and time.time() < deadline:
            time.sleep(0.1)
        runner.send_signal(signal.SIGTERM)
        self.assertEqual(runner.wait(timeout=30), 143)
        self.assertEqual(self.finish(), (143, "killed", "model"))

    def test_failed_event_write_keeps_the_verify_time_boundary(self) -> None:
        # Codex review on PR 469: --since was empty when the start event failed.
        self.state.mkdir(parents=True)
        (self.state / "claude-ticks.jsonl").mkdir()  # not writable as a file
        self.assertEqual(self.run_tick().wait(timeout=30), 0)
        since = Path(f"{self.calls}.since").read_text()
        self.assertRegex(since, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertFalse((self.state / "claude.lock").exists())

    def test_registered_agent_writes_events_in_its_own_folder(self) -> None:
        self.assertEqual(self.run_tick(EPIC_AGENT_ID="claude-2").wait(timeout=30), 0)
        home = self.state / "agents/claude-2"
        self.assertEqual(self.tick_events(home)[-1]["outcome"], "ok")
        self.assertFalse((self.state / "claude-ticks.jsonl").exists())

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

    # Shared reader (EPIC_SHARED_READER=1): exit codes 5 and 6, --recheck.

    def shared(self, **env: str) -> subprocess.Popen[bytes]:
        return self.run_tick(EPIC_SHARED_READER="1", **env)

    def rechecks(self) -> list[object]:
        return [c[1] for c in self.calls_made() if c[0] == "recheck"]

    def test_reader_off_runs_no_recheck(self) -> None:
        self.assertEqual(self.run_tick().wait(timeout=30), 0)
        self.assertEqual(self.rechecks(), [])
        self.assertEqual(len(self.model_targets()), 1)

    def test_reader_on_rechecks_before_the_model(self) -> None:
        self.assertEqual(self.shared().wait(timeout=30), 0)
        kinds = [c[0] for c in self.calls_made()]
        self.assertEqual(kinds[kinds.index("gate") + 1], "recheck")
        self.assertEqual(kinds[kinds.index("recheck") + 1], "claude")

    def test_blocked_selection_ends_the_tick_blocked(self) -> None:
        self.assertEqual(self.shared(TEST_SELECT_EXIT="5").wait(timeout=30), 75)
        self.assertEqual([c[0] for c in self.calls_made()], ["git", "select"])
        self.assertEqual(self.finish(), (75, "blocked", "select"))

    def test_stale_candidate_is_skipped_for_the_next_one(self) -> None:
        second = {"action": "review", "reason": "t", "pr": 2, "sha": "b" * 40}
        candidates = "\n".join(json.dumps(a) for a in (ACTION, second))
        runner = self.shared(TEST_CANDIDATES=candidates, TEST_RECHECK_EXITS="6,0")
        self.assertEqual(runner.wait(timeout=30), 0)
        self.assertEqual(self.rechecks(), [1, 2])
        gates = [c for c in self.calls_made() if c[0] == "gate"]
        self.assertEqual(
            gates, [["gate", "check"], ["gate", "check"], ["gate", "record"]]
        )
        self.assertEqual(len(self.model_targets()), 1)

    def test_only_stale_candidates_start_no_model_and_keep_no_seen_state(self) -> None:
        self.assertEqual(self.shared(TEST_RECHECK_EXITS="6").wait(timeout=30), 0)
        self.assertEqual(self.model_targets(), [])
        self.assertNotIn(["gate", "record"], self.calls_made())
        self.assertFalse((self.state / "claude-gate-seen.json").exists())
        self.assertEqual(self.finish(), (0, "ok", "recheck"))

    def test_blocked_recheck_ends_the_tick_blocked(self) -> None:
        self.assertEqual(self.shared(TEST_RECHECK_EXITS="5").wait(timeout=30), 75)
        self.assertEqual(self.model_targets(), [])
        self.assertNotIn(["gate", "record"], self.calls_made())
        self.assertFalse((self.state / "claude-gate-seen.json").exists())
        self.assertEqual(self.finish(), (75, "blocked", "recheck"))
        self.assertFalse((self.state / "claude.lock").exists())

    def test_recheck_timeout_is_a_blocked_read(self) -> None:
        runner = self.shared(EPIC_RECHECK_TIMEOUT_SECONDS="1", TEST_RECHECK_SLEEP="20")
        self.assertEqual(runner.wait(timeout=40), 75)
        self.assertEqual(self.model_targets(), [])
        self.assertEqual(self.finish(), (75, "blocked", "recheck"))

    def test_recheck_quota_error_stops_the_tick(self) -> None:
        self.assertEqual(self.shared(TEST_RECHECK_EXITS="4").wait(timeout=30), 75)
        self.assertEqual(self.model_targets(), [])
        self.assertFalse((self.state / "claude-gate-seen.json").exists())
        self.assertEqual(self.finish(), (75, "blocked", "quota"))

    def test_bad_recheck_timeout_is_refused(self) -> None:
        runner = self.shared(EPIC_RECHECK_TIMEOUT_SECONDS="0")
        self.assertEqual(runner.wait(timeout=30), 2)
        self.assertFalse(self.calls.exists())

    def test_budgets_include_the_recheck_time(self) -> None:
        env = {"EPIC_AGENT_ID": "claude-2", "EPIC_RECHECK_TIMEOUT_SECONDS": "30"}
        self.assertEqual(self.shared(**env).wait(timeout=30), 0)
        agent = json.loads((self.state / "agents/claude-2/agent.json").read_text())
        self.assertEqual(agent["budget_seconds"], 120 + 30 + 1800 + 10)

    def test_model_and_gate_get_the_write_check_inputs(self) -> None:
        self.assertEqual(self.shared().wait(timeout=30), 0)
        action_file, trusted = (
            Path(str(self.calls) + ".modelenv").read_text().splitlines()
        )
        self.assertEqual(action_file, str(self.state / "claude.lock/action.json"))
        self.assertEqual(trusted, str(self.repo.resolve()))
        args = self.model_targets()[0]
        config = json.loads(args.split("--mcp-config ", 1)[1].split(" --", 1)[0])
        env = config["mcpServers"]["epic-gate"]["env"]
        self.assertEqual(env["EPIC_TRUSTED_ROOT"], str(self.repo.resolve()))
        self.assertEqual(env["EPIC_SHARED_READER"], "1")
        self.assertIn("HOME", env)

    def worktree_calls(self) -> list[list[str]]:
        path = Path(str(self.calls) + ".worktree")
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_worktree_goes_to_the_model(self) -> None:
        wt = "/runner-wt/feat/1-x"
        self.assertEqual(self.run_tick(TEST_WORKTREE_OUT=wt).wait(timeout=30), 0)
        args = self.model_targets()[0]
        self.assertTrue(args.endswith(f"--add-dir {wt}"))
        self.assertEqual(Path(str(self.calls) + ".worktree-env").read_text(), wt)
        prompt = Path(str(self.calls) + ".prompt").read_text()
        self.assertIn(f"Runner worktree (edit only here): {wt}", prompt)
        (call,) = self.worktree_calls()
        default = self.repo.resolve().parent / "live.moafunk.de-claude-wt"
        self.assertEqual(call[call.index("--dir") + 1], str(default))
        self.assertEqual(call[call.index("--repo") + 1], str(self.repo.resolve()))

    def test_action_without_worktree_adds_no_dir(self) -> None:
        self.assertEqual(self.run_tick().wait(timeout=30), 0)
        self.assertNotIn("--add-dir", self.model_targets()[0])
        self.assertEqual(Path(str(self.calls) + ".worktree-env").read_text(), "")
        self.assertNotIn(
            "Runner worktree (edit only here)",
            Path(str(self.calls) + ".prompt").read_text(),
        )

    def test_worktree_stop_starts_no_model_and_tries_the_next_candidate(self) -> None:
        second = {"action": "review", "reason": "t", "pr": 2, "sha": "b" * 40}
        candidates = "\n".join(json.dumps(a) for a in (ACTION, second))
        runner = self.run_tick(
            TEST_CANDIDATES=candidates, TEST_WORKTREE_EXIT="3", TEST_WORKTREE_PR="1"
        )
        self.assertEqual(runner.wait(timeout=30), 0)
        self.assertEqual(len(self.model_targets()), 1)
        self.assertIn('"pr": 2', Path(str(self.calls) + ".prompt").read_text())
        self.assertIn(
            "fix stopped before the model", (self.state / "claude.log").read_text()
        )

    def test_only_stopped_candidates_start_no_model_and_record_nothing(self) -> None:
        self.assertEqual(self.run_tick(TEST_WORKTREE_EXIT="3").wait(timeout=30), 0)
        self.assertEqual(self.model_targets(), [])
        self.assertNotIn(["gate", "record"], self.calls_made())
        self.assertFalse((self.state / "claude-gate-seen.json").exists())
        self.assertFalse((self.state / "claude.lock").exists())

    def test_worktree_quota_error_stops_the_tick(self) -> None:
        self.assertEqual(self.run_tick(TEST_WORKTREE_EXIT="4").wait(timeout=30), 75)
        self.assertEqual(self.model_targets(), [])
        self.assertFalse((self.state / "claude-gate-seen.json").exists())
        self.assertEqual(self.finish(), (75, "blocked", "quota"))

    def test_worktree_error_stops_the_tick(self) -> None:
        self.assertEqual(self.run_tick(TEST_WORKTREE_EXIT="1").wait(timeout=30), 1)
        self.assertEqual(self.model_targets(), [])
        self.assertFalse((self.state / "claude-gate-seen.json").exists())

    def test_each_runner_has_its_own_worktree_dir(self) -> None:
        runner = self.run_tick(EPIC_AGENT_ID="claude-2")
        self.assertEqual(runner.wait(timeout=30), 0)
        (call,) = self.worktree_calls()
        expected = self.repo.resolve().parent / "live.moafunk.de-claude-2-wt"
        self.assertEqual(call[call.index("--dir") + 1], str(expected))

    def test_relative_worktree_dir_is_refused(self) -> None:
        runner = self.run_tick(EPIC_WORKTREE_DIR="wt")
        self.assertEqual(runner.wait(timeout=30), 2)
        self.assertFalse(self.calls.exists())

    def test_branch_held_by_a_human_checkout_needs_handoff(self) -> None:
        """Real Git and the real helper: no model and no foreign change until
        the human releases the branch; the repeat stays quiet."""
        real_git = shutil.which("git")
        assert real_git
        branch = "feat/1-held"
        root = self.root.resolve()

        def git(cwd: Path, *args: str) -> str:
            return subprocess.run(
                [real_git, *args], cwd=cwd, check=True, capture_output=True, text=True
            ).stdout

        origin = root / "phaabe" / "live.moafunk.de.git"
        origin.parent.mkdir()
        git(root, "init", "-q", "--bare", str(origin))
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.name", "t")
        git(self.repo, "config", "user.email", "t@t")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        git(self.repo, "remote", "add", "origin", str(origin))
        git(self.repo, "push", "-q", "origin", "HEAD:refs/heads/dev/312-interim")
        git(self.repo, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
        git(self.repo, "fetch", "-q", "origin")
        human = root / "human"
        git(self.repo, "worktree", "add", "-q", str(human), branch)
        (human / "draft.txt").write_text("human work\n")

        def human_state() -> tuple[str, str, str]:
            return (
                git(human, "rev-parse", "HEAD"),
                git(human, "status", "--porcelain"),
                (human / "draft.txt").read_text(),
            )

        before = human_state()
        # The real helper, importing the real modules instead of the stubs.
        epic = ROOT / "scripts/epic"
        (self.repo / "scripts/epic/runner_worktree.py").write_text(
            "import runpy, sys\n"
            f"sys.path.insert(0, {str(epic)!r})\n"
            f"runpy.run_path({str(epic / 'runner_worktree.py')!r}, run_name='__main__')\n"
        )
        # Git is real, except the runner's own pull.
        (self.root / "bin/git").write_text(
            "#!/bin/bash\n"
            'if [[ "$1" == pull ]]; then\n'
            '    printf \'["git", "%s"]\\n\' "$*" >> "$TEST_CALLS"\n'
            "    exit 0\n"
            "fi\n"
            f'exec {real_git} "$@"\n'
        )
        pr = {
            "state": "open",
            "head": {"ref": branch, "repo": {"full_name": "phaabe/live.moafunk.de"}},
            "base": {"ref": "dev/312-interim"},
            "body": "Executor: Claude\nReviewer: Codex\n",
        }
        wt = root / "claude-wt"
        env = {"TEST_GH_OUT": json.dumps(pr), "EPIC_WORKTREE_DIR": str(wt)}
        log = self.state / "claude.log"
        handoff = f"handoff needed: {branch} in {human}"

        for _ in range(2):
            self.assertEqual(self.run_tick(**env).wait(timeout=60), 0)
            self.assertEqual(self.model_targets(), [])
            self.assertEqual(human_state(), before)
            self.assertFalse((wt / branch).exists())
        self.assertEqual(log.read_text().count(handoff), 1)
        self.assertIn(
            "worktree: target 1 unchanged since the last report", log.read_text()
        )
        self.assertNotIn(["gate", "record"], self.calls_made())

        # Manual handoff: the human releases the branch; the next tick runs.
        (human / "draft.txt").unlink()
        git(self.repo, "worktree", "remove", str(human))
        self.assertEqual(self.run_tick(**env).wait(timeout=60), 0)
        (args,) = self.model_targets()
        self.assertTrue(args.endswith(f"--add-dir {wt / branch}"))
        self.assertEqual(git(wt / branch, "branch", "--show-current").strip(), branch)


if __name__ == "__main__":
    unittest.main()
