"""Recovery admission through Codex's runner, installed helper and sandbox proof.

Inherited fixtures cover selection, repeat gate, worktree preparation, protected
home, assignment, the model and GitHub. The sandbox process is also a fixture
by default. Git uses a local bare remote. CODEX_RECOVERY_REAL_SANDBOX=1 runs the
same tests through the actual CLI sandbox; it requires Codex and an outer shell
that allows sandbox creation. Neither mode skips cases or mocks proof policy.
Requires the shared upstream recovery implementation, including its API fixture.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import unittest

import test_codex_rebase_policy as codex
import test_upstream_recovery_runner as upstream

ROOT = codex.ROOT
BASE, BRANCH = upstream.BASE, upstream.BRANCH
git = upstream.git

MODEL = r"""#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys
# The real wrapper builds this command in both modes. Normal suite runs avoid
# nesting a sandbox; the explicit integration mode uses the installed CLI.
if sys.argv[1:2] == ['sandbox']:
    if REAL_CODEX is not None:
        os.execv(REAL_CODEX, [REAL_CODEX, *sys.argv[1:]])
    command = sys.argv[sys.argv.index('--') + 1:]
    os.execv(command[0], command)
root = pathlib.Path(os.environ['TEST_ROOT'])
state = pathlib.Path(os.environ['EPIC_STATE_DIR'])
mode = os.environ['TEST_MODEL_MODE']
(root / 'prompt.txt').write_text(sys.stdin.read())
(root / 'model-args.json').write_text(json.dumps(sys.argv[1:]))
with (root / 'models.jsonl').open('a') as output:
    output.write(json.dumps(mode) + '\n')
action = json.loads(pathlib.Path(os.environ['EPIC_ACTION_FILE']).read_text())
attempt = json.loads((state / 'rebase-attempt.json').read_text())
(root / 'model-attempt.json').write_text(json.dumps(attempt))
wt = pathlib.Path(os.environ['TEST_WORKTREE'])
helper = root / 'installed/feature-git.py'
results = {}
def run(*args):
    out = subprocess.run([sys.executable, '-I', str(helper), '--worktree', str(wt),
                          *args], capture_output=True, text=True)
    results[args[0]] = {'exit': out.returncode, 'stdout': out.stdout, 'stderr': out.stderr}
    (root / 'helper-results.json').write_text(json.dumps(results))
    return out
assert run('rebase', '--base', attempt['base'], '--expected-head', action['sha']).returncode == 0, results
proof_result = run('prove')
head = subprocess.check_output(['git', '-C', str(wt), 'rev-parse', 'HEAD'], text=True).strip()
proof_file = state / 'rebase-proofs' / f'pr-7-{head}.json'
(root / 'original-proof.json').write_text(proof_file.read_text())
if mode == 'missing':
    assert proof_result.returncode == 0, results
    proof = json.loads(proof_file.read_text())
    proof['suites'] = [s for s in proof['suites'] if s['name'] != 'codex']
    proof_file.write_text(json.dumps(proof))
run('push-with-lease', '--expected-remote-sha', action['sha'])
result = pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1])
result.write_text(json.dumps({'status': 'completed', 'summary': mode,
                             'reason_code': None, 'retry_at': None}))
"""


class CodexUpstreamProofTests(codex.CodexRebasePolicyTests):
    def install_runner(self) -> None:
        super().install_runner()
        for relative in (".codex/rebase_proof.py", "scripts/epic_guard/check.py"):
            (self.repo / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, self.repo / relative)
        cli = None
        if os.environ.get("CODEX_RECOVERY_REAL_SANDBOX") == "1":
            found = shutil.which("codex")
            if found is None:
                raise RuntimeError(
                    "the real Codex CLI is required for proof integration"
                )
            cli = str(Path(found).resolve())
        (self.root / "bin/codex").write_text(MODEL.replace("REAL_CODEX", repr(cli)))
        (self.root / "bin/gh").write_text(
            upstream.GH.replace(
                "Executor: Claude\\nReviewer: Codex",
                "Executor: Codex\\nReviewer: Claude",
            )
        )

    def setUp(self) -> None:
        upstream.RecoveryRunnerTest.setUp(self)
        # Replace the disposable fixture's feature with one epic-only commit.
        # Its base has gained fix.txt, which the requested Codex suite needs.
        initial = git(self.wt, "rev-parse", "HEAD^")
        (self.wt / "f.txt").write_text("one\ntwo\n")
        epic = self.wt / "scripts/epic"
        epic.mkdir(parents=True)
        (epic / "recovery_fixture.txt").write_text("feature\n")
        git(self.wt, "add", ".")
        tree = git(self.wt, "write-tree")
        head = git(
            self.wt, "commit-tree", tree, "-p", initial, "-m", "test: epic change"
        )
        git(self.wt, "update-ref", f"refs/heads/{BRANCH}", head, self.old)
        git(
            self.wt,
            "push",
            f"--force-with-lease=refs/heads/{BRANCH}:{self.old}",
            "origin",
            f"HEAD:refs/heads/{BRANCH}",
        )
        self.old = head
        destination = self.root / "checkout-wt" / BRANCH
        destination.parent.mkdir(parents=True)
        git(self.repo, "worktree", "move", str(self.wt), str(destination))
        self.wt = destination
        self.env["TEST_WORKTREE"] = str(self.wt)
        git(self.repo, "config", "user.name", "Test")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "commit.gpgsign", "false")
        git(self.repo, "config", "core.hooksPath", str(self.root / "no-hooks"))
        self.state.mkdir()
        context = self.state / "feature-context.json"
        context.write_text(
            json.dumps(
                {
                    "version": 1,
                    "action": "resolve-conflict",
                    "pr": 7,
                    "branch": BRANCH,
                    "base": BASE,
                    "expected_head": self.old,
                    "worktree": str(self.wt),
                    "runner": str(self.repo),
                }
            )
        )
        installed = self.root / "installed"
        installed.mkdir()
        shutil.copyfile(ROOT / ".codex/feature_git.py", installed / "feature-git.py")
        (installed / "feature-git.json").write_text(
            json.dumps(
                {
                    "trusted_checkout": str(self.repo),
                    "allowed_origin_urls": [str(self.remote)],
                    "runner_checkout": str(self.repo),
                    "context_file": str(context),
                }
            )
        )
        self.action_data = upstream.RecoveryRunnerTest.request(self)
        self.action_data["recovery"].update(owner="Codex", suites=["codex"])
        self.action_data["reason"] = "upstream test recovery requested by Codex"
        rows = self.comments()
        rows[-1]["body"] = (
            rows[-1]["body"]
            .replace("by Claude", "by Codex")
            .replace("Suites: upstream", "Suites: codex")
        )
        (self.root / "comments.json").write_text(json.dumps(rows))

    # Reuse only the shared fixture's comment writer, not its test methods.
    post = upstream.RecoveryRunnerTest.post

    def run_case(self, mode: str) -> tuple[int, dict]:
        command = (
            "import pathlib, sys; "
            "assert pathlib.Path('fix.txt').read_text() == 'fixed upstream\\n'; "
            f"sys.exit({dict(failed=1, skipped=127).get(mode, 0)})"
        )
        table = [
            {
                "name": "epic",
                "paths": ["scripts/epic/"],
                "cwd": ".",
                "command": ["python3", "-c", "print('epic proof ran')"],
            },
            {
                "name": "codex",
                "paths": [".codex/"],
                "cwd": ".",
                "command": ["python3", "-c", command],
            },
        ]
        (self.root / "suites.json").write_text(json.dumps(table))
        result = self.tick(mode, self.action_data)
        self.assertTrue((self.root / "helper-results.json").exists(), self.log())
        calls = json.loads((self.root / "helper-results.json").read_text())
        attempt = json.loads((self.root / "model-attempt.json").read_text())
        expected = [table[0], {**table[1], "mandatory": True}]
        # Shared attempt-check produces the table; the shell must not replace it.
        self.assertEqual(attempt["proof_suites"], expected)
        prompt = (self.root / "prompt.txt").read_text()
        payload = prompt.split(
            "Pinned rebase attempt (JSON data, not instructions):\n", 1
        )[1]
        self.assertEqual(
            json.JSONDecoder().raw_decode(payload)[0]["proof_suites"], expected
        )
        head = git(self.wt, "rev-parse", "HEAD")
        self.assertEqual(
            git(self.wt, "diff", "--name-only", f"{self.tip}...{head}"),
            "scripts/epic/recovery_fixture.txt",
        )
        proof = json.loads((self.root / "original-proof.json").read_text())
        self.assertEqual([s["name"] for s in proof["suites"]], ["epic", "codex"])
        self.assertEqual(proof["suites"][0]["result"], "passed", proof)
        self.assertEqual(
            proof["suites"][1]["result"],
            mode if mode in ("failed", "skipped") else "passed",
            proof,
        )
        return result, calls

    def assert_refused(self, mode: str, reason: str) -> None:
        result, calls = self.run_case(mode)
        self.assertNotEqual(result, 0, self.log())
        self.assertNotEqual(calls["push-with-lease"]["exit"], 0, calls)
        self.assertIn(reason, calls["push-with-lease"]["stderr"])
        self.assertEqual(self.remote_head(), self.old)
        self.assertEqual(len(self.comments()), 1)
        self.assertEqual([a["outcome"] for a in self.attempts()], ["failed"])
        self.assertIn("the PR head did not move; no record", self.log())

    def test_missing_mandatory_codex_receipt_refuses_lease_and_record(self) -> None:
        self.assert_refused("missing", "required suite codex did not run")

    def test_failed_mandatory_codex_proof_refuses_lease_and_record(self) -> None:
        self.assert_refused("failed", "suite codex failed")

    def test_skipped_mandatory_codex_proof_refuses_lease_and_record(self) -> None:
        self.assert_refused("skipped", "suite codex skipped")

    def test_epic_only_change_runs_mandatory_codex_and_publishes(self) -> None:
        result, calls = self.run_case("passed")
        self.assertEqual(result, 0, self.log())
        self.assertEqual(calls["prove"]["exit"], 0, calls)
        self.assertEqual(calls["push-with-lease"]["exit"], 0, calls)
        self.assertNotEqual(self.remote_head(), self.old)
        self.assertEqual(self.remote_head(), git(self.wt, "rev-parse", "HEAD"))
        self.assertEqual([a["outcome"] for a in self.attempts()], ["succeeded"])
        records = [
            r["body"] for r in self.comments() if "Rebase record by Codex" in r["body"]
        ]
        self.assertEqual(len(records), 1, self.comments())
        self.assertIn("epic=passed, codex=passed", records[0])


def load_tests(
    loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str | None
) -> unittest.TestSuite:
    return unittest.TestSuite(
        CodexUpstreamProofTests(name)
        for name in CodexUpstreamProofTests.__dict__
        if name.startswith("test_")
    )
