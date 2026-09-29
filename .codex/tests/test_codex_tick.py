"""Run the real Bash wrapper with isolated state and fake agent/API boundaries."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
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
        self.lock = self.state / "codex.lock"
        self.record = self.state / "codex-gate.json"
        self.updated_at = self.root / "updated-at"
        self.updated_at.write_text("2026-09-28T03:00:00Z")
        self.calls = self.root / "codex-calls.jsonl"
        self.gh_calls = self.root / "gh-calls.jsonl"
        self.pr_queries = self.root / "pr-queries.jsonl"
        self.selections = self.root / "selector-calls"
        self.pulls = self.root / "pull-calls"
        self.runner = self.repo / ".codex/codex-tick.sh"
        self.runner.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / "codex-tick.sh", self.runner)
        shutil.copyfile(ROOT / "epic-tick.md", self.runner.parent / "epic-tick.md")
        shutil.copyfile(ROOT / "epic_lock.py", self.runner.parent / "epic_lock.py")
        for filename in ("tick_backoff.py", "tick-result.schema.json"):
            shutil.copyfile(ROOT / filename, self.runner.parent / filename)
        selector = self.repo / "scripts/epic/next_action.py"
        selector.parent.mkdir(parents=True)
        for helper in (
            "tick_gate.py",
            "github_quota.py",
            "agents.py",
            "tick_events.py",
        ):
            shutil.copyfile(
                ROOT.parent / "scripts/epic" / helper, selector.parent / helper
            )
        selector.write_text(
            "import os, pathlib, sys, time\n"
            "from github_quota import QuotaExhausted, record, stop_on_quota\n"
            "assert sys.argv[1:] == ['--agent', 'codex']\n"
            "assert pathlib.Path(os.environ['TEST_PULLS']).exists()\n"
            "with open(os.environ['TEST_SELECTIONS'], 'a') as f: f.write('call\\n')\n"
            "if os.environ.get('TEST_SELECTOR_EXIT'): sys.exit(23)\n"
            "if os.environ.get('TEST_SELECT_QUOTA'):\n"
            "    sys.exit(stop_on_quota(QuotaExhausted('selector exhausted')))\n"
            "if os.environ.get('TEST_WAIT_AFTER_SELECT'):\n"
            "    record(pathlib.Path(os.environ['EPIC_QUOTA_DIR']), time.time(), os.environ['TEST_RESET'])\n"
            "if os.environ.get('TEST_PAUSE_AFTER_SELECT'):\n"
            "    (pathlib.Path.home() / '.epic-pause').touch()\n"
            "print(os.environ['TEST_DECISION'])\n"
        )
        self.bin = self.root / "bin"
        self.bin.mkdir()
        git = self.bin / "git"
        git.write_text(
            "#!/usr/bin/env python3\n"
            "import os, pathlib, sys, time\n"
            "assert sys.argv[1:] == ['pull', '--ff-only']\n"
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
            "if sys.argv[1:3] == ['api', 'graphql']:\n"
            "    assert sys.argv[3:] == ['-f', 'query=query{rateLimit{resetAt}}']\n"
            "    print(json.dumps({'data': {'rateLimit': {'resetAt': os.environ['TEST_RESET']}}}))\n"
            "    sys.exit(0)\n"
            "boundary = 'pr' if sys.argv[1:3] == ['pr', 'view'] else 'gate'\n"
            "if sys.argv[1:3] == ['pr', 'list']: boundary = 'selector'\n"
            "if os.environ.get('TEST_GH_QUOTA') == boundary:\n"
            "    print(json.dumps({'errors': [{'type': 'RATE_LIMITED'}]}))\n"
            "    sys.exit(0)\n"
            "if os.environ.get('TEST_WAIT_AFTER_GATE') and boundary == 'gate':\n"
            "    sys.path.insert(0, str(pathlib.Path(os.environ['TEST_REPO']) / 'scripts/epic'))\n"
            "    from github_quota import record\n"
            "    record(pathlib.Path(os.environ['EPIC_QUOTA_DIR']), time.time(), os.environ['TEST_RESET'])\n"
            "if os.environ.get('TEST_GH_FAILURE'): sys.exit(7)\n"
            "if sys.argv[1:3] == ['pr', 'view']:\n"
            "    assert sys.argv[4:] == ['--repo', 'phaabe/live.moafunk.de', "
            "'--json', 'body,headRefOid']\n"
            "    with open(os.environ['TEST_PR_QUERIES'], 'a') as f:\n"
            "        f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "    if os.environ.get('TEST_PR_FAILURE'): sys.exit(7)\n"
            "    print(os.environ['TEST_PR_METADATA'])\n"
            "    sys.exit(0)\n"
            "assert sys.argv[1] == 'api'\n"
            "print(pathlib.Path(os.environ['TEST_UPDATED_AT']).read_text())\n"
        )
        gh.chmod(0o755)
        codex = self.bin / "codex"
        codex.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, socket, sys\n"
            "with open(os.environ['TEST_CALLS'], 'a') as f:\n"
            "    f.write(json.dumps({'args': sys.argv[1:], 'prompt': sys.stdin.read()}) + '\\n')\n"
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
            "TEST_PULLS": str(self.pulls),
            "TEST_REPO": str(self.repo),
            "TEST_UPDATED_AT": str(self.updated_at),
            "TEST_DECISION": json.dumps(
                {"action": "review", "pr": 406, "sha": "a" * 40}
            ),
            "EPIC_TICK_TIMEOUT_SECONDS": "10",
            "EPIC_SELECT_TIMEOUT_SECONDS": "10",
            "EPIC_PULL_TIMEOUT_SECONDS": "10",
            "EPIC_STATE_DIR": str(self.state),
            "EPIC_REPEAT_TTL_SECONDS": "10800",
            "EPIC_BLOCKED_RETRY_SECONDS": "900",
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
                "--output-schema",
                str(self.repo.resolve() / ".codex/tick-result.schema.json"),
                "--output-last-message",
                str(self.state / "codex-result.json"),
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
            (agent["interval_seconds"], agent["budget_seconds"]), (180, 30)
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
        self.assertEqual(owner["max_age"], 40)
        self.assertEqual(self.run_tick().returncode, 0)
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)
        self.assertTrue(self.lock.is_dir())
        connection.sendall(b"x")
        self.assertEqual(process.wait(timeout=10), 0)
        self.assertFalse(self.lock.exists())

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
        process.send_signal(signal.SIGTERM)
        self.assertEqual(process.wait(timeout=15), 143)
        self.assertEqual(connection.recv(1), b"")
        self.assertFalse(self.lock.exists())
        self.assertFalse(self.record.exists())


if __name__ == "__main__":
    unittest.main()
