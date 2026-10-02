"""Exercise the installed helper against isolated local Git repositories."""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/epic"))
import isolated_env  # noqa: E402, F401 (before production modules or fixtures)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from feature_git import GIT  # noqa: E402

import json
import os
import shutil
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "feature_git.py"


class FeatureGitTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="feature-git-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_")
        }
        self.env["HOME"] = str(self.home)
        self.repo = self.root / "trusted repo"
        self.repo.mkdir()
        self.remote = self.root / "remote.git"
        self.git("init", "--bare", str(self.remote))
        self.git("init")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.hooksPath", str(self.repo / ".git/hooks"))
        self.git("checkout", "-b", "feat/381-integration-ci")
        self.git("remote", "add", "origin", str(self.remote))
        self.file = self.repo / "tracked.txt"
        self.file.write_text("initial\n")
        self.git("add", "tracked.txt")
        self.git("commit", "-m", "test: initial commit")
        self.before = self.git("rev-parse", "HEAD")
        self.helper = self.root / "installed-feature-git.py"
        shutil.copyfile(SCRIPT, self.helper)
        self.config = self.helper.with_suffix(".json")
        self.config.write_text(
            json.dumps(
                {
                    "trusted_checkout": str(self.repo),
                    "allowed_origin_urls": [str(self.remote)],
                }
            )
        )
        self.message = self.root / "commit message.txt"
        self.message.write_text("test: record a staged change\n")
        self.file.write_text("changed\n")
        self.git("add", "tracked.txt")

    def git(self, *args: str, cwd: Path | None = None) -> str:
        result = subprocess.run(
            [GIT, "-C", str(cwd or self.repo), *args],
            env=self.env,
            text=True,
            capture_output=True,
            check=True,
        )
        return result.stdout.strip()

    def run_helper(
        self,
        *args: str,
        worktree: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-I",
                str(self.helper),
                "--worktree",
                str(worktree or self.repo),
                *args,
            ],
            env=env or self.env,
            capture_output=True,
            text=True,
            check=False,
        )

    def commit(
        self,
        worktree: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return self.run_helper(
            "commit", "--message-file", str(self.message), worktree=worktree, env=env
        )

    def assert_refused(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.before)

    def test_commit_uses_staged_changes_and_message(self) -> None:
        result = self.commit()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("show", "HEAD:tracked.txt"), "changed")
        self.assertEqual(
            self.git("log", "-1", "--format=%s"), self.message.read_text().strip()
        )

    def test_linked_worktree_is_accepted(self) -> None:
        worktree = self.root / "linked worktree"
        self.git("worktree", "add", "-b", "fix/381-linked-tree", str(worktree))
        (worktree / "another.txt").write_text("linked\n")
        self.git("add", "another.txt", cwd=worktree)
        result = self.commit(worktree=worktree)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("show", "HEAD:another.txt", cwd=worktree), "linked")

    def test_protected_and_invalid_branches_are_refused(self) -> None:
        for branch in ("main", "dev/312-interim", "feat/no-issue", "feat/381-x/y"):
            with self.subTest(branch=branch):
                self.git("checkout", "-b", branch)
                self.assert_refused(self.commit())

    def test_detached_head_is_refused(self) -> None:
        self.git("checkout", "--detach")
        self.assert_refused(self.commit())

    def test_foreign_repository_is_refused(self) -> None:
        foreign = self.root / "foreign"
        self.git("init", str(foreign))
        self.assert_refused(self.commit(worktree=foreign))

    def test_bad_remote_is_refused(self) -> None:
        self.git("remote", "set-url", "origin", "https://example.invalid/repo.git")
        self.assert_refused(self.commit())

    def test_multiple_push_urls_are_refused(self) -> None:
        self.git("config", "--add", "remote.origin.pushurl", str(self.remote))
        self.git("config", "--add", "remote.origin.pushurl", str(self.remote))
        self.assert_refused(self.run_helper("push"))

    def test_multiple_fetch_urls_are_refused(self) -> None:
        self.git("config", "--add", "remote.origin.url", str(self.remote))
        self.assert_refused(self.run_helper("push"))

    def test_url_redirect_is_refused(self) -> None:
        self.git(
            "config", "url.https://example.invalid/.pushInsteadOf", str(self.remote)
        )
        self.assert_refused(self.run_helper("push"))

    def test_mirror_is_refused(self) -> None:
        self.git("config", "remote.origin.mirror", "true")
        self.assert_refused(self.run_helper("push"))

    def test_commit_hook_is_not_bypassed(self) -> None:
        hook = self.repo / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\nexit 17\n")
        hook.chmod(0o755)
        self.assert_refused(self.commit())

    def test_git_environment_overrides_are_removed(self) -> None:
        result = self.commit(
            env={
                **self.env,
                "GIT_DIR": str(self.root / "wrong.git"),
                "GIT_WORK_TREE": str(self.root / "wrong-tree"),
                "GIT_INDEX_FILE": str(self.root / "wrong-index"),
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "remote.origin.url",
                "GIT_CONFIG_VALUE_0": "https://example.invalid/repo.git",
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("show", "HEAD:tracked.txt"), "changed")

    def test_extra_options_are_refused(self) -> None:
        for args in (
            ("commit", "--message-file", str(self.message), "--amend"),
            ("commit", "--message-file", str(self.message), "--no-verify"),
            ("push", "--force"),
            ("push", "--delete", "main"),
            ("push", "origin", "HEAD:main"),
        ):
            with self.subTest(args=args):
                self.assert_refused(self.run_helper(*args))

    def test_push_only_publishes_current_branch(self) -> None:
        self.git("tag", "-a", "unwanted-tag", "-m", "unwanted")
        self.git("config", "push.followTags", "true")
        self.git("config", "remote.origin.push", "HEAD:refs/heads/main")
        result = self.run_helper("push")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.git("for-each-ref", "--format=%(refname)", cwd=self.remote),
            "refs/heads/feat/381-integration-ci",
        )

    def test_push_hook_is_not_bypassed(self) -> None:
        hook = self.repo / ".git/hooks/pre-push"
        hook.write_text("#!/bin/sh\nexit 17\n")
        hook.chmod(0o755)
        self.assert_refused(self.run_helper("push"))
        self.assertEqual(self.git("for-each-ref", cwd=self.remote), "")

    def test_push_refuses_non_fast_forward(self) -> None:
        self.git("commit", "-m", "test: existing remote commit")
        remote_head = self.git("rev-parse", "HEAD")
        self.git("push", "origin", "HEAD:refs/heads/feat/381-integration-ci")
        self.git("reset", "--soft", self.before)
        self.git("commit", "-m", "test: divergent local commit")
        result = self.run_helper("push")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(
            self.git(
                "rev-parse", "refs/heads/feat/381-integration-ci", cwd=self.remote
            ),
            remote_head,
        )

    def test_missing_or_invalid_installed_config_is_refused(self) -> None:
        self.config.unlink()
        self.assert_refused(self.commit())
        self.config.write_text('{"trusted_checkout": "/tmp"}')
        self.assert_refused(self.commit())


if __name__ == "__main__":
    unittest.main()
