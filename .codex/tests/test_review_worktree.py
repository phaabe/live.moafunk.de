"""Review lifecycle checks against real Git registrations and retained objects."""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/epic"))
import isolated_env  # noqa: E402, F401

import json
import os
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import review_worktree as review  # noqa: E402
import test_codex_tick as tick_fixture  # noqa: E402


class ReviewWorktreeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="review-lifecycle-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo, self.temp, self.state = (
            self.root / name for name in ("repo", "tmp", "state")
        )
        for path in (self.repo, self.temp, self.state):
            path.mkdir()
        self.context_file = self.state / "context.json"
        self.git("init", "-b", "dev/312-interim")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.hooksPath", str(self.root / "no-hooks"))
        self.git(
            "remote", "add", "origin", "https://github.com/phaabe/live.moafunk.de.git"
        )
        (self.repo / "tracked").write_text("initial\n")
        (self.repo / ".gitignore").write_text("ignored\n")
        self.git("add", ".")
        self.git("commit", "-m", "test: fixture")
        self.sha = self.git("rev-parse", "HEAD")
        self.action = {"action": "review", "pr": 431, "sha": self.sha}
        self.pr = {
            "state": "open",
            "draft": False,
            "title": "Fixture review",
            "labels": [],
            "body": "Executor: Claude\n",
            "head": {
                "ref": "feat/430-fixture",
                "sha": self.sha,
                "repo": {"full_name": review.REPO},
            },
            "base": {
                "ref": "dev/312-interim",
                "sha": self.sha,
                "repo": {"full_name": review.REPO},
            },
        }
        self.path = self.temp / f"moafunk-review-431-{self.sha}"
        self.ref = review.retained_ref(431, self.sha)
        self.addCleanup(patch.stopall)
        patch.object(review, "TEMP_ROOT", self.temp).start()
        self.requests = patch.object(
            review.github_quota, "run_gh", side_effect=lambda _: json.dumps(self.pr)
        ).start()
        original_git = review.git

        def local_git(path: Path, *args: str) -> str:
            # Only the transport is fake; objects, refs and worktrees use Git.
            if args[0] == "fetch":
                return original_git(
                    path, "fetch", "--no-tags", "--no-prune", str(self.repo), args[-1]
                )
            return original_git(path, *args)

        patch.object(review, "git", side_effect=local_git).start()
        self.helper = Path.home() / ".local/libexec/codex-cleanup-git.py"
        self.helper.parent.mkdir(parents=True)
        self.helper.write_text(
            "import pathlib, subprocess, sys\n"
            f"trusted = pathlib.Path({str(self.repo)!r})\n"
            f"root = pathlib.Path({str(self.temp)!r})\n"
            "def git(path, *args):\n"
            "    return subprocess.check_output(['/usr/bin/git', '-C', str(path), *args], text=True).strip()\n"
            "assert sys.argv[1] == '--worktree' and sys.argv[3] == 'remove-worktree'\n"
            "runner, path = pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[4])\n"
            "assert runner == trusted and path.parent == root and 'review' in path.name\n"
            "assert (path / git(path, 'rev-parse', '--git-common-dir')).resolve() == (runner / git(runner, 'rev-parse', '--git-common-dir')).resolve()\n"
            "assert not git(path, 'branch', '--show-current')\n"
            "assert not git(path, 'status', '--porcelain', '--untracked-files=all')\n"
            "sha = git(path, 'rev-parse', 'HEAD')\n"
            "assert git(runner, 'for-each-ref', '--contains=' + sha, '--format=%(refname)', 'refs/heads', 'refs/remotes', 'refs/tags')\n"
            "git(runner, 'worktree', 'remove', '--', str(path))\n"
        )

    def git(self, *args: str, path: Path | None = None) -> str:
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_")
        }
        return subprocess.check_output(
            ["/usr/bin/git", "-C", str(path or self.repo), *args],
            text=True,
            stderr=subprocess.PIPE,
            env=env,
        ).strip()

    def prepare(self) -> Path:
        return review.prepare(self.repo, self.action, self.state, self.context_file)

    def context(self) -> dict[str, object]:
        return review.load_context(self.context_file)

    def complete(self, *, published: bool = False) -> dict[str, object]:
        context = self.context()
        path = Path(context["artifact_dir"]) / "bundle.json"
        bundle = json.loads(path.read_text())
        bundle.update(
            status="published" if published else "complete",
            verdict="APPROVED",
            findings=[],
            comments=[
                {
                    "body": f"Review: APPROVED by Codex at {self.sha}",
                    "url": "https://github.com/phaabe/live.moafunk.de/issues/431#issuecomment-123"
                    if published
                    else None,
                }
            ],
        )
        candidate = self.state / "candidate.json"
        candidate.write_text(json.dumps(bundle))
        if published:
            review.validate_bundle(context, bundle)
            review.write_json(path, bundle)
        else:
            review.save_bundle(self.context_file, candidate)
        return bundle

    def test_reuses_clean_exact_head_and_keeps_evidence_external(self) -> None:
        self.assertEqual(self.prepare(), self.path)
        first = self.context()
        self.assertEqual(self.prepare(), self.path)
        second = self.context()
        self.assertNotEqual(first["attempt_dir"], second["attempt_dir"])
        self.assertEqual(first["review_started_at"], second["review_started_at"])
        self.assertTrue(review.valid_review_started_at(first["review_started_at"]))
        self.assertEqual(self.git("rev-parse", "HEAD", path=self.path), self.sha)
        self.assertEqual(self.git("branch", "--show-current", path=self.path), "")
        self.assertEqual(self.git("rev-parse", self.ref), self.sha)
        self.assertEqual(self.git("status", "--porcelain", path=self.path), "")
        self.assertTrue((Path(second["artifact_dir"]) / "bundle.json").is_file())

    def test_rejects_foreign_origin_before_any_metadata_or_worktree(self) -> None:
        self.git("remote", "set-url", "origin", "https://github.com/other/repo.git")
        with self.assertRaisesRegex(review.Refused, "origin"):
            self.prepare()
        self.requests.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_rejects_wrong_head_existing_checkout_without_changes(self) -> None:
        self.prepare()
        self.git("commit", "--allow-empty", "-m", "test: another head")
        other = self.git("rev-parse", "HEAD")
        self.git("switch", "--detach", other, path=self.path)
        with self.assertRaisesRegex(review.Refused, "HEAD"):
            self.prepare()
        self.assertEqual(self.git("rev-parse", "HEAD", path=self.path), other)

    def test_rejects_unregistered_foreign_directory(self) -> None:
        self.path.mkdir()
        self.git("init", path=self.path)
        marker = self.path / "keep"
        marker.write_text("foreign work\n")
        with self.assertRaisesRegex(review.Refused, "registered"):
            self.prepare()
        self.assertEqual(marker.read_text(), "foreign work\n")

    def test_rejects_tracked_and_untracked_changes(self) -> None:
        self.prepare()
        for name in ("tracked", "untracked"):
            with self.subTest(name=name):
                file = self.path / name
                file.write_text("keep\n")
                with self.assertRaisesRegex(review.Refused, "changes"):
                    self.prepare()
                self.assertEqual(file.read_text(), "keep\n")
                if name == "tracked":
                    file.write_text("initial\n")
                else:
                    file.unlink()

    def test_rejects_locked_checkout(self) -> None:
        self.prepare()
        self.git("worktree", "lock", str(self.path))
        with self.assertRaisesRegex(review.Refused, "unlocked"):
            self.prepare()
        with self.assertRaisesRegex(review.Refused, "unlocked"):
            review.cleanup(self.repo, self.context())
        self.assertTrue(self.path.exists())

    def test_ignored_build_outputs_do_not_prevent_cleanup(self) -> None:
        self.prepare()
        (self.path / "ignored").write_text("build output\n")
        review.cleanup(self.repo, self.context())
        self.assertFalse(self.path.exists())

    def test_evidence_refuses_another_checkout_and_symlink_parent(self) -> None:
        other = self.root / "other"
        self.git("worktree", "add", "--detach", str(other), self.sha)
        for state in (other / "state", self.root / "linked-state"):
            if state.name == "linked-state":
                state.symlink_to(self.state, target_is_directory=True)
            with self.subTest(state=state), self.assertRaises(review.Refused):
                review.prepare(self.repo, self.action, state, self.context_file)
        self.assertFalse(self.path.exists())

    def test_stale_bundle_and_context_are_archived_before_fresh_review(self) -> None:
        self.prepare()
        artifact = Path(self.context()["artifact_dir"])
        path = artifact / "bundle.json"
        for status in ("draft", "complete", "published"):
            with self.subTest(status=status):
                if status != "draft":
                    self.complete(published=status == "published")
                before = path.read_bytes()
                old_context = (artifact / "context.json").read_bytes()
                self.pr["title"] += " changed"
                self.prepare()
                archived = list((artifact / "archive").glob("*/bundle.json"))
                match = next(item for item in archived if item.read_bytes() == before)
                self.assertEqual(
                    match.with_name("context.json").read_bytes(), old_context
                )
                current = json.loads(path.read_text())
                self.assertEqual(current["status"], "draft")
                self.assertEqual(current["inputs"]["title"], self.pr["title"])
                self.assertTrue(
                    review.valid_review_started_at(current["review_started_at"])
                )

    def test_legacy_bundle_without_baseline_gets_fresh_review(self) -> None:
        self.prepare()
        self.complete()
        artifact = Path(self.context()["artifact_dir"])
        path = artifact / "bundle.json"
        legacy = json.loads(path.read_text())
        del legacy["review_started_at"]
        path.write_text(json.dumps(legacy))
        self.prepare()
        self.assertEqual(json.loads(path.read_text())["status"], "draft")
        archived = list((artifact / "archive").glob("*/bundle.json"))
        self.assertEqual(len(archived), 1)
        self.assertEqual(json.loads(archived[0].read_text()), legacy)

    def test_changed_base_body_or_labels_archives_completed_evidence(self) -> None:
        self.prepare()
        for field, value in (
            ("base", {**self.pr["base"], "sha": "a" * 40}),
            ("body", "Executor: Claude\nChanged body\n"),
            ("labels", [{"name": "project::Stream"}]),
        ):
            with self.subTest(field=field):
                self.complete()
                self.pr[field] = value
                self.prepare()
                path = Path(self.context()["artifact_dir"]) / "bundle.json"
                self.assertEqual(json.loads(path.read_text())["status"], "draft")

    def test_failed_archive_preserves_active_evidence(self) -> None:
        self.prepare()
        artifact = Path(self.context()["artifact_dir"])
        path = artifact / "bundle.json"
        before = path.read_bytes()
        (artifact / "archive").symlink_to(self.repo, target_is_directory=True)
        self.pr["title"] = "Changed review inputs"
        with self.assertRaisesRegex(review.Refused, "archive path"):
            self.prepare()
        self.assertEqual(path.read_bytes(), before)

    def test_archive_write_failure_preserves_active_bundle_and_context(self) -> None:
        self.prepare()
        artifact = Path(self.context()["artifact_dir"])
        before = {
            name: (artifact / name).read_bytes()
            for name in ("bundle.json", "context.json")
        }
        self.pr["title"] = "Changed review inputs"
        with patch.object(review.os, "fsync", side_effect=[None, OSError("disk full")]):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.prepare()
        for name, expected in before.items():
            self.assertEqual((artifact / name).read_bytes(), expected)

    def test_symlinked_attempt_parent_cannot_redirect_evidence(self) -> None:
        artifact = self.state / "reviews" / "431" / self.sha
        artifact.mkdir(parents=True)
        (artifact / "attempts").symlink_to(self.repo, target_is_directory=True)
        with self.assertRaisesRegex(review.Refused, "attempt path"):
            self.prepare()
        self.assertFalse(self.path.exists())

    def test_symlinked_checkout_is_not_reused(self) -> None:
        self.path.symlink_to(self.repo, target_is_directory=True)
        with self.assertRaisesRegex(review.Refused, "symlinks"):
            self.prepare()
        self.assertTrue(self.path.is_symlink())

    def test_retained_ref_collision_is_never_overwritten(self) -> None:
        self.git("commit", "--allow-empty", "-m", "test: another head")
        other = self.git("rev-parse", "HEAD")
        self.git("update-ref", self.ref, other)
        with self.assertRaisesRegex(review.Refused, "another commit"):
            self.prepare()
        self.assertEqual(self.git("rev-parse", self.ref), other)
        self.assertFalse(self.path.exists())

    def test_symbolic_retention_ref_cannot_redirect_cleanup(self) -> None:
        self.git("symbolic-ref", self.ref, "refs/heads/dev/312-interim")
        with self.assertRaisesRegex(review.Refused, "another commit"):
            self.prepare()
        self.assertEqual(self.git("rev-parse", "refs/heads/dev/312-interim"), self.sha)
        self.assertFalse(self.path.exists())

    def test_published_review_removes_checkout_ref_but_keeps_evidence(self) -> None:
        self.prepare()
        self.complete(published=True)
        context = self.context()
        evidence = Path(context["artifact_dir"]) / "bundle.json"
        before = evidence.read_bytes()
        review.cleanup(self.repo, context)
        self.assertFalse(self.path.exists())
        self.assertNotIn(self.path, review.registrations(self.repo))
        self.assertEqual(self.git("for-each-ref", self.ref), "")
        self.assertEqual(evidence.read_bytes(), before)

    def test_pending_bundle_keeps_exact_commit_reachable_after_cleanup(self) -> None:
        self.prepare()
        self.complete()
        review.cleanup(self.repo, self.context())
        self.assertFalse(self.path.exists())
        self.assertEqual(self.git("rev-parse", self.ref), self.sha)

    def test_helper_refusal_preserves_checkout_ref_and_bundle(self) -> None:
        self.prepare()
        self.helper.write_text("import sys\nsys.exit('fixture helper refusal')\n")
        with self.assertRaisesRegex(review.Refused, "fixture helper refusal"):
            review.cleanup(self.repo, self.context())
        self.assertTrue(self.path.exists())
        self.assertEqual(self.git("rev-parse", self.ref), self.sha)
        self.assertTrue((Path(self.context()["artifact_dir"]) / "bundle.json").exists())

    def test_completed_bundle_is_not_overwritten_or_rereviewed(self) -> None:
        self.prepare()
        bundle = self.complete()
        path = Path(self.context()["artifact_dir"]) / "bundle.json"
        before = path.read_bytes()
        with self.assertRaises(review.ExistingBundle):
            self.prepare()
        self.assertEqual(path.read_bytes(), before)
        bundle["findings"] = ["changed"]
        candidate = self.state / "changed.json"
        candidate.write_text(json.dumps(bundle))
        with self.assertRaisesRegex(review.Refused, "cannot be overwritten"):
            review.save_bundle(self.context_file, candidate)
        self.assertEqual(path.read_bytes(), before)

    def test_save_bundle_cannot_assert_publication(self) -> None:
        self.prepare()
        bundle = self.complete()
        path = Path(self.context()["artifact_dir"]) / "bundle.json"
        before = path.read_bytes()
        bundle["comments"][0]["url"] = (
            "https://github.com/phaabe/live.moafunk.de/issues/431#issuecomment-123"
        )
        candidate = self.state / "candidate.json"
        for status in ("complete", "published"):
            bundle["status"] = status
            candidate.write_text(json.dumps(bundle))
            with (
                self.subTest(status=status),
                self.assertRaisesRegex(review.Refused, "delivery helper"),
            ):
                review.save_bundle(self.context_file, candidate)
            self.assertEqual(path.read_bytes(), before)

    def test_draft_or_missing_completion_is_never_inferred_from_verdict(self) -> None:
        self.prepare()
        bundle = self.complete()
        candidate = self.state / "candidate.json"
        for status in ("draft", None):
            bundle["status"] = status
            candidate.write_text(json.dumps(bundle))
            with (
                self.subTest(status=status),
                self.assertRaisesRegex(review.Refused, "completed findings"),
            ):
                review.save_bundle(self.context_file, candidate)

    def test_only_publisher_can_record_a_pending_comment(self) -> None:
        self.prepare()
        bundle = self.complete()
        bundle["pending_comment"] = 0
        review.validate_bundle(self.context(), bundle)
        candidate = self.state / "candidate.json"
        candidate.write_text(json.dumps(bundle))
        with self.assertRaisesRegex(review.Refused, "delivery helper"):
            review.save_bundle(self.context_file, candidate)
        for invalid in (None, False, -1, len(bundle["comments"]), "0"):
            bundle["pending_comment"] = invalid
            with (
                self.subTest(invalid=invalid),
                self.assertRaisesRegex(review.Refused, "invalid pending"),
            ):
                review.validate_bundle(self.context(), bundle)

    def test_bundle_cannot_change_review_start_time(self) -> None:
        self.prepare()
        bundle = self.complete()
        bundle["review_started_at"] = "2026-01-01T00:00:00Z"
        candidate = self.state / "candidate.json"
        candidate.write_text(json.dumps(bundle))
        with self.assertRaisesRegex(review.Refused, "must match"):
            review.save_bundle(self.context_file, candidate)

    def test_duplicate_comment_bodies_cannot_share_publication_evidence(self) -> None:
        self.prepare()
        bundle = self.complete()
        bundle["comments"][:0] = [
            {"body": "A finding", "url": None},
            {"body": "A finding", "url": None},
        ]
        with self.assertRaisesRegex(review.Refused, "distinct"):
            review.validate_bundle(self.context(), bundle)

    def test_metadata_refuses_owner_base_fork_or_moved_head(self) -> None:
        original = json.dumps(self.pr)
        for field, value in (
            ("body", "Executor: Codex\n"),
            ("draft", True),
            ("state", "closed"),
            ("head", {**self.pr["head"], "sha": "f" * 40}),
            ("base", {**self.pr["base"], "ref": "main"}),
            ("head", {**self.pr["head"], "repo": {"full_name": "other/repo"}}),
        ):
            with self.subTest(field=field, value=value):
                self.pr = {**json.loads(original), field: value}
                with self.assertRaises(review.Refused):
                    self.prepare()
                self.assertFalse(self.path.exists())

    def test_crlf_ownership_body_prepares_and_preserves_original_inputs(self) -> None:
        self.pr["body"] = (
            "Executor: Claude\r\nIssue: https://github.com/phaabe/live.moafunk.de/issues/425\r\n"
        )
        self.prepare()
        self.assertEqual(self.context()["inputs"]["body"], self.pr["body"])

    def test_conflicted_pr_cannot_start_or_resume_review(self) -> None:
        for fields in ({"mergeable": False}, {"mergeable_state": "dirty"}):
            with self.subTest(fields=fields):
                self.pr.update(fields)
                with self.assertRaisesRegex(review.Refused, "merge conflicts"):
                    self.prepare()
                self.assertFalse(self.path.exists())
                for key in fields:
                    del self.pr[key]
        self.pr["mergeable"] = None
        self.assertEqual(self.prepare(), self.path)

    def test_sweep_lists_then_removes_only_clean_closed_known_reviews(self) -> None:
        self.prepare()
        self.pr["state"] = "closed"
        unknown = self.temp / "codex-review-unidentified"
        unknown.mkdir()
        review.sweep(self.repo, self.state, False)
        self.assertTrue(self.path.exists())
        review.sweep(self.repo, self.state, True)
        self.assertFalse(self.path.exists())
        self.assertTrue(unknown.exists())
        self.assertFalse((self.state / "codex.lock").exists())

    def test_sweep_respects_active_target_and_runner_lock(self) -> None:
        self.prepare()
        self.pr["state"] = "closed"
        lock_path = review.target_lock.paths(
            {"pr": 431}, review.target_lock.lock_dir()
        )[0]
        with lock_path.open("a") as lock:
            self.assertTrue(review.target_lock.acquire([lock.fileno()]))
            review.sweep(self.repo, self.state, True)
        self.assertTrue(self.path.exists())
        review.epic_lock.acquire(self.state / "codex.lock", os.getpid(), 300)
        with self.assertRaisesRegex(review.Refused, "runner is active"):
            review.sweep(self.repo, self.state, True)
        self.assertTrue(self.path.exists())

    def test_sweep_lists_closed_orphan_ref_without_deleting_pending_evidence(
        self,
    ) -> None:
        self.prepare()
        review.cleanup(self.repo, self.context())
        self.pr["state"] = "closed"
        bundle = Path(self.context()["artifact_dir"]) / "bundle.json"
        before = bundle.read_bytes()
        for apply in (False, True):
            with self.subTest(apply=apply), self.assertLogs(level="INFO") as logs:
                review.sweep(self.repo, self.state, apply)
            self.assertIn(self.ref, "\n".join(logs.output))
            self.assertIn(
                "closed PR 431, no registered checkout", "\n".join(logs.output)
            )
            self.assertIn("pending evidence or manual review", "\n".join(logs.output))
            self.assertEqual(self.git("rev-parse", self.ref), self.sha)
            self.assertEqual(bundle.read_bytes(), before)

    def test_sweep_open_orphan_ref_is_not_reported_closed(self) -> None:
        self.git("update-ref", self.ref, self.sha)
        self.git("commit", "--allow-empty", "-m", "test: move runner head")
        with self.assertLogs(level="INFO") as logs:
            review.sweep(self.repo, self.state, True)
        output = "\n".join(logs.output)
        self.assertIn(self.ref, output)
        self.assertIn("PR is open or its closed state is unknown", output)
        self.assertNotIn("no registered checkout", output)
        self.assertEqual(self.git("rev-parse", self.ref), self.sha)

    def test_sweep_attached_ref_has_only_checkout_report(self) -> None:
        self.prepare()
        self.pr["state"] = "closed"
        self.requests.reset_mock()
        with self.assertLogs(level="INFO") as logs:
            review.sweep(self.repo, self.state, False)
        output = "\n".join(logs.output)
        self.assertIn(str(self.path), output)
        self.assertNotIn(self.ref, output)
        self.requests.assert_called_once()

    def test_sweep_rechecks_closed_state_after_orphan_listing(self) -> None:
        self.prepare()
        self.git("commit", "--allow-empty", "-m", "test: another review head")
        orphan_sha = self.git("rev-parse", "HEAD")
        orphan = review.retained_ref(431, orphan_sha)
        self.git("update-ref", orphan, orphan_sha)
        self.requests.reset_mock()
        self.requests.side_effect = [
            json.dumps({"state": "closed"}),
            json.dumps({"state": "open"}),
        ]
        with self.assertLogs(level="INFO") as logs:
            review.sweep(self.repo, self.state, True)
        self.assertIn(orphan, "\n".join(logs.output))
        self.assertTrue(self.path.exists())
        self.assertEqual(self.requests.call_count, 2)
        self.assertEqual(self.git("rev-parse", orphan), orphan_sha)

    def test_sweep_reports_invalid_refs_without_network_or_deletion(self) -> None:
        malformed = "refs/remotes/codex-review/not-a-review"
        mismatch = review.retained_ref(432, "f" * 40)
        symbolic = review.retained_ref(433, self.sha)
        self.git("update-ref", malformed, self.sha)
        self.git("update-ref", mismatch, self.sha)
        self.git("symbolic-ref", symbolic, "refs/heads/dev/312-interim")
        self.requests.reset_mock()
        with self.assertLogs(level="WARNING") as logs:
            review.sweep(self.repo, self.state, True)
        output = "\n".join(logs.output)
        for ref in (malformed, mismatch, symbolic):
            self.assertIn(ref, output)
            self.assertEqual(self.git("rev-parse", ref), self.sha)
        self.requests.assert_not_called()

    def test_prepare_cli_blocks_failed_or_malformed_reads_without_evidence(
        self,
    ) -> None:
        action = self.state / "action.json"
        action.write_text(json.dumps(self.action))
        argv = [
            "review_worktree.py",
            "prepare",
            "--runner",
            str(self.repo),
            "--state-dir",
            str(self.state),
            "--action-file",
            str(action),
            "--context-file",
            str(self.context_file),
        ]
        for failure in (
            OSError("offline"),
            subprocess.CalledProcessError(1, "gh"),
            "{",
            "[]",
            json.dumps({"state": "open"}),
            json.dumps({"state": []}),
            json.dumps({**self.pr, "head": None}),
        ):
            with (
                self.subTest(failure=failure),
                patch.object(sys, "argv", argv),
                self.assertLogs(level="WARNING"),
            ):
                self.requests.side_effect = (
                    failure if isinstance(failure, Exception) else None
                )
                self.requests.return_value = failure
                self.assertEqual(review.main(), 5)
            self.assertFalse(self.path.exists())
            self.assertFalse(self.context_file.exists())
            self.assertFalse((self.state / "reviews").exists())

    def test_sweep_stops_on_failed_github_read_and_keeps_all_refs(self) -> None:
        second = review.retained_ref(432, self.sha)
        self.git("update-ref", self.ref, self.sha)
        self.git("update-ref", second, self.sha)
        self.git("commit", "--allow-empty", "-m", "test: move runner head")
        self.requests.reset_mock()
        self.requests.side_effect = subprocess.CalledProcessError(1, "gh")
        with self.assertRaises(review.github_state.ReadBlocked):
            review.sweep(self.repo, self.state, True)
        self.requests.assert_called_once()
        self.assertFalse((self.state / "codex.lock").exists())
        for ref in (self.ref, second):
            self.assertEqual(self.git("rev-parse", ref), self.sha)


class RunnerReviewLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = tick_fixture.TickTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.repo = self.fixture.repo.resolve()
        self.env = self.fixture.env
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        self.env["EPIC_STATE_DIR"] = str(self.fixture.state.resolve())
        self.temp = self.fixture.root.resolve() / "review-worktrees"
        self.temp.mkdir()
        self.git("init", "-b", "dev/312-interim")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.hooksPath", str(self.fixture.root / "no-hooks"))
        self.git(
            "remote", "add", "origin", "https://github.com/phaabe/live.moafunk.de.git"
        )
        # The wrapper imports the production module, changing only external
        # boundaries. All origin, worktree, ref and cleanup checks still run.
        (self.repo / ".codex/review_worktree.py").write_text(
            "import json, os, pathlib, sys\n"
            f"sys.path.insert(0, {str(tick_fixture.ROOT)!r})\n"
            "import review_worktree as review\n"
            f"review.TEMP_ROOT = pathlib.Path({str(self.temp)!r})\n"
            "review.github_quota.run_gh = lambda args: os.environ['TEST_REAL_REVIEW_PR']\n"
            "original_git = review.git\n"
            "def local_git(path, *args):\n"
            "    if args[0] == 'fetch':\n"
            f"        return original_git(path, 'fetch', '--no-tags', '--no-prune', {str(self.repo)!r}, args[-1])\n"
            "    return original_git(path, *args)\n"
            "review.git = local_git\n"
            "raise SystemExit(review.main())\n"
        )
        self.git("add", ".")
        self.git("commit", "-m", "test: runner lifecycle fixture")
        self.sha = self.git("rev-parse", "HEAD")
        self.path = self.temp / f"moafunk-review-406-{self.sha}"
        self.ref = review.retained_ref(406, self.sha)
        self.env["TEST_DECISION"] = json.dumps(
            {"action": "review", "pr": 406, "sha": self.sha}
        )
        self.env["TEST_REAL_REVIEW_PR"] = json.dumps(
            {
                "state": "open",
                "draft": False,
                "title": "Fixture review",
                "labels": [],
                "body": "Executor: Claude\n",
                "head": {
                    "ref": "feat/425-fixture",
                    "sha": self.sha,
                    "repo": {"full_name": review.REPO},
                },
                "base": {
                    "ref": "dev/312-interim",
                    "sha": self.sha,
                    "repo": {"full_name": review.REPO},
                },
            }
        )
        installed = self.fixture.home / ".local/libexec/codex-cleanup-git.py"
        installed.parent.mkdir(parents=True)
        installed.write_text(
            "import pathlib, subprocess, sys\n"
            f"trusted = pathlib.Path({str(self.repo)!r})\n"
            f"root = pathlib.Path({str(self.temp)!r})\n"
            "def git(path, *args):\n"
            "    return subprocess.check_output(['/usr/bin/git', '-C', str(path), *args], text=True).strip()\n"
            "assert sys.argv[1] == '--worktree' and sys.argv[3] == 'remove-worktree'\n"
            "runner, path = pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[4])\n"
            "assert runner == trusted and path.parent == root and 'review' in path.name\n"
            "assert (path / git(path, 'rev-parse', '--git-common-dir')).resolve() == (runner / git(runner, 'rev-parse', '--git-common-dir')).resolve()\n"
            "assert not git(path, 'branch', '--show-current')\n"
            "assert not git(path, 'status', '--porcelain', '--untracked-files=all')\n"
            "sha = git(path, 'rev-parse', 'HEAD')\n"
            "assert git(runner, 'for-each-ref', '--contains=' + sha, '--format=%(refname)', 'refs/heads', 'refs/remotes', 'refs/tags')\n"
            "git(runner, 'worktree', 'remove', '--', str(path))\n"
        )

    def git(self, *args: str) -> str:
        env = {
            key: value for key, value in self.env.items() if not key.startswith("GIT_")
        }
        return subprocess.check_output(
            ["/usr/bin/git", "-C", str(self.repo), *args],
            text=True,
            stderr=subprocess.PIPE,
            env=env,
        ).strip()

    def assert_review_cleaned_with_evidence(self) -> Path:
        artifact = self.fixture.state / "reviews" / "406" / self.sha
        context = json.loads((artifact / "context.json").read_text())
        attempt = Path(context["attempt_dir"])
        self.assertFalse(self.path.exists())
        self.assertNotIn(str(self.path), self.git("worktree", "list", "--porcelain"))
        self.assertEqual(self.git("rev-parse", self.ref), self.sha)
        self.assertEqual(
            json.loads((artifact / "bundle.json").read_text())["status"], "draft"
        )
        self.assertIn("fake Codex stdout", (attempt / "model.log").read_text())
        self.assertIn("fake Codex stderr", (attempt / "model.log").read_text())
        self.assertFalse(self.fixture.lock.exists())
        self.assertFalse(self.fixture.record.exists())
        return attempt

    def test_model_failure_removes_real_checkout_and_preserves_evidence(self) -> None:
        self.env["TEST_CODEX_EXIT"] = "17"
        result = self.fixture.run_tick()
        self.assertEqual(
            result.returncode,
            17,
            result.stdout
            + result.stderr
            + (self.fixture.state / "codex.log").read_text(),
        )
        attempt = self.assert_review_cleaned_with_evidence()
        self.assertEqual(
            json.loads((attempt / "result.json").read_text())["status"], "completed"
        )

    def test_model_timeout_stops_child_then_removes_real_checkout(self) -> None:
        self.env["EPIC_TICK_TIMEOUT_SECONDS"] = "60"
        timeout_pid = self.fixture.timeout_command("codex")
        process, connection = self.fixture.blocked_tick()
        # Startup has completed; expire the real GNU timeout process now.
        os.kill(int(timeout_pid.read_text()), signal.SIGALRM)
        self.assertEqual(process.wait(timeout=15), 124)
        self.assertEqual(connection.recv(1), b"")
        attempt = self.assert_review_cleaned_with_evidence()
        with self.assertRaises(ProcessLookupError):
            os.kill(int((attempt / "model.pid").read_text()), 0)


if __name__ == "__main__":
    unittest.main()
