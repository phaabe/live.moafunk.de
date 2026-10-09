"""Activity contract tests. Run: python3 -m unittest discover -s scripts/epic."""

from __future__ import annotations

import isolated_env  # noqa: F401  (first: hides live runner state)

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import activity
import tick_events

TICK = "2026-10-08T01:00:00Z"
ISSUES = "https://github.com/phaabe/live.moafunk.de/issues"


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
        out = rec({"event": "model-start", "tick": TICK, "action": "fix", "pr": 3})
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


class SharedFixtureTest(unittest.TestCase):
    """fixtures/activity-contract.json: the cases both runners must meet."""

    def cases(self) -> list[dict[str, Any]]:
        path = Path(__file__).with_name("fixtures") / "activity-contract.json"
        return json.loads(path.read_text(encoding="utf-8"))["cases"]

    def test_every_case(self) -> None:
        for case in self.cases():
            with self.subTest(case=case["name"]):
                self.assertEqual(activity.record(case["event"]), case["record"])

    def test_cases_cover_the_contract(self) -> None:
        records = [c["record"] for c in self.cases() if c["record"]]
        actions = {r["action"] for r in records}
        for action in ("refine", "review-refinement", "set-ready"):
            self.assertIn(action, actions)
        events = {r["event"] for r in records}
        self.assertLessEqual(
            {"start", "finish", "env-block", "model-start", "model-end"}, events
        )
        for record in records:
            self.assertEqual(tuple(record), activity.RECORD_KEYS)

    def test_tick_events_writes_fixture_shaped_records(self) -> None:
        # The runner's own writer gives the same record as the fixture reader.
        with tempfile.TemporaryDirectory() as tmp:
            action = Path(tmp) / "action.json"
            action.write_text(
                json.dumps({"action": "refine", "issue": f"{ISSUES}/681"})
            )
            out = Path(tmp) / "activity.jsonl"
            tick_events.activity_event(out, TICK, "model-start", action)
            written = json.loads(out.read_text())
        self.assertEqual(
            (written["activity"], written["target"]), ("refine", "issue:681")
        )
        self.assertEqual(tuple(written), activity.RECORD_KEYS)

    def test_every_record_reads_back_unchanged(self) -> None:
        # Codex review on PR 699: a collector reads written records again.
        for case in self.cases():
            if case["record"] is None:
                continue
            with self.subTest(case=case["name"]):
                serialized = json.loads(json.dumps(case["record"]))
                self.assertEqual(activity.record(serialized), case["record"])


class RoundTripTest(unittest.TestCase):
    """Codex review on PR 699: records the runner's writer serialized read
    back unchanged, model boundaries and null reasons included."""

    def written(self, kind: str, action: str, **wait: Any) -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as tmp:
            action_file = Path(tmp) / "action.json"
            action_file.write_text(json.dumps({"action": action, "pr": 699}))
            out = Path(tmp) / "activity.jsonl"
            tick_events.activity_event(out, TICK, kind, action_file, **wait)
            found: dict[str, Any] = json.loads(out.read_text())
        return found

    def test_model_boundaries_keep_their_activity_and_null_reason(self) -> None:
        for action, expected in (
            ("review-refinement", "review-refinement"),
            ("refine", "refine"),
            ("review", "review"),
            ("fix", "code"),
            ("merge", "runner"),
        ):
            for kind in ("model-start", "model-end"):
                with self.subTest(action=action, kind=kind):
                    written = self.written(kind, action)
                    self.assertEqual(written["activity"], expected)
                    self.assertIsNone(written["reason_code"])
                    read = rec(written)
                    self.assertEqual(read, written)
                    self.assertEqual(
                        (read["activity"], read["reason_code"], read["scope"]),
                        (expected, None, None),
                    )

    def test_wait_keeps_reason_scope_and_retry(self) -> None:
        written = self.written(
            "wait",
            "fix",
            reason_code="github_quota",
            scope="agent",
            retry_at="2099-01-01T00:00:00Z",
        )
        self.assertEqual(
            (written["reason_code"], written["scope"], written["retry_at"]),
            ("github_quota", "agent", "2099-01-01T00:00:00Z"),
        )
        self.assertEqual(rec(written), written)

    def test_finish_of_model_work_reads_back_as_model_work(self) -> None:
        first = rec(
            {"event": "finish", "tick": TICK, "action": "review", "pr": 1,
             "model_started": True, "outcome": "ok"}
        )  # fmt: skip
        self.assertEqual(first["activity"], "review")
        self.assertEqual(rec(json.loads(json.dumps(first))), first)

    def test_a_claimed_activity_never_makes_runner_work_model_work(self) -> None:
        # Only the action's own model activity is kept, never another one.
        for claimed in ("review", "refine", "paused"):
            with self.subTest(claimed=claimed):
                found = rec(
                    {"event": "finish", "tick": TICK, "action": "fix",
                     "activity": claimed}
                )  # fmt: skip
                self.assertEqual(found["activity"], "runner")


if __name__ == "__main__":
    unittest.main()
