"""Delivery retries use saved evidence and confirmed REST comments only."""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/epic"))
import isolated_env  # noqa: E402, F401

import json
import fcntl
import os
import subprocess
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import review_delivery as delivery  # noqa: E402
import test_review_worktree  # noqa: E402


class DeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = test_review_worktree.ReviewWorktreeTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.prepare()
        self.fixture.complete()
        self.context = self.fixture.context()
        self.context_file = Path(self.context["artifact_dir"]) / "context.json"
        self.bundle_file = self.context_file.with_name("bundle.json")
        self.started = delivery.github_quota.iso(time.time() - 60)
        self.context["review_started_at"] = self.started
        self.context_file.write_text(json.dumps(self.context))
        bundle = self.bundle()
        bundle["review_started_at"] = self.started
        bundle["comments"].insert(
            0, {"body": "Finding: handle a failed upload.", "url": None}
        )
        bundle["findings"] = ["Handle a failed upload."]
        self.bundle_file.write_text(json.dumps(bundle))
        policy = self.fixture.repo / ".github/epic-lanes.yml"
        policy.parent.mkdir()
        policy.write_text(
            json.dumps(
                {
                    "repository": delivery.review.REPO,
                    "trusted_reviewers": {"Codex": ["phaabe"]},
                }
            )
        )
        self.remote: list[dict[str, object]] = []
        self.reviews: list[dict[str, object]] = []
        self.posts: list[str] = []
        self.lose_response = False
        self.hide_post = False
        self.quota_after_post = False
        self.change_on_comments = False
        self.fixture.requests.side_effect = self.github
        quota = patch.object(
            delivery.github_quota, "STATE_DIR", self.fixture.state / "quota"
        )
        quota.start()
        self.addCleanup(quota.stop)
        path = delivery.target_lock.paths(
            {"pr": self.context["pr"]}, delivery.target_lock.lock_dir()
        )[0]
        self.lock = path.open("a")
        self.addCleanup(self.lock.close)
        try:
            previous = os.dup(8)
        except OSError:
            previous = None
        os.dup2(self.lock.fileno(), 8)
        self.assertTrue(delivery.target_lock.acquire([8]))

        def restore_fd() -> None:
            if previous is None:
                os.close(8)
            else:
                os.dup2(previous, 8)
                os.close(previous)

        self.addCleanup(restore_fd)

    def bundle(self) -> dict[str, object]:
        return json.loads(self.bundle_file.read_text())

    def comment(
        self, body: str, *, login: str = "phaabe", at: str | None = None
    ) -> dict[str, object]:
        number = 100 + len(self.remote)
        return {
            "id": number,
            "body": body,
            "html_url": f"https://github.com/phaabe/live.moafunk.de/issues/431#issuecomment-{number}",
            "created_at": at or delivery.github_quota.iso(time.time()),
            "updated_at": at or delivery.github_quota.iso(time.time()),
            "user": {"login": login},
        }

    def github(self, args: list[str]) -> str:
        if args[1].endswith("/pulls/431"):
            return json.dumps(self.fixture.pr)
        if "/reviews?" in args[1]:
            return json.dumps([self.reviews])
        if "POST" not in args:
            self.assertIn("/comments?", args[1])
            if self.change_on_comments:
                self.fixture.pr["title"] = "Changed while reading comments"
            return json.dumps([self.remote])
        body = args[args.index("--raw-field") + 1].removeprefix("body=")
        saved = self.bundle()
        self.assertEqual(saved["comments"][saved["pending_comment"]]["body"], body)
        self.posts.append(body)
        if not self.hide_post:
            self.remote.append(self.comment(body))
        if self.quota_after_post:
            delivery.github_quota.record(
                delivery.github_quota.STATE_DIR, time.time(), lookup=lambda: None
            )
        if self.lose_response:
            self.lose_response = False
            raise subprocess.TimeoutExpired("gh", 120)
        return "{}"

    def deliver(self) -> None:
        delivery.deliver(self.context_file)

    def test_findings_precede_verdict_and_published_retry_does_not_post(self) -> None:
        self.deliver()
        self.assertEqual(
            self.posts, [item["body"] for item in self.bundle()["comments"]]
        )
        self.assertEqual(self.bundle()["status"], "published")
        self.assertTrue(all(item["url"] for item in self.bundle()["comments"]))
        self.deliver()
        self.assertEqual(len(self.posts), 2)

    def test_accepted_post_with_lost_response_is_reconciled_on_restart(self) -> None:
        self.lose_response = True
        with self.assertRaises(delivery.github_state.ReadBlocked):
            self.deliver()
        self.assertIsNone(self.bundle()["comments"][0]["url"])
        delivery.resume(self.fixture.repo, self.fixture.action, self.fixture.state)
        self.assertEqual(len(self.posts), 2)
        self.assertEqual(self.bundle()["status"], "published")

    def test_quota_between_findings_and_verdict_preserves_and_resumes(self) -> None:
        self.quota_after_post = True
        with self.assertRaises(delivery.feature_worktree.QuotaWait):
            self.deliver()
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.bundle()["status"], "complete")
        self.quota_after_post = False
        (delivery.github_quota.STATE_DIR / delivery.github_quota.WAIT_FILE).unlink()
        self.deliver()
        self.assertEqual(len(self.posts), 2)

    def test_pause_makes_no_github_calls(self) -> None:
        pause = Path.home() / ".epic-pause"
        pause.touch()
        self.addCleanup(pause.unlink)
        self.fixture.requests.reset_mock()
        with self.assertRaisesRegex(delivery.Refused, "paused"):
            self.deliver()
        self.fixture.requests.assert_not_called()

    def test_shared_quota_wait_makes_no_github_calls(self) -> None:
        delivery.github_quota.record(
            delivery.github_quota.STATE_DIR, time.time(), lookup=lambda: None
        )
        self.fixture.requests.reset_mock()
        with self.assertRaises(delivery.feature_worktree.QuotaWait):
            self.deliver()
        self.fixture.requests.assert_not_called()

    def test_rest_limit_records_shared_wait_without_reset_lookup(self) -> None:
        def limited(args: list[str]) -> str:
            if "/comments?" in args[1]:
                raise subprocess.CalledProcessError(
                    1, "gh", stderr="API rate limit exceeded"
                )
            return self.github(args)

        self.fixture.requests.side_effect = limited
        with self.assertRaises(delivery.feature_worktree.QuotaWait):
            self.deliver()
        self.assertTrue(
            (delivery.github_quota.STATE_DIR / delivery.github_quota.WAIT_FILE).exists()
        )
        self.assertFalse(
            any(
                call.args[0][:2] == ["api", "graphql"]
                for call in self.fixture.requests.call_args_list
            )
        )
        self.assertEqual(self.posts, [])

    def test_changed_base_or_review_metadata_requires_fresh_review(self) -> None:
        for change in ("base", "title"):
            with self.subTest(change=change):
                previous = json.dumps(self.fixture.pr)
                if change == "base":
                    self.fixture.pr["base"]["sha"] = "b" * 40
                else:
                    self.fixture.pr["title"] = "Changed review inputs"
                with self.assertRaises(delivery.FreshReview):
                    self.deliver()
                self.fixture.pr = json.loads(previous)
        self.assertEqual(self.posts, [])

    def test_metadata_change_during_comment_read_prevents_post(self) -> None:
        self.change_on_comments = True
        with self.assertRaises(delivery.FreshReview):
            self.deliver()
        self.assertEqual(self.posts, [])

    def test_newer_opposite_codex_verdict_blocks_pending_bundle(self) -> None:
        self.remote.append(
            self.comment(f"Review: CHANGES REQUESTED by Codex at {self.fixture.sha}")
        )
        with self.assertRaisesRegex(delivery.Refused, "newer conflicting"):
            self.deliver()
        self.assertEqual(self.posts, [])

    def test_older_opposite_codex_verdict_does_not_block_new_review(self) -> None:
        self.remote.append(
            self.comment(
                f"Review: CHANGES REQUESTED by Codex at {self.fixture.sha}",
                at="2020-01-01T00:00:00Z",
            )
        )
        self.deliver()
        self.assertEqual(len(self.posts), 2)

    def test_newer_opposite_formal_review_blocks_pending_bundle(self) -> None:
        self.reviews.append(
            {
                "state": "CHANGES_REQUESTED",
                "user": {"login": "phaabe"},
                "commit_id": self.fixture.sha,
                "submitted_at": delivery.github_quota.iso(time.time()),
            }
        )
        with self.assertRaisesRegex(
            delivery.Refused, "newer conflicting GitHub review"
        ):
            self.deliver()
        self.assertEqual(self.posts, [])

    def test_untrusted_and_old_matching_comments_do_not_confirm_delivery(self) -> None:
        for item in self.bundle()["comments"]:
            self.remote.append(self.comment(item["body"], login="outsider"))
            self.remote.append(self.comment(item["body"], at="2020-01-01T00:00:00Z"))
        self.deliver()
        self.assertEqual(len(self.posts), 2)

    def test_successful_post_not_visible_in_readback_is_not_repeated(self) -> None:
        self.hide_post = True
        for _ in range(2):
            with self.assertRaisesRegex(delivery.Refused, "did not confirm"):
                self.deliver()
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.bundle()["status"], "complete")
        self.assertEqual(self.bundle()["pending_comment"], 0)

    def test_lost_response_and_delayed_visibility_never_repost_on_restart(self) -> None:
        self.hide_post = True
        self.lose_response = True
        with self.assertRaises(delivery.github_state.ReadBlocked):
            self.deliver()
        with self.assertRaisesRegex(delivery.Refused, "did not confirm"):
            delivery.resume(self.fixture.repo, self.fixture.action, self.fixture.state)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.bundle()["pending_comment"], 0)
        self.remote.append(self.comment(self.posts[0]))
        self.hide_post = False
        delivery.resume(self.fixture.repo, self.fixture.action, self.fixture.state)
        self.assertEqual(len(self.posts), 2)
        self.assertEqual(self.bundle()["status"], "published")
        self.assertNotIn("pending_comment", self.bundle())

    def test_pause_after_pending_intent_clears_unsent_request(self) -> None:
        original_write = delivery.write_json
        pause = Path.home() / ".epic-pause"

        def pause_on_intent(path: Path, value: dict[str, object]) -> None:
            original_write(path, value)
            if "pending_comment" in value:
                pause.touch()

        with patch.object(delivery, "write_json", side_effect=pause_on_intent):
            with self.assertRaisesRegex(delivery.Refused, "paused"):
                self.deliver()
        pause.unlink()
        self.assertNotIn("pending_comment", self.bundle())
        self.assertEqual(self.posts, [])
        self.deliver()
        self.assertEqual(len(self.posts), 2)

    def test_definitive_rate_limit_rejection_clears_unsent_request(self) -> None:
        def reject_post(args: list[str]) -> str:
            if "POST" in args:
                raise delivery.github_quota.QuotaExhausted("RATE_LIMITED")
            return self.github(args)

        self.fixture.requests.side_effect = reject_post
        with self.assertRaises(delivery.feature_worktree.QuotaWait):
            self.deliver()
        self.assertNotIn("pending_comment", self.bundle())
        self.assertEqual(self.posts, [])
        (delivery.github_quota.STATE_DIR / delivery.github_quota.WAIT_FILE).unlink()
        self.fixture.requests.side_effect = self.github
        self.deliver()
        self.assertEqual(len(self.posts), 2)

    def test_deleted_confirmed_comment_is_not_reposted(self) -> None:
        self.deliver()
        self.remote.pop(0)
        with self.assertRaisesRegex(delivery.Refused, "missing or edited"):
            self.deliver()
        self.assertEqual(len(self.posts), 2)

    def test_absent_inherited_lock_blocks_before_network(self) -> None:
        os.close(8)
        try:
            self.fixture.requests.reset_mock()
            with self.assertRaisesRegex(delivery.Refused, "inherited target lock"):
                self.deliver()
            self.fixture.requests.assert_not_called()
        finally:
            os.dup2(self.lock.fileno(), 8)

    def test_contended_matching_lock_blocks_before_network(self) -> None:
        fcntl.flock(8, fcntl.LOCK_UN)
        path = delivery.target_lock.lock_dir() / "431.lock"
        with path.open("a") as other:
            self.assertTrue(delivery.target_lock.acquire([other.fileno()]))
            self.fixture.requests.reset_mock()
            with self.assertRaisesRegex(delivery.Refused, "another runner"):
                self.deliver()
            self.fixture.requests.assert_not_called()

    def test_pr_closed_or_owner_changed_blocks_all_posts(self) -> None:
        for field, value in (("state", "closed"), ("body", "Executor: Codex\n")):
            previous = json.dumps(self.fixture.pr)
            self.fixture.pr[field] = value
            with self.subTest(field=field), self.assertRaises(delivery.Refused):
                self.deliver()
            self.fixture.pr = json.loads(previous)
        self.assertEqual(self.posts, [])

    def test_changed_head_requires_fresh_review(self) -> None:
        self.fixture.pr["head"]["sha"] = "f" * 40
        with self.assertRaises(delivery.FreshReview):
            self.deliver()
        self.assertEqual(self.posts, [])

    def test_missing_verdict_cannot_infer_approval(self) -> None:
        bundle = self.bundle()
        bundle["verdict"] = None
        self.bundle_file.write_text(json.dumps(bundle))
        with self.assertRaises(delivery.Refused):
            self.deliver()
        self.assertEqual(self.posts, [])

    def test_malformed_comment_pages_block_all_writes(self) -> None:
        self.remote.append({"body": "incomplete"})
        with self.assertRaises(delivery.github_state.ReadBlocked):
            self.deliver()
        self.assertEqual(self.posts, [])

    def test_draft_and_legacy_bundle_require_fresh_review(self) -> None:
        bundle = self.bundle()
        for value in (
            {**bundle, "status": "draft"},
            {key: value for key, value in bundle.items() if key != "review_started_at"},
        ):
            self.bundle_file.write_text(json.dumps(value))
            with self.assertRaises(delivery.FreshReview):
                self.deliver()
        self.assertEqual(self.posts, [])


if __name__ == "__main__":
    unittest.main()
