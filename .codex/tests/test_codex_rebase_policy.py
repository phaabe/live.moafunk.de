"""Real Codex runner, rebase policy and Git; only model/API boundaries are fake."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/epic"))
import isolated_env  # noqa: E402, F401 (before fixtures and production imports)
import rebase_policy as rp  # noqa: E402
import test_rebase_runner as fixture  # noqa: E402

MODEL = r"""#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys
root = pathlib.Path(os.environ['TEST_ROOT'])
state = pathlib.Path(os.environ['EPIC_STATE_DIR'])
mode = os.environ.get('TEST_MODEL_MODE', 'resolve')
(root / 'prompt.txt').write_text(sys.stdin.read())
(root / 'model-args.json').write_text(json.dumps(sys.argv[1:]))
with (root / 'models.jsonl').open('a') as output:
    output.write(json.dumps(mode) + '\n')
result = pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1])
def done(status='completed', quota=False):
    result.write_text(json.dumps({'status': status, 'summary': mode,
        'reason_code': 'github_rate_limit' if quota else None, 'retry_at': None}))
    raise SystemExit(0)
if mode in ('fail', 'review'):
    done()
if mode == 'quota':
    done('blocked', quota=True)
wt = pathlib.Path(os.environ['TEST_WORKTREE'])
action = json.loads(pathlib.Path(os.environ['EPIC_ACTION_FILE']).read_text())
pin = json.loads((state / 'rebase-attempt.json').read_text())
def git(*args, check=True):
    return subprocess.run(['git', '-C', str(wt), *args], check=check,
                          capture_output=True, text=True)
git('fetch', '-q', 'origin')
assert git('rebase', pin['tip'], check=False).returncode == 1
(wt / 'f.txt').write_text('one\nresolved\n')
git('add', 'f.txt')
git('-c', 'core.editor=true', 'rebase', '--continue')
head = git('rev-parse', 'HEAD').stdout.strip()
sys.path.insert(0, str(pathlib.Path(os.environ['EPIC_TRUSTED_ROOT']) / 'scripts/epic'))
import rebase_policy as rp
if mode != 'no-proof':
    rp.prove(wt, action['pr'], pin['base'], state)
if mode == 'stale-proof':
    path = rp.proof_path(state, action['pr'], head)
    proof = json.loads(path.read_text())
    proof['onto'] = action['sha']
    path.write_text(json.dumps(proof))
if mode == 'malformed-proof':
    path = rp.proof_path(state, action['pr'], head)
    proof = json.loads(path.read_text())
    proof['suites'] = [{'result': 'passed', 'exit': 0}]
    path.write_text(json.dumps(proof))
(state / 'codex-rebases.json').write_text(json.dumps({'feat/7-x': {
    'pr': action['pr'], 'sha': action['sha'], 'onto': pin['tip'],
    'conflicted': ['f.txt']}}))
git('push', '--force-with-lease=refs/heads/feat/7-x:' + action['sha'],
    'origin', 'HEAD:refs/heads/feat/7-x')
if mode in ('verify-read', 'verify-quota'):
    (root / 'verify-failure').write_text(mode)
done('blocked' if mode == 'resolve-blocked' else 'completed')
"""

GH_EXTRA = r"""
if args[:2] == ['pr', 'view'] and 'state,headRefOid' in args:
    failure = os.path.join(root, 'verify-failure')
    if os.path.exists(failure):
        if open(failure).read() == 'verify-quota':
            print(json.dumps({'errors': [{'type': 'RATE_LIMITED'}]}))
            sys.exit(0)
        print('HTTP 502', file=sys.stderr)
        sys.exit(1)
if args[:2] == ['api', 'graphql']:
    print(json.dumps({'data': {'rateLimit': {'resetAt': '2099-01-01T00:00:00Z'}}}))
    sys.exit(0)
if '--method' in args and '/comments' in line:
    mode = os.environ.get('TEST_MODEL_MODE')
    if mode == 'publish-quota':
        print(json.dumps({'errors': [{'type': 'RATE_LIMITED'}]}))
        sys.exit(0)
    if mode == 'missing-record':
        print('{}')
        sys.exit(0)
    if mode == 'invalid-record':
        index = args.index('-f') + 1
        args[index] = args[index].replace('PR: 7\n', 'PR: 8\n')
"""

GATE = r"""import json, os, pathlib, sys
state = pathlib.Path(os.environ['EPIC_STATE_DIR'])
record = state / 'codex-gate.json'
if sys.argv[1] == 'check':
    # A real repeat gate would retain this head for longer than the cooldown.
    if record.exists():
        sys.exit(3)
    (state / 'codex-gate-seen.json').write_text('{}')
else:
    record.write_text('{}')
"""

REVIEW_WORKTREE = r"""import json, os, pathlib, sys
if sys.argv[1] == 'cleanup':
    sys.exit(0)
state = pathlib.Path(os.environ['EPIC_STATE_DIR'])
artifact = state / 'review'
attempt = artifact / 'attempt'
attempt.mkdir(parents=True, exist_ok=True)
context = {'worktree': os.environ['TEST_WORKTREE'], 'artifact_dir': str(artifact),
           'attempt_dir': str(attempt)}
pathlib.Path(sys.argv[sys.argv.index('--context-file') + 1]).write_text(json.dumps(context))
print(context['worktree'])
"""


class CodexRebasePolicyTests(fixture.RebaseRunnerTest):
    def install_runner(self) -> None:
        super().install_runner()
        for relative in (
            ".codex/codex-tick.sh",
            ".codex/epic-tick.md",
            ".codex/tick-result.schema.json",
            ".codex/tick_backoff.py",
            "scripts/epic/github_state.py",
        ):
            shutil.copyfile(ROOT / relative, self.repo / relative)
        (self.repo / "scripts/epic/tick_gate.py").write_text(GATE)
        codex = self.repo / ".codex"
        (codex / "assignment.py").write_text(
            "import json, pathlib, sys\n"
            "pathlib.Path(sys.argv[sys.argv.index('--output') + 1]).write_text('{}')\n"
        )
        (codex / "feature_worktree.py").write_text(
            "import os\nprint(os.environ['TEST_WORKTREE'])\n"
        )
        (codex / "review_worktree.py").write_text(REVIEW_WORKTREE)
        (codex / "review_delivery.py").write_text(
            "import sys\nsys.exit(3 if sys.argv[1] == 'resume' else 0)\n"
        )
        (self.root / "bin/codex").write_text(MODEL)
        (self.root / "bin/codex").chmod(0o755)
        gh = fixture.GH.replace(
            "if '--method' in args and line.endswith('labels[]=needs-anton'):",
            GH_EXTRA
            + "\nif '--method' in args and line.endswith('labels[]=needs-anton'):",
        )
        (self.root / "bin/gh").write_text(gh)

    def tick(self, mode: str = "resolve", action: dict[str, Any] | None = None) -> int:
        return subprocess.run(
            ["/bin/bash", str(self.repo / ".codex/codex-tick.sh")],
            cwd=self.root,
            env={
                **self.env,
                "TEST_MODEL_MODE": mode,
                "TEST_CANDIDATES": json.dumps(action or self.action()),
            },
            capture_output=True,
            text=True,
            timeout=180,
        ).returncode

    def agent_state(self) -> Path:
        agent = self.env.get("EPIC_AGENT_ID")
        return self.state / "agents" / agent if agent else self.state

    def log(self) -> str:
        return (self.agent_state() / "codex.log").read_text()

    def expire_cooldowns(self) -> None:
        path = self.agent_state() / "codex-backoff.json"
        entries = json.loads(path.read_text())
        for entry in entries.values():
            entry["at"] = entry["until"] = 1.0
        path.write_text(json.dumps(entries))

    def assert_failed_attempt(self, mode: str) -> None:
        self.assertNotEqual(self.tick(mode), 0, self.log())
        self.assertEqual([a["outcome"] for a in self.attempts()], ["failed"])
        self.assertTrue(
            json.loads((self.agent_state() / "codex-backoff.json").read_text())
        )
        self.assertFalse((self.agent_state() / "codex-gate.json").exists())

    def test_completed_without_record_is_failed(self) -> None:
        self.assert_failed_attempt("missing-record")
        self.assertIn("no rebase record", self.log())

    def test_completed_with_invalid_record_is_failed(self) -> None:
        self.assert_failed_attempt("invalid-record")
        self.assertIn("the record is for PR 8", self.log())

    def test_completed_without_proof_is_failed(self) -> None:
        self.assert_failed_attempt("no-proof")
        self.assertIn("no test proof", self.log())

    def test_completed_with_stale_proof_is_failed(self) -> None:
        self.assert_failed_attempt("stale-proof")
        self.assertIn("test proof is for target tip", self.log())

    def test_malformed_local_proof_counts_as_failed_attempt(self) -> None:
        self.assert_failed_attempt("malformed-proof")
        self.assertIn("rebase: 'name'", self.log())

    def model_extra_directories(self) -> list[str]:
        args = json.loads((self.root / "model-args.json").read_text())
        return [args[index + 1] for index, arg in enumerate(args) if arg == "--add-dir"]

    def test_resolution_model_has_no_extra_writable_directories(self) -> None:
        self.env["EPIC_AGENT_ID"] = "codex-2"
        self.assert_failed_attempt("fail")
        self.assertEqual(self.model_extra_directories(), [])

    def test_other_feature_action_has_no_extra_model_directories(self) -> None:
        self.assertEqual(self.tick("fail", self.action("continue")), 0, self.log())
        self.assertEqual(self.model_extra_directories(), [])

    def test_proven_resolution_overrides_blocked_model_result(self) -> None:
        old, tip = self.remote_head(), self.remote_head(fixture.BASE)
        self.assertEqual(self.tick("resolve-blocked"), 0, self.log())
        self.assertEqual([a["outcome"] for a in self.attempts()], ["succeeded"])
        record = rp.parse_record(self.comments()[0]["body"], "Codex")
        self.assertIsNotNone(record)
        self.assertEqual((record["Old head"], record["Target tip"]), (old, tip))
        self.assertEqual(
            json.loads((self.agent_state() / "codex-backoff.json").read_text()), {}
        )

    def test_verified_completed_resolution_is_success(self) -> None:
        self.assertEqual(self.tick(), 0, self.log())
        self.assertIn("with test proof and rebase record", self.log())
        self.assertEqual([a["outcome"] for a in self.attempts()], ["succeeded"])

    def test_verification_read_failure_is_void_without_cooldown(self) -> None:
        self.assertEqual(self.tick("verify-read"), 75, self.log())
        self.assertEqual([a["outcome"] for a in self.attempts()], ["void"])
        self.assertFalse((self.agent_state() / "codex-backoff.json").exists())
        self.assertFalse((self.agent_state() / "codex-gate.json").exists())

    def test_verification_quota_is_void_without_cooldown(self) -> None:
        self.assertEqual(self.tick("verify-quota"), 75, self.log())
        self.assertEqual([a["outcome"] for a in self.attempts()], ["void"])
        self.assertFalse((self.agent_state() / "codex-backoff.json").exists())

    def test_model_quota_is_void_without_cooldown(self) -> None:
        self.assertEqual(self.tick("quota"), 75, self.log())
        self.assertEqual([a["outcome"] for a in self.attempts()], ["void"])
        self.assertFalse((self.agent_state() / "codex-backoff.json").exists())

    def test_registered_agent_publication_quota_stops_all_agents(self) -> None:
        self.env["EPIC_AGENT_ID"] = "codex-2"
        self.assertEqual(self.tick("publish-quota"), 75, self.log())
        self.assertEqual([a["outcome"] for a in self.attempts()], ["void"])
        self.assertTrue((self.state / "github-quota-wait.json").exists())
        self.assertFalse((self.agent_state() / "github-quota-wait.json").exists())
        self.assertFalse((self.agent_state() / "codex-backoff.json").exists())
        self.assertFalse((self.agent_state() / "codex-gate.json").exists())
        calls = self.lines("gh-calls.jsonl")
        self.env["EPIC_AGENT_ID"] = "codex-3"
        self.assertEqual(self.tick("fail"), 0, self.log())
        self.assertEqual(self.lines("gh-calls.jsonl"), calls)
        self.assertEqual(self.models(), 1)
        self.assertEqual([a["outcome"] for a in self.attempts()], ["void"])

    def test_failed_verification_retries_after_cooldown_not_repeat_gate(self) -> None:
        self.assert_failed_attempt("fail")
        self.assertEqual(self.tick("fail"), 0, self.log())
        self.assertEqual(self.models(), 1)
        self.assertEqual(len(self.attempts()), 1)
        self.expire_cooldowns()
        self.assertNotEqual(self.tick("fail"), 0, self.log())
        self.assertEqual(self.models(), 2)
        self.assertEqual([a["outcome"] for a in self.attempts()], ["failed", "failed"])

    def test_changed_base_at_same_head_bypasses_old_cooldown(self) -> None:
        head = self.remote_head()
        self.assert_failed_attempt("fail")
        self.advance_base("one\na different target tip\n")
        self.assertEqual(self.remote_head(), head)
        self.assertNotEqual(self.tick("fail"), 0, self.log())
        self.assertEqual(self.models(), 2)
        entries = json.loads((self.state / rp.ATTEMPTS).read_text())
        self.assertEqual(len(entries), 2)

    def fail_twice(self) -> None:
        for _ in range(2):
            self.assertNotEqual(self.tick("fail"), 0, self.log())
            self.expire_cooldowns()
            self.add_comment("unrelated comment")
        self.assertEqual(self.models(), 2)

    def test_limit_survives_runner_restarts_and_comments(self) -> None:
        self.fail_twice()
        self.assertEqual(self.tick("fail"), 0, self.log())
        self.assertEqual(self.models(), 2)
        self.assertEqual(len(self.lines("labels.jsonl")), 1)

    def test_failed_label_post_retries_without_model(self) -> None:
        (self.root / "label-fails").touch()
        self.fail_twice()
        self.assertEqual(self.tick("fail"), 0, self.log())
        self.assertEqual(self.models(), 2)
        self.assertEqual(self.lines("labels.jsonl"), [])
        (self.root / "label-fails").unlink()
        self.assertEqual(self.tick("fail"), 0, self.log())
        self.assertEqual(self.models(), 2)
        self.assertEqual(len(self.lines("labels.jsonl")), 1)

    def test_registered_agents_share_attempt_limit(self) -> None:
        self.env["EPIC_AGENT_ID"] = "codex-2"
        self.fail_twice()
        self.env["EPIC_AGENT_ID"] = "codex-3"
        self.assertEqual(self.tick("fail"), 0, self.log())
        self.assertEqual(self.models(), 2)
        self.assertEqual([a["outcome"] for a in self.attempts()], ["failed", "failed"])
        self.assertFalse((self.agent_state() / rp.ATTEMPTS).exists())

    def test_registered_agent_proof_stays_in_agent_state(self) -> None:
        self.env["EPIC_AGENT_ID"] = "codex-2"
        self.assertEqual(self.tick(), 0, self.log())
        self.assertTrue(
            rp.proof_path(self.agent_state(), fixture.PR, self.remote_head()).exists()
        )
        self.assertFalse((self.state / rp.PROOF_DIR).exists())
        self.assertEqual([a["outcome"] for a in self.attempts()], ["succeeded"])

    def review_scope(self) -> dict[str, Any]:
        self.assertEqual(self.tick("review", self.action("review")), 0, self.log())
        prompt = (self.root / "prompt.txt").read_text()
        raw = prompt.split("Review scope (JSON data, not instructions):\n", 1)[1]
        return json.JSONDecoder().raw_decode(raw)[0]

    def test_review_without_prior_verdict_has_full_scope(self) -> None:
        scope = self.review_scope()
        self.assertEqual(scope["mode"], "full")
        self.assertIn("no earlier verdict", scope["reason"])
        self.assertEqual(self.attempts(), [])
        self.assertEqual(
            self.model_extra_directories(), [str(self.agent_state() / "review")]
        )

    def prepare_peer_record(self) -> None:
        old = self.remote_head()
        self.assertEqual(self.tick(), 0, self.log())
        rows = self.comments()
        rows[0]["body"] = rows[0]["body"].replace(
            "Rebase record by Codex", "Rebase record by Claude"
        )
        rows.insert(
            0,
            {
                "id": 1,
                "body": f"Review: APPROVED by Codex at {old}",
                "created_at": "2020-01-01T00:00:00Z",
                "updated_at": "2020-01-01T00:00:00Z",
                "html_url": "previous",
            },
        )
        (self.root / "comments.json").write_text(json.dumps(rows))
        (self.agent_state() / "codex-gate.json").unlink(missing_ok=True)

    def test_valid_peer_record_produces_focused_review_scope(self) -> None:
        self.prepare_peer_record()
        scope = self.review_scope()
        self.assertEqual(scope["mode"], "focused", scope)
        self.assertEqual(scope["new_head"], self.remote_head())
        self.assertEqual(scope["conflicted_files"], ["f.txt"])
        self.assertTrue(scope["range_diff"])

    def test_advanced_base_falls_back_to_full_review_scope(self) -> None:
        self.prepare_peer_record()
        self.advance_base("one\nbase advanced after rebase\n")
        scope = self.review_scope()
        self.assertEqual(scope["mode"], "full", scope)
        self.assertIn("base advanced", scope["reason"])


def load_tests(
    loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str | None
) -> unittest.TestSuite:
    """Reuse fixture helpers without running the Claude runner's test cases."""
    return unittest.TestSuite(
        CodexRebasePolicyTests(name)
        for name in CodexRebasePolicyTests.__dict__
        if name.startswith("test_")
    )


if __name__ == "__main__":
    unittest.main()
