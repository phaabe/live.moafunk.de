"""Prepare real Git worktrees before the runner starts an editing session."""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/epic"))
import isolated_env  # noqa: E402, F401 (before production modules or fixtures)

import json
import shutil
import subprocess
import unittest

import test_codex_tick as tick_fixture


ISSUE = "https://github.com/phaabe/live.moafunk.de/issues/430"
BASE = "dev/312-interim"
BRANCH = "feat/430-codex-work"
REPO = "phaabe/live.moafunk.de"


class FeatureWorktreeTests(unittest.TestCase):
    def setUp(self) -> None:
        # Compose the existing runner fixture without inheriting its test cases.
        self.fixture = tick_fixture.TickTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.repo = self.fixture.repo.resolve()
        self.env = self.fixture.env
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        for name in tuple(self.env):
            if name.startswith("GIT_"):
                del self.env[name]
        self.state = self.fixture.state
        self.state.mkdir(parents=True)
        self.metadata_calls = self.fixture.root / "worktree-metadata-calls"
        self.env["TEST_WORKTREE_METADATA_CALLS"] = str(self.metadata_calls)
        self.worktrees = self.repo.with_name(
            self.repo.name.removesuffix("-runner") + "-wt"
        )
        self.destination = self.worktrees / BRANCH
        for name in ("feature_worktree.py", "feature_git.py"):
            shutil.copyfile(tick_fixture.ROOT / name, self.repo / ".codex" / name)
        selector = self.repo / "scripts/epic/next_action.py"
        selector.write_text(
            "def project_items():\n"
            "    import json, os\n"
            "    with open(os.environ['TEST_WORKTREE_METADATA_CALLS'], 'a') as calls:\n"
            "        calls.write('board\\n')\n"
            "    return json.loads(os.environ['TEST_BOARD'])\n" + selector.read_text()
        )
        gh = self.fixture.bin / "gh"
        original = gh.read_text().split("\n", 1)[1]
        gh.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "if (sys.argv[1:3] == ['api', '-i']\n"
            "        and sys.argv[-1].endswith('/pulls/431')):\n"
            "    with open(os.environ['TEST_WORKTREE_METADATA_CALLS'], 'a') as calls:\n"
            "        calls.write('pr\\n')\n"
            "    status = int(os.environ.get('TEST_WORKTREE_HTTP_STATUS', '200'))\n"
            "    print(f'HTTP/2.0 {status} Test\\n\\n' + os.environ['TEST_WORKTREE_PR'])\n"
            "    sys.exit(0 if status == 200 else 1)\n"
            "if sys.argv[1:] == ['api', 'repos/phaabe/live.moafunk.de/pulls/431']:\n"
            "    with open(os.environ['TEST_WORKTREE_METADATA_CALLS'], 'a') as calls:\n"
            "        calls.write('pr\\n')\n"
            "    print(os.environ['TEST_WORKTREE_PR'])\n"
            "    sys.exit(0)\n"
            "if sys.argv[1:] == ['api', 'repos/phaabe/live.moafunk.de/issues/430']:\n"
            "    with open(os.environ['TEST_WORKTREE_METADATA_CALLS'], 'a') as calls:\n"
            "        calls.write('issue\\n')\n"
            "    print(os.environ['TEST_WORKTREE_ISSUE'])\n"
            "    sys.exit(0)\n" + original
        )
        self.git("init", "-b", BASE)
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.hooksPath", str(self.repo / ".git/hooks"))
        self.git(
            "remote", "add", "origin", "https://github.com/phaabe/live.moafunk.de.git"
        )
        (self.repo / "tracked.txt").write_text("initial\n")
        self.git("add", ".")
        self.git("commit", "-m", "test: initial fixture")
        self.head = self.git("rev-parse", "HEAD")
        self.git("update-ref", f"refs/remotes/origin/{BASE}", self.head)
        self.action_file = self.fixture.root / "action.json"
        self.action: dict[str, object] = {"action": "claim", "issue": ISSUE}
        self.env["TEST_WORKTREE_ISSUE"] = json.dumps(
            {"number": 430, "html_url": ISSUE, "state": "open"}
        )
        self.board = [
            {
                "content": {"number": 430, "url": ISSUE, "type": "Issue"},
                "executor": "Codex",
                "status": "Ready",
            }
        ]
        self.env["TEST_BOARD"] = json.dumps(self.board)
        self.pr = {
            "state": "open",
            "body": f"Executor: Codex\nIssue: {ISSUE}\n",
            "head": {"ref": BRANCH, "sha": self.head, "repo": {"full_name": REPO}},
            "base": {"ref": BASE, "repo": {"full_name": REPO}},
        }
        self.env["TEST_WORKTREE_PR"] = json.dumps(self.pr)
        self.installed = self.fixture.home / ".local/libexec/codex-feature-git.py"
        self.installed.parent.mkdir(parents=True)
        shutil.copyfile(self.repo / ".codex/feature_git.py", self.installed)
        self.context = self.state.resolve() / "feature-git-context.json"
        self.config = {
            "trusted_checkout": str(self.repo),
            "runner_checkout": str(self.repo),
            "context_file": str(self.context),
            "allowed_origin_urls": ["https://github.com/phaabe/live.moafunk.de.git"],
        }
        self.installed.with_suffix(".json").write_text(json.dumps(self.config))

    def git(self, *args: str, cwd: Path | None = None) -> str:
        result = subprocess.run(
            ["/usr/bin/git", "-C", str(cwd or self.repo), *args],
            env=self.env,
            text=True,
            capture_output=True,
            check=True,
        )
        return result.stdout.strip()

    def prepare(self) -> subprocess.CompletedProcess[str]:
        self.action_file.write_text(json.dumps(self.action))
        return subprocess.run(
            [
                sys.executable,
                str(self.repo / ".codex/feature_worktree.py"),
                "--runner",
                str(self.repo),
                "--action-file",
                str(self.action_file),
                "--state-dir",
                str(self.state),
            ],
            env=self.env,
            text=True,
            capture_output=True,
            timeout=10,
        )

    def run_tick(self) -> subprocess.CompletedProcess[str]:
        self.env["TEST_DECISION"] = json.dumps(self.action)
        return self.fixture.run_tick()

    def use_pr(self, action: str = "fix", branch: str = BRANCH) -> None:
        self.action = {"action": action, "pr": 431, "sha": self.head}
        self.pr["head"]["ref"] = branch
        self.env["TEST_WORKTREE_PR"] = json.dumps(self.pr)
        self.git("update-ref", f"refs/remotes/origin/{branch}", self.head)

    def assert_refused(self, code: int = 7) -> None:
        result = self.prepare()
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.git("rev-parse", "HEAD"), self.head)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_claim_creates_feature_tree_and_runs_model_there(self) -> None:
        result = self.run_tick()
        self.assertEqual(result.returncode, 0, result.stderr)
        call = json.loads(self.fixture.calls.read_text())
        self.assertEqual(
            call["args"][call["args"].index("--cd") + 1], str(self.destination)
        )
        self.assertIn("Prepared feature worktree", call["prompt"])
        self.assertIn(str(self.destination), call["prompt"])
        self.assertIn(str(self.worktrees), call["prompt"])
        self.assertEqual(
            self.git("branch", "--show-current", cwd=self.destination), BRANCH
        )
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.destination), self.head)
        self.assertEqual(self.git("branch", "--show-current"), BASE)

    def test_claim_ignores_other_agents_remote_issue_branch(self) -> None:
        remote = "refs/remotes/origin/fix/430-claude-leaf"
        self.git("update-ref", remote, self.head)
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(self.destination))
        self.assertEqual(self.git("rev-parse", remote), self.head)
        self.assertEqual(self.git("for-each-ref", "refs/heads/fix/430-claude-leaf"), "")
        self.assertEqual(
            self.git("branch", "--show-current", cwd=self.destination), BRANCH
        )

    def test_dirty_tree_and_unpublished_commit_resume_unchanged(self) -> None:
        self.use_pr()
        first = self.prepare()
        self.assertEqual(first.returncode, 0, first.stderr)
        (self.destination / "tracked.txt").write_text("committed work\n")
        self.git("add", "tracked.txt", cwd=self.destination)
        self.git("commit", "-m", "test: unpublished work", cwd=self.destination)
        local_head = self.git("rev-parse", "HEAD", cwd=self.destination)
        (self.destination / "tracked.txt").write_text("staged work\n")
        self.git("add", "tracked.txt", cwd=self.destination)
        (self.destination / "tracked.txt").write_text("unstaged work\n")
        (self.destination / "untracked.txt").write_text("keep me\n")
        status = self.git("status", "--porcelain", cwd=self.destination)
        for action in ("fix", "fix-checks", "resolve-conflict"):
            self.action["action"] = action
            result = self.prepare()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), str(self.destination))
            self.assertEqual(
                self.git("rev-parse", "HEAD", cwd=self.destination), local_head
            )
            self.assertEqual(
                self.git("status", "--porcelain", cwd=self.destination), status
            )
            self.assertEqual(
                self.git("show", ":tracked.txt", cwd=self.destination), "staged work"
            )
            self.assertEqual(
                (self.destination / "tracked.txt").read_text(), "unstaged work\n"
            )
            self.assertEqual(
                (self.destination / "untracked.txt").read_text(), "keep me\n"
            )

    def test_crlf_pr_body_prepares_selected_branch(self) -> None:
        self.use_pr()
        self.pr["body"] = f"Executor: Codex\r\nIssue: {ISSUE}\r\n"
        self.env["TEST_WORKTREE_PR"] = json.dumps(self.pr)
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(self.destination))

    def test_conflict_session_requires_current_installed_helper(self) -> None:
        self.use_pr("resolve-conflict")
        for missing in (False, True):
            with self.subTest(missing=missing):
                if missing:
                    self.installed.unlink()
                else:
                    self.installed.write_text("# old helper\n")
                result = self.run_tick()
                self.assertEqual(result.returncode, 75, result.stderr)
                self.assertFalse(self.fixture.calls.exists())
                self.assertFalse(self.destination.exists())
                self.assertFalse(self.context.exists())
                entries = json.loads((self.state / "codex-backoff.json").read_text())
                self.assertIn(
                    "rebase policy unavailable",
                    entries[f"pr:431:{self.head}"]["reason"],
                )
                self.fixture.expire_cooldown()

    def test_conflict_context_is_bound_to_fresh_selected_pr(self) -> None:
        self.use_pr("resolve-conflict")
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(self.context.read_text()),
            {
                "version": 1,
                "action": "resolve-conflict",
                "pr": 431,
                "branch": BRANCH,
                "base": BASE,
                "expected_head": self.head,
                "worktree": str(self.destination),
                "runner": str(self.repo),
            },
        )

    def test_conflict_session_removes_context_after_model(self) -> None:
        self.use_pr("resolve-conflict")
        result = self.run_tick()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.fixture.calls.exists())
        self.assertFalse(self.context.exists())

    def test_conflict_policy_rejects_wrong_runner_or_context_location(self) -> None:
        self.use_pr("resolve-conflict")
        for key, value in (
            ("runner_checkout", str(self.destination)),
            ("context_file", str(self.repo / "context.json")),
        ):
            with self.subTest(key=key):
                config = {**self.config, key: value}
                self.installed.with_suffix(".json").write_text(json.dumps(config))
                self.assert_refused()
                self.assertFalse(self.context.exists())

    def start_conflicting_rebase(self, *, recorded: bool) -> Path:
        self.use_pr()
        self.assertEqual(self.prepare().returncode, 0)
        (self.destination / "tracked.txt").write_text("feature change\n")
        self.git("add", "tracked.txt", cwd=self.destination)
        self.git("commit", "-m", "test: feature change", cwd=self.destination)
        self.head = self.git("rev-parse", "HEAD", cwd=self.destination)
        self.pr["head"]["sha"] = self.head
        self.use_pr("resolve-conflict")
        (self.repo / "tracked.txt").write_text("base change\n")
        self.git("add", "tracked.txt")
        self.git("commit", "-m", "test: base change")
        onto = self.git("rev-parse", "HEAD")
        self.git("update-ref", f"refs/remotes/origin/{BASE}", onto)
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        state = {
            **json.loads(self.context.read_text()),
            "original_head": self.head,
            "onto": onto,
        }
        record = self.state / "rebase-431.json"
        if recorded:
            record.write_text(json.dumps(state))
        result = subprocess.run(
            ["/usr/bin/git", "-C", str(self.destination), "rebase", "--merge", onto],
            env=self.env,
            text=True,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.git("branch", "--show-current", cwd=self.destination), "")
        return record

    def test_recorded_detached_rebase_resumes_model_and_preserves_state(self) -> None:
        record = self.start_conflicting_rebase(recorded=True)
        before = record.read_bytes()
        status = self.git("status", "--porcelain", cwd=self.destination)
        result = self.run_tick()
        self.assertEqual(result.returncode, 0, result.stderr)
        call = json.loads(self.fixture.calls.read_text())
        self.assertEqual(
            call["args"][call["args"].index("--cd") + 1], str(self.destination)
        )
        self.assertEqual(record.read_bytes(), before)
        self.assertEqual(
            self.git("status", "--porcelain", cwd=self.destination), status
        )
        self.assertFalse(self.context.exists())

    def test_unrecorded_rebase_is_refused_and_preserved(self) -> None:
        self.start_conflicting_rebase(recorded=False)
        status = self.git("status", "--porcelain", cwd=self.destination)
        result = self.run_tick()
        self.assertEqual(result.returncode, 75, result.stderr)
        self.assertFalse(self.fixture.calls.exists())
        self.assertTrue((self.state / "codex-backoff.json").exists())
        self.assertEqual(
            self.git("status", "--porcelain", cwd=self.destination), status
        )

    def test_recorded_completed_rebase_resumes_before_lease_push(self) -> None:
        record = self.start_conflicting_rebase(recorded=True)
        (self.destination / "tracked.txt").write_text("resolved change\n")
        self.git("add", "tracked.txt", cwd=self.destination)
        self.git("-c", "core.editor=true", "rebase", "--continue", cwd=self.destination)
        rewritten = self.git("rev-parse", "HEAD", cwd=self.destination)
        self.assertNotEqual(rewritten, self.head)
        result = self.run_tick()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.fixture.calls.exists())
        self.assertTrue(record.exists())
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.destination), rewritten)

    def test_recorded_rebase_rejects_a_changed_selected_head(self) -> None:
        record = self.start_conflicting_rebase(recorded=True)
        before = record.read_bytes()
        newer = self.git("rev-parse", "HEAD")
        self.action["sha"] = newer
        self.pr["head"]["sha"] = newer
        self.env["TEST_WORKTREE_PR"] = json.dumps(self.pr)
        self.git("update-ref", f"refs/remotes/origin/{BRANCH}", newer)
        result = self.run_tick()
        self.assertEqual(result.returncode, 75, result.stderr)
        self.assertFalse(self.fixture.calls.exists())
        self.assertEqual(record.read_bytes(), before)

    def test_held_pr_branch_behind_remote_still_reports_handoff(self) -> None:
        self.use_pr()
        foreign = self.fixture.root.resolve() / "developer checkout"
        self.git("worktree", "add", "-b", BRANCH, str(foreign))
        (foreign / "tracked.txt").write_text("operator work\n")
        self.git("add", "tracked.txt", cwd=foreign)
        index_path = Path(
            self.git(
                "rev-parse",
                "--path-format=absolute",
                "--git-path",
                "index",
                cwd=foreign,
            )
        )
        index = index_path.read_bytes()
        self.git("commit", "--allow-empty", "-m", "test: newer published head")
        published = self.git("rev-parse", "HEAD")
        self.git("update-ref", f"refs/remotes/origin/{BRANCH}", published)
        self.action["sha"] = published
        self.pr["head"]["sha"] = published
        self.env["TEST_WORKTREE_PR"] = json.dumps(self.pr)
        first_requests = None
        for expected in (75, 0):
            result = self.run_tick()
            self.assertEqual(result.returncode, expected, result.stderr)
            self.assertFalse(self.fixture.calls.exists())
            self.assertEqual(self.git("rev-parse", "HEAD", cwd=foreign), self.head)
            self.assertEqual(index_path.read_bytes(), index)
            self.assertEqual((foreign / "tracked.txt").read_text(), "operator work\n")
            if first_requests is None:
                first_requests = self.metadata_calls.read_bytes()
            else:
                self.assertEqual(self.metadata_calls.read_bytes(), first_requests)
        self.assertEqual(
            (self.state / "codex.log")
            .read_text()
            .count(f"handoff needed: {BRANCH} in {foreign}"),
            1,
        )

    def test_held_branch_logs_handoff_once_and_release_retries_immediately(
        self,
    ) -> None:
        self.action["action"] = "continue"
        self.board[0]["status"] = "In progress"
        self.env["TEST_BOARD"] = json.dumps(self.board)
        foreign = self.fixture.root.resolve() / "developer checkout"
        self.git("worktree", "add", "-b", BRANCH, str(foreign))
        (foreign / "tracked.txt").write_text("staged\n")
        self.git("add", "tracked.txt", cwd=foreign)
        (foreign / "tracked.txt").write_text("unstaged\n")
        index_path = Path(
            self.git(
                "rev-parse",
                "--path-format=absolute",
                "--git-path",
                "index",
                cwd=foreign,
            )
        )
        before = index_path.read_bytes()
        first_requests = None
        for expected in (75, 0):
            result = self.run_tick()
            self.assertEqual(result.returncode, expected, result.stderr)
            self.assertFalse(self.fixture.calls.exists())
            self.assertFalse(self.destination.exists())
            self.assertEqual(self.git("rev-parse", "HEAD", cwd=foreign), self.head)
            self.assertEqual(index_path.read_bytes(), before)
            self.assertEqual((foreign / "tracked.txt").read_text(), "unstaged\n")
            self.assertFalse((self.state / "codex-backoff.json").exists())
            self.assertEqual(self.fixture.tick_events()[-1]["event"], "finish")
            if expected == 75:
                self.assertEqual(self.fixture.tick_events()[-1]["phase"], "gate")
            if first_requests is None:
                first_requests = self.metadata_calls.read_bytes()
            else:
                self.assertEqual(self.metadata_calls.read_bytes(), first_requests)
        log = (self.state / "codex.log").read_text()
        self.assertEqual(log.count(f"handoff needed: {BRANCH} in {foreign}"), 1)
        self.git("switch", "--detach", cwd=foreign)
        result = self.run_tick()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.fixture.calls.read_text().splitlines()), 1)
        self.assertEqual(
            self.git("branch", "--show-current", cwd=self.destination), BRANCH
        )
        self.assertNotEqual(self.metadata_calls.read_bytes(), first_requests)

    def test_available_local_owned_pr_branch_is_reused(self) -> None:
        branch = "fix/430-existing-work"
        self.use_pr(branch=branch)
        self.git("branch", branch)
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(self.worktrees / branch))
        self.assertEqual(
            self.git("branch", "--show-current", cwd=self.worktrees / branch), branch
        )

    def test_handoff_allows_later_review_candidate(self) -> None:
        foreign = self.fixture.root / "developer checkout"
        self.git("worktree", "add", "-b", BRANCH, str(foreign))
        self.fixture.candidates(self.action, self.fixture.review_action(406))
        result = self.fixture.run_tick()
        self.assertEqual(result.returncode, 0, result.stderr)
        [call] = [
            json.loads(line) for line in self.fixture.calls.read_text().splitlines()
        ]
        self.assertEqual(call["action"]["action"], "review")
        self.assertEqual(call["action"]["pr"], 406)
        self.assertFalse(self.destination.exists())

    def test_destination_failure_cools_down_and_allows_later_review(self) -> None:
        self.destination.mkdir(parents=True)
        marker = self.destination / "operator-file"
        marker.write_text("do not change\n")
        self.fixture.candidates(self.action, self.fixture.review_action(406))
        first = self.fixture.run_tick()
        self.assertEqual(first.returncode, 0, first.stderr)
        [call] = [
            json.loads(line) for line in self.fixture.calls.read_text().splitlines()
        ]
        self.assertEqual(call["action"]["action"], "review")
        self.assertEqual(call["action"]["pr"], 406)
        cooldown = self.state / "codex-backoff.json"
        before = cooldown.read_bytes()
        entry = json.loads(before)[f"issue:{ISSUE}"]
        self.assertEqual(entry["until"] - entry["at"], 900)
        requests = self.metadata_calls.read_bytes()
        self.assertEqual(marker.read_text(), "do not change\n")
        second = self.fixture.run_tick()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(cooldown.read_bytes(), before)
        self.assertEqual(self.metadata_calls.read_bytes(), requests)
        self.assertEqual(len(self.fixture.calls.read_text().splitlines()), 1)
        marker.unlink()
        self.destination.rmdir()
        self.fixture.expire_cooldown()
        third = self.fixture.run_tick()
        self.assertEqual(third.returncode, 0, third.stderr)
        calls = [
            json.loads(line) for line in self.fixture.calls.read_text().splitlines()
        ]
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[-1]["action"]["action"], "claim")
        self.assertEqual(
            self.git("branch", "--show-current", cwd=self.destination), BRANCH
        )

    def test_missing_foreign_worktree_handoff_explains_prune(self) -> None:
        foreign = self.fixture.root.resolve() / "removed developer checkout"
        self.git("worktree", "add", "-b", BRANCH, str(foreign))
        shutil.rmtree(foreign)
        result = self.run_tick()
        self.assertEqual(result.returncode, 75, result.stderr)
        self.assertFalse(self.fixture.calls.exists())
        log = (self.state / "codex.log").read_text()
        self.assertIn(f"handoff needed: {BRANCH} in {foreign}", log)
        self.assertIn("git worktree prune", log)

    def test_handoff_revalidates_metadata_after_github_change(self) -> None:
        self.use_pr()
        foreign = self.fixture.root.resolve() / "developer checkout"
        self.git("worktree", "add", "-b", BRANCH, str(foreign))
        first = self.run_tick()
        self.assertEqual(first.returncode, 75, first.stderr)
        requests = self.metadata_calls.read_bytes()
        self.fixture.updated_at.write_text("2026-09-28T03:01:00Z")
        self.pr["body"] = f"Executor: Claude\nIssue: {ISSUE}\n"
        self.env["TEST_WORKTREE_PR"] = json.dumps(self.pr)
        changed = self.run_tick()
        self.assertEqual(changed.returncode, 75, changed.stderr)
        self.assertNotEqual(self.metadata_calls.read_bytes(), requests)
        self.assertFalse(self.fixture.calls.exists())
        self.assertFalse(self.destination.exists())
        self.assertTrue((self.state / "codex-backoff.json").exists())

    def test_expired_handoff_revalidates_metadata(self) -> None:
        self.use_pr()
        foreign = self.fixture.root.resolve() / "developer checkout"
        self.git("worktree", "add", "-b", BRANCH, str(foreign))
        self.assertEqual(self.run_tick().returncode, 75)
        requests = self.metadata_calls.read_bytes()
        record_file = self.state / "codex-gate.json"
        record = json.loads(record_file.read_text())
        record["at"] = 0
        record["targets"]["431"]["at"] = 0
        record_file.write_text(json.dumps(record))
        renewed = self.run_tick()
        self.assertEqual(renewed.returncode, 75, renewed.stderr)
        self.assertNotEqual(self.metadata_calls.read_bytes(), requests)
        self.assertFalse(self.fixture.calls.exists())
        self.assertFalse((self.state / "codex-backoff.json").exists())

    def test_malformed_gate_state_is_global_error_without_cooldown(self) -> None:
        (self.state / "codex-gate.json").write_text("[]")
        (self.state / "codex-gate-seen.json").write_text("{}")
        result = self.prepare()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse((self.state / "codex-backoff.json").exists())
        self.assertFalse(self.metadata_calls.exists())

    def test_shared_metadata_failure_starts_no_model_and_sets_no_cooldown(self) -> None:
        self.use_pr()
        self.env["EPIC_SHARED_READER"] = "1"
        self.env["TEST_WORKTREE_HTTP_STATUS"] = "503"
        cache = self.state / "github-cache"
        cache.mkdir()
        (cache / "auth-context").write_text("test")
        result = self.run_tick()
        self.assertEqual(result.returncode, 75, result.stderr)
        self.assertFalse(self.fixture.calls.exists())
        self.assertFalse((self.state / "codex-backoff.json").exists())
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.fixture.tick_events()[-1]["event"], "finish")
        self.assertEqual(self.fixture.tick_events()[-1]["phase"], "gate")

    def test_shared_metadata_prepares_pr_worktree(self) -> None:
        self.use_pr()
        self.env["EPIC_SHARED_READER"] = "1"
        cache = self.state / "github-cache"
        cache.mkdir()
        (cache / "auth-context").write_text("test")
        result = self.run_tick()
        self.assertEqual(result.returncode, 0, result.stderr)
        call = json.loads(self.fixture.calls.read_text())
        self.assertEqual(
            call["args"][call["args"].index("--cd") + 1], str(self.destination)
        )

    def test_malformed_metadata_is_refused_without_traceback(self) -> None:
        self.use_pr()
        for metadata in ([], None, {"head": None}, {**self.pr, "head": None}):
            with self.subTest(metadata=metadata):
                self.env["TEST_WORKTREE_PR"] = json.dumps(metadata)
                result = self.prepare()
                self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertFalse(self.destination.exists())

    def test_remote_owned_pr_branch_is_reused(self) -> None:
        branch = "fix/430-published-work"
        self.use_pr(branch=branch)
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(self.worktrees / branch))

    def test_continue_ignores_other_issue_branches(self) -> None:
        self.action["action"] = "continue"
        self.board[0]["status"] = "In progress"
        self.env["TEST_BOARD"] = json.dumps(self.board)
        self.git("branch", "fix/430-one")
        self.git("branch", "feat/430-two")
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(self.destination))
        for branch in ("fix/430-one", "feat/430-two"):
            self.assertEqual(self.git("rev-parse", branch), self.head)

    def test_wrong_origin_is_refused(self) -> None:
        self.git("remote", "set-url", "origin", "https://github.com/other/repo.git")
        self.assert_refused(code=1)

    def test_stale_remote_head_is_refused(self) -> None:
        self.use_pr()
        self.git("update-ref", "-d", f"refs/remotes/origin/{BRANCH}")
        self.assert_refused()

    def test_local_branch_behind_selected_pr_head_is_refused(self) -> None:
        self.use_pr()
        self.git("branch", BRANCH)
        self.git("commit", "--allow-empty", "-m", "test: published commit")
        published = self.git("rev-parse", "HEAD")
        self.git("update-ref", f"refs/remotes/origin/{BRANCH}", published)
        self.action["sha"] = published
        self.pr["head"]["sha"] = published
        self.env["TEST_WORKTREE_PR"] = json.dumps(self.pr)
        self.head = published
        self.assert_refused()

    def test_unrelated_existing_destination_is_preserved(self) -> None:
        self.destination.mkdir(parents=True)
        marker = self.destination / "operator-file"
        marker.write_text("do not change\n")
        result = self.prepare()
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        self.assertEqual(marker.read_text(), "do not change\n")
        self.assertEqual(list(self.destination.iterdir()), [marker])

    def test_registered_destination_with_wrong_branch_is_refused(self) -> None:
        self.destination.parent.mkdir(parents=True)
        self.git("worktree", "add", "-b", "fix/999-other-task", str(self.destination))
        (self.destination / "tracked.txt").write_text("operator work\n")
        result = self.prepare()
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        self.assertEqual(
            self.git("branch", "--show-current", cwd=self.destination),
            "fix/999-other-task",
        )
        self.assertEqual(
            (self.destination / "tracked.txt").read_text(), "operator work\n"
        )

    def test_symlinked_destination_parent_is_refused(self) -> None:
        elsewhere = self.fixture.root / "elsewhere"
        elsewhere.mkdir()
        self.worktrees.symlink_to(elsewhere, target_is_directory=True)
        self.assert_refused()
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_pr_metadata_must_match_repo_owner_base_branch_and_head(self) -> None:
        self.use_pr()
        original = json.dumps(self.pr)
        for field, value in (
            ("state", "closed"),
            ("body", f"Executor: Claude\nIssue: {ISSUE}\n"),
            ("body", f"Executor: Codex\nExecutor: Claude\nIssue: {ISSUE}\n"),
            (
                "body",
                "Executor: Codex\nIssue: https://github.com/phaabe/live.moafunk.de/issues/999\n",
            ),
            (
                "head",
                {"ref": BRANCH, "sha": self.head, "repo": {"full_name": "other/repo"}},
            ),
            ("head", {"ref": "main", "sha": self.head, "repo": {"full_name": REPO}}),
            (
                "head",
                {
                    "ref": "feat/430-task/escape",
                    "sha": self.head,
                    "repo": {"full_name": REPO},
                },
            ),
            ("head", {"ref": BRANCH, "sha": "f" * 40, "repo": {"full_name": REPO}}),
            ("base", {"ref": "main", "repo": {"full_name": REPO}}),
            ("base", {"ref": BASE, "repo": {"full_name": "other/repo"}}),
        ):
            with self.subTest(field=field, value=value):
                metadata = json.loads(original)
                metadata[field] = value
                self.env["TEST_WORKTREE_PR"] = json.dumps(metadata)
                self.assert_refused()

    def test_issue_requires_codex_ready_board_item(self) -> None:
        for field, value in (("executor", "Claude"), ("status", "Done")):
            with self.subTest(field=field):
                board = json.loads(json.dumps(self.board))
                board[0][field] = value
                self.env["TEST_BOARD"] = json.dumps(board)
                self.assert_refused()

    def test_claim_rejects_foreign_issue_url(self) -> None:
        self.action["issue"] = "https://github.com/other/repo/issues/430"
        self.assert_refused()

    def test_nonediting_actions_keep_runner_and_do_not_create_worktrees(self) -> None:
        for action in ("review", "merge", "adopt", "idle"):
            with self.subTest(action=action):
                self.action = {"action": action, "pr": 431, "sha": self.head}
                result = self.prepare()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), str(self.repo))
                self.assertFalse(self.worktrees.exists())


if __name__ == "__main__":
    unittest.main()
