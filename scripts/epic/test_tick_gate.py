"""Tests for tick_gate. Run: python3 -m unittest discover -s scripts/epic"""

from __future__ import annotations

import unittest

from tick_gate import fingerprint, should_skip, target_number

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


if __name__ == "__main__":
    unittest.main()
