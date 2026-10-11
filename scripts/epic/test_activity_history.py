"""Activity history ledger tests. Run: python3 -m unittest discover -s scripts/epic."""

from __future__ import annotations

import isolated_env  # noqa: F401  (first: hides live runner state)

import contextlib
from datetime import datetime, timezone
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

import activity_history as ah
from activity_history import Interval, Wait

T0 = 1_791_000_000 - 1_791_000_000 % 7200  # a 2 h-aligned UTC time


def iso(at: int) -> str:
    return datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def line(event: str, at: int, **extra: Any) -> str:
    return json.dumps({"event": event, "tick": iso(at), "at": iso(at), **extra}) + "\n"


class LedgerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = ah.default_path(self.dir)
        self.conn = ah.open_ledger(self.db)
        self.file = self.dir / "claude-activity.jsonl"

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def write(self, *lines: str, mode: str = "a") -> None:
        with self.file.open(mode) as handle:
            handle.write("".join(lines))

    def ingest(self) -> ah.IngestResult:
        return ah.ingest(self.conn, "claude", self.file)

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]


class IngestTest(LedgerCase):
    def test_replay_after_restart_stores_nothing_twice(self) -> None:
        self.write(line("start", T0), line("finish", T0 + 30, outcome="ok"))
        self.assertEqual(self.ingest().stored, 2)
        self.conn.close()
        self.conn = ah.open_ledger(self.db)
        self.assertEqual(self.ingest().stored, 0)
        self.conn.execute("DELETE FROM cursors")
        again = self.ingest()
        self.assertEqual((again.stored, again.duplicates), (0, 2))
        self.assertEqual(self.count(), 2)

    def test_partial_line_waits_for_its_newline(self) -> None:
        self.write(line("start", T0), '{"event": "finish"')
        self.assertEqual(self.ingest().stored, 1)
        self.write(f', "tick": "{iso(T0)}", "at": "{iso(T0 + 5)}"}}\n')
        self.assertEqual(self.ingest().stored, 1)
        self.assertEqual(self.count(), 2)

    def test_malformed_lines_are_counted_not_stored(self) -> None:
        self.write(
            "not json\n", '{"event": "start"}\n', "x" * 5000 + "\n", line("start", T0)
        )
        result = self.ingest()
        self.assertEqual((result.stored, result.invalid), (1, 3))

    def test_model_text_never_enters_the_ledger(self) -> None:
        self.write(line("env-block", T0, reason="secret prompt text"))
        self.ingest()
        (text,) = self.conn.execute("SELECT record FROM events").fetchone()
        self.assertNotIn("secret", text)
        self.assertEqual(json.loads(text)["reason_code"], "environment_failure")

    def test_replaced_or_truncated_file_is_read_from_the_start(self) -> None:
        self.write(line("start", T0), line("finish", T0 + 30))
        self.ingest()
        self.write(line("start", T0 + 60), mode="w")
        self.assertEqual(self.ingest().stored, 1)
        self.assertEqual(self.count(), 3)

    def test_missing_file_is_no_error(self) -> None:
        self.assertEqual(self.ingest().stored, 0)

    def test_corrupt_store_raises_and_keeps_the_file(self) -> None:
        bad = self.dir / "bad.sqlite3"
        bad.write_bytes(b"garbage" * 200)
        with self.assertRaises(ah.LedgerError):
            ah.open_ledger(bad)
        self.assertEqual(bad.read_bytes(), b"garbage" * 200)


class IntervalTest(LedgerCase):
    def test_model_work_then_runner_then_idle(self) -> None:
        self.write(
            line("start", T0),
            line("model-start", T0 + 10, action="claim"),
            line("model-end", T0 + 70, action="claim"),
            line("finish", T0 + 80, action="claim", outcome="ok"),
        )
        self.ingest()
        ah.observe(self.conn, "claude", T0, T0 + 100)
        got = [
            (i.state, i.start - T0, i.end - T0)
            for i in ah.intervals(self.conn, "claude", T0, T0 + 100)
        ]
        self.assertEqual(
            got,
            [
                ("runner", 0, 10),
                ("code", 10, 70),
                ("runner", 70, 80),
                ("idle", 80, 100),
            ],
        )

    def test_outage_is_unknown_not_carried_work(self) -> None:
        self.write(line("model-start", T0, action="review"))
        self.ingest()
        ah.observe(self.conn, "claude", T0, T0 + 60)
        got = [
            (i.state, i.start - T0, i.end - T0)
            for i in ah.intervals(self.conn, "claude", T0, T0 + 600)
        ]
        self.assertEqual(got, [("review", 0, 60), ("unknown", 60, 600)])

    def test_state_at_window_start_comes_from_earlier_event(self) -> None:
        self.write(line("model-start", T0, action="fix"))
        self.ingest()
        ah.observe(self.conn, "claude", T0, T0 + 3600)
        got = ah.intervals(self.conn, "claude", T0 + 600, T0 + 660)
        self.assertEqual([(i.state, i.end - i.start) for i in got], [("code", 60)])

    def test_wait_keeps_reason_and_scope(self) -> None:
        self.write(line("wait", T0, reason_code="target_lock", scope="target"))
        self.ingest()
        ah.observe(self.conn, "claude", T0, T0 + 10)
        (got,) = ah.intervals(self.conn, "claude", T0, T0 + 10)
        self.assertEqual(
            (got.state, got.reason_code, got.scope),
            ("waiting", "target_lock", "target"),
        )

    def test_no_negative_or_overlapping_intervals_after_clock_step_back(self) -> None:
        self.write(line("start", T0 + 50), line("finish", T0 + 20))
        self.ingest()
        ah.observe(self.conn, "claude", T0, T0 + 100)
        got = ah.intervals(self.conn, "claude", T0, T0 + 100)
        self.assertTrue(all(i.end > i.start for i in got))
        self.assertEqual(sum(i.end - i.start for i in got), 100)
        for a, b in zip(got, got[1:]):
            self.assertLessEqual(a.end, b.start)


class DominantTest(unittest.TestCase):
    def test_eight_minutes_coding_beats_two_blocked(self) -> None:
        state, share, _ = ah.dominant(
            [Interval("code", 0, 480), Interval("waiting", 480, 600)], 0, 600
        )
        self.assertEqual((state, share), ("code", 0.8))

    def test_winning_share_may_be_below_half(self) -> None:
        items = [
            Interval("code", 0, 40),
            Interval("review", 40, 70),
            Interval("idle", 70, 100),
        ]
        state, share, _ = ah.dominant(items, 0, 100)
        self.assertEqual((state, share), ("code", 0.4))

    def test_tie_goes_to_state_seen_latest(self) -> None:
        state, _, _ = ah.dominant(
            [Interval("idle", 0, 50), Interval("code", 50, 100)], 0, 100
        )
        self.assertEqual(state, "code")
        state, _, _ = ah.dominant(
            [Interval("code", 0, 50), Interval("idle", 50, 100)], 0, 100
        )
        self.assertEqual(state, "idle")

    def test_unknown_time_takes_part(self) -> None:
        state, share, breakdown = ah.dominant([Interval("code", 0, 20)], 0, 100)
        self.assertEqual((state, share, breakdown["unknown"]), ("unknown", 0.8, 80))

    def test_short_failure_does_not_override(self) -> None:
        items = [Interval("code", 0, 590), Interval("waiting", 590, 600)]
        state, _, breakdown = ah.dominant(items, 0, 600)
        self.assertEqual((state, breakdown["waiting"]), ("code", 10))


class ResolutionTest(unittest.TestCase):
    def test_thresholds(self) -> None:
        cases = {
            600: "10s",
            399 * 10: "10s",
            399 * 10 + 1: "1m",
            399 * 60: "1m",
            399 * 60 + 1: "5m",
            399 * 300 + 1: "30m",
            399 * 1800 + 1: "2h",
            30 * 86_400: "2h",
        }
        for range_s, name in cases.items():
            with self.subTest(range_s=range_s):
                self.assertEqual(ah.resolution(range_s), name)

    def test_every_preset_fits_400_buckets(self) -> None:
        for range_s in (600, 1800, 3600, 7200, 21600, 86400, 7 * 86400, 30 * 86400):
            width = ah.RESOLUTIONS[ah.resolution(range_s)]
            self.assertLessEqual(-(-range_s // width) + 1, ah.MAX_BUCKETS)

    def test_invalid_ranges_raise(self) -> None:
        for range_s in (0, -1, 30 * 86400 + 1):
            with self.subTest(range_s=range_s), self.assertRaises(ValueError):
                ah.resolution(range_s)

    def test_unaligned_range_is_enclosed(self) -> None:
        starts = list(ah.bucket_starts(T0 + 7, T0 + 7 + 60, 10))
        self.assertEqual(starts[0], T0)
        self.assertEqual(starts[-1] + 10, T0 + 70)


LOKI_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "activity-loki-queries.json").read_text()
)


class LokiQueryTest(unittest.TestCase):
    """fixtures/activity-loki-queries.json: step, alignment, one series per agent."""

    def test_requests_step_on_bucket_ends(self) -> None:
        for case in LOKI_FIXTURE["requests"]:
            with self.subTest(case["name"]):
                got = ah.overview_request(case["from"], case["to"])
                self.assertEqual(
                    {k: got[k] for k in ("resolution", "start", "end", "step")},
                    {k: case[k] for k in ("resolution", "start", "end", "step")},
                )
                self.assertEqual(got["step"], ah.RESOLUTIONS[got["resolution"]])
                for at in (got["start"], got["end"]):
                    self.assertEqual(at % got["step"], 0)  # a bucket end
                points = (got["end"] - got["start"]) // got["step"] + 1
                self.assertEqual(points, case["points"])
                self.assertLessEqual(points, ah.MAX_BUCKETS)
                # The buckets enclose the selected range.
                self.assertLessEqual(got["start"] - got["step"], case["from"])
                self.assertGreaterEqual(got["end"], case["to"])

    def test_query_parses_only_the_state_code_per_agent(self) -> None:
        got = ah.overview_request(T0, T0 + 3600)
        query = got["query"]
        self.assertNotIn("$resolution", query)
        self.assertIn('resolution="10s"', query)
        self.assertIn("[10s]", query)
        self.assertTrue(query.startswith("max by (agent) (max_over_time("))
        self.assertIn('| json state_code="state_code" | unwrap state_code', query)

    def test_request_fits_the_query_length_limit(self) -> None:
        # Worst case: 30 days with both ends unaligned, plus the range lookback.
        got = ah.overview_request(T0 + 1, T0 + 1 + ah.MAX_RANGE_S)
        span = got["end"] - (got["start"] - got["step"]) + got["step"]
        self.assertLessEqual(span, ah.MAX_QUERY_LENGTH_S)

    def test_invalid_ranges_query_nothing(self) -> None:
        for case in LOKI_FIXTURE["invalid"]:
            with self.subTest(case["name"]), self.assertRaises(ValueError):
                ah.overview_request(case["from"], case["to"])

    def test_series_read_exactly_the_bucket_ending_at_each_point(self) -> None:
        fixture = LOKI_FIXTURE["series"]
        request = ah.overview_request(
            fixture["request"]["from"], fixture["request"]["to"]
        )
        records = []
        for push in fixture["push"]:
            record = dict(push, state_code=ah.STATE_ORDER[push["state"]])
            self.assertEqual(
                push["ts"], push["start"] + ah.RESOLUTIONS[push["resolution"]]
            )
            records.append((push["ts"], record))
        got = ah.evaluate_overview(records, request)
        expected = {
            agent: {int(at): ah.STATE_ORDER[state] for at, state in points.items()}
            for agent, points in fixture["expected"].items()
        }
        self.assertEqual(got, expected)

    def test_twelve_agents_give_twelve_series(self) -> None:
        request = ah.overview_request(T0, T0 + 30 * 86_400)
        records = [
            (at + 7200, {"agent": f"a{n}", "resolution": "2h", "state_code": n % 13})
            for n in range(12)
            for at in range(T0, T0 + 30 * 86_400, 7200)
        ]
        got = ah.evaluate_overview(records, request)
        self.assertEqual(len(got), 12)
        loki = (
            Path(__file__).parents[2] / "tools/agent-monitoring/loki.yaml"
        ).read_text()
        self.assertIn("max_query_series: 5000", loki)
        self.assertLessEqual(len(got), 5000)
        self.assertTrue(all(len(points) == 360 for points in got.values()))

    def test_sealed_bucket_carries_its_state_code(self) -> None:
        bucket = ah.Bucket("claude", "1m", T0, T0 + 60, "waiting", 1.0)
        self.assertEqual(bucket.record()["state_code"], ah.STATE_ORDER["waiting"])


class SealTest(LedgerCase):
    def setUp(self) -> None:
        super().setUp()
        self.write(
            line("model-start", T0, action="claim"),
            line("finish", T0 + 48, action="claim", outcome="ok"),
        )
        self.ingest()
        ah.observe(self.conn, "claude", T0, T0 + 3600)

    def test_open_bucket_is_not_sealed(self) -> None:
        self.assertIsNone(ah.seal(self.conn, "claude", "1m", T0, now=T0 + 65))

    def test_sealed_bucket_is_immutable_and_replay_safe(self) -> None:
        first = ah.seal(self.conn, "claude", "1m", T0, now=T0 + 80)
        assert first is not None
        self.assertEqual((first.state, first.results), ("code", {"ok": 1}))
        self.write(line("wait", T0 + 50, reason_code="target_lock", scope="target"))
        self.ingest()
        again = ah.seal(self.conn, "claude", "1m", T0, now=T0 + 200)
        self.assertEqual(again, first)
        rows = self.conn.execute("SELECT id, ts, payload FROM outbox").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], T0 + 60)  # Loki timestamp: bucket end

    def test_late_record_flags_the_next_rollup(self) -> None:
        ah.seal(self.conn, "claude", "1m", T0, now=T0 + 80)
        ah.mark_late(self.conn, "claude", T0 + 30, T0 + 200)
        bucket = ah.build_bucket(self.conn, "claude", "2h", T0, sealed_until=T0 + 300)
        self.assertTrue(bucket.late_correction)

    def late_rows(self) -> list[tuple[int, int]]:
        return self.conn.execute(
            "SELECT start, end FROM coverage WHERE kind = 'late' ORDER BY start"
        ).fetchall()

    def test_only_a_record_inside_a_sealed_bucket_is_late(self) -> None:
        ah.seal(self.conn, "claude", "1m", T0, now=T0 + 80)
        ah.mark_late(self.conn, "claude", T0 + 70, T0 + 200)  # after the bucket
        self.assertEqual(self.late_rows(), [])

    def test_ingest_marks_a_new_record_in_a_sealed_bucket(self) -> None:
        ah.seal(self.conn, "claude", "1m", T0, now=T0 + 80)
        self.write(
            line("wait", T0 + 50, reason_code="target_lock", scope="target"),
            line("start", T0 + 90),
        )
        self.assertEqual(ah.ingest(self.conn, "claude", self.file, T0 + 200).stored, 2)
        self.assertEqual(self.late_rows(), [(T0 + 50, T0 + 200)])
        # A replay stores nothing, so it marks nothing again.
        self.conn.execute("DELETE FROM cursors")
        ah.ingest(self.conn, "claude", self.file, T0 + 300)
        self.assertEqual(self.late_rows(), [(T0 + 50, T0 + 200)])

    def sealed(self, name: str) -> list[int]:
        return [
            row[0]
            for row in self.conn.execute(
                "SELECT start FROM buckets WHERE resolution = ? ORDER BY start", (name,)
            )
        ]

    def test_seal_due_starts_at_first_coverage_and_resumes(self) -> None:
        self.assertEqual(ah.seal_due(self.conn, "claude", T0 + 75), 7)  # 6 x 10s, 1m
        self.assertEqual(self.sealed("10s"), [T0 + i * 10 for i in range(6)])
        self.assertEqual(self.sealed("1m"), [T0])
        self.assertEqual(ah.seal_due(self.conn, "claude", T0 + 75), 0)  # replay
        ah.seal_due(self.conn, "claude", T0 + 135)
        self.assertEqual(self.sealed("1m"), [T0, T0 + 60])
        self.assertEqual(self.sealed("5m"), [])  # still in progress
        outbox = self.conn.execute("SELECT count(*) FROM outbox").fetchone()[0]
        self.assertEqual(outbox, 12 + 2)

    def test_seal_due_is_bounded_and_catches_up(self) -> None:
        now = T0 + 3_600
        self.assertEqual(
            ah.seal_due(self.conn, "claude", now, limit=50), 50 + 50 + 11 + 1
        )
        self.assertEqual(self.sealed("10s")[-1], T0 + 490)
        ah.seal_due(self.conn, "claude", now, limit=1_000)
        self.assertEqual(len(self.sealed("10s")), 359)  # last one inside the grace
        self.assertEqual(len(self.sealed("1m")), 59)

    def test_seal_due_needs_coverage_and_respects_retention(self) -> None:
        self.assertEqual(ah.seal_due(self.conn, "codex", T0 + 600), 0)
        now = T0 + ah.RETENTION_S + 7_200
        ah.seal_due(self.conn, "claude", now, limit=1)
        self.assertEqual(self.sealed("2h"), [T0 + 7_200])

    def test_flags_carry_partial_edge_and_in_progress(self) -> None:
        bucket = ah.build_bucket(
            self.conn, "claude", "2h", T0, sealed_until=T0 + 100, view=(T0 + 5, T0 + 60)
        )
        flags = bucket.record()["flags"]
        self.assertEqual(
            flags,
            {
                "partial": True,  # coverage ends after 1 h of the 2 h
                "late_correction": False,
                "edge": True,
                "in_progress": True,
            },
        )
        outside = ah.build_bucket(
            self.conn, "claude", "1m", T0 + 7200, sealed_until=T0 + 9999
        )
        self.assertTrue(outside.partial)


class LateTest(unittest.TestCase):
    BASE = dict(interval=300.0, budget=1800.0, registered=None)  # overdue 2400 s

    def check(self, now: float, **kw: Any) -> bool:
        return ah.late(
            now=now, last_start=0.0, paused=kw.pop("paused", False), **self.BASE, **kw
        )

    def test_age_rule(self) -> None:
        self.assertFalse(self.check(2400))
        self.assertTrue(self.check(2401))

    def test_agent_wait_suppresses_until_retry_then_recovers(self) -> None:
        wait = Wait("agent", "github_quota", since=100, retry_at=4000)
        self.assertFalse(self.check(3000, waits=[wait]))
        self.assertTrue(self.check(4001, waits=[wait]))

    def test_target_wait_does_not_suppress(self) -> None:
        self.assertTrue(
            self.check(3000, waits=[Wait("target", "target_lock", since=2900)])
        )

    def test_ended_wait_does_not_suppress(self) -> None:
        wait = Wait("agent", "operator_wait", since=2500, ended_at=2600)
        self.assertTrue(self.check(3000, waits=[wait]))

    def test_stale_wait_without_expiry_stops_suppressing(self) -> None:
        wait = Wait("agent", "connection_failure", since=1000)
        self.assertFalse(self.check(3000, waits=[wait]))
        self.assertTrue(self.check(3401, waits=[wait]))

    def test_pause_is_never_late(self) -> None:
        self.assertFalse(self.check(99999, paused=True))

    def test_new_agent_counts_from_registration(self) -> None:
        self.assertFalse(
            ah.late(
                now=100,
                last_start=None,
                registered=None,
                interval=1,
                budget=1,
                paused=False,
            )
        )
        self.assertTrue(
            ah.late(
                now=100,
                last_start=None,
                registered=0,
                interval=1,
                budget=1,
                paused=False,
            )
        )


class RetentionTest(LedgerCase):
    def test_prune_keeps_the_boundary_state(self) -> None:
        old = T0 - ah.RETENTION_S - 3600
        self.write(
            line("start", old),
            line("model-start", old + 10, action="claim"),
            line("finish", T0, outcome="ok"),
        )
        self.ingest()
        self.conn.execute(
            "INSERT INTO outbox VALUES ('x', 'agent_activity_events', ?, '{}', 'pending')",
            (old,),
        )
        ah.prune(self.conn, now=T0 + 10)
        states = [
            json.loads(r)["event"]
            for (r,) in self.conn.execute("SELECT record FROM events ORDER BY at")
        ]
        self.assertEqual(states, ["model-start", "finish"])
        ah.observe(self.conn, "claude", T0 - ah.RETENTION_S + 10, T0)
        (first, *_rest) = ah.intervals(
            self.conn, "claude", T0 - ah.RETENTION_S + 10, T0
        )
        self.assertEqual(first.state, "code")
        (state,) = self.conn.execute("SELECT state FROM outbox").fetchone()
        self.assertEqual(state, "expired")

    def test_prune_is_bounded(self) -> None:
        old = T0 - ah.RETENTION_S - 10_000
        self.write(*(line("start", old + i) for i in range(20)))
        self.ingest()
        ah.prune(self.conn, now=T0, batch=5)
        self.assertEqual(self.count(), 15)

    def test_disk_bytes_counts_wal(self) -> None:
        self.assertGreater(ah.disk_bytes(self.db), 0)
        self.assertTrue(os.path.exists(self.db))


if __name__ == "__main__":
    unittest.main()


class CollectorTest(LedgerCase):
    def collector(self) -> ah.Collector:
        collector = ah.Collector(self.dir)
        self.addCleanup(collector.close)
        return collector

    def spans(self) -> list[tuple[int, int]]:
        return ah.fresh_spans(self.conn, "claude", T0 - 3_600, T0 + 3_600)

    def test_missing_source_and_long_gaps_stay_unknown(self) -> None:
        collector = self.collector()
        collector.cycle([("claude", self.file)], T0)  # no file yet: no coverage
        self.write(line("start", T0))
        collector.cycle([("claude", self.file)], T0 + 30)
        collector.cycle([("claude", self.file)], T0 + 60)
        gap = T0 + 60 + ah.FRESH_GAP_S + 1
        collector.cycle([("claude", self.file)], gap)
        collector.cycle([("claude", self.file)], gap + 30)
        self.assertTrue(collector.healthy)
        self.assertEqual(self.spans(), [(T0 + 30, T0 + 60), (gap, gap + 30)])

    def test_restart_starts_a_new_span(self) -> None:
        self.write(line("start", T0))
        first = self.collector()
        first.cycle([("claude", self.file)], T0)
        first.cycle([("claude", self.file)], T0 + 30)
        first.close()
        second = self.collector()
        second.cycle([("claude", self.file)], T0 + 60)
        second.cycle([("claude", self.file)], T0 + 90)
        # The time the collector was down stays unknown.
        self.assertEqual(self.spans(), [(T0, T0 + 30), (T0 + 60, T0 + 90)])
        self.assertEqual(self.count(), 1)

    def test_cycle_seals_finished_buckets(self) -> None:
        self.write(
            line("model-start", T0, action="claim"),
            line("finish", T0 + 40, action="claim", outcome="ok"),
        )
        collector = self.collector()
        for at in range(T0, T0 + 91, 30):
            collector.cycle([("claude", self.file)], at)
        rows = self.conn.execute(
            "SELECT resolution, start, record FROM buckets ORDER BY resolution, start"
        ).fetchall()
        self.assertEqual(
            [(name, start - T0) for name, start, _ in rows],
            [("10s", 0), ("10s", 10), ("10s", 20), ("10s", 30), ("10s", 40)]
            + [("10s", 50), ("10s", 60), ("10s", 70), ("1m", 0)],
        )
        minute = json.loads(rows[-1][2])
        self.assertEqual(
            (minute["state"], minute["breakdown"], minute["results"]),
            ("code", {"code": 40, "idle": 20}, {"ok": 1}),
        )

    def test_current_waits_reads_only_the_last_tick(self) -> None:
        quota = {"reason_code": "github_quota", "scope": "agent"}
        self.write(
            line("start", T0),
            line("wait", T0 + 1, action="claim", retry_at=iso(T0 + 600), **quota),
            line("finish", T0 + 2, outcome="blocked"),
        )
        self.ingest()
        self.assertEqual(
            ah.current_waits(self.conn, "claude"),
            [Wait("agent", "github_quota", since=T0 + 1, retry_at=T0 + 600)],
        )
        self.write(line("start", T0 + 60))
        self.ingest()
        self.assertEqual(ah.current_waits(self.conn, "claude"), [])


class BenchSampleTest(unittest.TestCase):
    """A small deterministic sample of the separate scale benchmark."""

    def sample(self) -> dict[str, Any]:
        import activity_bench

        with tempfile.TemporaryDirectory() as tmp:
            return activity_bench.run(Path(tmp), agents=3, days=1 / 24, seed=7)

    def test_sample_is_deterministic_and_healthy(self) -> None:
        first, second = self.sample(), self.sample()
        for key in ("attempts", "events_written", "cycles", "rows"):
            self.assertEqual(first[key], second[key], key)
        self.assertEqual(first["unhealthy_cycles"], 0)
        # Every written event is stored once, despite daily rotation replays.
        self.assertEqual(first["rows"]["events"], first["events_written"])
        # The collector outage (300 s) skips 10 of 121 cycle times.
        self.assertEqual(first["cycles"], 111)
        self.assertEqual(first["queries_s"]["resolution"], "10s")
        self.assertGreater(first["queries_s"]["overview_buckets"], 0)
        for key in ("runtime_s", "peak_rss_bytes", "disk_bytes"):
            self.assertGreater(first[key], 0, key)

    def test_retired_agent_is_replaced(self) -> None:
        import activity_bench

        with tempfile.TemporaryDirectory() as tmp:
            activity_bench.run(Path(tmp), agents=2, days=1 / 24, seed=7)
            conn = ah.open_ledger(ah.default_path(Path(tmp)))
            self.addCleanup(conn.close)
            spans = {
                agent: (lo, hi)
                for agent, lo, hi in conn.execute(
                    "SELECT agent, min(at), max(at) FROM events GROUP BY agent"
                )
            }
        retire = activity_bench.START + 3_600 // 3
        self.assertLess(spans["a01"][1], retire)
        self.assertGreaterEqual(spans["a99"][0], retire)
        self.assertIn("a00", spans)

    def test_rejects_invalid_arguments(self) -> None:
        import activity_bench

        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            activity_bench.main(["--agents", "1"])
