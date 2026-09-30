"""Exercise bounded rebase and lease pushes against real local Git repositories."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "feature_git.py"
BASE = "dev/312-interim"
BRANCH = "feat/431-example"


class FeatureRebaseTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="feature-rebase-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_")
        }
        self.env.update(HOME=str(self.home), GIT_CONFIG_NOSYSTEM="1")
        self.repo = self.root / "live"
        self.repo.mkdir()
        self.remote = self.root / "origin.git"
        self.git("init", "--bare", str(self.remote))
        self.git("init")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.hooksPath", str(self.repo / ".git/hooks"))
        self.git("checkout", "-b", BASE)
        self.git("remote", "add", "origin", str(self.remote))
        (self.repo / "tracked.txt").write_text("initial\n")
        self.git("add", "tracked.txt")
        self.git("commit", "-m", "test: seed repository")
        self.initial = self.git("rev-parse", "HEAD")
        self.runner = self.root / "live-runner"
        self.git("worktree", "add", "-b", "chore/999-runner", str(self.runner))
        self.worktree = self.root / "live-wt" / BRANCH
        self.git("worktree", "add", "-b", BRANCH, str(self.worktree))
        (self.worktree / "tracked.txt").write_text("feature\n")
        self.git("add", "tracked.txt", cwd=self.worktree)
        self.git("commit", "-m", "test: feature change", cwd=self.worktree)
        self.before = self.git("rev-parse", "HEAD", cwd=self.worktree)
        self.git("push", "origin", BASE, BRANCH)
        self.helper = self.root / "installed-feature-git.py"
        shutil.copyfile(SCRIPT, self.helper)
        self.context = self.root / "runner-state" / "context.json"
        self.context.parent.mkdir()
        self.state = self.context.with_name("rebase-431.json")
        self.context_data: dict[str, str | int] = {
            "version": 1,
            "action": "resolve-conflict",
            "pr": 431,
            "branch": BRANCH,
            "base": BASE,
            "expected_head": self.before,
            "worktree": str(self.worktree),
            "runner": str(self.runner),
        }
        self.write_context()
        self.config = self.helper.with_suffix(".json")
        self.config.write_text(
            json.dumps(
                {
                    "trusted_checkout": str(self.repo),
                    "allowed_origin_urls": [str(self.remote)],
                    "runner_checkout": str(self.runner),
                    "context_file": str(self.context),
                }
            )
        )

    def git(self, *args: str, cwd: Path | None = None) -> str:
        result = subprocess.run(
            ["/usr/bin/git", "-C", str(cwd or self.repo), *args],
            env=self.env,
            text=True,
            capture_output=True,
            check=True,
        )
        return result.stdout.strip()

    def write_context(self) -> None:
        self.context.write_text(json.dumps(self.context_data))

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
                str(worktree or self.worktree),
                *args,
            ],
            env=env or self.env,
            capture_output=True,
            text=True,
            check=False,
        )

    def rebase(self) -> subprocess.CompletedProcess[str]:
        # Keep the command explicit at call sites that test rejected arguments.
        return self.run_helper("rebase", "--base", BASE, "--expected-head", self.before)

    def advance_base(self, *, conflict: bool = False) -> str:
        filename = "tracked.txt" if conflict else "base.txt"
        (self.repo / filename).write_text("base change\n")
        self.git("add", filename)
        self.git("commit", "-m", "test: advance base")
        self.git("push", "origin", BASE)
        return self.git("rev-parse", "HEAD")

    def remote_head(self) -> str:
        return self.git("rev-parse", f"refs/heads/{BRANCH}", cwd=self.remote)

    def assert_refused(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.worktree), self.before)
        self.assertEqual(self.remote_head(), self.before)

    def test_clean_rebase_and_lease_push_only_update_context_branch(self) -> None:
        base_head = self.advance_base()
        result = self.rebase()
        self.assertEqual(result.returncode, 0, result.stderr)
        rebased = self.git("rev-parse", "HEAD", cwd=self.worktree)
        self.assertNotEqual(rebased, self.before)
        self.assertEqual(self.git("rev-parse", "HEAD^", cwd=self.worktree), base_head)
        self.assertEqual(
            self.git("show", "HEAD:tracked.txt", cwd=self.worktree), "feature"
        )
        self.assertEqual(self.remote_head(), self.before)
        self.assertTrue(self.state.exists())
        self.git("tag", "-a", "unwanted-tag", "-m", "unwanted", cwd=self.worktree)
        self.git("config", "push.followTags", "true")
        self.git("config", "remote.origin.push", "HEAD:refs/heads/main")
        result = self.run_helper(
            "push-with-lease", "--expected-remote-sha", self.before
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.remote_head(), rebased)
        self.assertFalse(self.state.exists())
        self.assertEqual(
            self.git(
                "for-each-ref", "--format=%(refname)", cwd=self.remote
            ).splitlines(),
            [f"refs/heads/{BASE}", f"refs/heads/{BRANCH}"],
        )

    def test_rebase_fetch_does_not_apply_configured_ref_mapping(self) -> None:
        base_head = self.advance_base()
        self.git("branch", "main", self.initial)
        self.git(
            "config",
            "--replace-all",
            "remote.origin.fetch",
            f"+refs/heads/{BASE}:refs/heads/main",
        )
        result = self.rebase()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD^", cwd=self.worktree), base_head)
        self.assertEqual(self.git("rev-parse", "refs/heads/main"), self.initial)
        self.assertEqual(self.remote_head(), self.before)

    def test_rebase_does_not_update_sibling_branch_with_update_refs_enabled(
        self,
    ) -> None:
        base_head = self.advance_base()
        sibling = "feat/432-sibling"
        self.git("branch", sibling, self.before)
        self.git("config", "rebase.updateRefs", "true")
        result = self.rebase()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD^", cwd=self.worktree), base_head)
        self.assertNotEqual(
            self.git("rev-parse", "HEAD", cwd=self.worktree), self.before
        )
        self.assertEqual(self.git("rev-parse", f"refs/heads/{sibling}"), self.before)
        self.assertEqual(self.remote_head(), self.before)

    def test_conflict_continue_preserves_staged_resolution_and_ignores_editors(
        self,
    ) -> None:
        base_head = self.advance_base(conflict=True)
        result = self.rebase()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(
            self.git("diff", "--name-only", "--diff-filter=U", cwd=self.worktree)
        )
        (self.worktree / "tracked.txt").write_text("resolved\n")
        self.git("add", "tracked.txt", cwd=self.worktree)
        marker = self.root / "editor-ran"
        editor = self.root / "editor.sh"
        editor.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 19\n")
        editor.chmod(0o755)
        self.git("config", "core.editor", str(editor))
        self.git("config", "sequence.editor", str(editor))
        recorded = self.state.read_bytes()
        shutil.copyfile(SCRIPT, self.helper)
        result = self.run_helper(
            "rebase-continue",
            env={
                **self.env,
                "GIT_EDITOR": str(editor),
                "GIT_SEQUENCE_EDITOR": str(editor),
                "EDITOR": str(editor),
                "VISUAL": str(editor),
                "GIT_DIR": str(self.root / "wrong.git"),
                "GIT_WORK_TREE": str(self.root / "wrong-tree"),
                "GIT_INDEX_FILE": str(self.root / "wrong-index"),
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())
        self.assertEqual(self.git("rev-parse", "HEAD^", cwd=self.worktree), base_head)
        self.assertEqual(
            self.git("show", "HEAD:tracked.txt", cwd=self.worktree), "resolved"
        )
        self.assertEqual(
            self.git("log", "-1", "--format=%s", cwd=self.worktree),
            "test: feature change",
        )
        self.assertEqual(self.remote_head(), self.before)
        self.assertEqual(self.state.read_bytes(), recorded)

    def test_conflict_abort_restores_original_branch_and_content(self) -> None:
        self.advance_base(conflict=True)
        self.assertNotEqual(self.rebase().returncode, 0)
        result = self.run_helper("rebase-abort")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.worktree), self.before)
        self.assertEqual(
            self.git("branch", "--show-current", cwd=self.worktree), BRANCH
        )
        self.assertEqual((self.worktree / "tracked.txt").read_text(), "feature\n")
        self.assertEqual(self.git("status", "--porcelain", cwd=self.worktree), "")
        self.assertEqual(self.remote_head(), self.before)
        self.assertFalse(self.state.exists())

    def test_stale_lease_preserves_new_remote_commit(self) -> None:
        self.advance_base()
        result = self.rebase()
        self.assertEqual(result.returncode, 0, result.stderr)
        recorded = self.state.read_bytes()
        self.git("checkout", "-b", "feat/999-concurrent", self.before, cwd=self.runner)
        (self.runner / "concurrent.txt").write_text("someone else's change\n")
        self.git("add", "concurrent.txt", cwd=self.runner)
        self.git("commit", "-m", "test: concurrent remote change", cwd=self.runner)
        concurrent = self.git("rev-parse", "HEAD", cwd=self.runner)
        self.git("push", "origin", f"HEAD:refs/heads/{BRANCH}", cwd=self.runner)
        self.git("fetch", "origin", cwd=self.worktree)
        result = self.run_helper(
            "push-with-lease", "--expected-remote-sha", self.before
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.remote_head(), concurrent)
        self.assertEqual(self.state.read_bytes(), recorded)

    def test_initial_dirty_worktree_is_refused(self) -> None:
        self.advance_base()
        (self.worktree / "tracked.txt").write_text("unstaged\n")
        self.assert_refused(self.rebase())
        self.git("add", "tracked.txt", cwd=self.worktree)
        self.assert_refused(self.rebase())
        self.git(
            "restore",
            "--source=HEAD",
            "--staged",
            "--worktree",
            "tracked.txt",
            cwd=self.worktree,
        )
        (self.worktree / "untracked.txt").write_text("keep me\n")
        self.assert_refused(self.rebase())
        self.assertEqual((self.worktree / "untracked.txt").read_text(), "keep me\n")

    def test_wrong_base_or_expected_head_is_refused(self) -> None:
        self.advance_base()
        for base, head in (("main", self.before), (BASE, self.initial), (BASE, "HEAD")):
            with self.subTest(base=base, head=head):
                self.assert_refused(
                    self.run_helper("rebase", "--base", base, "--expected-head", head)
                )

    def test_context_fields_must_match(self) -> None:
        self.advance_base()
        for key, value in (
            ("action", "implement"),
            ("branch", "feat/432-other"),
            ("base", "main"),
            ("expected_head", self.initial),
            ("worktree", str(self.runner)),
            ("runner", str(self.repo)),
        ):
            with self.subTest(field=key):
                original = self.context_data[key]
                self.context_data[key] = value
                self.write_context()
                self.assert_refused(self.rebase())
                self.context_data[key] = original
                self.write_context()

    def test_missing_context_is_refused(self) -> None:
        self.context.unlink()
        self.assert_refused(self.rebase())

    def test_arbitrary_linked_worktree_is_refused_even_with_matching_context(
        self,
    ) -> None:
        other = self.root / "arbitrary-worktree"
        branch = "feat/432-other"
        self.git("worktree", "add", "-b", branch, str(other), self.before)
        self.git("push", "origin", branch)
        self.context_data.update(branch=branch, worktree=str(other))
        self.write_context()
        self.assert_refused(
            self.run_helper(
                "rebase", "--base", BASE, "--expected-head", self.before, worktree=other
            )
        )
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=other), self.before)

    def test_unrelated_rebase_cannot_be_continued_or_aborted(self) -> None:
        self.advance_base(conflict=True)
        result = subprocess.run(
            ["/usr/bin/git", "-C", str(self.worktree), "rebase", f"origin/{BASE}"],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        conflicted_head = self.git("rev-parse", "HEAD", cwd=self.worktree)
        for command in ("rebase-continue", "rebase-abort"):
            with self.subTest(command=command):
                result = self.run_helper(command)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual(
                    self.git("rev-parse", "HEAD", cwd=self.worktree), conflicted_head
                )
                self.assertEqual(
                    self.git(
                        "diff", "--name-only", "--diff-filter=U", cwd=self.worktree
                    ),
                    "tracked.txt",
                )
        self.git("rebase", "--abort", cwd=self.worktree)

    def test_lease_push_requires_recorded_rebase_and_original_expected_sha(
        self,
    ) -> None:
        self.assert_refused(
            self.run_helper("push-with-lease", "--expected-remote-sha", self.before)
        )
        self.advance_base()
        result = self.rebase()
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_helper(
            "push-with-lease", "--expected-remote-sha", self.initial
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.remote_head(), self.before)

    def test_pre_rebase_hook_is_not_bypassed(self) -> None:
        self.advance_base()
        hook = self.repo / ".git/hooks/pre-rebase"
        hook.write_text("#!/bin/sh\nexit 17\n")
        hook.chmod(0o755)
        self.assert_refused(self.rebase())
        self.assertTrue(self.state.exists())
        result = self.run_helper("rebase-abort")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.state.exists())
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.worktree), self.before)
        self.assertEqual(self.git("status", "--porcelain", cwd=self.worktree), "")

    def test_plain_push_cannot_publish_pending_rebase(self) -> None:
        self.advance_base()
        result = self.rebase()
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_helper("push")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.remote_head(), self.before)

    def test_empty_rebase_result_cannot_be_published(self) -> None:
        self.advance_base()
        self.git("cherry-pick", self.before)
        self.git("push", "origin", BASE)
        onto = self.git("rev-parse", "HEAD")
        result = self.rebase()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.worktree), onto)
        recorded = self.state.read_bytes()
        for args in (
            ("rebase-continue",),
            ("push-with-lease", "--expected-remote-sha", self.before),
        ):
            with self.subTest(command=args[0]):
                result = self.run_helper(*args)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("no commits beyond the base", result.stderr)
                self.assertEqual(self.remote_head(), self.before)
                self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.worktree), onto)
                self.assertEqual(self.state.read_bytes(), recorded)

    def test_plain_push_refuses_malformed_records_without_traceback(self) -> None:
        for value in ([], None, True, {}, {"worktree": 1}):
            with self.subTest(record=value):
                self.state.write_text(json.dumps(value))
                recorded = self.state.read_bytes()
                result = self.run_helper("push")
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("invalid rebase record", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(self.remote_head(), self.before)
                self.assertEqual(self.state.read_bytes(), recorded)

    def test_pre_push_hook_is_not_bypassed(self) -> None:
        self.advance_base()
        result = self.rebase()
        self.assertEqual(result.returncode, 0, result.stderr)
        recorded = self.state.read_bytes()
        hook = self.repo / ".git/hooks/pre-push"
        hook.write_text("#!/bin/sh\nexit 17\n")
        hook.chmod(0o755)
        result = self.run_helper(
            "push-with-lease", "--expected-remote-sha", self.before
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.remote_head(), self.before)
        self.assertEqual(self.state.read_bytes(), recorded)

    def test_completed_rebase_can_be_continued_repeatedly(self) -> None:
        self.advance_base()
        result = self.rebase()
        self.assertEqual(result.returncode, 0, result.stderr)
        head = self.git("rev-parse", "HEAD", cwd=self.worktree)
        recorded = self.state.read_bytes()
        for _ in range(2):
            result = self.run_helper("rebase-continue")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.worktree), head)
            self.assertEqual(self.state.read_bytes(), recorded)
            self.assertEqual(self.remote_head(), self.before)

    def test_mismatched_git_rebase_metadata_is_preserved_and_refused(self) -> None:
        self.advance_base(conflict=True)
        self.assertNotEqual(self.rebase().returncode, 0)
        directory = Path(
            self.git(
                "rev-parse",
                "--path-format=absolute",
                "--git-path",
                "rebase-merge",
                cwd=self.worktree,
            )
        )
        recorded = self.state.read_bytes()
        head = self.git("rev-parse", "HEAD", cwd=self.worktree)
        for name, invalid in (
            ("orig-head", self.initial),
            ("onto", self.before),
            ("head-name", "refs/heads/main"),
        ):
            metadata = directory / name
            original = metadata.read_bytes()
            metadata.write_text(invalid + "\n")
            for command in ("rebase-continue", "rebase-abort"):
                with self.subTest(metadata=name, command=command):
                    result = self.run_helper(command)
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertEqual(
                        self.git("rev-parse", "HEAD", cwd=self.worktree), head
                    )
                    self.assertEqual(metadata.read_text(), invalid + "\n")
                    self.assertEqual(self.state.read_bytes(), recorded)
            metadata.write_bytes(original)
        result = self.run_helper("rebase-abort")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.state.exists())

    def test_unresolved_continue_retains_conflicts_and_record(self) -> None:
        self.advance_base(conflict=True)
        self.assertNotEqual(self.rebase().returncode, 0)
        recorded = self.state.read_bytes()
        conflict = (self.worktree / "tracked.txt").read_bytes()
        result = self.run_helper("rebase-continue")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.state.read_bytes(), recorded)
        self.assertEqual((self.worktree / "tracked.txt").read_bytes(), conflict)

    def test_stale_remote_head_before_rebase_does_not_create_record(self) -> None:
        self.advance_base()
        self.git("checkout", "-b", "feat/999-concurrent", self.before, cwd=self.runner)
        (self.runner / "concurrent.txt").write_text("concurrent\n")
        self.git("add", "concurrent.txt", cwd=self.runner)
        self.git("commit", "-m", "test: advance remote feature", cwd=self.runner)
        concurrent = self.git("rev-parse", "HEAD", cwd=self.runner)
        self.git("push", "origin", f"HEAD:refs/heads/{BRANCH}", cwd=self.runner)
        result = self.rebase()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.remote_head(), concurrent)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.worktree), self.before)
        self.assertFalse(self.state.exists())

    def assert_unsafe_origins_refused(self, *args: str) -> None:
        head = self.git("rev-parse", "HEAD", cwd=self.worktree)
        for key, values in (
            ("remote.origin.url", ["https://example.invalid/repo.git"]),
            ("remote.origin.url", [str(self.remote), str(self.remote)]),
            ("remote.origin.pushurl", [str(self.remote), str(self.remote)]),
            ("remote.origin.mirror", ["true"]),
            ("url.https://example.invalid/.insteadOf", [str(self.remote)]),
            ("url.https://example.invalid/.pushInsteadOf", [str(self.remote)]),
        ):
            with self.subTest(setting=key, values=values):
                self.git("config", "--replace-all", key, values[0])
                for value in values[1:]:
                    self.git("config", "--add", key, value)
                result = self.run_helper(*args)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.worktree), head)
                self.assertEqual(self.remote_head(), self.before)
                self.git("config", "--unset-all", key)
                if key == "remote.origin.url":
                    self.git("config", key, str(self.remote))

    def test_unsafe_origin_settings_prevent_rebase(self) -> None:
        self.advance_base()
        self.assert_unsafe_origins_refused(
            "rebase", "--base", BASE, "--expected-head", self.before
        )
        self.assertFalse(self.state.exists())

    def test_unsafe_origin_settings_prevent_lease_push(self) -> None:
        self.advance_base()
        result = self.rebase()
        self.assertEqual(result.returncode, 0, result.stderr)
        recorded = self.state.read_bytes()
        self.assert_unsafe_origins_refused(
            "push-with-lease", "--expected-remote-sha", self.before
        )
        self.assertEqual(self.state.read_bytes(), recorded)

    def test_protected_branch_and_detached_head_are_refused(self) -> None:
        for branch in ("main", "master", "develop", "production", "release"):
            with self.subTest(branch=branch):
                self.git("checkout", "-b", branch, cwd=self.worktree)
                self.assert_refused(self.rebase())
        self.git("checkout", "--detach", cwd=self.worktree)
        self.assert_refused(self.rebase())
        self.assertFalse(self.state.exists())

    def test_foreign_repository_cannot_rebase_or_push(self) -> None:
        foreign = self.root / "foreign"
        self.git("clone", "--branch", BRANCH, str(self.remote), str(foreign))
        self.context_data["worktree"] = str(foreign)
        self.write_context()
        for args in (
            ("rebase", "--base", BASE, "--expected-head", self.before),
            ("push-with-lease", "--expected-remote-sha", self.before),
        ):
            with self.subTest(command=args[0]):
                self.assert_refused(self.run_helper(*args, worktree=foreign))
                self.assertEqual(
                    self.git("rev-parse", "HEAD", cwd=foreign), self.before
                )
        self.assertFalse(self.state.exists())

    def test_extra_options_and_arbitrary_git_forms_are_refused(self) -> None:
        rebase = ("rebase", "--base", BASE, "--expected-head", self.before)
        lease = ("push-with-lease", "--expected-remote-sha", self.before)
        cases = (
            (
                "rebase",
                "--base",
                BASE,
                "--expected-head",
                self.before,
                "--onto",
                "main",
            ),
            ("rebase", "--base", BASE, "--expected-head", self.before, "--interactive"),
            (
                "rebase",
                "--base",
                BASE,
                "--expected-head",
                self.before,
                "--exec",
                "true",
            ),
            ("rebase", "--base", BASE, "--expected-head", self.before, "--skip"),
            ("rebase", "--ba", BASE, "--expected-head", self.before),
            ("rebase-continue", "--no-verify"),
            ("rebase-abort", "--quit"),
            ("push-with-lease", "--expected-remote-sha", self.before, "--force"),
            (
                "push-with-lease",
                "--expected-remote-sha",
                self.before,
                "origin",
                "HEAD:main",
            ),
            (
                "push-with-lease",
                "--expected-remote-sha",
                self.before,
                "--force-with-lease",
            ),
            ("push-with-lease",),
            ("push-with-lease", "--force-with-lease"),
            ("rebase-skip",),
            ("rebase", "--skip"),
            ("rebase-continue", "--skip"),
            ("-c", "core.hooksPath=/dev/null", *rebase),
            (*rebase, "-c", "core.hooksPath=/dev/null"),
        )
        cases += tuple(
            (*rebase, option)
            for option in ("-i", "-f", "--force-rebase", "--autostash", "--no-verify")
        )
        cases += ((*rebase, "-x", "true"),)
        cases += tuple(
            (*lease, option)
            for option in (
                "-f",
                "+HEAD:refs/heads/main",
                "--all",
                "--mirror",
                "--tags",
                "--delete",
                "--no-verify",
                "--force-with-lease=refs/heads/main:" + self.before,
            )
        )
        for args in cases:
            with self.subTest(args=args):
                result = self.run_helper(*args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assert_refused(result)
                self.assertFalse(self.state.exists())


if __name__ == "__main__":
    unittest.main()
