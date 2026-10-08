"""Activity contract tests. Run: python3 -m unittest discover -s scripts/epic."""

from __future__ import annotations

import isolated_env  # noqa: F401  (first: hides live runner state)

import json
import unittest

import activity
import tick_events

TICK = "2026-10-08T01:00:00Z"


def rec(event: object) -> dict[str, object]:
    out = activity.record(event)
    if out is None:
        raise AssertionError(f"event dropped: {event!r}")
    return out


class ActivityForTest(unittest.TestCase):
    def test_code_actions_count_only_after_the_model_started(self) -> None:
        for action in ("claim", "continue", "fix", "fix-checks", "resolve-conflict"):
            with self.subTest(action=action):
                self.assertEqual(activity.activity_for(action, True), "code")
                self.assertEqual(activity.activity_for(action, False), "runner")

    def test_review_and_refinement_actions(self) -> None:
        self.assertEqual(activity.activity_for("review", True), "review")
        self.assertEqual(activity.activity_for("refine", True), "refine")
        self.assertEqual(
            activity.activity_for("review-refinement", True), "review-refinement"
        )
        self.assertEqual(activity.activity_for("refine", False), "runner")

    def test_runner_only_actions(self) -> None:
        for action in ("merge", "adopt", "set-ready", "escalate"):
            with self.subTest(action=action):
                self.assertEqual(activity.activity_for(action, False), "runner")
                self.assertEqual(activity.activity_for(action, True), "runner")

    def test_selector_states(self) -> None:
        self.assertEqual(activity.activity_for("stop", False), "paused")
        self.assertEqual(activity.activity_for("idle", False), "idle")
        self.assertEqual(activity.activity_for("wait", False), "waiting")

    def test_unknown_or_invalid_action_is_other(self) -> None:
        for action in ("deploy", "", "Fix", None, 3, "x" * 40):
            with self.subTest(action=action):
                self.assertEqual(activity.activity_for(action, True), "other")

    def test_every_monitor_action_has_a_known_activity(self) -> None:
        import monitor

        for action in monitor.ACTIONS:
            with self.subTest(action=action):
                self.assertNotEqual(activity.activity_for(action, True), "other")

    def test_derived_states_are_not_runner_activities(self) -> None:
        for state in ("retired", "late", "new", "unknown"):
            self.assertIn(state, activity.STATES)
            self.assertNotIn(state, activity.ACTIVITIES)


class ReasonTest(unittest.TestCase):
    def test_known_code_scope_and_retry(self) -> None:
        self.assertEqual(
            activity.reason("github_quota", "agent", "2026-10-08T02:00:00Z"),
            {
                "reason_code": "github_quota",
                "scope": "agent",
                "retry_at": "2026-10-08T02:00:00Z",
            },
        )

    def test_unknown_values_fall_back_safely(self) -> None:
        self.assertEqual(
            activity.reason("waiting: Codex fixes it", "everyone", "soon"),
            {"reason_code": "unknown", "scope": "target", "retry_at": None},
        )

    def test_impossible_retry_time_is_dropped(self) -> None:
        self.assertIsNone(
            activity.reason("retry_backoff", "agent", "2026-02-30T00:00:00Z")[
                "retry_at"
            ]
        )


class RecordTest(unittest.TestCase):
    def test_env_block_text_never_passes(self) -> None:
        secret = "ssh denied for /Users/anton/.ssh/id_ed25519 token=abc"
        event = {
            "v": 1,
            "event": "env-block",
            "tick": TICK,
            "at": TICK,
            "action": "fix",
            "target": "pr:12",
            "reason": secret,
            "hold": True,
        }
        out = rec(event)
        self.assertEqual(out["reason_code"], "environment_failure")
        self.assertEqual(out["scope"], "agent")
        self.assertEqual(out["activity"], "waiting")
        self.assertNotIn("reason", out)
        self.assertNotIn("anton", json.dumps(out))
        self.assertNotIn("abc", json.dumps(out))

    def test_record_keeps_only_allowlisted_keys(self) -> None:
        event = {
            "v": 1,
            "event": "finish",
            "tick": TICK,
            "at": TICK,
            "action": "review",
            "pr": 5,
            "outcome": "ok",
            "prompt": "do things",
            "command": "rm -rf /",
            "reason": "free text",
        }
        out = rec(event)
        self.assertEqual(tuple(out), activity.RECORD_KEYS)
        self.assertEqual(out["target"], "pr:5")
        self.assertEqual(out["outcome"], "ok")
        # A legacy finish has no model boundary: it is not review time.
        self.assertEqual(out["activity"], "runner")
        self.assertIsNone(out["reason_code"])

    def test_reason_text_is_never_classified(self) -> None:
        out = rec(
            {
                "event": "finish",
                "tick": TICK,
                "action": "wait",
                "reason": "github quota",
            }
        )
        self.assertIsNone(out["reason_code"])

    def test_reason_code_field_is_validated(self) -> None:
        out = rec(
            {
                "event": "wait",
                "tick": TICK,
                "action": "wait",
                "issue": "https://github.com/phaabe/live.moafunk.de/issues/681",
                "reason_code": "review_wait",
                "scope": "target",
            }
        )
        self.assertEqual(out["reason_code"], "review_wait")
        self.assertEqual(out["target"], "issue:681")
        bad = rec({"event": "wait", "tick": TICK, "reason_code": "rm -rf"})
        self.assertEqual(bad["reason_code"], "unknown")

    def test_stop_and_idle_carry_their_reason(self) -> None:
        stop = rec({"event": "start", "tick": TICK, "action": "stop"})
        self.assertEqual(
            (stop["activity"], stop["reason_code"]), ("paused", "pause_requested")
        )
        idle = rec({"event": "start", "tick": TICK, "action": "idle"})
        self.assertEqual(
            (idle["activity"], idle["reason_code"]), ("idle", "no_eligible_work")
        )

    def test_model_start_marks_model_work(self) -> None:
        out = rec(
            {"event": "model-start", "tick": TICK, "action": "fix", "pr": 3}
        )
        self.assertEqual(out["activity"], "code")

    def test_unknown_outcome_is_not_success(self) -> None:
        out = rec({"event": "finish", "tick": TICK, "outcome": "success"})
        self.assertIsNone(out["outcome"])

    def test_malformed_events_are_dropped(self) -> None:
        for event in (
            None,
            [],
            {},
            {"event": "start"},
            {"event": "start", "tick": "now"},
            {"event": "Start!", "tick": TICK},
        ):
            with self.subTest(event=event):
                self.assertIsNone(activity.record(event))

    def test_outcomes_match_the_wire_names(self) -> None:
        self.assertEqual(activity.OUTCOMES, tick_events.OUTCOMES)


if __name__ == "__main__":
    unittest.main()
