"""Tests for tick_gate. Run: python3 -m unittest discover -s scripts/epic"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tick_gate import (
    SKIP,
    check,
    fingerprint,
    record as save,
    should_skip,
    target_number,
)

R = "https://github.com/phaabe/live.moafunk.de/issues"
CLAIM = {"action": "claim", "reason": "Ready leaf assigned to me", "issue": f"{R}/338"}
TTL = 3600


def record(action: dict, updated: str | None = "t1", at: float = 1000.0) -> dict:
    return {"fingerprint": fingerprint(action), "updated_at": updated, "at": at}


class TickGateTest(unittest.TestCase):
    def test_same_action_without_change_is_skipped(self) -> None:
        self.assertTrue(should_skip(CLAIM, "t1", record(CLAIM), 1100.0, TTL))

    def test_first_tick_runs(self) -> None:
        self.assertFalse(should_skip(CLAIM, "t1", None, 1100.0, TTL))

    def test_new_comment_runs(self) -> None:
        self.assertFalse(should_skip(CLAIM, "t2", record(CLAIM), 1100.0, TTL))

    def test_other_action_runs(self) -> None:
        review = {"action": "review", "pr": 5, "sha": "a" * 40}
        self.assertFalse(should_skip(review, "t1", record(CLAIM), 1100.0, TTL))

    def test_new_head_sha_runs(self) -> None:
        old = {"action": "fix", "pr": 5, "sha": "a" * 40}
        new = {**old, "sha": "b" * 40}
        self.assertFalse(should_skip(new, "t1", record(old), 1100.0, TTL))

    def test_retry_after_ttl(self) -> None:
        self.assertFalse(should_skip(CLAIM, "t1", record(CLAIM), 1000.0 + TTL, TTL))

    def test_continue_is_never_skipped(self) -> None:
        cont = {"action": "continue", "pr": 5, "sha": "a" * 40}
        self.assertFalse(should_skip(cont, "t1", record(cont), 1100.0, TTL))

    def test_unknown_updated_at_runs(self) -> None:
        self.assertFalse(should_skip(CLAIM, None, record(CLAIM, None), 1100.0, TTL))

    def test_target_number(self) -> None:
        self.assertEqual(target_number({"pr": 410}), 410)
        self.assertEqual(target_number({"issue": f"{R}/338"}), 338)
        self.assertIsNone(target_number({"action": "idle"}))


class CheckRecordTest(unittest.TestCase):
    """check -> session -> record -> next check, with a fake GitHub clock."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.updated = "t1"

    def tick(self, now: float) -> int:
        return check("claude", CLAIM, lambda _: self.updated, now, TTL, self.dir)

    def test_noop_tick_skips_the_next_one(self) -> None:
        self.assertEqual(self.tick(1000.0), 0)
        save("claude", CLAIM, 1010.0, self.dir)
        self.assertEqual(self.tick(1020.0), SKIP)

    def test_comment_during_session_is_not_marked_seen(self) -> None:
        # Codex review on PR 412: a comment posted while the model ran was
        # recorded as seen, and the next tick was skipped for up to 3 hours.
        self.assertEqual(self.tick(1000.0), 0)
        self.updated = "t2"
        save("claude", CLAIM, 1010.0, self.dir)
        self.assertEqual(self.tick(1020.0), 0)

    def test_record_without_check_fails(self) -> None:
        with self.assertRaises(FileNotFoundError):
            save("claude", CLAIM, 1010.0, self.dir)

    def test_record_of_other_action_fails(self) -> None:
        self.assertEqual(self.tick(1000.0), 0)
        with self.assertRaises(ValueError):
            save("claude", {"action": "review", "pr": 5}, 1010.0, self.dir)


if __name__ == "__main__":
    unittest.main()
