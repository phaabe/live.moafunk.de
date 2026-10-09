"""Run the real claude-tick.sh with isolated state and fake git/selector/gate/model."""

from __future__ import annotations

import isolated_env  # noqa: F401  (first: hides live runner state)

import json
import os
import re
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


def activity_of(state: Path) -> list[tuple[object, ...]]:
    """(event, activity, target, reason_code, scope, retry_at) per record."""
    path = state / "claude-activity.jsonl"
    if not path.exists():
        return []
    keys = ("event", "activity", "target", "reason_code", "scope", "retry_at")
    return [
        tuple(json.loads(line)[k] for k in keys)
        for line in path.read_text().splitlines()
    ]


# Python line for a stub: the other runner stores a quota wait now.
WAIT_WRITE = (
    "open(os.path.join(os.environ['EPIC_STATE_DIR'], 'github-quota-wait.json'), 'w')"
    '.write(\'{"retry_at": "2099-01-01T00:00:00Z"}\')'
)

# The runner resolves the shared reader with `github_state.py resolve`. The
# fixture runs the real module from this repository (its next_action.py is a
# stub); TEST_READER_DEFAULT=1 stands for the planned default-on.
STATE_PROXY = (
    "import importlib.util, os, sys\n"
    f"real = {str(ROOT / 'scripts/epic')!r}\n"
    "sys.path.insert(0, real)\n"
    "spec = importlib.util.spec_from_file_location('github_state', real + '/github_state.py')\n"
    "state = importlib.util.module_from_spec(spec)\n"
    "sys.modules['github_state'] = state\n"
    "spec.loader.exec_module(state)\n"
    "if os.environ.get('TEST_READER_DEFAULT') == '1':\n"
    "    state.DEFAULT_ENABLED = True\n"
    "sys.exit(state.main(sys.argv[1:]))\n"
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
            "scripts/epic/activity.py",
            "scripts/epic/target_lock.py",
            "scripts/epic/tick_cooldown.py",
            "scripts/epic/claude_usage.py",
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
        (self.repo / "scripts/epic/github_state.py").write_text(STATE_PROXY)
        # --recheck: exits from TEST_RECHECK_EXITS in order (the last repeats).
        (self.repo / "scripts/epic/next_action.py").write_text(
            "import json, os, sys, time\n"
            "if '--recheck' in sys.argv:\n"
            "    calls = os.environ['TEST_CALLS']\n"
            "    pr = json.load(open(sys.argv[-1])).get('pr')\n"
            "    with open(calls, 'a') as f:\n"
            "        f.write(json.dumps(['recheck', pr]) + '\\n')\n"
            "    with open(calls + '.recheck-env', 'a') as f:\n"
            "        f.write(json.dumps([os.environ.get(k, '<unset>') for k in "
            "('EPIC_SHARED_READER', 'EPIC_RECHECK_TIMEOUT_SECONDS')]) + '\\n')\n"
            "    time.sleep(float(os.environ.get('TEST_RECHECK_SLEEP', '0')))\n"
            "    codes = os.environ.get('TEST_RECHECK_EXITS', '0').split(',')\n"
            "    n = sum(1 for line in open(calls) if line.startswith('[\"recheck\"'))\n"
            "    sys.exit(int(codes[min(n, len(codes)) - 1]))\n"
            "else:\n"
            "    with open(os.environ['TEST_CALLS'], 'a') as f:\n"
            "        f.write(json.dumps(['select']) + '\\n')\n"
            "    with open(os.environ['TEST_CALLS'] + '.select-env', 'a') as f:\n"
            "        f.write(os.environ.get('EPIC_SHARED_READER', '<unset>') + '\\n')\n"
            "    if os.environ.get('TEST_SELECT_WAIT'):\n"
            f"        {WAIT_WRITE}\n"
            "    code = int(os.environ.get('TEST_SELECT_EXIT', '0'))\n"
            f"    out = os.environ.get('TEST_CANDIDATES') or {json.dumps(json.dumps(ACTION))}\n"
            "    print(out) if code == 0 else sys.exit(code)\n"
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
        # Close step: its own call file, so the call indexes stay. It records
        # whether the selector already ran.
        (self.repo / "scripts/epic/close_merged.py").write_text(
            "import os, sys\n"
            "calls = os.environ['TEST_CALLS']\n"
            "ran = os.path.exists(calls) and '[\"select\"]' in open(calls).read()\n"
            "with open(calls + '.close', 'a') as f:\n"
            "    f.write(f'after-select={ran}\\n')\n"
            "sys.exit(int(os.environ.get('TEST_CLOSE_EXIT', '0')))\n"
        )
        # Worktree step: its own call file, so the call indexes above stay.
        # A stop writes its evidence: kind from the exit (3 repeat, 7 refusal,
        # 75 handoff), cause from TEST_WORKTREE_CAUSE.
        (self.repo / "scripts/epic/runner_worktree.py").write_text(
            "import json, os, sys\n"
            "with open(os.environ['TEST_CALLS'] + '.worktree', 'a') as f:\n"
            "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "if os.environ.get('TEST_WORKTREE_OUT'):\n"
            "    print(os.environ['TEST_WORKTREE_OUT'])\n"
            "code = int(os.environ.get('TEST_WORKTREE_EXIT', '0'))\n"
            "only = os.environ.get('TEST_WORKTREE_PR')\n"
            "action = json.load(open(sys.argv[sys.argv.index('--action-file') + 1]))\n"
            "code = 0 if only and str(action.get('pr')) != only else code\n"
            "kind = {3: 'repeat', 7: 'refusal', 75: 'handoff'}.get(code)\n"
            "if kind and '--evidence-file' in sys.argv:\n"
            "    cause = os.environ.get('TEST_WORKTREE_CAUSE', 'handoff')\n"
            "    evidence = sys.argv[sys.argv.index('--evidence-file') + 1]\n"
            "    open(evidence, 'w').write(json.dumps({'kind': kind, 'cause': cause}))\n"
            "sys.exit(code)\n"
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
            'python3 -I -c \'import json, sys; print(json.dumps(["claude", " ".join(sys.argv[1:])]))\' "$@" >> "$TEST_CALLS"\n'
            'echo $$ > "$TEST_MODEL_PID"\n'
            'printf \'%s\\n%s\\n\' "${EPIC_ACTION_FILE:-}" "${EPIC_TRUSTED_ROOT:-}" > "$TEST_CALLS.modelenv"\n'
            'printf \'%s\' "${EPIC_WORKTREE:-}" > "$TEST_CALLS.worktree-env"\n'
            'printf \'%s\' "${EPIC_SHARED_READER-<unset>}" > "$TEST_CALLS.reader-env"\n'
            'printf \'%s\' "${GIT_EDITOR:-}" > "$TEST_CALLS.editor"\n'
            'printf \'%s\' "${EPIC_BODY_DIR:-}" > "$TEST_CALLS.body-dir"\n'
            'printf \'%s\' "${EPIC_BODY_DIR_ID:-}" > "$TEST_CALLS.body-id"\n'
            'if [[ -n "${EPIC_BODY_DIR:-}" ]]; then\n'
            "    python3 -I -c 'import os, sys; print(os.stat(sys.argv[1]).st_mode & 0o777)' "
            '"$EPIC_BODY_DIR" > "$TEST_CALLS.body-mode"\n'
            "    python3 -I -c 'import os, sys; s = os.stat(sys.argv[1]); "
            'print(f"{s.st_dev}:{s.st_ino}")\' '
            '"$EPIC_BODY_DIR" > "$TEST_CALLS.body-inode"\n'
            "fi\n"
            'cat > "$TEST_CALLS.prompt"\n'
            # Usage locks (fd 18, 19) that reached the model; none should.
            "for fd in 18 19; do if [[ -e /dev/fd/$fd ]]; then printf '%s ' \"$fd\"; fi; done "
            '> "$TEST_CALLS.usage-fds"\n'
            # The session's runtime environment: prefix, updater, python3.
            "printf '%s\\n%s\\n%s\\n' \"${CLAUDE_CODE_SHELL_PREFIX:-}\" "
            '"${DISABLE_AUTOUPDATER:-}" '
            # The binary `python3` really runs, resolved while the session runs.
            "\"$(python3 -I -c 'import os, sys; print(os.path.realpath(sys.executable))')\" "
            '> "$TEST_CALLS.session-env"\n'
            # A tool child started through the prefix, left running.
            'if [[ -n "${TEST_PREFIX_CHILD:-}" ]]; then\n'
            '    "$CLAUDE_CODE_SHELL_PREFIX" "sleep $TEST_PREFIX_CHILD" > /dev/null 2>&1 &\n'
            '    echo $! > "$TEST_CALLS.child"\n'
            "fi\n"
            + TRANSCRIPT_STUB
            # A terminal CLI result for this session (usage_result()), exit 1.
            + 'if [[ -n "${TEST_MODEL_LIMIT:-}" ]]; then\n'
            + "    printf '%s' \"${TEST_MODEL_LIMIT//@SESSION@/$session}\"\n"
            + "    exit 1\n"
            + "fi\n"
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
    def test_close_step_runs_before_the_selector(self) -> None:
        self.assertEqual(self.run_tick().wait(timeout=30), 0)
        close = Path(str(self.calls) + ".close").read_text()
        self.assertEqual(close, "after-select=False\n")
        self.assertEqual(len(self.model_targets()), 1)
        self.assertNotIn("close step failed", (self.state / "claude.log").read_text())

    def test_failed_close_step_never_stops_the_tick(self) -> None:
        for code, logged in (("1", True), ("3", False)):
            with self.subTest(code=code):
                self.calls.unlink(missing_ok=True)
                log = self.state / "claude.log"
                log.unlink(missing_ok=True)
                tick = self.run_tick(TEST_CLOSE_EXIT=code)
                self.assertEqual(tick.wait(timeout=30), 0)
                self.assertEqual(len(self.model_targets()), 1)
                self.assertEqual(
                    f"close step failed with exit {code}" in log.read_text(), logged
                )

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
        # The model holds the target long enough for a slow second runner.
        first = self.run_tick(TEST_MODEL_SLEEP="10")
        # Up to 30 s: under a parallel suite the first model can start late.
        for _ in range(600):
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
        # Off by default, and the gate still gets the explicit value.
        self.assertEqual(env["EPIC_SHARED_READER"], "0")

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
        # Codex review on PR 699: the hold has its record, before any tick.
        self.assertEqual(
            activity_of(self.state),
            [
                (
                    "wait",
                    "waiting",
                    None,
                    "github_quota",
                    "agent",
                    "2099-01-01T00:00:00Z",
                )
            ],
        )

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
        # Codex review on PR 699: the stored expiry reaches the record.
        self.assertEqual(
            activity_of(self.state),
            [
                (
                    "wait",
                    "waiting",
                    None,
                    "github_quota",
                    "agent",
                    "2099-01-01T00:00:00Z",
                )
            ],
        )

    def test_wait_stored_during_the_gate_check_starts_no_model(self) -> None:
        self.assertEqual(self.run_tick(TEST_GATE_WAIT="1").wait(timeout=30), 0)
        self.assertEqual(
            self.calls_made(),
            [["git", "pull -q --ff-only"], ["select"], ["gate", "check"]],
        )
        # Codex review on PR 699: the last check before the model has a record.
        self.assertEqual(
            activity_of(self.state),
            [
                (
                    "wait",
                    "waiting",
                    "pr:1",
                    "github_quota",
                    "agent",
                    "2099-01-01T00:00:00Z",
                )
            ],
        )

    def test_cooling_target_records_its_cooldown_end(self) -> None:
        # Codex review on PR 699: a backoff wait names its stored end.
        self.state.mkdir(parents=True)
        key = f"claude:fix:pr:1:{ACTION['sha']}"
        until = 4102444800.0  # 2100-01-01T00:00:00Z
        (self.state / "claude-cooldown.json").write_text(
            json.dumps({key: {"at": 1.0, "until": until, "reason": "blocked"}})
        )
        self.assertEqual(self.run_tick().wait(timeout=30), 0)
        self.assertNotIn("claude", [c[0] for c in self.calls_made()])
        self.assertEqual(
            activity_of(self.state),
            [
                (
                    "wait",
                    "waiting",
                    "pr:1",
                    "retry_backoff",
                    "target",
                    "2100-01-01T00:00:00Z",
                )
            ],
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
        self.assertEqual(agent["budget_seconds"], 1990)
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

    def test_activity_file_records_the_model_boundaries(self) -> None:
        # https://github.com/phaabe/live.moafunk.de/issues/694
        path = self.state / "claude-activity.jsonl"
        for env, expected in (
            ({}, [("model-start", "code"), ("model-end", "code")]),
            # A selected action alone is not model work: no record.
            ({"TEST_SELECT_EXIT": "3"}, None),
        ):
            with self.subTest(env=env):
                shutil.rmtree(self.state, ignore_errors=True)
                self.run_tick(**env).wait(timeout=30)
                if expected is None:
                    self.assertFalse(path.exists())
                    continue
                records = [json.loads(x) for x in path.read_text().splitlines()]
                self.assertEqual(
                    [(r["event"], r["activity"]) for r in records], expected
                )
                tick = self.tick_events()[-1]["tick"]
                self.assertTrue(all(r["tick"] == tick for r in records))
                self.assertTrue(all(r["target"] == "pr:1" for r in records))
                # The tick ledger's file holds only its own events.
                self.assertEqual(
                    [e["event"] for e in self.tick_events()], ["start", "finish"]
                )

    def activity_records(self) -> list[dict[str, object]]:
        path = self.state / "claude-activity.jsonl"
        return [json.loads(x) for x in path.read_text().splitlines()]

    def test_quota_stop_records_an_agent_wait(self) -> None:
        # https://github.com/phaabe/live.moafunk.de/issues/694
        self.assertEqual(self.run_tick(TEST_SELECT_EXIT="4").wait(timeout=30), 75)
        self.assertEqual(
            [
                (r["event"], r["activity"], r["reason_code"], r["scope"])
                for r in self.activity_records()
            ],
            [("wait", "waiting", "github_quota", "agent")],
        )

    def test_locked_target_records_a_target_wait(self) -> None:
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
        self.assertEqual(
            [
                (r["event"], r["activity"], r["target"], r["reason_code"], r["scope"])
                for r in self.activity_records()
            ],
            [
                ("wait", "waiting", "pr:1", "target_lock", "target"),
                ("model-start", "review", "pr:2", None, None),
                ("model-end", "review", "pr:2", None, None),
            ],
        )

    def test_finish_event_measures_each_step(self) -> None:
        # https://github.com/phaabe/live.moafunk.de/issues/655
        for env, steps in (
            (
                {},
                ["refresh", "closing", "selection", "model", "validation", "cleanup"],
            ),
            ({"TEST_SELECT_EXIT": "3"}, ["refresh", "closing", "selection", "cleanup"]),
        ):
            with self.subTest(env=env):
                shutil.rmtree(self.state, ignore_errors=True)
                self.run_tick(**env).wait(timeout=30)
                durations = self.tick_events()[-1]["durations"]
                self.assertIsInstance(durations, dict)
                self.assertEqual(list(durations), steps)
                self.assertTrue(all(v >= 0 for v in durations.values()))
        # The marks file goes with the lock dir.
        self.assertFalse((self.state / "claude.lock").exists())

    def test_cleanup_duration_includes_the_teardown(self) -> None:
        # https://github.com/phaabe/live.moafunk.de/pull/660: a slow removal of
        # the tick files counts in `cleanup`, not after the finish event.
        slow_bin = self.root / "slow-bin"
        slow_bin.mkdir()
        rm = slow_bin / "rm"
        rm.write_text(
            "#!/bin/bash\n"
            'case "$*" in *claude.lock/prompt.txt*) sleep 1 ;; esac\n'
            'exec /bin/rm "$@"\n'
        )
        rm.chmod(0o755)
        path = f"{slow_bin}:{self.env['PATH']}"
        self.assertEqual(self.run_tick(PATH=path).wait(timeout=30), 0)
        durations = self.tick_events()[-1]["durations"]
        self.assertGreaterEqual(durations["cleanup"], 1.0)
        self.assertFalse((self.state / "claude.lock").exists())

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
                    # Long enough for the stub to store its transcript before
                    # the timeout, also when the full suite loads the machine.
                    "EPIC_TICK_TIMEOUT_SECONDS": "10",
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
        self.assertEqual(agent["budget_seconds"], 60 + 120 + 30 + 1800 + 10)

    # One resolver (github_state.enabled()), resolved once, explicit to children.

    def default_on(self) -> None:
        """The planned default, only in this fixture (STATE_PROXY)."""
        self.env["TEST_READER_DEFAULT"] = "1"

    def child_settings(self) -> dict[str, object]:
        """The setting each child saw: selector, recheck, model and gate."""
        args = self.model_targets()[0] if self.model_targets() else ""
        gate = None
        if args:
            config = json.loads(args.split("--mcp-config ", 1)[1].split(" --", 1)[0])
            gate = config["mcpServers"]["epic-gate"]["env"].get("EPIC_SHARED_READER")
        model = Path(str(self.calls) + ".reader-env")
        recheck = Path(str(self.calls) + ".recheck-env")
        return {
            "select": Path(str(self.calls) + ".select-env").read_text().split(),
            "recheck": [json.loads(line) for line in recheck.read_text().splitlines()]
            if recheck.exists()
            else [],
            "model": model.read_text() if model.exists() else None,
            "gate": gate,
        }

    def test_unset_empty_and_zero_export_an_explicit_zero(self) -> None:
        for value in (None, "", "0"):
            with self.subTest(value=value):
                for path in self.root.glob("calls.jsonl*"):
                    path.unlink()
                env = {} if value is None else {"EPIC_SHARED_READER": value}
                self.assertEqual(self.run_tick(**env).wait(timeout=30), 0)
                self.assertEqual(
                    self.child_settings(),
                    {"select": ["0"], "recheck": [], "model": "0", "gate": "0"},
                )
        log = (self.state / "claude.log").read_text()
        self.assertEqual(log.count("tick: shared reader=0 recheck=0s\n"), 3)
        self.assertNotIn("fresh check passed", log)

    def test_explicit_one_reaches_every_child_with_the_budget(self) -> None:
        self.assertEqual(self.shared().wait(timeout=30), 0)
        self.assertEqual(
            self.child_settings(),
            {"select": ["1"], "recheck": [["1", "60"]], "model": "1", "gate": "1"},
        )
        # Evidence: the effective setting and the fresh check in the log.
        log = (self.state / "claude.log").read_text()
        self.assertIn("tick: shared reader=1 recheck=60s\n", log)
        self.assertIn("tick: fix fresh check passed\n", log)

    def test_non_default_recheck_budget_reaches_the_recheck(self) -> None:
        self.assertEqual(
            self.shared(EPIC_RECHECK_TIMEOUT_SECONDS="25").wait(timeout=30), 0
        )
        self.assertEqual(self.child_settings()["recheck"], [["1", "25"]])

    def test_invalid_setting_stops_before_selection(self) -> None:
        for value in ("yes", "2", " 1"):
            with self.subTest(value=value):
                runner = self.run_tick(EPIC_SHARED_READER=value)
                self.assertEqual(runner.wait(timeout=30), 2)
                self.assertFalse(self.calls.exists())
                self.assertIn(
                    "bad shared-reader setting", (self.state / "claude.log").read_text()
                )

    def test_default_on_rechecks_before_the_model(self) -> None:
        self.default_on()
        for value in (None, ""):
            with self.subTest(value=value):
                for path in self.root.glob("calls.jsonl*"):
                    path.unlink()
                env = {} if value is None else {"EPIC_SHARED_READER": value}
                self.assertEqual(self.run_tick(**env).wait(timeout=30), 0)
                kinds = [c[0] for c in self.calls_made()]
                self.assertEqual(kinds[kinds.index("recheck") + 1], "claude")
                self.assertEqual(
                    self.child_settings(),
                    {
                        "select": ["1"],
                        "recheck": [["1", "60"]],
                        "model": "1",
                        "gate": "1",
                    },
                )

    def test_default_on_stale_or_blocked_check_starts_no_model(self) -> None:
        self.default_on()
        for exits, code in (("6", 0), ("5", 75)):
            with self.subTest(exits=exits):
                for path in self.root.glob("calls.jsonl*"):
                    path.unlink()
                runner = self.run_tick(TEST_RECHECK_EXITS=exits)
                self.assertEqual(runner.wait(timeout=30), code)
                self.assertEqual(self.model_targets(), [])
                self.assertNotIn(["gate", "record"], self.calls_made())
                self.assertFalse((self.state / "claude-gate-seen.json").exists())
                self.assertFalse((self.state / "claude-cooldown.json").exists())

    def test_default_on_explicit_zero_runs_no_recheck(self) -> None:
        self.default_on()
        self.assertEqual(self.run_tick(EPIC_SHARED_READER="0").wait(timeout=30), 0)
        self.assertEqual(
            self.child_settings(),
            {"select": ["0"], "recheck": [], "model": "0", "gate": "0"},
        )

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

    def test_prompt_names_the_trusted_status_helper(self) -> None:
        # write_checks.py accepts only the helper of the runner's code root.
        self.assertEqual(self.run_tick().wait(timeout=30), 0)
        prompt = Path(str(self.calls) + ".prompt").read_text()
        line = re.search(r"^Status helper \(.*\): python3 (\S+)$", prompt, re.M)
        assert line, prompt
        self.assertEqual(
            Path(line.group(1)).resolve(),
            (self.repo / "scripts/epic/set_status.py").resolve(),
        )

    def test_action_without_worktree_adds_no_dir(self) -> None:
        self.assertEqual(self.run_tick().wait(timeout=30), 0)
        self.assertNotIn("--add-dir", self.model_targets()[0])
        self.assertEqual(Path(str(self.calls) + ".worktree-env").read_text(), "")
        self.assertNotIn(
            "Runner worktree (edit only here)",
            Path(str(self.calls) + ".prompt").read_text(),
        )

    def test_worktree_stop_starts_no_model_and_tries_the_next_candidate(self) -> None:
        # A later candidate that runs reports its own result.
        second = {"action": "review", "reason": "t", "pr": 2, "sha": "b" * 40}
        candidates = "\n".join(json.dumps(a) for a in (ACTION, second))
        for code in ("3", "7", "75"):
            with self.subTest(code=code):
                shutil.rmtree(self.state, ignore_errors=True)
                self.calls.unlink(missing_ok=True)
                runner = self.run_tick(
                    TEST_CANDIDATES=candidates,
                    TEST_WORKTREE_EXIT=code,
                    TEST_WORKTREE_PR="1",
                )
                self.assertEqual(runner.wait(timeout=30), 0)
                self.assertEqual(len(self.model_targets()), 1)
                self.assertIn('"pr": 2', Path(str(self.calls) + ".prompt").read_text())
                log = (self.state / "claude.log").read_text()
                self.assertIn("fix stopped before the model", log)
                self.assertNotIn("preparation blocked", log)
                self.assertEqual(self.finish(), (0, "ok", "record"))

    def test_only_stopped_candidates_start_no_model_and_record_nothing(self) -> None:
        self.assertEqual(self.run_tick(TEST_WORKTREE_EXIT="3").wait(timeout=30), 75)
        self.assertEqual(self.model_targets(), [])
        self.assertNotIn(["gate", "record"], self.calls_made())
        self.assertFalse((self.state / "claude-gate-seen.json").exists())
        self.assertFalse((self.state / "claude.lock").exists())

    def test_all_candidates_stopped_by_preparation_end_blocked(self) -> None:
        # https://github.com/phaabe/live.moafunk.de/issues/690
        second = {"action": "fix-checks", "reason": "t", "pr": 2, "sha": "b" * 40}
        candidates = "\n".join(json.dumps(a) for a in (ACTION, second))
        for code, cause, kind in (
            ("75", "handoff", "handoff"),
            ("7", "refusal", "refusal"),
            ("3", "handoff", "repeat"),
        ):
            with self.subTest(code=code):
                shutil.rmtree(self.state, ignore_errors=True)
                self.calls.unlink(missing_ok=True)
                runner = self.run_tick(
                    TEST_CANDIDATES=candidates,
                    TEST_WORKTREE_EXIT=code,
                    TEST_WORKTREE_CAUSE=cause,
                )
                self.assertEqual(runner.wait(timeout=30), 75)
                self.assertEqual(self.model_targets(), [])
                self.assertEqual(self.finish(), (75, "blocked", "gate"))
                log = (self.state / "claude.log").read_text()
                self.assertIn(f"fix stopped before the model ({kind}, {cause})", log)
                self.assertIn(
                    "tick: preparation blocked: 2 candidate(s), "
                    f"cause: {cause}; no model",
                    log,
                )
                self.assertNotIn(["gate", "record"], self.calls_made())
                self.assertFalse((self.state / "claude-gate-seen.json").exists())
                self.assertFalse((self.state / "claude.lock").exists())
                # Preparation never counts as a model attempt or a cooldown.
                self.assertFalse((self.state / "claude-cooldowns.json").exists())
                waits = [a for a in activity_of(self.state) if a[0] == "wait"]
                self.assertEqual(len(waits), 2)

    def test_mixed_causes_are_named_once_each(self) -> None:
        second = {"action": "fix-checks", "reason": "t", "pr": 2, "sha": "b" * 40}
        third = {"action": "fix-checks", "reason": "t", "pr": 3, "sha": "c" * 40}
        candidates = "\n".join(json.dumps(a) for a in (ACTION, second, third))
        # A stub that refuses PR 2 and hands off the others.
        stub = self.repo / "scripts/epic/runner_worktree.py"
        stub.write_text(
            stub.read_text().replace(
                "cause = os.environ.get('TEST_WORKTREE_CAUSE', 'handoff')",
                "cause = 'refusal' if action.get('pr') == 2 else 'handoff'",
            )
        )
        self.assertEqual(
            self.run_tick(TEST_CANDIDATES=candidates, TEST_WORKTREE_EXIT="3").wait(
                timeout=30
            ),
            75,
        )
        self.assertIn(
            "tick: preparation blocked: 3 candidate(s), cause: handoff refusal;",
            (self.state / "claude.log").read_text(),
        )

    def test_preparation_cooldown_stays_blocked_and_empty_queue_stays_ok(self) -> None:
        # The first tick reports the refusal, later ticks skip it (cooldown or
        # repeat): the outcome never switches to ok in between.
        for code in ("7", "3", "3"):
            self.run_tick(TEST_WORKTREE_EXIT=code, TEST_WORKTREE_CAUSE="refusal").wait(
                timeout=30
            )
        idle = json.dumps({"action": "idle", "reason": "nothing to do"})
        self.assertEqual(self.run_tick(TEST_CANDIDATES=idle).wait(timeout=30), 0)
        finishes = [e for e in self.tick_events() if e["event"] == "finish"]
        self.assertEqual(
            [(e["exit"], e["outcome"]) for e in finishes],
            [(75, "blocked")] * 3 + [(0, "ok")],
        )

    def test_blocked_ticks_keep_the_failure_streak_in_both_readers(self) -> None:
        # Errors separated by blocked ticks keep the streak; ok resets it.
        # The real event ledger and the real runner log, read by ticks.py.
        import monitor
        import ticks

        for env in (
            {"TEST_VERIFY_EXIT": "1"},
            {"TEST_WORKTREE_EXIT": "75"},
            {"TEST_VERIFY_EXIT": "1"},
            {"TEST_WORKTREE_EXIT": "3"},
        ):
            self.run_tick(**env).wait(timeout=30)

        def streaks() -> tuple[float, float]:
            now = time.time() + 5
            runtime = self.root / "runtime"
            shutil.rmtree(runtime, ignore_errors=True)
            events = ticks.EventLedger(
                "claude",
                self.state / "claude-ticks.jsonl",
                runtime / "events-claude.json",
                monitor.action_labels,
                source="events",
            )
            log = ticks.LogLedger(
                "claude",
                self.state / "claude.log",
                runtime / "ticks-claude.json",
                monitor.action_labels,
            )
            found = []
            for ledger in (events, log):
                # A fresh checkpoint starts at the end; read the whole file.
                ledger.state = ledger.fresh(now, 0)
                ledger.update(now)
                metrics = monitor.Metrics()
                ticks.export(metrics, ledger, now)
                key = 'epic_tick_consecutive_failures{agent="claude"}'
                found.append(
                    next(
                        float(line.rsplit(" ", 1)[1])
                        for line in metrics.render().splitlines()
                        if line.startswith(key)
                    )
                )
            return found[0], found[1]

        self.assertEqual(streaks(), (2, 2))
        self.run_tick().wait(timeout=30)
        self.assertEqual(streaks(), (0, 0))

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

        # Handoff (75), then a repeated notice (3): both ticks end blocked.
        for _ in range(2):
            self.assertEqual(self.run_tick(**env).wait(timeout=60), 75)
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


def usage_result(**fields: object) -> str:
    """A terminal CLI result for TEST_MODEL_LIMIT; the stub fills in its session."""
    data: dict[str, object] = {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "terminal_reason": "api_error",
        "api_error_status": 429,
        "result": "You've hit your session limit · resets 1am (UTC)",
        "session_id": "@SESSION@",
        "duration_api_ms": 0,
        "num_turns": 1,
        "total_cost_usd": 0,
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "permission_denials": [],
    }
    data.update(fields)
    return json.dumps(data, ensure_ascii=False)


class UsageWaitTickTest(RunnerHarness):
    """The Claude usage wait in the real tick (claude_usage.py)."""

    def usage(self, key: str = "default") -> dict[str, object] | None:
        path = self.state / "claude-usage" / key / "state.json"
        return json.loads(path.read_text()) if path.exists() else None

    def last_finish(self) -> tuple[object, object, object]:
        events = (self.state / "claude-ticks.jsonl").read_text().splitlines()
        finish = json.loads(events[-1])
        return finish["exit"], finish["outcome"], finish["phase"]

    def log(self) -> str:
        return (self.state / "claude.log").read_text()

    def fresh_calls(self) -> None:
        self.calls.unlink(missing_ok=True)

    def wait_for_model(self) -> int:
        deadline = time.time() + 30
        while not self.model_pid.exists() and time.time() < deadline:
            time.sleep(0.1)
        return int(self.model_pid.read_text())

    def test_session_limit_stores_a_wait_and_the_next_tick_waits(self) -> None:
        tick = self.run_tick(TEST_MODEL_LIMIT=usage_result(), TEST_VERIFY_EXIT="1")
        self.assertEqual(tick.wait(timeout=30), 75)
        self.assertEqual(self.last_finish(), (75, "blocked", "usage"))
        usage = self.usage()
        assert usage is not None
        wait = usage["wait"]
        assert isinstance(wait, dict)
        self.assertEqual(wait["reason"], "session_limit")
        self.assertEqual(usage["admissions"], {})
        # No repeat-gate record and no target cooldown.
        self.assertEqual(
            [c for c in self.calls_made() if c[0] == "gate"], [["gate", "check"]]
        )
        self.assertFalse((self.state / "claude-gate-seen.json").exists())
        cooldowns = self.state / "claude-cooldown.json"
        self.assertTrue(
            not cooldowns.exists() or json.loads(cooldowns.read_text()) == {}
        )
        self.assertIn("Claude session limit", self.log())

        # The next tick stops before the selector: no GitHub selection, no model.
        self.fresh_calls()
        self.assertEqual(self.run_tick().wait(timeout=30), 0)
        kinds = {c[0] for c in self.calls_made()}
        self.assertNotIn("select", kinds)
        self.assertNotIn("claude", kinds)
        self.assertEqual(self.last_finish(), (0, "ok", "usage"))
        self.assertIn(
            f"claude usage (default): session_limit until {wait['retry_at']}; no model",
            self.log(),
        )

    def test_a_wait_stored_after_the_first_check_stops_the_model(self) -> None:
        # Another runner stores the wait while this tick runs its gate.
        (self.repo / "scripts/epic/tick_gate.py").write_text(
            "import json, os, sys\n"
            f"sys.path.insert(0, {str(self.repo / 'scripts/epic')!r})\n"
            "import claude_usage as cu\n"
            "with open(os.environ['TEST_CALLS'], 'a') as f:\n"
            "    f.write(json.dumps(['gate', sys.argv[1]]) + '\\n')\n"
            "if sys.argv[1] == 'check':\n"
            "    open(os.path.join(os.environ['EPIC_STATE_DIR'], 'claude-gate-seen.json'), 'w').write('{}')\n"
            "    store = cu.Store.from_env(os.environ)\n"
            "    store.create()\n"
            "    data = cu.empty()\n"
            "    data['generation'] = 1\n"
            "    data['wait'] = {'reason': 'session_limit', 'observed_at': '2026-01-01T00:00:00Z',\n"
            "                    'reset_at': None, 'retry_at': '2099-01-01T00:00:00Z',\n"
            "                    'source': 'fallback', 'fallback_count': 1, 'detail': 'past_reset'}\n"
            "    store.save(data)\n"
        )
        self.assertEqual(self.run_tick().wait(timeout=30), 0)
        self.assertEqual(self.model_targets(), [])
        self.assertNotIn(["gate", "record"], self.calls_made())
        self.assertFalse((self.state / "claude-gate-seen.json").exists())
        self.assertEqual(self.last_finish(), (0, "ok", "usage"))
        self.assertEqual(
            list((self.state / "claude-usage/default/admissions").iterdir()), []
        )
        # Codex review on PR 699: the late refusal has its record, no model-start.
        self.assertEqual(
            activity_of(self.state),
            [
                (
                    "wait",
                    "waiting",
                    "pr:1",
                    "model_usage_limit",
                    "agent",
                    "2099-01-01T00:00:00Z",
                )
            ],
        )

    def test_other_api_errors_keep_the_next_tick_running(self) -> None:
        transient = usage_result(
            result="API Error: 529 Overloaded", api_error_status=529
        )
        self.assertEqual(self.run_tick(TEST_MODEL_LIMIT=transient).wait(timeout=30), 1)
        usage = self.usage()
        assert usage is not None
        self.assertIsNone(usage["wait"])
        self.fresh_calls()
        self.assertEqual(self.run_tick().wait(timeout=30), 0)
        self.assertEqual(len(self.model_targets()), 1)

    def test_landed_work_stays_done_with_the_session_limit(self) -> None:
        # fix lands (verify passes) and the session also hit the limit.
        tick = self.run_tick(TEST_MODEL_LIMIT=usage_result(), TEST_VERIFY_EXIT="0")
        self.assertEqual(tick.wait(timeout=30), 1)  # the model's own exit, as before
        self.assertIn(["gate", "record"], self.calls_made())
        usage = self.usage()
        assert usage is not None
        self.assertIsNotNone(usage["wait"])

    def test_usage_locks_never_reach_the_model(self) -> None:
        self.assertEqual(self.run_tick().wait(timeout=30), 0)
        self.assertEqual(Path(f"{self.calls}.usage-fds").read_text(), "")
        usage = self.usage()
        assert usage is not None
        self.assertEqual((usage["wait"], usage["admissions"]), (None, {}))

    def test_a_running_model_shows_as_a_running_admission(self) -> None:
        tick = self.run_tick(TEST_MODEL_SLEEP="30")
        self.wait_for_model()
        status = subprocess.run(
            ["python3", str(self.repo / "scripts/epic/claude_usage.py"), "status"],
            env={**self.env, "EPIC_QUOTA_DIR": str(self.state)},
            capture_output=True, text=True, timeout=30,
        )  # fmt: skip
        self.assertIn("running since", status.stdout)
        tick.send_signal(signal.SIGTERM)
        self.assertEqual(tick.wait(timeout=30), 143)
        # The stopped tick resolved its admission: the next tick runs.
        usage = self.usage()
        assert usage is not None
        self.assertEqual(usage["admissions"], {})
        self.model_pid.unlink()
        self.fresh_calls()
        self.assertEqual(self.run_tick().wait(timeout=30), 0)
        self.assertEqual(len(self.model_targets()), 1)

    def test_a_killed_tick_waits_for_repair(self) -> None:
        tick = self.run_tick(TEST_MODEL_SLEEP="30")
        model = self.wait_for_model()
        tick.kill()
        tick.wait(timeout=30)
        os.kill(model, signal.SIGKILL)
        time.sleep(0.5)
        usage = self.usage()
        assert usage is not None
        admissions = usage["admissions"]
        assert isinstance(admissions, dict)
        (admission,) = admissions
        # The killed tick leaves its lock dir for manual review, as before.
        shutil.rmtree(self.state / "claude.lock")
        self.fresh_calls()
        self.assertEqual(self.run_tick().wait(timeout=30), 0)
        self.assertEqual(self.model_targets(), [])
        self.assertIn(f"unresolved admission {admission}", self.log())
        repaired = subprocess.run(
            ["python3", str(self.repo / "scripts/epic/claude_usage.py"), "repair", "--id", admission],
            env={**self.env, "EPIC_QUOTA_DIR": str(self.state)},
            capture_output=True, text=True, timeout=30,
        )  # fmt: skip
        self.assertEqual(repaired.returncode, 0, repaired.stderr)
        self.model_pid.unlink()
        self.assertEqual(self.run_tick().wait(timeout=30), 0)
        self.assertEqual(len(self.model_targets()), 1)

    def test_an_unusable_store_or_key_stops_the_tick(self) -> None:
        self.state.mkdir(mode=0o700)
        (self.state / "claude-usage").mkdir(mode=0o755)
        (self.state / "claude-usage").chmod(0o755)
        self.assertEqual(self.run_tick().wait(timeout=30), 1)
        self.assertEqual(self.model_targets(), [])
        self.assertNotIn("select", {c[0] for c in self.calls_made()})
        self.assertIn("must have mode 0700", self.log())
        (self.state / "claude-usage").chmod(0o700)
        self.fresh_calls()
        tick = self.run_tick(EPIC_CLAUDE_ACCOUNT_KEY="bad key")
        self.assertEqual(tick.wait(timeout=30), 2)
        self.assertEqual(self.model_targets(), [])

    def test_another_account_is_not_held(self) -> None:
        self.assertEqual(
            self.run_tick(TEST_MODEL_LIMIT=usage_result(), TEST_VERIFY_EXIT="1").wait(
                timeout=30
            ),
            75,
        )
        self.fresh_calls()
        tick = self.run_tick(EPIC_CLAUDE_ACCOUNT_KEY="second")
        self.assertEqual(tick.wait(timeout=30), 0)
        self.assertEqual(len(self.model_targets()), 1)


if __name__ == "__main__":
    unittest.main()
