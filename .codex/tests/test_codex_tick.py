"""Run the real Bash wrapper with isolated state and fake agent/API boundaries."""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/epic"))
import isolated_env  # noqa: E402, F401 (before production modules or fixtures)

import fcntl
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
REAL_GIT = shutil.which("git")


class TickTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="epic-tick-", dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "checkout with spaces"
        self.home = self.root / "home"
        self.home.mkdir()
        self.state = self.home / ".local/state/epic-loop"
        self.target_locks = self.root / "target-locks"
        self.lock = self.state / "codex.lock"
        self.record = self.state / "codex-gate.json"
        self.updated_at = self.root / "updated-at"
        self.updated_at.write_text("2026-09-28T03:00:00Z")
        self.calls = self.root / "codex-calls.jsonl"
        self.gh_calls = self.root / "gh-calls.jsonl"
        self.pr_queries = self.root / "pr-queries.jsonl"
        self.selections = self.root / "selector-calls"
        self.rechecks = self.root / "recheck-calls"
        self.pulls = self.root / "pull-calls"
        self.review_calls = self.root / "review-calls.jsonl"
        self.runner = self.repo / ".codex/codex-tick.sh"
        self.runner.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / "codex-tick.sh", self.runner)
        shutil.copyfile(ROOT / "epic-tick.md", self.runner.parent / "epic-tick.md")
        shutil.copyfile(ROOT / "epic_lock.py", self.runner.parent / "epic_lock.py")
        (self.runner.parent / "review_delivery.py").write_text(
            "import os, sys, time\n"
            "command = sys.argv[1]\n"
            "if os.environ.get('TEST_DELIVERY_SLEEP') == command: time.sleep(60)\n"
            "raise SystemExit(int(os.environ.get('TEST_DELIVERY_' + command.upper() + '_EXIT', '3')))\n"
        )
        (self.runner.parent / "assignment.py").write_text(
            "import argparse, json, os, pathlib\n"
            "parser = argparse.ArgumentParser()\n"
            "parser.add_argument('--action-file')\n"
            "parser.add_argument('--output')\n"
            "args = parser.parse_args()\n"
            "pathlib.Path(args.output).write_text(json.dumps({'result': 'not_applicable', 'eligible': True}))\n"
            "raise SystemExit(int(os.environ.get('TEST_ASSIGNMENT_EXIT', '0')))\n"
        )
        (self.runner.parent / "feature_worktree.py").write_text(
            "import argparse\n"
            "parser = argparse.ArgumentParser()\n"
            "parser.add_argument('--runner', required=True)\n"
            "parser.add_argument('--action-file', required=True)\n"
            "parser.add_argument('--state-dir', required=True)\n"
            "print(parser.parse_args().runner)\n"
        )
        (self.runner.parent / "review_worktree.py").write_text(
            """import argparse, json, os, pathlib, shutil, sys, time
parser = argparse.ArgumentParser()
parser.add_argument('command', choices=('prepare', 'cleanup'))
parser.add_argument('--runner', required=True)
parser.add_argument('--action-file')
parser.add_argument('--state-dir')
parser.add_argument('--context-file', required=True)
args = parser.parse_args()
context_file = pathlib.Path(args.context_file)
calls = pathlib.Path(os.environ['TEST_REVIEW_CALLS'])
if args.command == 'prepare':
    if os.environ.get('TEST_REVIEW_REFUSE_EARLY'):
        sys.exit(2)
    action = json.loads(pathlib.Path(args.action_file).read_text())
    count = len(calls.read_text().splitlines()) if calls.exists() else 0
    artifact = pathlib.Path(args.state_dir) / 'reviews' / str(action['pr']) / action['sha']
    attempt = artifact / f'attempt-{count}'
    attempt.mkdir(parents=True)
    worktree = calls.parent / f'review-{action["pr"]}-{action["sha"]}'
    worktree.mkdir(exist_ok=True)
    context = {'worktree': str(worktree), 'artifact_dir': str(artifact), 'attempt_dir': str(attempt)}
    context_file.write_text(json.dumps(context))
    entry = {'command': 'prepare', **context}
else:
    context = json.loads(context_file.read_text())
    if os.environ.get('TEST_REVIEW_CLEANUP_SLEEP'):
        time.sleep(60)
    attempt = pathlib.Path(context['attempt_dir'])
    pid_file = attempt / 'model.pid'
    alive = False
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text()), 0)
            alive = True
        except ProcessLookupError:
            pass
    entry = {'command': 'cleanup', **context, 'model_alive': alive}
    for name in ('result.json', 'model.log'):
        path = attempt / name
        entry[name] = path.read_text() if path.exists() else None
    state = pathlib.Path(os.environ['EPIC_STATE_DIR'])
    entry['gate_recorded'] = (state / 'codex-gate.json').exists()
    entry['runner_log'] = (state / 'codex.log').read_text()
with calls.open('a') as output:
    output.write(json.dumps(entry) + '\\n')
if args.command == 'prepare':
    exits = json.loads(os.environ.get('TEST_REVIEW_PREPARE_EXITS', '{}'))
    code = int(exits.get(str(action['pr']), os.environ.get('TEST_REVIEW_PREPARE_EXIT', '0')))
    if code:
        sys.exit(code)
    print(context['worktree'])
elif os.environ.get('TEST_REVIEW_CLEANUP_EXIT'):
    print('review: retained ' + context['worktree'], file=sys.stderr)
    sys.exit(int(os.environ['TEST_REVIEW_CLEANUP_EXIT']))
else:
    shutil.rmtree(context['worktree'])
"""
        )
        for filename in ("tick_backoff.py", "tick-result.schema.json"):
            shutil.copyfile(ROOT / filename, self.runner.parent / filename)
        selector = self.repo / "scripts/epic/next_action.py"
        selector.parent.mkdir(parents=True)
        for helper in (
            "tick_gate.py",
            "github_quota.py",
            "github_state.py",
            "agents.py",
            "tick_events.py",
            "target_lock.py",
            "routing.py",
            "tick_verify.py",
            "write_checks.py",
        ):
            shutil.copyfile(
                ROOT.parent / "scripts/epic" / helper, selector.parent / helper
            )
        shutil.copyfile(
            ROOT.parent / "scripts/epic/next_action.py",
            selector.parent / "selector_contract.py",
        )
        self.noise_checks = self.root / "noise-checks"
        (selector.parent / "gitnexus_noise.py").write_text(
            "import os, pathlib, sys, time\n"
            "pathlib.Path(os.environ['TEST_NOISE_CHECKS']).write_text('checked')\n"
            "if os.environ.get('TEST_NOISE_SLEEP'): time.sleep(60)\n"
            "if os.environ.get('TEST_PAUSE_AFTER_NOISE'):\n"
            "    (pathlib.Path.home() / '.epic-pause').touch()\n"
            "sys.exit(int(os.environ.get('TEST_NOISE_EXIT', '0')))\n"
        )
        selector_code = (
            "import json, os, pathlib, sys, time\n"
            "from github_quota import QuotaExhausted, record, stop_on_quota\n"
            "if '--recheck' in sys.argv:\n"
            "    assert sys.argv[1:4] == ['--agent', 'codex', '--recheck']\n"
            "    seen = pathlib.Path(os.environ['EPIC_STATE_DIR']) / 'codex-gate-seen.json'\n"
            "    assert seen.exists(), 'recheck must follow the gate'\n"
            "    action = json.loads(pathlib.Path(sys.argv[4]).read_text())\n"
            "    with open(os.environ['TEST_RECHECKS'], 'a') as f:\n"
            "        f.write(json.dumps(action) + '\\n')\n"
            "    if os.environ.get('TEST_RECHECK_SLEEP'): time.sleep(60)\n"
            "    exits = json.loads(os.environ.get('TEST_RECHECK_EXITS', '{}'))\n"
            "    sys.exit(int(exits.get(str(action.get('pr')), os.environ.get('TEST_RECHECK_EXIT', '0'))))\n"
            "assert sys.argv[1:] == ['--agent', 'codex', '--candidates']\n"
            "assert pathlib.Path(os.environ['TEST_PULLS']).exists()\n"
            "with open(os.environ['TEST_SELECTIONS'], 'a') as f: f.write('call\\n')\n"
            "if os.environ.get('TEST_SELECTOR_EXIT'): sys.exit(23)\n"
            "if os.environ.get('TEST_SELECT_READ_EXIT'): sys.exit(int(os.environ['TEST_SELECT_READ_EXIT']))\n"
            "if os.environ.get('TEST_SELECT_QUOTA'):\n"
            "    sys.exit(stop_on_quota(QuotaExhausted('selector exhausted')))\n"
            "if os.environ.get('TEST_WAIT_AFTER_SELECT'):\n"
            "    record(pathlib.Path(os.environ['EPIC_QUOTA_DIR']), time.time(), os.environ['TEST_RESET'])\n"
            "if os.environ.get('TEST_PAUSE_AFTER_SELECT'):\n"
            "    (pathlib.Path.home() / '.epic-pause').touch()\n"
            "print(os.environ['TEST_DECISION'])\n"
        )
        selector.write_text(
            "from selector_contract import BASES, EPIC, PROJECT_API, REPO, body_digest, issue_url, other\n"
            "if __name__ == '__main__':\n"
            + "\n".join(f"    {line}" for line in selector_code.splitlines())
            + "\n"
        )
        self.bin = self.root / "bin"
        self.bin.mkdir()
        git = self.bin / "git"
        git.write_text(
            "#!/usr/bin/env python3\n"
            "import os, pathlib, sys, time\n"
            f"REAL_GIT = {REAL_GIT!r}\n"
            "if sys.argv[1:] != ['pull', '--ff-only']:\n"
            "    os.execv(REAL_GIT, [REAL_GIT, *sys.argv[1:]])\n"
            "assert pathlib.Path(os.environ['TEST_NOISE_CHECKS']).exists()\n"
            "assert pathlib.Path.cwd() == pathlib.Path(os.environ['TEST_REPO']).resolve()\n"
            # The runner exports its own state dir; the lock is held there.
            "assert (pathlib.Path(os.environ['EPIC_STATE_DIR']) / 'codex.lock/owner.json').exists()\n"
            "with open(os.environ['TEST_PULLS'], 'a') as f: f.write('pull\\n')\n"
            "if os.environ.get('TEST_PAUSE_AFTER_PULL'):\n"
            "    (pathlib.Path.home() / '.epic-pause').touch()\n"
            "if os.environ.get('TEST_PULL_SLEEP'): time.sleep(60)\n"
            "sys.exit(int(os.environ.get('TEST_PULL_EXIT', '0')))\n"
        )
        git.chmod(0o755)
        gh = self.bin / "gh"
        gh.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, sys, time\n"
            "with open(os.environ['TEST_GH_CALLS'], 'a') as f:\n"
            "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "if sys.argv[1:3] == ['api', '-i']:\n"
            "    board = json.loads(os.environ.get('TEST_BOARD_RESPONSES', '{}'))\n"
            "    if sys.argv[-1] in board:\n"
            "        print(board[sys.argv[-1]])\n"
            "        sys.exit(0)\n"
            "    if os.environ.get('TEST_FRESH_SLEEP'): time.sleep(60)\n"
            "    status = 200\n"
            "    if sys.argv[-1] == 'https://api.github.com/user':\n"
            "        data = {'login': 'test-user'}\n"
            "    else:\n"
            "        assert sys.argv[-1] == 'https://api.github.com/repos/phaabe/live.moafunk.de/pulls/417'\n"
            "        metadata = json.loads(os.environ['TEST_PR_METADATA'])\n"
            "        data = {'body': metadata['body'], 'head': {'sha': metadata['headRefOid']}}\n"
            "        status = int(os.environ.get('TEST_FRESH_STATUS', '200'))\n"
            "    print(f'HTTP/2.0 {status} Test\\n\\n' + json.dumps(data))\n"
            "    sys.exit(0 if status == 200 else 1)\n"
            "if sys.argv[1:3] == ['api', 'graphql']:\n"
            "    assert sys.argv[3:] == ['-f', 'query=query{rateLimit{resetAt}}']\n"
            "    print(json.dumps({'data': {'rateLimit': {'resetAt': os.environ['TEST_RESET']}}}))\n"
            "    sys.exit(0)\n"
            "boundary = 'pr' if sys.argv[1:3] == ['pr', 'view'] else 'gate'\n"
            "if sys.argv[1:3] == ['pr', 'list']: boundary = 'selector'\n"
            "if sys.argv[1:3] == ['api', 'repos/phaabe/live.moafunk.de/pulls/406']:\n"
            "    boundary = 'verify'\n"
            "if os.environ.get('TEST_GH_QUOTA') == boundary:\n"
            "    print(json.dumps({'errors': [{'type': 'RATE_LIMITED'}]}))\n"
            "    sys.exit(0)\n"
            "if os.environ.get('TEST_WAIT_AFTER_GATE') and boundary == 'gate':\n"
            "    sys.path.insert(0, str(pathlib.Path(os.environ['TEST_REPO']) / 'scripts/epic'))\n"
            "    from github_quota import record\n"
            "    record(pathlib.Path(os.environ['EPIC_QUOTA_DIR']), time.time(), os.environ['TEST_RESET'])\n"
            "if os.environ.get('TEST_GH_FAILURE'): sys.exit(7)\n"
            "if sys.argv[1:3] == ['api', 'repos/phaabe/live.moafunk.de/pulls/406']:\n"
            "    print(json.dumps({'body': os.environ.get('TEST_ADOPT_BODY', '')}))\n"
            "    sys.exit(0)\n"
            "if sys.argv[1:3] == ['pr', 'view']:\n"
            "    assert sys.argv[4:] == ['--repo', 'phaabe/live.moafunk.de', "
            "'--json', 'body,headRefOid']\n"
            "    with open(os.environ['TEST_PR_QUERIES'], 'a') as f:\n"
            "        f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "    if os.environ.get('TEST_PR_FAILURE'): sys.exit(7)\n"
            "    print(os.environ['TEST_PR_METADATA'])\n"
            "    sys.exit(0)\n"
            "assert sys.argv[1] == 'api'\n"
            "if os.environ.get('TEST_PAUSE_AFTER_GATE'):\n"
            "    (pathlib.Path.home() / '.epic-pause').touch()\n"
            "target = sys.argv[2].rsplit('/', 1)[-1]\n"
            "states = json.loads(os.environ.get('TEST_TARGET_STATES', '{}'))\n"
            "default_state = pathlib.Path(os.environ['TEST_UPDATED_AT']).read_text()\n"
            "print(states.get(target, default_state))\n"
        )
        gh.chmod(0o755)
        codex = self.bin / "codex"
        codex.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, shlex, socket, subprocess, sys\n"
            # Model core inheritance at the tool boundary, not the CLI process.
            "tool_env = {key: os.environ[key] for key in ('HOME', 'PATH')}\n"
            "tool_env['EXISTING_USER_SETTING'] = 'preserved'\n"
            "for index, arg in enumerate(sys.argv[:-1]):\n"
            "    if arg != '-c': continue\n"
            "    key, value = sys.argv[index + 1].split('=', 1)\n"
            "    assert key not in ('shell_environment_policy', "
            "'shell_environment_policy.set', 'shell_environment_policy.inherit')\n"
            "    prefix = 'shell_environment_policy.set.'\n"
            "    if key.startswith(prefix):\n"
            # The runner emits JSON strings, a TOML basic-string subset.
            "        tool_env[key[len(prefix):]] = json.loads(value)\n"
            "with open(os.environ['TEST_CALLS'], 'a') as f:\n"
            "    action_file = pathlib.Path(os.environ['EPIC_ACTION_FILE'])\n"
            "    assert action_file.is_absolute()\n"
            "    assert os.environ['EPIC_TRUSTED_ROOT'] == str(pathlib.Path(os.environ['TEST_REPO']).resolve())\n"
            "    f.write(json.dumps({'args': sys.argv[1:], 'prompt': sys.stdin.read(), "
            "'action_file': str(action_file), 'action': json.loads(action_file.read_text()), "
            "'tool_env': tool_env}) + '\\n')\n"
            "if os.environ.get('EPIC_REVIEW_ATTEMPT_DIR'):\n"
            "    (pathlib.Path(os.environ['EPIC_REVIEW_ATTEMPT_DIR']) / 'model.pid').write_text(str(os.getpid()))\n"
            "if os.environ.get('TEST_MODEL_GUARD'):\n"
            "    repo = pathlib.Path(os.environ['TEST_REPO'])\n"
            "    body = repo / 'adopt body.md'\n"
            "    body.write_text(os.environ['TEST_ADOPT_BODY'])\n"
            "    codes = []\n"
            "    for number in (406, 407):\n"
            "        command = ('gh api --method PATCH ' "
            "+ f'repos/phaabe/live.moafunk.de/pulls/{number} -F ' "
            "+ shlex.quote(f'body=@{body}'))\n"
            "        payload = {'tool_name': 'exec_command', 'cwd': str(repo), "
            "'tool_input': {'cmd': command}}\n"
            "        hook = repo / '.codex/hooks/scripts/epic-guard.sh'\n"
            "        checked = subprocess.run(['/bin/bash', str(hook)], "
            "input=json.dumps(payload), text=True, capture_output=True, env=tool_env)\n"
            "        codes.append(checked.returncode)\n"
            "    pathlib.Path(os.environ['TEST_MODEL_GUARD']).write_text(json.dumps(codes))\n"
            "if os.environ.get('TEST_MODEL_ENV_PROBE'):\n"
            "    probe = subprocess.run([sys.executable, "
            "os.environ['TEST_MODEL_ENV_PROBE']], env=tool_env, "
            "capture_output=True, text=True, check=True)\n"
            "    pathlib.Path(os.environ['TEST_MODEL_ENV_RESULT']).write_text(probe.stdout)\n"
            "print('fake Codex stdout', flush=True)\n"
            "if os.environ.get('TEST_TOKENS'):\n"
            "    print('tokens used', os.environ['TEST_TOKENS'], sep='\\n', flush=True)\n"
            "print('fake Codex stderr', file=sys.stderr, flush=True)\n"
            "if os.environ.get('TEST_SOCKET'):\n"
            "    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:\n"
            "        s.connect(os.environ['TEST_SOCKET'])\n"
            "        s.sendall(b'ready')\n"
            "        s.recv(1)\n"
            "if os.environ.get('TEST_WAIT_DURING_MODEL'):\n"
            "    sys.path.insert(0, str(pathlib.Path(os.environ['TEST_REPO']) / 'scripts/epic'))\n"
            "    import time\n"
            "    from github_quota import record\n"
            "    record(pathlib.Path(os.environ['EPIC_QUOTA_DIR']), time.time(), os.environ['TEST_RESET'])\n"
            "if os.environ.get('TEST_REMOVE_GATE_SNAPSHOT'):\n"
            "    (pathlib.Path(os.environ['EPIC_STATE_DIR']) / 'codex-gate-seen.json').unlink()\n"
            "if not os.environ.get('TEST_RESULT_MISSING'):\n"
            "    result = pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1])\n"
            "    result.write_text(os.environ.get('TEST_RESULT', "
            "json.dumps({'status': 'completed', 'summary': 'Action completed.'})))\n"
            "sys.exit(int(os.environ.get('TEST_CODEX_EXIT', '0')))\n"
        )
        codex.chmod(0o755)
        self.env = {
            **os.environ,
            "HOME": str(self.home),
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "TEST_CALLS": str(self.calls),
            "TEST_PR_QUERIES": str(self.pr_queries),
            "TEST_GH_CALLS": str(self.gh_calls),
            "TEST_SELECTIONS": str(self.selections),
            "TEST_RECHECKS": str(self.rechecks),
            "TEST_PULLS": str(self.pulls),
            "TEST_REVIEW_CALLS": str(self.review_calls),
            "TEST_NOISE_CHECKS": str(self.noise_checks),
            "TEST_REPO": str(self.repo),
            "TEST_UPDATED_AT": str(self.updated_at),
            "TEST_DECISION": json.dumps(
                {"action": "review", "pr": 406, "sha": "a" * 40}
            ),
            "EPIC_TICK_TIMEOUT_SECONDS": "10",
            "EPIC_SELECT_TIMEOUT_SECONDS": "10",
            "EPIC_PULL_TIMEOUT_SECONDS": "10",
            "EPIC_STATE_DIR": str(self.state),
            "EPIC_LOCK_DIR": str(self.target_locks),
            "EPIC_REPEAT_TTL_SECONDS": "10800",
            "EPIC_BLOCKED_RETRY_SECONDS": "900",
            "EPIC_SHARED_READER": "0",
            "EPIC_SNAPSHOT_LOCK_SECONDS": "1",
            "EPIC_SNAPSHOT_REFRESH_SECONDS": "1",
            "EPIC_RECHECK_TIMEOUT_SECONDS": "2",
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

    def expire_cooldown(self) -> None:
        path = self.state / "codex-backoff.json"
        entries = json.loads(path.read_text())
        self.assertTrue(entries)
        for entry in entries.values():
            entry["at"] = 0
            # The stored expiry is what the runner obeys.
            if "until" in entry:
                entry["until"] = 0
        path.write_text(json.dumps(entries))

    def quota_clock(self) -> None:
        """Control the clock at the Python process boundary, outside product code."""
        self.clock = self.root / "clock"
        self.clock.write_text("2000000000")
        self.env["TEST_CLOCK"] = str(self.clock)
        self.env["TEST_RESET"] = "2033-05-18T03:43:20Z"  # clock + 600 seconds
        python = self.bin / "python3"
        python.write_text(
            f"#!{sys.executable}\n"
            "import os, pathlib, runpy, sys, time\n"
            "time.time = lambda: float(pathlib.Path(os.environ['TEST_CLOCK']).read_text())\n"
            "sys.argv = sys.argv[1:]\n"
            "if sys.argv[0] == '-c':\n"
            "    code = sys.argv[1]\n"
            "    sys.argv = ['-c', *sys.argv[2:]]\n"
            "    exec(compile(code, '<string>', 'exec'), {'__name__': '__main__'})\n"
            "else:\n"
            "    sys.path.insert(0, str(pathlib.Path(sys.argv[0]).resolve().parent))\n"
            "    runpy.run_path(sys.argv[0], run_name='__main__')\n"
        )
        python.chmod(0o755)

    def store_quota_wait(self) -> dict[str, object]:
        """Seed exactly the shared helper's persisted format through its CLI."""
        result = subprocess.run(
            [
                str(self.bin / "python3"),
                str(self.repo / "scripts/epic/github_quota.py"),
                "record",
                "--state-dir",
                str(self.state),
                "--reset-at",
                self.env["TEST_RESET"],
            ],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads((self.state / "github-quota-wait.json").read_text())

    def assert_quota_only(self, *, model_calls: int = 0) -> None:
        self.assertTrue((self.state / "github-quota-wait.json").exists())
        self.assertFalse((self.state / "codex-backoff.json").exists())
        self.assertFalse(self.record.exists())
        self.assertFalse(self.lock.exists())
        calls = self.calls.read_text().splitlines() if self.calls.exists() else []
        self.assertEqual(len(calls), model_calls)

    def test_shared_quota_wait_prevents_pull_api_and_model_until_reset(self) -> None:
        self.quota_clock()
        wait = self.store_quota_wait()
        self.assertEqual(wait["retry_at"], "2033-05-18T03:44:20Z")
        for now in (2000000000, 2000000659):
            with self.subTest(now=now):
                self.clock.write_text(str(now))
                self.assertEqual(self.run_tick().returncode, 0)
                self.assertFalse(self.pulls.exists())
                self.assertFalse(self.selections.exists())
                self.assertFalse(self.gh_calls.exists())
                self.assert_quota_only()
                self.assertEqual(self.last_finish(), (0, "ok", "quota"))
        self.clock.write_text("2000000660")
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)
        self.assertEqual(self.selections.read_text(), "call\n")
        self.assertTrue(self.record.exists())

    def test_registered_agent_obeys_the_shared_root_quota_wait(self) -> None:
        self.quota_clock()
        self.store_quota_wait()
        self.env["EPIC_AGENT_ID"] = "codex-2"
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertFalse(self.pulls.exists())
        self.assertFalse(self.gh_calls.exists())
        self.assertFalse(self.calls.exists())
        agent = self.state / "agents/codex-2"
        for name in ("github-quota-wait.json", "codex-gate.json", "codex-backoff.json"):
            self.assertFalse((agent / name).exists(), name)

    def test_malformed_quota_wait_fails_without_git_api_or_model(self) -> None:
        self.state.mkdir(parents=True)
        (self.state / "github-quota-wait.json").write_text('{"retry_at": null}')
        self.assertEqual(self.run_tick().returncode, 2)
        self.assertFalse(self.pulls.exists())
        self.assertFalse(self.gh_calls.exists())
        self.assertFalse(self.calls.exists())
        self.assertFalse(self.record.exists())
        self.assertFalse((self.state / "codex-backoff.json").exists())

    def test_selector_quota_exit_defers_without_target_state(self) -> None:
        self.quota_clock()
        self.env["TEST_SELECT_QUOTA"] = "1"
        self.assertEqual(self.run_tick().returncode, 75)
        self.assert_quota_only()
        self.assertEqual(self.last_finish(), (75, "blocked", "quota"))
        queries = [json.loads(line) for line in self.gh_calls.read_text().splitlines()]
        self.assertEqual(
            queries, [["api", "graphql", "-f", "query=query{rateLimit{resetAt}}"]]
        )
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertEqual(len(self.gh_calls.read_text().splitlines()), 1)
        self.assertEqual(self.selections.read_text(), "call\n")

    def test_real_selector_http_200_quota_stops_before_an_action_is_used(self) -> None:
        self.quota_clock()
        shutil.copyfile(
            ROOT.parent / "scripts/epic/next_action.py",
            self.repo / "scripts/epic/next_action.py",
        )
        self.env["TEST_GH_QUOTA"] = "selector"
        self.assertEqual(self.run_tick().returncode, 75)
        self.assert_quota_only()
        queries = [json.loads(line) for line in self.gh_calls.read_text().splitlines()]
        self.assertEqual(len(queries), 2)
        self.assertEqual(queries[0][:2], ["pr", "list"])
        self.assertEqual(
            queries[1], ["api", "graphql", "-f", "query=query{rateLimit{resetAt}}"]
        )

    def test_real_selector_nonquota_failure_stays_visible(self) -> None:
        shutil.copyfile(
            ROOT.parent / "scripts/epic/next_action.py",
            self.repo / "scripts/epic/next_action.py",
        )
        self.env["TEST_GH_FAILURE"] = "1"
        self.assertNotEqual(self.run_tick().returncode, 0)
        self.assertFalse(self.calls.exists())
        self.assertFalse(self.record.exists())
        self.assertFalse((self.state / "github-quota-wait.json").exists())
        self.assertFalse((self.state / "codex-backoff.json").exists())
        self.assertIn("CalledProcessError", (self.state / "codex.log").read_text())

    def test_gate_http_200_quota_error_defers_without_target_state(self) -> None:
        self.quota_clock()
        self.env["TEST_GH_QUOTA"] = "gate"
        self.assertEqual(self.run_tick().returncode, 75)
        self.assert_quota_only()
        self.assertEqual(self.last_finish(), (75, "blocked", "quota"))
        self.assertFalse((self.state / "codex-gate-seen.json").exists())
        self.assertEqual(len(self.gh_calls.read_text().splitlines()), 2)

    def test_quota_during_pr_cooldown_lookup_preserves_issue_state(self) -> None:
        self.quota_clock()
        self.block_issue_then_select_draft_pr()
        backoff = self.state / "codex-backoff.json"
        previous_backoff = backoff.read_bytes()
        previous_gate = self.record.read_bytes()
        self.env["TEST_GH_QUOTA"] = "pr"
        self.assertEqual(self.run_tick().returncode, 75)
        self.assertTrue((self.state / "github-quota-wait.json").exists())
        self.assertEqual(backoff.read_bytes(), previous_backoff)
        self.assertEqual(self.record.read_bytes(), previous_gate)
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)

    def test_wait_created_during_selection_prevents_gate_and_model(self) -> None:
        self.quota_clock()
        self.env["TEST_WAIT_AFTER_SELECT"] = "1"
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertFalse(self.gh_calls.exists())
        self.assert_quota_only()
        self.assertEqual(self.last_finish(), (0, "ok", "quota"))

    def test_wait_created_during_gate_prevents_model(self) -> None:
        self.quota_clock()
        self.env["TEST_WAIT_AFTER_GATE"] = "1"
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertEqual(len(self.gh_calls.read_text().splitlines()), 1)
        self.assert_quota_only()
        self.assertEqual(self.last_finish(), (0, "ok", "quota"))

    def prepare_wait_during_model(self) -> tuple[Path, bytes]:
        self.quota_clock()
        self.env["TEST_RESULT"] = json.dumps(
            {"status": "blocked", "summary": "Permission denied."}
        )
        self.assertEqual(self.run_tick().returncode, 75)
        self.expire_cooldown()
        self.updated_at.write_text("2026-09-28T03:01:00Z")
        backoff = self.state / "codex-backoff.json"
        previous_gate = self.record.read_bytes()
        del self.env["TEST_RESULT"]
        self.env["TEST_WAIT_DURING_MODEL"] = "1"
        return backoff, previous_gate

    def test_wait_during_completed_model_records_success_and_clears_backoff(
        self,
    ) -> None:
        backoff, previous_gate = self.prepare_wait_during_model()
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertEqual(json.loads(backoff.read_text()), {})
        self.assertNotEqual(self.record.read_bytes(), previous_gate)
        self.assertEqual(
            json.loads(self.record.read_text())["updated_at"],
            self.updated_at.read_text(),
        )
        self.assertTrue((self.state / "github-quota-wait.json").exists())
        self.assertEqual(self.last_finish(), (0, "ok", "record"))
        self.assertEqual(len(self.calls.read_text().splitlines()), 2)

    def test_wait_during_blocked_model_records_gate_and_target_cooldown(self) -> None:
        backoff, previous_gate = self.prepare_wait_during_model()
        self.env["TEST_RESULT"] = json.dumps(
            {"status": "blocked", "summary": "Build needs a dependency."}
        )
        self.assertEqual(self.run_tick().returncode, 75)
        entry = json.loads(backoff.read_text())[f"pr:406:{'a' * 40}"]
        self.assertEqual(
            entry["reason"], "model reported blocked: Build needs a dependency."
        )
        self.assertGreater(entry["until"], float(self.clock.read_text()))
        self.assertNotEqual(self.record.read_bytes(), previous_gate)
        self.assertEqual(self.last_finish(), (75, "blocked", "result"))
        self.assertEqual(len(self.calls.read_text().splitlines()), 2)

    def test_wait_during_failed_model_preserves_exit_and_records_failure(self) -> None:
        backoff, previous_gate = self.prepare_wait_during_model()
        self.env["TEST_CODEX_EXIT"] = "17"
        self.assertEqual(self.run_tick().returncode, 17)
        entry = json.loads(backoff.read_text())[f"pr:406:{'a' * 40}"]
        self.assertEqual(entry["reason"], "model exited 17")
        self.assertGreater(entry["until"], float(self.clock.read_text()))
        self.assertEqual(self.record.read_bytes(), previous_gate)
        self.assertEqual(self.last_finish(), (17, "error", "model"))
        self.assertEqual(len(self.calls.read_text().splitlines()), 2)

    def test_model_quota_result_reuses_a_concurrent_wait_without_querying_reset(
        self,
    ) -> None:
        self.quota_clock()
        self.env["TEST_WAIT_DURING_MODEL"] = "1"
        self.env["TEST_RESULT"] = json.dumps(
            {
                "status": "blocked",
                "summary": "GitHub GraphQL quota exhausted.",
                "reason_code": "github_rate_limit",
                "retry_at": None,
            }
        )
        self.assertEqual(self.run_tick().returncode, 75)
        self.assert_quota_only(model_calls=1)
        queries = [json.loads(line) for line in self.gh_calls.read_text().splitlines()]
        self.assertEqual(len(queries), 1)
        self.assertEqual(
            queries[0][:2], ["api", "repos/phaabe/live.moafunk.de/issues/406"]
        )
        wait = json.loads((self.state / "github-quota-wait.json").read_text())
        self.assertEqual(wait["reset_at"], self.env["TEST_RESET"])
        self.assertEqual(wait["retry_at"], "2033-05-18T03:44:20Z")
        self.assertEqual(wait["source"], "caller")
        self.assertEqual(self.last_finish(), (75, "blocked", "quota"))

    def test_model_quota_result_waits_then_renews_without_target_backoff(self) -> None:
        self.quota_clock()
        self.env["TEST_RESULT"] = json.dumps(
            {
                "status": "blocked",
                "summary": "GitHub GraphQL quota exhausted.",
                "reason_code": "github_rate_limit",
                "retry_at": self.env["TEST_RESET"],
            }
        )
        self.assertEqual(self.run_tick().returncode, 75)
        self.assert_quota_only(model_calls=1)
        original_wait = (self.state / "github-quota-wait.json").read_bytes()
        queries = self.gh_calls.read_bytes()
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertEqual(self.gh_calls.read_bytes(), queries)
        self.assertEqual(
            (self.state / "github-quota-wait.json").read_bytes(), original_wait
        )
        self.clock.write_text("2000000660")
        self.env["TEST_RESET"] = "2033-05-18T03:53:20Z"
        result = json.loads(self.env["TEST_RESULT"])
        result["retry_at"] = self.env["TEST_RESET"]
        self.env["TEST_RESULT"] = json.dumps(result)
        self.assertEqual(self.run_tick().returncode, 75)
        self.assert_quota_only(model_calls=2)
        self.assertNotEqual(
            (self.state / "github-quota-wait.json").read_bytes(), original_wait
        )
        self.assertEqual(self.run_tick().returncode, 0)
        self.assert_quota_only(model_calls=2)

    def test_completed_result_with_nullable_quota_fields_records_success(self) -> None:
        self.env["TEST_RESULT"] = json.dumps(
            {
                "status": "completed",
                "summary": "Done.",
                "reason_code": None,
                "retry_at": None,
            }
        )
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertTrue(self.record.exists())
        self.assertFalse((self.state / "github-quota-wait.json").exists())

    def test_model_failure_with_quota_result_preserves_exit_and_shared_wait(
        self,
    ) -> None:
        self.quota_clock()
        self.env["TEST_CODEX_EXIT"] = "17"
        self.env["TEST_RESULT"] = json.dumps(
            {
                "status": "blocked",
                "summary": "GitHub GraphQL quota exhausted.",
                "reason_code": "github_rate_limit",
                "retry_at": self.env["TEST_RESET"],
            }
        )
        self.assertEqual(self.run_tick().returncode, 17)
        self.assert_quota_only(model_calls=1)
        queries = self.gh_calls.read_bytes()
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertEqual(self.gh_calls.read_bytes(), queries)
        self.assert_quota_only(model_calls=1)

    def test_registered_agent_records_new_quota_wait_at_shared_root(self) -> None:
        self.quota_clock()
        self.env["EPIC_AGENT_ID"] = "codex-2"
        self.env["TEST_SELECT_QUOTA"] = "1"
        self.assertEqual(self.run_tick().returncode, 75)
        self.assertTrue((self.state / "github-quota-wait.json").exists())
        agent = self.state / "agents/codex-2"
        for name in ("github-quota-wait.json", "codex-gate.json", "codex-backoff.json"):
            self.assertFalse((agent / name).exists(), name)
        queries = self.gh_calls.read_bytes()
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertEqual(self.gh_calls.read_bytes(), queries)
        self.assertFalse(self.calls.exists())

    def block_issue_then_select_draft_pr(self) -> dict[str, object]:
        issue = "https://github.com/phaabe/live.moafunk.de/issues/381"
        self.env["TEST_DECISION"] = json.dumps({"action": "continue", "issue": issue})
        self.env["TEST_RESULT"] = json.dumps(
            {"status": "blocked", "summary": "Commit permission denied."}
        )
        self.assertEqual(self.run_tick().returncode, 75)
        entries = json.loads((self.state / "codex-backoff.json").read_text())
        entry = entries[f"issue:{issue}"]
        self.env["TEST_DECISION"] = json.dumps(
            {"action": "continue", "pr": 417, "sha": "a" * 40}
        )
        self.env["TEST_PR_METADATA"] = json.dumps(
            {"body": f"Executor: Codex\nIssue: {issue}\n", "headRefOid": "a" * 40}
        )
        del self.env["TEST_RESULT"]
        return entry

    def test_issue_cooldown_follows_draft_pr_without_blocking_new_head(self) -> None:
        entry = self.block_issue_then_select_draft_pr()
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)
        self.assertEqual(
            json.loads((self.state / "codex-backoff.json").read_text()),
            {f"pr:417:{'a' * 40}": entry},
        )
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)
        self.assertEqual(len(self.pr_queries.read_text().splitlines()), 1)
        self.env["TEST_DECISION"] = json.dumps(
            {"action": "continue", "pr": 417, "sha": "b" * 40}
        )
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertEqual(len(self.calls.read_text().splitlines()), 2)

    def test_migrated_issue_cooldown_expires_for_same_pr_head(self) -> None:
        self.block_issue_then_select_draft_pr()
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)
        self.expire_cooldown()
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertEqual(len(self.calls.read_text().splitlines()), 2)
        self.assertEqual(
            json.loads((self.state / "codex-backoff.json").read_text()), {}
        )

    def test_pr_lookup_failure_and_head_race_preserve_issue_cooldown(self) -> None:
        self.block_issue_then_select_draft_pr()
        path = self.state / "codex-backoff.json"
        original = path.read_text()
        self.env["TEST_PR_FAILURE"] = "1"
        self.assertEqual(self.run_tick().returncode, 75)
        self.assertEqual(path.read_text(), original)
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)
        del self.env["TEST_PR_FAILURE"]
        metadata = json.loads(self.env["TEST_PR_METADATA"])
        metadata["headRefOid"] = "b" * 40
        self.env["TEST_PR_METADATA"] = json.dumps(metadata)
        self.assertEqual(self.run_tick().returncode, 75)
        self.assertEqual(path.read_text(), original)
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)

    def test_unchanged_blocked_claim_stays_suppressed_after_cooldown(self) -> None:
        self.env["TEST_DECISION"] = json.dumps(
            {
                "action": "claim",
                "issue": "https://github.com/phaabe/live.moafunk.de/issues/381",
            }
        )
        self.env["TEST_RESULT"] = json.dumps(
            {"status": "blocked", "summary": "Prerequisite still incomplete."}
        )
        self.assertEqual(self.run_tick().returncode, 75)
        for _ in range(2):
            self.expire_cooldown()
            self.assertEqual(self.run_tick().returncode, 0)
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)

        self.updated_at.write_text("2026-09-28T03:01:00Z")
        self.assertEqual(self.run_tick().returncode, 75)
        self.assertEqual(len(self.calls.read_text().splitlines()), 2)

    def test_pause_never_calls_selector_or_codex(self) -> None:
        (self.home / ".epic-pause").touch()
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertFalse(self.selections.exists())
        self.assertFalse(self.calls.exists())
        self.assertFalse(self.lock.exists())
        self.assertFalse(self.pulls.exists())

    def assert_no_work_after_noise_failure(self) -> None:
        self.assertFalse(self.pulls.exists())
        self.assertFalse(self.selections.exists())
        self.assertFalse(self.calls.exists())
        self.assertFalse(self.gh_calls.exists())
        self.assertFalse(self.lock.exists())
        self.assertFalse(self.record.exists())
        self.assertFalse((self.state / "codex-backoff.json").exists())

    def test_noise_failure_stops_before_pull_and_records_reason(self) -> None:
        self.env["TEST_NOISE_EXIT"] = "1"
        self.assertEqual(self.run_tick().returncode, 1)
        self.assert_no_work_after_noise_failure()
        self.assertIn(
            "checkout noise check failed exit=1", (self.state / "codex.log").read_text()
        )

    def test_noise_timeout_stops_before_pull(self) -> None:
        self.env["TEST_NOISE_SLEEP"] = "1"
        self.env["EPIC_PULL_TIMEOUT_SECONDS"] = "1"
        self.assertEqual(self.run_tick().returncode, 124)
        self.assert_no_work_after_noise_failure()
        self.assertEqual(self.last_finish(), (124, "timeout", "refresh"))

    def test_pause_during_noise_check_stops_before_pull(self) -> None:
        self.env["TEST_PAUSE_AFTER_NOISE"] = "1"
        self.assertEqual(self.run_tick().returncode, 0)
        self.assert_no_work_after_noise_failure()

    def test_real_noise_helper_preserves_work_and_cleans_only_generated_blocks(
        self,
    ) -> None:
        assert REAL_GIT is not None
        helper = self.repo / "scripts/epic/gitnexus_noise.py"
        shutil.copyfile(ROOT.parent / "scripts/epic/gitnexus_noise.py", helper)
        # The pull boundary verifies the real helper already cleaned the tree.
        pull_stub = self.bin / "git"
        stub = pull_stub.read_text().replace(
            "assert pathlib.Path(os.environ['TEST_NOISE_CHECKS']).exists()",
            "assert subprocess.check_output([REAL_GIT, 'status', '--porcelain', "
            "'--untracked-files=no'], text=True) == ''",
        )
        pull_stub.write_text(
            stub.replace(
                "import os, pathlib, sys, time",
                "import os, pathlib, sys, time, subprocess",
            )
        )
        self.env["TEST_DECISION"] = json.dumps({"action": "idle"})
        doc = (
            "Header\n<!-- gitnexus:start -->\nstats 1\n<!-- gitnexus:end -->\nFooter\n"
        )
        agent_doc = self.repo / "AGENTS.md"
        claude_doc = self.repo / "CLAUDE.md"
        for path in (agent_doc, claude_doc):
            path.write_text(doc)

        def git(*args: str) -> str:
            return subprocess.check_output(
                [REAL_GIT, "-C", str(self.repo), *args],
                text=True,
                stderr=subprocess.STDOUT,
                env=self.env,
            )

        git("init", "-q")
        git("add", ".")
        git(
            "-c",
            "user.name=Runner test",
            "-c",
            "user.email=runner@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "fixture",
        )
        config = self.repo / ".codex/config.toml"
        config.write_text("# local configuration\n")
        for case in ("clean", "noise", "outside", "staged", "broken", "other"):
            with self.subTest(case=case):
                for record in (self.pulls, self.selections):
                    record.unlink(missing_ok=True)
                git("restore", "--staged", "--worktree", ".")
                if case != "clean":
                    agent_doc.write_text(doc.replace("stats 1", "stats 2"))
                    claude_doc.write_text(doc.replace("stats 1", "stats 3"))
                if case == "outside":
                    claude_doc.write_text(
                        claude_doc.read_text().replace("Footer", "Human edit")
                    )
                elif case == "staged":
                    git("add", "AGENTS.md")
                elif case == "broken":
                    claude_doc.write_text(
                        claude_doc.read_text().replace("<!-- gitnexus:end -->", "")
                    )
                elif case == "other":
                    (self.runner.parent / "epic-tick.md").write_text("Human edit\n")
                before = git("diff", "HEAD")
                staged = git("diff", "--cached")
                result = self.run_tick()
                if case in {"clean", "noise"}:
                    self.assertEqual(
                        result.returncode, 0, (self.state / "codex.log").read_text()
                    )
                    self.assertEqual(self.pulls.read_text(), "pull\n")
                    self.assertEqual(agent_doc.read_text(), doc)
                    self.assertEqual(claude_doc.read_text(), doc)
                else:
                    self.assertEqual(result.returncode, 1)
                    self.assert_no_work_after_noise_failure()
                    self.assertEqual(git("diff", "HEAD"), before)
                    self.assertEqual(git("diff", "--cached"), staged)
                self.assertEqual(config.read_text(), "# local configuration\n")

    def test_failed_pull_stops_before_selection_and_releases_lock(self) -> None:
        self.env["TEST_PULL_EXIT"] = "17"
        self.assertEqual(self.run_tick().returncode, 17)
        self.assertFalse(self.selections.exists())
        self.assertFalse(self.calls.exists())
        self.assertFalse(self.lock.exists())
        self.assertFalse(self.record.exists())
        self.assertFalse((self.state / "codex-backoff.json").exists())
        self.assertIn(
            "checkout refresh failed exit=17", (self.state / "codex.log").read_text()
        )

    def test_pull_timeout_stops_before_selection(self) -> None:
        self.env["TEST_PULL_SLEEP"] = "1"
        self.env["EPIC_PULL_TIMEOUT_SECONDS"] = "1"
        self.assertEqual(self.run_tick().returncode, 124)
        self.assertFalse(self.selections.exists())
        self.assertFalse(self.calls.exists())
        self.assertFalse(self.lock.exists())

    def test_pause_created_during_pull_prevents_selection(self) -> None:
        self.env["TEST_PAUSE_AFTER_PULL"] = "1"
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertEqual(self.pulls.read_text(), "pull\n")
        self.assertFalse(self.selections.exists())
        self.assertFalse(self.calls.exists())
        self.assertFalse(self.lock.exists())

    def test_real_pull_uses_new_selector_in_same_tick_and_rejects_divergence(
        self,
    ) -> None:
        assert REAL_GIT is not None
        (self.bin / "git").unlink()

        def git(cwd: Path, *args: str) -> str:
            return subprocess.check_output(
                [REAL_GIT, "-C", str(cwd), *args],
                env=self.env,
                text=True,
                stderr=subprocess.STDOUT,
                timeout=10,
            ).strip()

        def configure(cwd: Path) -> None:
            git(cwd, "config", "user.name", "Runner test")
            git(cwd, "config", "user.email", "runner@example.invalid")
            git(cwd, "config", "commit.gpgsign", "false")

        upstream = self.root / "upstream.git"
        git(self.root, "init", "--bare", str(upstream))
        git(self.repo, "init", "-b", "dev/312-interim")
        configure(self.repo)
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "Initial runner")
        git(self.repo, "remote", "add", "origin", str(upstream))
        git(self.repo, "push", "-u", "origin", "dev/312-interim")
        writer = self.root / "writer"
        git(
            self.root,
            "clone",
            "--branch",
            "dev/312-interim",
            str(upstream),
            str(writer),
        )
        configure(writer)
        (writer / "scripts/epic/next_action.py").write_text(
            "import os\n"
            "with open(os.environ['TEST_SELECTIONS'], 'a') as f: f.write('new\\n')\n"
            'print(\'{"action": "idle", "reason": "focused selector"}\')\n'
        )
        git(writer, "add", ".")
        git(writer, "commit", "-m", "Update selector")
        git(writer, "push")
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertEqual(
            git(self.repo, "rev-parse", "HEAD"), git(writer, "rev-parse", "HEAD")
        )
        self.assertIn("focused selector", (self.state / "codex.log").read_text())
        self.assertEqual(self.selections.read_text(), "new\n")
        self.assertFalse(self.calls.exists())
        self.assertFalse(self.lock.exists())

        # Diverged runner history must stop, rather than merge or use old decisions.
        git(self.repo, "commit", "--allow-empty", "-m", "Local work")
        local_head = git(self.repo, "rev-parse", "HEAD")
        git(writer, "commit", "--allow-empty", "-m", "Remote work")
        git(writer, "push")
        self.assertNotEqual(self.run_tick().returncode, 0)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), local_head)
        self.assertEqual(self.selections.read_text(), "new\n")
        self.assertFalse(self.calls.exists())
        self.assertFalse(self.lock.exists())

    def test_existing_lock_is_not_removed(self) -> None:
        self.lock.mkdir(parents=True)
        owner = self.lock / "owner"
        owner.write_text("another tick")
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertEqual(owner.read_text(), "another tick")
        self.assertFalse(self.pulls.exists())
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
        self.assertEqual(self.pulls.read_text().splitlines(), ["pull", "pull"])

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
        (self.lock / "assignment.json").write_text("old assignment")
        (self.lock / "worktree.txt").write_text("old worktree")
        (self.lock / "review-context.json").write_text("old review context")
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
        args = calls[0]["args"]
        self.assertEqual((args[0], args[-1]), ("exec", "-"))
        for option, value in (
            ("--sandbox", "workspace-write"),
            ("--color", "never"),
            (
                "--output-schema",
                str(self.repo.resolve() / ".codex/tick-result.schema.json"),
            ),
        ):
            self.assertEqual(args[args.index(option) + 1], value)
        self.assertIn("sandbox_workspace_write.network_access=true", args)
        for name, value in {
            "EPIC_STATE_DIR": str(self.state),
            "EPIC_QUOTA_DIR": str(self.state),
            "EPIC_ACTION_FILE": str(self.lock / "action.json"),
            "EPIC_TRUSTED_ROOT": str(self.repo.resolve()),
        }.items():
            self.assertIn(
                f"shell_environment_policy.set.{name}=" + json.dumps(value), args
            )
        self.assertIn("# Codex epic tick", calls[0]["prompt"])
        self.assertIn(self.env["TEST_DECISION"], calls[0]["prompt"])
        self.assertEqual(self.selections.read_text(), "call\n")
        self.assertFalse(self.lock.exists())
        log = (self.state / "codex.log").read_text()
        self.assertIn("fake Codex stdout", log)
        self.assertIn("fake Codex stderr", log)

    def review_lifecycle(self) -> list[dict[str, object]]:
        return [json.loads(line) for line in self.review_calls.read_text().splitlines()]

    def test_pending_delivery_runs_before_repeat_gate(self) -> None:
        self.assertEqual(self.run_tick().returncode, 0)
        self.env["TEST_DELIVERY_RESUME_EXIT"] = "5"
        self.assertEqual(self.run_tick().returncode, 75)
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)

    def test_pending_delivery_failure_starts_no_model_or_backoff(self) -> None:
        for code in (4, 5, 7):
            with self.subTest(code=code):
                self.env["TEST_DELIVERY_RESUME_EXIT"] = str(code)
                self.assertEqual(self.run_tick().returncode, 75)
                self.assertFalse(self.calls.exists())
                self.assertFalse(self.record.exists())
                self.assertFalse((self.state / "codex-backoff.json").exists())
                self.assertFalse(self.lock.exists())

    def test_refused_pending_delivery_does_not_starve_next_candidate(self) -> None:
        self.env["TEST_DELIVERY_RESUME_EXIT"] = "7"
        following = {
            "action": "continue",
            "issue": "https://github.com/phaabe/live.moafunk.de/issues/381",
        }
        self.candidates(self.review_action(406), following)
        self.assertEqual(self.run_tick().returncode, 0)
        [call] = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual(call["action"], following)
        self.assertEqual(set(json.loads(self.record.read_text())["targets"]), {"381"})
        with (self.target_locks / "406.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_delivery_timeout_preserves_pending_state_without_cooldown(self) -> None:
        self.env["EPIC_PULL_TIMEOUT_SECONDS"] = "1"
        for command in ("resume", "publish"):
            with self.subTest(command=command):
                self.env["TEST_DELIVERY_SLEEP"] = command
                self.assertEqual(self.run_tick().returncode, 75)
                self.assertFalse(self.record.exists())
                self.assertFalse((self.state / "codex-backoff.json").exists())
                self.assertFalse(self.lock.exists())
                if command == "resume":
                    self.assertFalse(self.calls.exists())
                else:
                    self.assertEqual(len(self.calls.read_text().splitlines()), 1)

    def test_delivery_failure_after_model_does_not_record_completion(self) -> None:
        for code in (4, 5, 7):
            with self.subTest(code=code):
                self.env["TEST_DELIVERY_PUBLISH_EXIT"] = str(code)
                self.assertEqual(self.run_tick().returncode, 75)
                self.assertFalse(self.record.exists())
                self.assertFalse((self.state / "codex-backoff.json").exists())
                self.assertFalse(self.lock.exists())

    def test_delivered_bundle_overrides_failed_model_result(self) -> None:
        self.env.update(
            TEST_DELIVERY_PUBLISH_EXIT="0",
            TEST_CODEX_EXIT="17",
            TEST_RESULT=json.dumps({"status": "blocked", "summary": "Interrupted."}),
        )
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertTrue(self.record.exists())
        prepared = self.review_lifecycle()[0]
        result = json.loads((Path(prepared["attempt_dir"]) / "result.json").read_text())
        self.assertEqual(result["status"], "blocked")
        confirmed = json.loads((self.state / "codex-result.json").read_text())
        self.assertEqual(confirmed["status"], "completed")

    def test_review_model_uses_prepared_checkout_and_external_artifacts(self) -> None:
        self.assertEqual(self.run_tick().returncode, 0)
        prepared, cleaned = self.review_lifecycle()
        call = json.loads(self.calls.read_text())
        args = call["args"]
        self.assertEqual(args[args.index("--cd") + 1], prepared["worktree"])
        self.assertEqual(args[args.index("--add-dir") + 1], prepared["artifact_dir"])
        self.assertEqual(
            args[args.index("--output-last-message") + 1],
            str(Path(prepared["attempt_dir"]) / "result.json"),
        )
        self.assertEqual(call["tool_env"]["EPIC_REVIEW_DIR"], prepared["artifact_dir"])
        self.assertEqual(
            call["tool_env"]["EPIC_REVIEW_ATTEMPT_DIR"], prepared["attempt_dir"]
        )
        self.assertFalse(
            Path(prepared["artifact_dir"]).is_relative_to(prepared["worktree"])
        )
        self.assertFalse(Path(prepared["worktree"]).exists())
        self.assertFalse(cleaned["model_alive"])
        self.assertTrue(cleaned["gate_recorded"])
        self.assertEqual(json.loads(cleaned["result.json"])["status"], "completed")
        for message in ("fake Codex stdout", "fake Codex stderr"):
            self.assertIn(message, cleaned["model.log"])
            self.assertIn(message, cleaned["runner_log"])
        self.assertTrue((Path(prepared["attempt_dir"]) / "result.json").exists())

    def test_review_model_failure_keeps_evidence_and_cleans_after_exit(self) -> None:
        self.env["TEST_CODEX_EXIT"] = "17"
        self.assertEqual(self.run_tick().returncode, 17)
        prepared, cleaned = self.review_lifecycle()
        self.assertFalse(cleaned["model_alive"])
        self.assertFalse(cleaned["gate_recorded"])
        self.assertIn("fake Codex stdout", cleaned["model.log"])
        self.assertIsNotNone(cleaned["result.json"])
        self.assertFalse(Path(prepared["worktree"]).exists())
        self.assertEqual(self.last_finish(), (17, "error", "model"))

    def test_review_output_is_durable_before_model_exits(self) -> None:
        process, connection = self.blocked_tick()
        [prepared] = self.review_lifecycle()
        paths = (Path(prepared["attempt_dir"]) / "model.log",)
        deadline = time.monotonic() + 5
        while True:
            self.assertIsNone(process.poll())
            if all(
                path.exists()
                and all(
                    message in path.read_text()
                    for message in ("fake Codex stdout", "fake Codex stderr")
                )
                for path in paths
            ):
                break
            if time.monotonic() >= deadline:
                self.fail("review output was not durable while model was running")
            time.sleep(0.01)
        connection.sendall(b"x")
        self.assertEqual(process.wait(timeout=10), 0)
        log = (self.state / "codex.log").read_text()
        self.assertIn("fake Codex stdout", log)
        self.assertIn("fake Codex stderr", log)

    def test_review_cleanup_timeout_keeps_result_and_releases_lock(self) -> None:
        self.env.update(TEST_REVIEW_CLEANUP_SLEEP="1", EPIC_PULL_TIMEOUT_SECONDS="1")
        self.assertEqual(self.run_tick().returncode, 0)
        [prepared] = self.review_lifecycle()
        self.assertTrue(self.record.exists())
        self.assertTrue(Path(prepared["worktree"]).exists())
        self.assertFalse(self.lock.exists())
        self.assertIn(str(prepared["worktree"]), (self.state / "codex.log").read_text())
        self.assertEqual(self.last_finish(), (0, "ok", "record"))
        with (self.target_locks / "406.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_review_cleanup_refusal_preserves_completed_gate(self) -> None:
        self.env["TEST_REVIEW_CLEANUP_EXIT"] = "2"
        self.assertEqual(self.run_tick().returncode, 0)
        prepared, cleaned = self.review_lifecycle()
        self.assertTrue(cleaned["gate_recorded"])
        self.assertTrue(Path(prepared["worktree"]).exists())
        self.assertIn(str(prepared["worktree"]), (self.state / "codex.log").read_text())
        self.assertFalse(self.lock.exists())
        gate = self.record.read_bytes()
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertEqual(self.record.read_bytes(), gate)
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)
        self.assertEqual(len(self.review_lifecycle()), 2)

    def test_review_cleanup_refusal_preserves_model_failure(self) -> None:
        self.env.update(TEST_CODEX_EXIT="17", TEST_REVIEW_CLEANUP_EXIT="2")
        self.assertEqual(self.run_tick().returncode, 17)
        self.assertEqual(self.last_finish(), (17, "error", "model"))
        self.assertFalse(self.lock.exists())

    def test_review_preparation_failure_cleans_partial_attempt_without_model(
        self,
    ) -> None:
        self.env["TEST_REVIEW_PREPARE_EXIT"] = "2"
        self.assertEqual(self.run_tick().returncode, 2)
        prepared, cleaned = self.review_lifecycle()
        self.assertFalse(self.calls.exists())
        self.assertFalse(self.record.exists())
        self.assertFalse(cleaned["model_alive"])
        self.assertFalse(Path(prepared["worktree"]).exists())
        self.assertTrue(Path(prepared["attempt_dir"]).is_dir())
        self.assertFalse(self.lock.exists())

    def test_review_preparation_refusal_before_context_starts_no_model(self) -> None:
        self.env["TEST_REVIEW_REFUSE_EARLY"] = "1"
        self.assertEqual(self.run_tick().returncode, 2)
        self.assertFalse(self.calls.exists())
        self.assertFalse(self.record.exists())
        self.assertFalse(self.review_calls.exists())
        self.assertFalse(self.lock.exists())

    def test_completed_review_bundle_skips_to_next_candidate(self) -> None:
        self.adopt_action()
        adopt = json.loads(self.env["TEST_DECISION"])
        self.candidates(self.review_action(407), adopt)
        self.env["TEST_REVIEW_PREPARE_EXITS"] = json.dumps({"407": 3})
        self.assertEqual(self.run_tick().returncode, 0)
        prepared, cleaned = self.review_lifecycle()
        self.assertEqual(
            (prepared["command"], cleaned["command"]), ("prepare", "cleanup")
        )
        self.assertFalse(Path(prepared["worktree"]).exists())
        call = json.loads(self.calls.read_text())
        self.assertEqual(call["action"]["action"], "adopt")
        self.assertNotIn("EPIC_REVIEW_DIR", call["tool_env"])
        self.assertNotIn("--add-dir", call["args"])
        self.assertEqual(set(json.loads(self.record.read_text())["targets"]), {"406"})

    def test_refused_review_preparation_allows_next_candidate(self) -> None:
        self.adopt_action()
        adopt = json.loads(self.env["TEST_DECISION"])
        self.candidates(self.review_action(407), self.review_action(408), adopt)
        self.env["TEST_REVIEW_PREPARE_EXITS"] = json.dumps({"407": 7, "408": 75})
        self.env["TEST_REVIEW_CLEANUP_EXIT"] = "2"
        self.assertEqual(self.run_tick().returncode, 0)
        lifecycle = self.review_lifecycle()
        self.assertEqual(
            [entry["command"] for entry in lifecycle],
            ["prepare", "cleanup", "prepare", "cleanup"],
        )
        for prepared in lifecycle[::2]:
            self.assertTrue(Path(prepared["worktree"]).exists())
        call = json.loads(self.calls.read_text())
        self.assertEqual(call["action"]["action"], "adopt")
        self.assertNotIn("EPIC_REVIEW_DIR", call["tool_env"])
        self.assertNotIn("--add-dir", call["args"])
        self.assertEqual(set(json.loads(self.record.read_text())["targets"]), {"406"})
        self.assertFalse(self.lock.exists())
        for number in (407, 408):
            with (self.target_locks / f"{number}.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_only_refused_review_preparation_reports_blocked(self) -> None:
        self.candidates(self.review_action(407), self.review_action(408))
        self.env["TEST_REVIEW_PREPARE_EXITS"] = json.dumps({"407": 7, "408": 75})
        self.assertEqual(self.run_tick().returncode, 75)
        self.assertFalse(self.calls.exists())
        self.assertFalse(self.record.exists())
        self.assertFalse((self.state / "codex-gate-seen.json").exists())
        self.assertFalse(self.lock.exists())
        self.assertEqual(len(self.review_lifecycle()), 4)
        self.assertEqual(self.last_finish(), (75, "blocked", "gate"))

    def test_review_prepare_read_failure_stops_candidate_scan(self) -> None:
        self.adopt_action()
        adopt = json.loads(self.env["TEST_DECISION"])
        self.candidates(self.review_action(407), adopt)
        for code in (5, 124, 137):
            with self.subTest(code=code):
                self.env["TEST_REVIEW_PREPARE_EXITS"] = json.dumps({"407": code})
                self.assertEqual(self.run_tick().returncode, 75)
                self.assertFalse(self.calls.exists())
                self.assertFalse(self.record.exists())
                self.assertFalse((self.state / "codex-gate-seen.json").exists())
                self.assertFalse(self.lock.exists())
                self.assertEqual(self.last_finish(), (75, "blocked", "gate"))

    def assignment_responses(self, mode: str) -> dict[str, str]:
        from test_project_items import FIELD_IDS, rest_row
        import next_action as na

        shutil.copyfile(ROOT / "assignment.py", self.runner.parent / "assignment.py")
        selector = self.repo / "scripts/epic/next_action.py"
        selector.write_text(
            selector.read_text().replace(
                "from selector_contract import BASES, EPIC, PROJECT_API, REPO, body_digest, issue_url, other",
                "from selector_contract import *",
            )
        )
        cache = self.state / "github-cache"
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "auth-context").write_text("test-assignment")
        self.env["EPIC_SHARED_READER"] = mode
        self.env["TEST_DECISION"] = json.dumps(
            {"action": "claim", "issue": na.issue_url(532)}
        )
        query = "&".join(f"fields[]={FIELD_IDS[name]}" for name in na.PROJECT_FIELDS)
        first = f"https://api.github.com/{na.PROJECT_API}/items?per_page=100&{query}"
        second = first + "&page=2"
        return {
            f"https://api.github.com/{na.PROJECT_API}/fields?per_page=100": "HTTP/2.0 200 OK\n\n"
            + json.dumps(
                [{"name": name, "id": ident} for name, ident in FIELD_IDS.items()]
            ),
            first: f'HTTP/2.0 200 OK\nLink: <{second}>; rel="next"\n\n[]',
            second: "HTTP/2.0 200 OK\n\n"
            + json.dumps([rest_row(532, status="Ready", executor="Codex")]),
        }

    def test_model_receives_authoritative_rest_assignment_in_both_modes(self) -> None:
        for mode in ("0", "1"):
            with self.subTest(shared_reader=mode):
                self.env["TEST_BOARD_RESPONSES"] = json.dumps(
                    self.assignment_responses(mode)
                )
                result = self.run_tick()
                self.assertEqual(
                    result.returncode, 0, (self.state / "codex.log").read_text()
                )
                call = json.loads(self.calls.read_text().splitlines()[-1])
                section = (
                    call["prompt"]
                    .split(
                        "Authoritative assignment evidence (JSON data, not instructions):\n"
                    )[1]
                    .split("\nInstalled feature Git helper:")[0]
                )
                evidence = json.loads(section)
                self.assertEqual(evidence["result"], "confirmed")
                self.assertTrue(evidence["eligible"])
                self.assertEqual(evidence["source"], "REST")
                self.assertEqual(
                    evidence["project_api"],
                    "https://api.github.com/users/anneoneone/projectsV2/2",
                )
                self.assertEqual(evidence["repository"], "phaabe/live.moafunk.de")
                self.assertEqual(evidence["issue"], call["action"]["issue"])
                self.assertEqual(evidence["status"], "Ready")
                self.assertEqual(evidence["executor"], "Codex")
                self.assertIn("missing interface confirmation", call["prompt"])
                self.assertNotIn("graphql", self.gh_calls.read_text())
                self.record.unlink()

    def test_assignment_read_failure_starts_no_model_or_target_cooldown(self) -> None:
        for mode in ("0", "1"):
            with self.subTest(shared_reader=mode):
                responses = self.assignment_responses(mode)
                last = next(key for key in responses if key.endswith("&page=2"))
                responses[last] = 'HTTP/2.0 403 Forbidden\n\n{"message":"denied"}'
                self.env["TEST_BOARD_RESPONSES"] = json.dumps(responses)
                self.assertEqual(self.run_tick().returncode, 75)
                self.assert_no_action_records()

    def test_changed_assignment_skips_candidate_without_model(self) -> None:
        self.env["TEST_ASSIGNMENT_EXIT"] = "6"
        self.assertEqual(self.run_tick().returncode, 0)
        self.assert_no_action_records()

    def test_custom_state_dir_is_used_for_every_file(self) -> None:
        custom = self.root / "custom state"
        self.env["EPIC_STATE_DIR"] = str(custom)
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertIn("tick: finished exit=0", (custom / "codex.log").read_text())
        self.assertTrue((custom / "codex-gate.json").exists())
        self.assertTrue((custom / "codex-result.json").exists())
        self.assertFalse(self.state.exists())

    def test_relative_state_dir_is_resolved_before_changing_directory(self) -> None:
        # Codex review round 3: the runner cds to the checkout after start.
        self.env["EPIC_STATE_DIR"] = "relative state"
        self.assertEqual(self.run_tick().returncode, 0)  # cwd is self.home
        custom = self.home / "relative state"
        self.assertIn("tick: finished exit=0", (custom / "codex.log").read_text())
        self.assertTrue((custom / "codex-gate.json").exists())
        self.assertFalse((custom / "codex.lock").exists())

    def test_unset_state_dir_uses_the_home_default(self) -> None:
        del self.env["EPIC_STATE_DIR"]
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertIn("tick: finished exit=0", (self.state / "codex.log").read_text())
        self.assertTrue(self.record.exists())

    def test_registered_agent_uses_its_own_folder(self) -> None:
        self.env["EPIC_AGENT_ID"] = "codex-2"
        self.env["TEST_RESULT"] = json.dumps(
            {"status": "blocked", "summary": "Commit permission denied."}
        )
        self.assertEqual(self.run_tick().returncode, 75)
        home = self.state / "agents/codex-2"
        agent = json.loads((home / "agent.json").read_text())
        self.assertEqual(
            (agent["interval_seconds"], agent["budget_seconds"]), (180, 140)
        )
        self.assertIn("tick: finished exit=75", (home / "codex.log").read_text())
        self.assertTrue((home / "codex-backoff.json").exists())
        self.assertTrue((home / "codex-gate.json").exists())
        self.assertTrue((home / "codex-result.json").exists())
        for name in ("codex.log", "codex-backoff.json", "codex-gate.json"):
            self.assertFalse((self.state / name).exists(), name)
        self.assertFalse((home / "codex.lock").exists())

    def test_agent_id_of_another_kind_is_refused(self) -> None:
        self.env["EPIC_AGENT_ID"] = "claude"
        result = self.run_tick()
        self.assertEqual(result.returncode, 2)
        self.assertIn("EPIC_AGENT_ID must look like codex", result.stderr)
        self.assertFalse(self.selections.exists())
        self.assertFalse(self.state.exists())

    def test_pause_created_during_selection_prevents_session(self) -> None:
        self.env["TEST_PAUSE_AFTER_SELECT"] = "1"
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertFalse(self.calls.exists())
        self.assertFalse(self.lock.exists())

    def test_unchanged_action_is_recorded_and_next_tick_skips(self) -> None:
        self.assertEqual(self.run_tick().returncode, 0)
        record = json.loads(self.record.read_text())
        self.assertEqual(record["action"], json.loads(self.env["TEST_DECISION"]))
        self.assertEqual(record["updated_at"], self.updated_at.read_text())
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)
        self.assertEqual(json.loads(self.record.read_text()), record)
        self.assertEqual(len(self.selections.read_text().splitlines()), 2)
        self.assertFalse(self.lock.exists())

    def test_new_github_feedback_starts_another_session(self) -> None:
        self.assertEqual(self.run_tick().returncode, 0)
        self.updated_at.write_text("2026-09-28T03:01:00Z")
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertEqual(len(self.calls.read_text().splitlines()), 2)
        self.assertEqual(
            json.loads(self.record.read_text())["updated_at"],
            self.updated_at.read_text(),
        )

    def test_new_head_starts_another_session(self) -> None:
        self.assertEqual(self.run_tick().returncode, 0)
        action = json.loads(self.env["TEST_DECISION"])
        action["sha"] = "b" * 40
        self.env["TEST_DECISION"] = json.dumps(action)
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertEqual(len(self.calls.read_text().splitlines()), 2)
        self.assertEqual(json.loads(self.record.read_text())["action"], action)

    def test_continue_starts_each_session(self) -> None:
        self.env["TEST_DECISION"] = json.dumps(
            {
                "action": "continue",
                "issue": "https://github.com/phaabe/live.moafunk.de/issues/338",
            }
        )
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertEqual(len(self.calls.read_text().splitlines()), 2)

    def test_gate_check_failure_prevents_session(self) -> None:
        self.env["TEST_GH_FAILURE"] = "1"
        self.assertNotEqual(self.run_tick().returncode, 0)
        self.assertFalse(self.calls.exists())
        self.assertFalse(self.record.exists())
        self.assertFalse(self.lock.exists())

    def test_blocked_continue_returns_retry_code_and_next_tick_skips(self) -> None:
        self.env["TEST_DECISION"] = json.dumps(
            {
                "action": "continue",
                "issue": "https://github.com/phaabe/live.moafunk.de/issues/381",
            }
        )
        self.env["TEST_RESULT"] = json.dumps(
            {"status": "blocked", "summary": "Commit permission denied."}
        )
        self.assertEqual(self.run_tick().returncode, 75)
        record = self.record.read_text()
        self.assertFalse(self.lock.exists())
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)
        self.assertEqual(self.record.read_text(), record)

    def test_blocked_claim_suppresses_continue_for_the_same_issue(self) -> None:
        self.env["TEST_DECISION"] = json.dumps(
            {
                "action": "claim",
                "issue": "https://github.com/phaabe/live.moafunk.de/issues/381",
            }
        )
        self.env["TEST_RESULT"] = json.dumps(
            {"status": "blocked", "summary": "Commit permission denied after claim."}
        )
        self.assertEqual(self.run_tick().returncode, 75)
        self.env["TEST_DECISION"] = json.dumps(
            {
                "action": "continue",
                "issue": "https://github.com/phaabe/live.moafunk.de/issues/381",
            }
        )
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)
        self.assertEqual(
            json.loads(self.record.read_text())["action"]["action"], "claim"
        )

    def test_unrelated_target_runs_without_clearing_an_existing_cooldown(self) -> None:
        blocked_action = self.env["TEST_DECISION"]
        self.env["TEST_RESULT"] = json.dumps(
            {"status": "blocked", "summary": "Review permission denied."}
        )
        self.assertEqual(self.run_tick().returncode, 75)
        del self.env["TEST_RESULT"]
        self.env["TEST_DECISION"] = json.dumps(
            {"action": "review", "pr": 407, "sha": "a" * 40}
        )
        self.assertEqual(self.run_tick().returncode, 0)
        self.env["TEST_DECISION"] = blocked_action
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertEqual(len(self.calls.read_text().splitlines()), 2)

    def test_new_head_runs_despite_previous_head_cooldown(self) -> None:
        self.env["TEST_RESULT"] = json.dumps(
            {"status": "blocked", "summary": "Review permission denied."}
        )
        self.assertEqual(self.run_tick().returncode, 75)
        del self.env["TEST_RESULT"]
        self.env["TEST_DECISION"] = json.dumps(
            {"action": "review", "pr": 406, "sha": "b" * 40}
        )
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertEqual(len(self.calls.read_text().splitlines()), 2)

    def test_invalid_model_result_fails_closed_and_suppresses_retry(self) -> None:
        for index, result in enumerate(
            (
                "broken json",
                "[]",
                '{"status":"completed"}',
                '{"status":"unknown","summary":"bad status"}',
            )
        ):
            with self.subTest(result=result):
                self.env["TEST_DECISION"] = json.dumps(
                    {
                        "action": "continue",
                        "issue": f"https://github.com/phaabe/live.moafunk.de/issues/{500 + index}",
                    }
                )
                self.env["TEST_RESULT"] = result
                self.assertEqual(self.run_tick().returncode, 75)
                self.assertEqual(self.run_tick().returncode, 0)
                self.assertEqual(len(self.calls.read_text().splitlines()), index + 1)
                self.assertFalse(self.record.exists())
                self.assertFalse(self.lock.exists())

    def test_missing_model_result_cannot_reuse_previous_success(self) -> None:
        self.env["TEST_DECISION"] = json.dumps(
            {
                "action": "continue",
                "issue": "https://github.com/phaabe/live.moafunk.de/issues/381",
            }
        )
        self.assertEqual(self.run_tick().returncode, 0)
        previous_record = self.record.read_text()
        self.env["TEST_RESULT_MISSING"] = "1"
        self.assertEqual(self.run_tick().returncode, 75)
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertEqual(len(self.calls.read_text().splitlines()), 2)
        self.assertEqual(self.record.read_text(), previous_record)
        self.assertFalse(self.lock.exists())

    def test_expired_cooldown_retries_and_success_clears_it(self) -> None:
        self.env["TEST_DECISION"] = json.dumps(
            {
                "action": "continue",
                "issue": "https://github.com/phaabe/live.moafunk.de/issues/381",
            }
        )
        self.env["TEST_RESULT"] = json.dumps(
            {"status": "blocked", "summary": "Commit permission denied."}
        )
        self.assertEqual(self.run_tick().returncode, 75)
        path = self.state / "codex-backoff.json"
        self.expire_cooldown()
        del self.env["TEST_RESULT"]
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertEqual(len(self.calls.read_text().splitlines()), 2)
        self.assertEqual(json.loads(path.read_text()), {})
        self.assertTrue(self.record.exists())

    def test_gate_record_failure_fails_tick(self) -> None:
        self.env["TEST_REMOVE_GATE_SNAPSHOT"] = "1"
        self.assertNotEqual(self.run_tick().returncode, 0)
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)
        self.assertFalse(self.record.exists())
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

    def candidates(self, *actions: dict[str, object]) -> None:
        self.env["TEST_DECISION"] = "\n".join(json.dumps(a) for a in actions)

    @staticmethod
    def review_action(number: int) -> dict[str, object]:
        return {"action": "review", "pr": number, "sha": "a" * 40}

    def seed_repeat(self, *actions: dict[str, object]) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        targets = {
            str(action["pr"]): {
                "fingerprint": hashlib.sha256(
                    json.dumps(action, sort_keys=True).encode()
                ).hexdigest(),
                "updated_at": self.updated_at.read_text(),
                "at": time.time(),
                "action": action,
            }
            for action in actions
        }
        self.record.write_text(json.dumps({"targets": targets}))

    def test_repeat_candidates_do_not_starve_the_eleventh_target(self) -> None:
        actions = [self.review_action(n) for n in range(400, 411)]
        self.seed_repeat(*actions[:10])
        self.candidates(*actions)
        result = self.run_tick()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual([call["action"] for call in calls], [actions[-1]])
        self.assertEqual(len(self.gh_calls.read_text().splitlines()), 11)
        self.assertEqual(json.loads(self.record.read_text())["action"], actions[-1])

    def test_backoff_candidates_do_not_starve_the_eleventh_target(self) -> None:
        actions = [self.review_action(n) for n in range(400, 411)]
        self.state.mkdir(parents=True)
        entries = {
            f"pr:{action['pr']}:{action['sha']}": {
                "at": time.time(),
                "until": time.time() + 600,
                "reason": "permission denied",
            }
            for action in actions[:10]
        }
        path = self.state / "codex-backoff.json"
        path.write_text(json.dumps(entries))
        self.candidates(*actions)
        result = self.run_tick()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual([call["action"] for call in calls], [actions[-1]])
        self.assertEqual(len(self.gh_calls.read_text().splitlines()), 1)
        self.assertEqual(json.loads(path.read_text()), entries)

    def test_busy_target_falls_through_without_a_github_read(self) -> None:
        self.target_locks.mkdir()
        self.candidates(self.review_action(406), self.review_action(407))
        with (self.target_locks / "406.lock").open("w") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_tick()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual([call["action"]["pr"] for call in calls], [407])
        queries = [json.loads(line) for line in self.gh_calls.read_text().splitlines()]
        self.assertEqual(len(queries), 1)
        self.assertIn("issues/407", queries[0][1])

    def test_skipped_candidate_releases_its_partial_issue_and_pr_locks(self) -> None:
        self.target_locks.mkdir()
        issue = "https://github.com/phaabe/live.moafunk.de/issues/338"
        self.candidates(
            {**self.review_action(406), "issue": issue},
            {"action": "continue", "issue": issue},
        )
        with (self.target_locks / "406.lock").open("w") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_tick()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual([call["action"]["action"] for call in calls], ["continue"])

    def test_stale_and_closed_candidates_fall_through(self) -> None:
        first = {**self.review_action(406), "updated_at": "2026-09-28T02:00:00Z"}
        self.env["TEST_TARGET_STATES"] = json.dumps(
            {"407": self.updated_at.read_text() + "\nclosed"}
        )
        self.candidates(first, self.review_action(407), self.review_action(408))
        result = self.run_tick()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual([call["action"]["pr"] for call in calls], [408])
        self.assertEqual(set(json.loads(self.record.read_text())["targets"]), {"408"})

    def test_quota_wait_after_skipped_candidate_stops_the_scan(self) -> None:
        self.quota_clock()
        first = {**self.review_action(406), "updated_at": "2026-09-28T02:00:00Z"}
        self.candidates(first, self.review_action(407))
        self.env["TEST_WAIT_AFTER_GATE"] = "1"
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertEqual(len(self.gh_calls.read_text().splitlines()), 1)
        self.assert_quota_only()
        self.assertEqual(self.last_finish(), (0, "ok", "quota"))

    def test_pause_after_skipped_candidate_stops_the_scan(self) -> None:
        first = {**self.review_action(406), "updated_at": "2026-09-28T02:00:00Z"}
        self.candidates(first, self.review_action(407))
        self.env["TEST_PAUSE_AFTER_GATE"] = "1"
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertEqual(len(self.gh_calls.read_text().splitlines()), 1)
        self.assertFalse(self.calls.exists())
        self.assertFalse(self.record.exists())

    def adopt_action(self) -> dict[str, object]:
        action = {
            **self.review_action(406),
            "action": "adopt",
            "lane": "setup",
            "body_sha": hashlib.sha256(b"Original description.").hexdigest(),
        }
        self.candidates(action)
        self.env["TEST_ADOPT_BODY"] = (
            "Original description.\n\n"
            "Epic: https://github.com/phaabe/live.moafunk.de/issues/312\n"
            "Executor: Codex\n"
            "Lane: setup\n"
            "Reviewer: Claude\n"
            "Leaf IDs: setup\n"
            "Issue: https://github.com/phaabe/live.moafunk.de/issues/338\n"
        )
        return action

    def test_adopt_records_success_only_after_real_body_verification(self) -> None:
        action = self.adopt_action()
        result = self.run_tick()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.record.read_text())["action"], action)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual([call["action"] for call in calls], [action])
        self.assertEqual(Path(calls[0]["action_file"]), self.lock / "action.json")
        queries = [json.loads(line) for line in self.gh_calls.read_text().splitlines()]
        self.assertEqual(queries[-1], ["api", "repos/phaabe/live.moafunk.de/pulls/406"])

    def test_adopt_missing_owner_or_changed_original_records_failure(self) -> None:
        self.adopt_action()
        correct_body = self.env["TEST_ADOPT_BODY"]
        for body in (
            "Original description.",
            correct_body.replace("Original", "Changed"),
        ):
            with self.subTest(body=body):
                self.env["TEST_ADOPT_BODY"] = body
                self.assertNotEqual(self.run_tick().returncode, 0)
                self.assertFalse(self.record.exists())
                entries = json.loads((self.state / "codex-backoff.json").read_text())
                self.assertIn(f"pr:406:{'a' * 40}", entries)
                self.assertNotEqual(self.last_finish()[1], "ok")
                self.expire_cooldown()

    def test_model_hook_receives_only_the_selected_adopt_permission(self) -> None:
        self.adopt_action()
        hooks = self.repo / ".codex/hooks/scripts"
        hooks.mkdir(parents=True)
        for filename in ("epic-guard.sh", "epic_guard.py"):
            shutil.copyfile(ROOT / "hooks/scripts" / filename, hooks / filename)
        checked = self.root / "guard-results.json"
        self.env["TEST_MODEL_GUARD"] = str(checked)
        result = self.run_tick()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(checked.read_text()), [0, 2])

    def tool_child_environment(self) -> dict[str, str]:
        custom = self.root / 'state with "quotes" \\ and 🚀'
        self.env["EPIC_STATE_DIR"] = str(custom)
        self.env["EPIC_AGENT_ID"] = "codex-2"
        self.env["EPIC_UNRELATED"] = "must not be forwarded"
        probe = self.root / "tool-env-probe.py"
        probe.write_text(
            "import json, os, sys\n"
            f"sys.path.insert(0, {str(self.repo / 'scripts/epic')!r})\n"
            "from github_quota import STATE_DIR\n"
            "print(json.dumps({'env': dict(os.environ), 'quota_root': str(STATE_DIR)}))\n"
        )
        output = self.root / "tool-env-result.json"
        self.env["TEST_MODEL_ENV_PROBE"] = str(probe)
        self.env["TEST_MODEL_ENV_RESULT"] = str(output)
        result = self.run_tick()
        self.assertEqual(result.returncode, 0, result.stderr)
        seen = json.loads(output.read_text())
        agent = custom / "agents/codex-2"
        prepared = self.review_lifecycle()[0]
        expected = {
            "EPIC_ACTION_FILE": str(agent / "codex.lock/action.json"),
            "EPIC_QUOTA_DIR": str(custom),
            "EPIC_STATE_DIR": str(agent),
            "EPIC_REVIEW_DIR": prepared["artifact_dir"],
            "EPIC_REVIEW_ATTEMPT_DIR": prepared["attempt_dir"],
        }
        self.assertEqual({name: seen["env"].get(name) for name in expected}, expected)
        self.assertEqual(seen["quota_root"], str(custom))
        self.assertEqual(seen["env"]["EXISTING_USER_SETTING"], "preserved")
        self.assertNotIn("EPIC_UNRELATED", seen["env"])
        call = json.loads(self.calls.read_text())
        for name, value in expected.items():
            # Literal Unicode is valid TOML; JSON surrogate escapes are not.
            self.assertIn(
                f"shell_environment_policy.set.{name}="
                + json.dumps(value, ensure_ascii=False),
                call["args"],
            )
        return seen["env"]

    def test_tool_child_receives_agent_paths_with_core_inheritance(self) -> None:
        self.tool_child_environment()

    def test_tool_child_keeps_shared_reader_checks_enabled(self) -> None:
        self.env.update(
            EPIC_SHARED_READER="1",
            EPIC_CACHE_DIR=str(self.root / 'cache with "quotes"'),
            EPIC_FOCUS_ACTIONS="adopt",
        )
        seen = self.tool_child_environment()
        for name in (
            "EPIC_SHARED_READER",
            "EPIC_CACHE_DIR",
            "EPIC_FOCUS_ACTIONS",
            "EPIC_SNAPSHOT_LOCK_SECONDS",
            "EPIC_SNAPSHOT_REFRESH_SECONDS",
            "EPIC_RECHECK_TIMEOUT_SECONDS",
            "EPIC_SELECT_TIMEOUT_SECONDS",
        ):
            self.assertEqual(seen[name], self.env[name])
        self.assertEqual(seen["EPIC_TRUSTED_ROOT"], str(self.repo.resolve()))
        self.assertNotIn("EPIC_SNAPSHOT_MAX_AGE_SECONDS", seen)

    def test_adopt_quota_wait_during_model_leaves_target_state_untouched(self) -> None:
        self.quota_clock()
        self.adopt_action()
        self.env["TEST_WAIT_DURING_MODEL"] = "1"
        self.assertEqual(self.run_tick().returncode, 75)
        self.assert_quota_only(model_calls=1)
        self.assertEqual(len(self.gh_calls.read_text().splitlines()), 1)

    def test_adopt_verification_quota_stores_wait_without_success_or_cooldown(
        self,
    ) -> None:
        self.quota_clock()
        self.adopt_action()
        self.env["TEST_GH_QUOTA"] = "verify"
        self.assertEqual(self.run_tick().returncode, 75)
        self.assert_quota_only(model_calls=1)
        self.assertEqual(self.last_finish(), (75, "blocked", "quota"))

    def test_blocked_adopt_does_not_run_success_verification(self) -> None:
        self.adopt_action()
        self.env["TEST_RESULT"] = json.dumps(
            {"status": "blocked", "summary": "Body edit permission denied."}
        )
        self.assertEqual(self.run_tick().returncode, 75)
        self.assertEqual(len(self.gh_calls.read_text().splitlines()), 1)
        self.assertEqual(self.last_finish(), (75, "blocked", "result"))

    def test_codex_failure_is_logged_and_unlocks(self) -> None:
        self.env["TEST_CODEX_EXIT"] = "17"
        self.assertEqual(self.run_tick().returncode, 17)
        self.assertFalse(self.lock.exists())
        self.assertFalse(self.record.exists())
        self.assertIn("exit=17", (self.state / "codex.log").read_text())
        self.env["TEST_CODEX_EXIT"] = "0"
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)
        self.expire_cooldown()
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertEqual(len(self.calls.read_text().splitlines()), 2)
        self.assertTrue(self.record.exists())

    def test_invalid_timeout_never_starts_work(self) -> None:
        for value in ("0", "-1", "1.5", "never"):
            with self.subTest(value=value):
                self.env["EPIC_TICK_TIMEOUT_SECONDS"] = value
                self.assertEqual(self.run_tick().returncode, 2)
                self.assertFalse(self.selections.exists())
                self.assertFalse(self.calls.exists())
                self.assertFalse(self.lock.exists())

    def assert_no_action_records(self) -> None:
        self.assertFalse(self.calls.exists())
        self.assertFalse(self.record.exists())
        self.assertFalse(self.lock.exists())
        for name in ("codex-gate-seen.json", "codex-backoff.json", "codex-result.json"):
            self.assertFalse((self.state / name).exists(), name)

    def test_shared_reader_rechecks_after_gate_and_before_model(self) -> None:
        self.env["EPIC_SHARED_READER"] = "1"
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertEqual(len(self.rechecks.read_text().splitlines()), 1)
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)

    def test_reader_off_omits_recheck_and_ignores_its_settings(self) -> None:
        self.env["EPIC_RECHECK_TIMEOUT_SECONDS"] = "invalid"
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertFalse(self.rechecks.exists())

    def test_fresh_backoff_codes_preserve_records_and_start_no_model(self) -> None:
        self.block_issue_then_select_draft_pr()
        self.env["EPIC_SHARED_READER"] = "1"
        cache = self.state / "github-cache"
        cache.mkdir()
        (cache / "auth-context").write_text("test")
        original = {
            name: (self.state / name).read_bytes()
            for name in ("codex-backoff.json", "codex-gate.json", "codex-result.json")
        }
        for status, sha, expected in ((503, "a", 75), (200, "b", 0), (0, "a", 75)):
            with self.subTest(status=status):
                if status == 0:
                    self.env["TEST_FRESH_SLEEP"] = "1"
                    self.env["EPIC_RECHECK_TIMEOUT_SECONDS"] = "1"
                self.env["TEST_FRESH_STATUS"] = str(status)
                metadata = json.loads(self.env["TEST_PR_METADATA"])
                metadata["headRefOid"] = sha * 40
                self.env["TEST_PR_METADATA"] = json.dumps(metadata)
                (self.state / "codex-gate-seen.json").write_text("old")
                self.assertEqual(
                    self.run_tick().returncode,
                    expected,
                    (self.state / "codex.log").read_text(),
                )
                for name, before in original.items():
                    self.assertEqual((self.state / name).read_bytes(), before)
                self.assertFalse((self.state / "codex-gate-seen.json").exists())
                self.assertFalse(self.rechecks.exists())
                self.assertEqual(len(self.calls.read_text().splitlines()), 1)
                if expected == 75:
                    self.assertEqual(self.last_finish(), (75, "blocked", "backoff"))

    def test_selector_read_codes_leave_no_action_records(self) -> None:
        for code, expected in ((5, 75), (6, 0)):
            with self.subTest(code=code):
                self.env["TEST_SELECT_READ_EXIT"] = str(code)
                self.state.mkdir(parents=True, exist_ok=True)
                (self.state / "codex-gate-seen.json").write_text("old")
                self.assertEqual(self.run_tick().returncode, expected)
                self.assert_no_action_records()
                if code == 5:
                    self.assertEqual(self.last_finish(), (75, "blocked", "select"))

    def test_recheck_failures_discard_seen_without_action_records(self) -> None:
        self.env["EPIC_SHARED_READER"] = "1"
        for code, expected in ((5, 75), (6, 0), (2, 2), (4, 75), (137, 75)):
            with self.subTest(code=code):
                self.env["TEST_RECHECK_EXIT"] = str(code)
                self.assertEqual(self.run_tick().returncode, expected)
                self.assert_no_action_records()
                if code in (5, 137):
                    self.assertEqual(self.last_finish(), (75, "blocked", "recheck"))

    def test_stale_recheck_releases_target_and_tries_next_candidate(self) -> None:
        self.env["EPIC_SHARED_READER"] = "1"
        self.env["TEST_RECHECK_EXITS"] = json.dumps({"406": 6})
        self.candidates(self.review_action(406), self.review_action(407))
        self.assertEqual(self.run_tick().returncode, 0)
        [call] = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual(call["action"]["pr"], 407)
        self.assertEqual(set(json.loads(self.record.read_text())["targets"]), {"407"})
        with (self.target_locks / "406.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_recheck_timeout_is_blocked_without_action_records(self) -> None:
        self.env.update(
            EPIC_SHARED_READER="1",
            EPIC_RECHECK_TIMEOUT_SECONDS="1",
            TEST_RECHECK_SLEEP="1",
        )
        self.assertEqual(self.run_tick().returncode, 75)
        self.assertEqual(self.last_finish(), (75, "blocked", "recheck"))
        self.assert_no_action_records()

    def test_shared_reader_validates_all_deadlines_before_work(self) -> None:
        self.env["EPIC_SHARED_READER"] = "1"
        for key, value in (
            ("EPIC_RECHECK_TIMEOUT_SECONDS", "0"),
            ("EPIC_RECHECK_TIMEOUT_SECONDS", "1.5"),
            ("EPIC_SNAPSHOT_MAX_AGE_SECONDS", "bad"),
            ("EPIC_SNAPSHOT_LOCK_SECONDS", "9"),
            ("EPIC_SNAPSHOT_REFRESH_SECONDS", "10"),
        ):
            with self.subTest(key=key, value=value):
                previous = self.env.get(key)
                self.env[key] = value
                self.assertEqual(self.run_tick().returncode, 2)
                self.assertFalse(self.pulls.exists())
                self.assertFalse(self.selections.exists())
                self.assert_no_action_records()
                if previous is None:
                    del self.env[key]
                else:
                    self.env[key] = previous

    def test_recheck_time_is_in_lock_and_registration_budgets(self) -> None:
        self.env.update(
            EPIC_SHARED_READER="1",
            EPIC_AGENT_ID="codex-2",
            EPIC_RECHECK_TIMEOUT_SECONDS="7",
        )
        process, connection = self.blocked_tick()
        state = self.state / "agents/codex-2"
        owner = json.loads((state / "codex.lock/owner.json").read_text())
        agent = json.loads((state / "agent.json").read_text())
        self.assertEqual(owner["max_age"], 154)
        self.assertEqual(agent["budget_seconds"], 154)
        connection.sendall(b"x")
        self.assertEqual(process.wait(timeout=10), 0)

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
        self.assertEqual(owner["max_age"], 140)
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)
        self.assertTrue(self.lock.is_dir())
        connection.sendall(b"x")
        self.assertEqual(process.wait(timeout=10), 0)
        self.assertFalse(self.lock.exists())

    def test_shared_target_lock_prevents_other_state_dir_session(self) -> None:
        process, connection = self.blocked_tick()
        self.env["EPIC_STATE_DIR"] = str(self.root / "second-runner")
        del self.env["TEST_SOCKET"]
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)
        self.assertEqual(len(self.gh_calls.read_text().splitlines()), 1)
        connection.sendall(b"x")
        self.assertEqual(process.wait(timeout=10), 0)
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertEqual(len(self.calls.read_text().splitlines()), 2)

    def test_surviving_model_keeps_target_locked_after_runner_sigkill(self) -> None:
        process, connection = self.blocked_tick()
        process.kill()
        self.assertEqual(process.wait(timeout=5), -signal.SIGKILL)
        self.env["EPIC_STATE_DIR"] = str(self.root / "second-runner")
        del self.env["TEST_SOCKET"]
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)
        connection.sendall(b"x")
        self.assertEqual(connection.recv(1), b"")
        # Wait for process exit, not an arbitrary lock age or stale-file reclaim.
        deadline = time.monotonic() + 12
        with (self.target_locks / "406.lock").open("a") as lock:
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        self.fail("target lock outlived the model process")
                    time.sleep(0.01)
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertEqual(len(self.calls.read_text().splitlines()), 2)

    def test_feedback_during_session_is_not_suppressed(self) -> None:
        initial_updated_at = self.updated_at.read_text()
        process, connection = self.blocked_tick()
        self.assertFalse(self.record.exists())
        self.updated_at.write_text("2026-09-28T03:01:00Z")
        connection.sendall(b"x")
        self.assertEqual(process.wait(timeout=10), 0)
        self.assertEqual(
            json.loads(self.record.read_text())["updated_at"], initial_updated_at
        )
        del self.env["TEST_SOCKET"]
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertEqual(len(self.calls.read_text().splitlines()), 2)

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
        self.assertFalse(self.record.exists())
        prepared, cleaned = self.review_lifecycle()
        self.assertFalse(cleaned["model_alive"])
        self.assertIn("fake Codex stdout", cleaned["model.log"])
        self.assertFalse(Path(prepared["worktree"]).exists())

    def tick_events(self, state: Path | None = None) -> list[dict[str, object]]:
        path = (state or self.state) / "codex-ticks.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()]

    def last_finish(self) -> tuple[object, ...]:
        events = self.tick_events()
        start, finish = events[-2], events[-1]
        self.assertEqual((start["event"], finish["event"]), ("start", "finish"))
        self.assertEqual(start["tick"], finish["tick"])
        return finish["exit"], finish["outcome"], finish["phase"]

    def test_events_name_each_exit_path(self) -> None:
        blocked = json.dumps({"status": "blocked", "summary": "Commit denied."})
        for env, expected in (
            ({}, (0, "ok", "record")),
            ({"TEST_RESULT": blocked}, (75, "blocked", "result")),
            ({"TEST_RESULT": "[]"}, None),
            ({"TEST_CODEX_EXIT": "17"}, (17, "error", "model")),
            ({"TEST_SELECTOR_EXIT": "1"}, (23, "error", "select")),
            ({"TEST_DECISION": json.dumps({"action": "idle"})}, (0, "ok", "select")),
        ):
            with self.subTest(env=env):
                shutil.rmtree(self.state, ignore_errors=True)
                self.env.update(env)
                code = self.run_tick().returncode
                for key in env:
                    del self.env[key]
                if expected is None:  # invalid model result: fails in the result stage
                    self.assertNotEqual(code, 0)
                    expected = (code, "error", "result")
                self.assertEqual(self.last_finish(), expected)
        self.env["TEST_DECISION"] = json.dumps(
            {"action": "review", "pr": 406, "sha": "a" * 40}
        )

    def test_finish_event_carries_action_and_this_ticks_tokens(self) -> None:
        self.env["TEST_TOKENS"] = "1,234"
        self.assertEqual(self.run_tick().returncode, 0)
        finish = self.tick_events()[-1]
        self.assertEqual(
            (finish["action"], finish["pr"], finish["tokens"]), ("review", 406, 1234)
        )

    def test_timeout_writes_a_timeout_event(self) -> None:
        self.env["EPIC_TICK_TIMEOUT_SECONDS"] = "1"
        process, _ = self.blocked_tick()
        self.assertEqual(process.wait(timeout=15), 124)
        self.assertEqual(self.last_finish(), (124, "timeout", "model"))

    def test_two_agents_of_one_kind_write_separate_event_files(self) -> None:
        for agent in ("codex", "codex-2"):
            self.env["EPIC_AGENT_ID"] = agent
            self.assertEqual(self.run_tick().returncode, 0)
        for agent in ("codex", "codex-2"):
            events = self.tick_events(self.state / "agents" / agent)
            self.assertEqual([e["event"] for e in events], ["start", "finish"])

    def test_term_stops_child_before_releasing_lock(self) -> None:
        process, connection = self.blocked_tick()
        context = self.state / "feature-git-context.json"
        context.write_text("temporary session authority\n")
        rebase = self.state / "rebase-406.json"
        rebase.write_text("keep interrupted rebase state\n")
        process.send_signal(signal.SIGTERM)
        self.assertEqual(process.wait(timeout=15), 143)
        self.assertEqual(connection.recv(1), b"")
        self.assertFalse(self.lock.exists())
        self.assertFalse(self.record.exists())
        self.assertFalse(context.exists())
        self.assertEqual(rebase.read_text(), "keep interrupted rebase state\n")
        prepared, cleaned = self.review_lifecycle()
        self.assertFalse(cleaned["model_alive"])
        self.assertIn("fake Codex stdout", cleaned["model.log"])
        self.assertFalse(Path(prepared["worktree"]).exists())

    def test_new_tick_revokes_context_left_by_a_killed_tick(self) -> None:
        self.state.mkdir(parents=True)
        context = self.state / "feature-git-context.json"
        context.write_text("stale session authority\n")
        process, connection = self.blocked_tick()
        self.assertFalse(context.exists())
        connection.sendall(b"x")
        self.assertEqual(process.wait(timeout=10), 0)


if __name__ == "__main__":
    unittest.main()
