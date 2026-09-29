"""Prepare real Git worktrees before the runner starts an editing session."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
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
            "    return json.loads(os.environ['TEST_BOARD'])\n" + selector.read_text()
        )
        gh = self.fixture.bin / "gh"
        original = gh.read_text().split("\n", 1)[1]
        gh.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "if (sys.argv[1:3] == ['api', '-i']\n"
            "        and sys.argv[-1].endswith('/pulls/431')):\n"
            "    status = int(os.environ.get('TEST_WORKTREE_HTTP_STATUS', '200'))\n"
            "    print(f'HTTP/2.0 {status} Test\\n\\n' + os.environ['TEST_WORKTREE_PR'])\n"
            "    sys.exit(0 if status == 200 else 1)\n"
            "if sys.argv[1:] == ['api', 'repos/phaabe/live.moafunk.de/pulls/431']:\n"
            "    print(os.environ['TEST_WORKTREE_PR'])\n"
            "    sys.exit(0)\n"
            "if sys.argv[1:] == ['api', 'repos/phaabe/live.moafunk.de/issues/430']:\n"
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

    def use_pr(self, action: str = "fix") -> None:
        self.action = {"action": action, "pr": 431, "sha": self.head}
        self.git("update-ref", f"refs/remotes/origin/{BRANCH}", self.head)

    def assert_refused(self) -> None:
        result = self.prepare()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
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
        for expected in (75, 0):
            result = self.run_tick()
            self.assertEqual(result.returncode, expected, result.stderr)
            self.assertFalse(self.fixture.calls.exists())
            self.assertEqual(self.git("rev-parse", "HEAD", cwd=foreign), self.head)
            self.assertEqual(index_path.read_bytes(), index)
            self.assertEqual((foreign / "tracked.txt").read_text(), "operator work\n")
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
        log = (self.state / "codex.log").read_text()
        self.assertEqual(log.count(f"handoff needed: {BRANCH} in {foreign}"), 1)
        self.git("switch", "--detach", cwd=foreign)
        result = self.run_tick()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.fixture.calls.read_text().splitlines()), 1)
        self.assertEqual(
            self.git("branch", "--show-current", cwd=self.destination), BRANCH
        )

    def test_available_local_issue_branch_is_reused(self) -> None:
        branch = "fix/430-existing-work"
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
        for metadata in ([], None, {"head": None}):
            with self.subTest(metadata=metadata):
                self.env["TEST_WORKTREE_PR"] = json.dumps(metadata)
                result = self.prepare()
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertFalse(self.destination.exists())

    def test_remote_issue_branch_is_reused(self) -> None:
        branch = "fix/430-published-work"
        self.git("update-ref", f"refs/remotes/origin/{branch}", self.head)
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(self.worktrees / branch))

    def test_ambiguous_issue_branches_are_refused(self) -> None:
        self.git("branch", "fix/430-one")
        self.git("branch", "feat/430-two")
        self.assert_refused()

    def test_wrong_origin_is_refused(self) -> None:
        self.git("remote", "set-url", "origin", "https://github.com/other/repo.git")
        self.assert_refused()

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
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(marker.read_text(), "do not change\n")
        self.assertEqual(list(self.destination.iterdir()), [marker])

    def test_registered_destination_with_wrong_branch_is_refused(self) -> None:
        self.destination.parent.mkdir(parents=True)
        self.git("worktree", "add", "-b", "fix/999-other-task", str(self.destination))
        (self.destination / "tracked.txt").write_text("operator work\n")
        result = self.prepare()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
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
