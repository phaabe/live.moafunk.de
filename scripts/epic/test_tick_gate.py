"""Tests for tick_gate. Run: python3 -m unittest discover -s scripts/epic"""

from __future__ import annotations

import isolated_env  # noqa: F401  (first: hides live runner state)

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tick_gate
from tick_gate import (
    SKIP,
    check,
    fingerprint,
    record as save,
    should_skip,
    stale,
    target_number,
    target_state,
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

    def tick(self, now: float, action: dict = CLAIM) -> int:
        def lookup(_: dict) -> dict:
            return {"updated_at": self.updated, "state": "open"}

        return check("claude", action, lookup, now, TTL, self.dir)

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

    def test_suppressed_target_does_not_block_others(self) -> None:
        # Starvation: a no-op on 338 must not stop a later action on PR 5.
        review = {"action": "review", "pr": 5, "sha": "a" * 40}
        for action in (CLAIM, review):
            self.assertEqual(self.tick(1000.0, action), 0)
            save("claude", action, 1010.0, self.dir)
        self.assertEqual(self.tick(1020.0, CLAIM), SKIP)
        self.assertEqual(self.tick(1020.0, review), SKIP)
        self.assertEqual(self.tick(1020.0, {**review, "pr": 6}), 0)

    def test_top_level_stays_the_last_session_for_the_monitor(self) -> None:
        self.assertEqual(self.tick(1000.0), 0)
        save("claude", CLAIM, 1010.0, self.dir)
        data = json.loads((self.dir / "claude-gate.json").read_text())
        self.assertEqual((data["action"], data["at"]), (CLAIM, 1010.0))
        self.assertEqual(set(data["targets"]), {"338"})

    def test_old_single_record_still_skips_its_target(self) -> None:
        (self.dir / "claude-gate.json").write_text(
            json.dumps(record(CLAIM) | {"action": CLAIM})
        )
        self.assertEqual(self.tick(1100.0), SKIP)

    def test_expired_target_records_are_dropped(self) -> None:
        self.assertEqual(self.tick(1000.0), 0)
        save("claude", CLAIM, 1010.0, self.dir)
        review = {"action": "review", "pr": 5, "sha": "a" * 40}
        self.assertEqual(self.tick(1010.0 + TTL, review), 0)
        save("claude", review, 1010.0 + TTL, self.dir, TTL)
        data = json.loads((self.dir / "claude-gate.json").read_text())
        self.assertEqual(set(data["targets"]), {"5"})

    def test_changed_target_is_skipped_without_a_seen_record(self) -> None:
        # Recheck after the lock: another runner acted since selection.
        action = {**CLAIM, "updated_at": "t0"}
        self.assertEqual(self.tick(1000.0, action), SKIP)
        self.assertFalse((self.dir / "claude-gate-seen.json").exists())


class StaleTest(unittest.TestCase):
    def test_same_start_state_runs(self) -> None:
        self.assertIsNone(
            stale({"updated_at": "t1"}, {"updated_at": "t1", "state": "open"})
        )

    def test_closed_target_is_stale(self) -> None:
        self.assertIsNotNone(stale(CLAIM, {"updated_at": "t1", "state": "closed"}))

    def test_changed_target_is_stale(self) -> None:
        self.assertIsNotNone(stale({"updated_at": "t1"}, {"updated_at": "t2"}))

    def test_without_selection_time_or_target_nothing_is_stale(self) -> None:
        self.assertIsNone(stale(CLAIM, {"updated_at": "t2", "state": None}))
        self.assertIsNone(stale({"updated_at": "t1"}, None))

    def test_changed_head_is_stale(self) -> None:
        action = {"action": "review", "pr": 5, "sha": "a" * 40}
        self.assertIsNotNone(stale(action, {"head": "b" * 40}))
        self.assertIsNone(stale(action, {"head": "a" * 40, "mergeable": "MERGEABLE"}))


class ConflictTest(unittest.TestCase):
    """The pre-model conflict rule is the selector's (next_action.decide)."""

    def verdict(self, kind: str, mergeable: str) -> str | None:
        action = {"action": kind, "pr": 5, "sha": "a" * 40}
        return stale(action, {"head": "a" * 40, "mergeable": mergeable})

    def test_conflict_stops_review_fix_fix_checks_and_merge(self) -> None:
        for kind in ("review", "fix", "fix-checks", "merge"):
            with self.subTest(kind=kind):
                self.assertIn("conflicts", self.verdict(kind, "CONFLICTING") or "")
                self.assertIsNone(self.verdict(kind, "MERGEABLE"))
                self.assertIsNone(self.verdict(kind, "UNKNOWN"))

    def test_cleared_conflict_stops_resolve_conflict(self) -> None:
        self.assertIsNotNone(self.verdict("resolve-conflict", "MERGEABLE"))
        self.assertIsNone(self.verdict("resolve-conflict", "CONFLICTING"))
        self.assertIsNone(self.verdict("resolve-conflict", "UNKNOWN"))

    def test_other_actions_ignore_mergeability(self) -> None:
        for kind in ("escalate", "continue", "adopt"):
            with self.subTest(kind=kind):
                self.assertIsNone(self.verdict(kind, "CONFLICTING"))


class TargetStateTest(unittest.TestCase):
    """The gate's GitHub reads, with a fake `gh`."""

    REVIEW = {"action": "review", "pr": 5, "sha": "a" * 40}

    def read(self, action: dict, pull: object, issue: str = "t1\nopen") -> dict | None:
        calls: list[list[str]] = []

        def gh(args: list[str], timeout: int = 120) -> str:
            calls.append(args)
            return issue if "/issues/" in args[1] else json.dumps(pull)

        with patch.object(tick_gate, "run_gh", gh):
            found = target_state(action)
        self.calls = calls
        return found

    def test_pr_read_adds_head_and_mergeable(self) -> None:
        for raw, merge in (
            (True, "MERGEABLE"),
            (False, "CONFLICTING"),
            (None, "UNKNOWN"),
        ):
            with self.subTest(mergeable=raw):
                pull = {"head": {"sha": "a" * 40}, "mergeable": raw}
                self.assertEqual(
                    self.read(self.REVIEW, pull),
                    {
                        "updated_at": "t1",
                        "state": "open",
                        "head": "a" * 40,
                        "mergeable": merge,
                    },
                )

    def test_issue_and_closed_pr_need_no_pr_read(self) -> None:
        self.read(CLAIM, None)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.read(self.REVIEW, None, "t1\nclosed")["state"], "closed")
        self.assertEqual(len(self.calls), 1)

    def test_malformed_pr_fails_and_never_becomes_unknown(self) -> None:
        good = {"head": {"sha": "a" * 40}, "mergeable": True}
        for pull in (
            [],
            {"mergeable": True},
            {**good, "head": {"sha": "short"}},
            {"head": good["head"]},  # no mergeable key
            {**good, "mergeable": "dirty"},
            {**good, "mergeable": 0},
        ):
            with self.subTest(pull=pull), self.assertRaises(ValueError):
                self.read(self.REVIEW, pull)

    def test_failed_or_timed_out_read_raises(self) -> None:
        for error in (
            subprocess.CalledProcessError(1, ["gh"], "", "HTTP 401"),
            subprocess.TimeoutExpired(["gh"], 60),
        ):

            def gh(args: list[str], timeout: int = 120, error=error) -> str:
                if "/pulls/" in args[1]:
                    raise error
                return "t1\nopen"

            with (
                self.subTest(error=type(error).__name__),
                patch.object(tick_gate, "run_gh", gh),
                self.assertRaises(type(error)),
            ):
                target_state(self.REVIEW)


if __name__ == "__main__":
    unittest.main()
