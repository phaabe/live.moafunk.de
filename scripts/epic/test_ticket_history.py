"""Ticket status history tests. Run: python3 -m unittest discover -s scripts/epic."""

from __future__ import annotations

import isolated_env  # noqa: F401  (first: hides live runner state)

from datetime import datetime, timezone
import json
import re
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import monitor
import ticket_history as th
import tickets
import test_delivery
from test_tickets import ticket
from test_monitor import URL, snapshot

NOW = int(datetime(2026, 10, 5, 12, tzinfo=timezone.utc).timestamp())
DAY = 86400
H = 3600
ISSUE = f"{URL}/issues"
PULL = f"{URL}/pull"


class Sink(test_delivery.Sink):
    def rows(self, name: str, **labels: str) -> list[tuple[str, dict[str, str], float]]:
        """(metric, labels, value) of every sample of `name` with these labels."""
        found = []
        for line in self.render().splitlines():
            if line.startswith("#"):
                continue
            head, value = line.rsplit(" ", 1)
            metric, _, rest = head.partition("{")
            if metric != f"epic_{name}":
                continue
            got = dict(re.findall(r'(\w+)="((?:[^"\\]|\\.)*)"', rest))
            if all(got.get(k) == v for k, v in labels.items()):
                found.append((metric, got, float(value)))
        return found


def tick(
    start: float,
    end: float | None,
    action: str,
    target: str,
    source: str = "events",
    ident: str = "",
) -> monitor.Json:
    return {
        "id": ident or f"t{start}",
        "start": float(start),
        "end": None if end is None else float(end),
        "action": action,
        "target": target,
        "source": source,
    }


class Base(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.history = self.fresh()

    def fresh(self, now: float = NOW) -> th.History:
        """A new History on the same files, as after a restart."""
        history = th.History(self.root)
        history.load(now)
        return history

    def see(self, at: int, statuses: dict[int, str], executor: str = "Claude") -> bool:
        return self.history.observe({n: (s, executor) for n, s in statuses.items()}, at)

    def walk(self, issue: int, steps: list[tuple[int, str]], every: int = 120) -> None:
        """Snapshots every `every` seconds through the given (time, status)."""
        for (at, status), after in zip(steps, [*steps[1:], None], strict=True):
            end = after[0] if after else at + every
            for t in range(at, end, every):
                self.see(t, {issue: status})

    def lines(self, name: str) -> list[monitor.Json]:
        path = self.root / name
        return (
            [json.loads(x) for x in path.read_text().splitlines()]
            if path.exists()
            else []
        )


class LedgerTest(Base):
    def test_first_snapshot_seeds_then_records_observed_changes(self) -> None:
        self.see(NOW, {1: "Ready", 2: "Backlog"})
        self.see(NOW + 120, {1: "In progress", 2: "Backlog"})
        rows = self.lines(th.LEDGER)
        self.assertEqual([r.get("source") for r in rows], ["seed", "seed", None])
        self.assertEqual(
            rows[2],
            {
                "issue": 1,
                "from": "Ready",
                "to": "In progress",
                "seen_before": NOW,
                "seen_at": NOW + 120,
                "executor": "Claude",
            },
        )

    def test_cached_snapshot_appends_nothing(self) -> None:
        self.assertTrue(self.see(NOW, {1: "Ready"}))
        self.assertFalse(self.see(NOW, {1: "In progress"}))
        self.assertFalse(self.see(NOW - 60, {1: "In progress"}))
        self.assertEqual(len(self.lines(th.LEDGER)), 1)

    def test_outage_with_the_same_status_stores_a_gap_and_splits_the_segment(
        self,
    ) -> None:
        self.see(NOW - 120, {1: "Ready"})
        self.see(NOW, {1: "In progress"})
        self.see(NOW + 120, {1: "In progress"})
        # The collector was down for an hour (and restarted); nothing changed.
        self.history = self.fresh(NOW + 120 + H)
        self.see(NOW + 120 + H, {1: "In progress"})
        self.assertEqual(
            self.lines(th.COVERAGE),
            [{"gap_start": NOW + 120, "gap_end": NOW + 120 + H}],
        )
        self.assertEqual(len(self.lines(th.LEDGER)), 2)
        history = self.fresh(NOW + 2 * H)
        self.assertEqual(history.gaps, [th.Gap(NOW + 120, NOW + 120 + H)])
        segments = th.intervals(history.entries[1], history.gaps)
        self.assertEqual(
            [(s.status, s.start, s.end) for s in segments],
            [
                ("Ready", NOW - 120, NOW),
                ("In progress", NOW, NOW + 120),
                ("gap", NOW + 120, NOW + 120 + H),
                ("In progress", NOW + 120 + H, None),
            ],
        )
        # Time in status overlaps the gap: "?" in the table.
        self.assertEqual(history.summary(NOW + 2 * H).tickets[1].exact, "gap")
        # The next Done ends an In progress time that overlaps the gap.
        history.observe({1: ("Done", "Claude")}, NOW + 240 + H)
        [episode] = history.summary(NOW + 2 * H).episodes
        self.assertNotIn("In progress", episode.times)
        self.assertIsNone(episode.cycle)

    def test_change_across_a_gap_is_uncertain_and_left_out(self) -> None:
        self.walk(1, [(NOW, "Ready"), (NOW + H, "In progress")])
        # In review is first seen 20 min after the last snapshot.
        self.see(NOW + 2 * H + 1200, {1: "In review"})
        self.walk(1, [(NOW + 3 * H, "In review"), (NOW + 4 * H, "Done")])
        rows = self.lines(th.LEDGER)
        self.assertTrue(rows[2]["uncertain"])
        [episode] = self.history.summary(NOW + 5 * H).episodes
        # Ready and In progress end at the uncertain entry; In review starts there.
        self.assertEqual(episode.times, {})
        self.assertIsNone(episode.cycle)  # its interval overlaps the gap
        self.assertIsNone(episode.lead)

    def test_broken_last_line_is_cut_and_a_broken_middle_line_fails(self) -> None:
        self.see(NOW, {1: "Ready"})
        self.see(NOW + 120, {1: "In progress"})
        path = self.root / th.LEDGER
        good = path.read_bytes()
        path.write_bytes(good + b'{"issue": 1, "to": "In rev')
        with self.assertLogs(level="WARNING"):
            history = self.fresh()
        self.assertEqual(path.read_bytes(), good)
        self.assertEqual(len(history.entries[1]), 2)
        # The next append starts on a clean line.
        history.observe({1: ("In review", "Claude")}, NOW + 240)
        self.assertEqual(len(self.lines(th.LEDGER)), 3)
        path.write_bytes(b"not json\n" + good)
        with self.assertRaises(ValueError):
            self.fresh()

    def test_restart_replay_drops_duplicates_and_keeps_the_last_snapshot(self) -> None:
        self.see(NOW, {1: "Ready"})
        self.see(NOW + 120, {1: "In progress"})
        path = self.root / th.LEDGER
        path.write_text(path.read_text() * 2)
        history = self.fresh()
        self.assertEqual([e.to for e in history.entries[1]], ["Ready", "In progress"])
        self.assertEqual(len(self.lines(th.LEDGER)), 2)
        self.assertEqual(history.last, NOW + 120)
        self.assertFalse(history.observe({1: ("Ready", "Claude")}, NOW + 120))

    def test_lost_state_file_makes_the_next_change_uncertain(self) -> None:
        self.see(NOW, {1: "Ready"})
        (self.root / th.STATE).unlink()
        history = self.fresh()
        history.observe({1: ("In progress", "Claude")}, NOW + 120)
        self.assertTrue(history.entries[1][-1].uncertain)


class EpisodeTest(Base):
    def test_two_episodes_have_their_own_ids_and_only_the_first_a_lead_time(
        self,
    ) -> None:
        t = NOW - 5 * DAY
        self.walk(
            7,
            [
                (t, "Ready"),
                (t + 2 * H, "In progress"),
                (t + 6 * H, "Done"),
                (t + DAY, "In progress"),
                (t + DAY + 3 * H, "Done"),
            ],
        )
        summary = self.history.summary(NOW)
        second, first = summary.episodes
        # Ready is the seed: no lead time and no Ready time for episode 1.
        self.assertIsNone(first.lead)
        self.assertEqual(first.cycle, 4 * H)
        self.assertEqual(second.cycle, 3 * H)
        self.assertIsNone(second.lead)
        sink = Sink()
        th.history_metrics(sink, summary, [7], NOW)
        self.assertEqual(
            sorted(row[1]["episode"] for row in sink.rows("ticket_done_seconds")),
            sorted([str(t + 6 * H), str(t + DAY + 3 * H)]),
        )
        [episode] = [e for e in self.lines(th.LEDGER) if e["to"] == "Done"][:1]
        self.assertEqual(episode["episode"], t + 6 * H)

    def test_lead_cycle_and_time_in_status(self) -> None:
        t = NOW - 2 * DAY
        self.walk(
            3,
            [
                (t, "Backlog"),
                (t + H, "Ready"),
                (t + 3 * H, "In progress"),
                (t + 5 * H, "In review"),
                (t + 6 * H, "In progress"),
                (t + 7 * H, "In review"),
                (t + 8 * H, "Done"),
            ],
        )
        [episode] = self.history.summary(NOW).episodes
        self.assertEqual(episode.lead, 7 * H)
        self.assertEqual(episode.cycle, 5 * H)
        # Repeat visits add up.
        self.assertEqual(
            episode.times, {"Ready": 2 * H, "In progress": 3 * H, "In review": 2 * H}
        )

    def test_retention_keeps_an_old_visit_of_a_recent_episode(self) -> None:
        old = NOW - 100 * DAY
        self.walk(5, [(old, "Ready"), (old + 600, "In progress")], every=600)
        # Same ticket, other old ticket finished long ago.
        self.walk(6, [(old, "Ready"), (old + 600, "Done")], every=600)
        self.see(old + 1200, {5: "In progress", 6: "Done"})
        self.history.last = NOW - 3 * DAY - 120
        self.walk(
            5,
            [
                (NOW - 3 * DAY, "Ready"),
                (NOW - 2 * DAY, "In progress"),
                (NOW - DAY, "Done"),
            ],
        )
        history = self.fresh(NOW)
        # Issue 6's old episode is past retention; issue 5's old visit stays.
        self.assertNotIn(6, history.entries)
        [episode] = history.summary(NOW).episodes
        self.assertEqual(episode.issue, 5)
        self.assertEqual(episode.cycle, NOW - DAY - (old + 600))
        self.assertEqual(history.entries[5][1].seen_at, old + 600)

    def test_cycle_starts_at_the_first_visit_after_retention(self) -> None:
        old = NOW - 100 * DAY
        self.walk(5, [(old, "Ready"), (old + 600, "In progress")], every=600)
        # No gap: snapshots every 10 min for 100 days would be slow, so
        # rebuild the files directly.
        rows = self.lines(th.LEDGER)
        rows.append(
            th.Entry(
                5, "Done", NOW - DAY, "Codex", "In progress", NOW - DAY - 120
            ).row()
        )
        th.rewrite_lines(self.root / th.LEDGER, rows)
        history = self.fresh(NOW)
        [episode] = history.summary(NOW).episodes
        self.assertEqual(episode.cycle, NOW - DAY - (old + 600))

    def test_done_per_day_uses_the_stored_executor(self) -> None:
        t = NOW - 2 * H
        self.walk(4, [(t, "In review")])
        self.history.observe({4: ("Done", "Codex")}, t + 120)
        # The board Executor changes later; a restart replays the ledger.
        self.history.observe({4: ("Done", "Anton")}, t + 240)
        history = self.fresh(NOW)
        sink = Sink()
        th.history_metrics(sink, history.summary(NOW), [4], NOW)
        day = th.berlin_day(t + 120)
        self.assertEqual(sink.value("tickets_done_day", day=day, executor="Codex"), 1)
        self.assertEqual(sink.value("tickets_done_day", day=day, executor="Anton"), 0)
        self.assertEqual(len(sink.rows("tickets_done_day")), 14 * len(th.EXECUTORS))

    def test_done_days_are_berlin_calendar_days_across_dst(self) -> None:
        """Codex review of https://github.com/phaabe/live.moafunk.de/pull/640."""
        berlin = th.LOCAL
        for now, done_at in (
            # Spring: 2026-03-29 has 23 hours.
            (
                datetime(2026, 3, 30, 0, 30, tzinfo=berlin),
                datetime(2026, 3, 29, 12, tzinfo=berlin),
            ),
            # Autumn: 2026-10-25 has 25 hours.
            (
                datetime(2026, 10, 25, 23, 30, tzinfo=berlin),
                datetime(2026, 10, 25, 1, tzinfo=berlin),
            ),
        ):
            with self.subTest(now=now):
                at = int(done_at.timestamp())
                episode = th.Episode(
                    1,
                    th.Entry(1, "Done", at, "Codex", "In review", at - 120),
                    None,
                    None,
                    {},
                )
                sink = Sink()
                th.history_metrics(
                    sink, th.Summary(episodes=[episode]), [], now.timestamp()
                )
                rows = sink.rows("tickets_done_day")
                days = {labels["day"] for _, labels, _ in rows}
                self.assertEqual(len(days), 14)
                self.assertEqual(len(rows), 14 * len(th.EXECUTORS))
                self.assertEqual(
                    sink.value(
                        "tickets_done_day",
                        day=done_at.date().isoformat(),
                        executor="Codex",
                    ),
                    1,
                )

    def test_seeded_done_is_not_counted(self) -> None:
        self.see(NOW - H, {4: "Done"})
        self.assertEqual(self.history.summary(NOW).episodes, [])

    def test_quantiles_use_all_episodes_but_export_fifty(self) -> None:
        rows = []
        for i in range(60):
            start = NOW - 10 * DAY + i * 600
            cycle = (i + 1) * 60 if i < 10 else 10 * H
            rows.append(
                th.Entry(
                    100 + i, "In progress", start, "Codex", "Ready", start - 120
                ).row()
            )
            rows.append(
                th.Entry(
                    100 + i,
                    "Done",
                    start + cycle,
                    "Codex",
                    "In progress",
                    start + cycle - 120,
                ).row()
            )
        th.rewrite_lines(self.root / th.LEDGER, rows)
        history = self.fresh(NOW)
        summary = history.summary(NOW)
        sink = Sink()
        th.history_metrics(sink, summary, [], NOW)
        self.assertEqual(len(sink.rows("ticket_cycle_seconds")), th.MAX_EPISODES)
        exported = sorted(v for _, _, v in sink.rows("ticket_cycle_seconds"))
        p50 = sink.value("ticket_cycle_quantile_seconds", quantile="0.5")
        self.assertEqual(p50, 10 * H)
        # The ten short cycles are the oldest: not exported, still counted.
        self.assertEqual(min(exported), 10 * H)
        all_cycles = sorted(e.cycle for e in summary.episodes)
        self.assertEqual(all_cycles[0], 60)
        self.assertEqual(
            sink.value("ticket_cycle_quantile_seconds", quantile="0.85"),
            th.nearest_rank(all_cycles, 0.85),
        )

    def test_no_samples_no_quantiles(self) -> None:
        sink = Sink()
        th.history_metrics(sink, th.Summary(), [], NOW)
        self.assertEqual(sink.rows("ticket_cycle_quantile_seconds"), [])
        self.assertEqual(sink.rows("ticket_time_in_status_seconds"), [])
        self.assertEqual(sink.rows("ticket_lead_median_seconds"), [])


class SegmentTest(Base):
    def update(self, views, now, shown=(1,), prs=None) -> None:  # type: ignore[no-untyped-def]
        self.history.update_segments(list(shown), prs or {}, views, now)

    def latest(self) -> dict[str, monitor.Json]:
        found: dict[str, monitor.Json] = {}
        for row in self.lines(th.SEGMENTS):
            if (
                row["segment_id"] not in found
                or row["rev"] >= found[row["segment_id"]]["rev"]
            ):
                found[row["segment_id"]] = row
        return found

    def test_agents_from_events_and_unverified_events_never_the_log(self) -> None:
        self.see(NOW, {1: "In progress"})
        views = {
            "codex": [tick(NOW + 10, NOW + 60, "continue", f"{ISSUE}/1")],
            "claude": [
                tick(NOW + 70, NOW + 100, "continue", f"{ISSUE}/1", "events_unverified")
            ],
            "claude-2": [tick(NOW + 110, NOW + 150, "continue", f"{ISSUE}/1", "log")],
        }
        self.update(views, NOW + 200)
        [row] = self.latest().values()
        self.assertEqual((row["agent"], row["verified"]), ("claude", 0))
        summary = self.history.summary(NOW + 200)
        self.assertEqual(summary.tickets[1].agent, "claude (unverified)")

    def test_continue_on_the_draft_pr_names_the_agent(self) -> None:
        self.see(NOW, {1: "In progress"})
        self.update(
            {"codex": [tick(NOW, NOW + 60, "continue", f"{PULL}/9")]},
            NOW + 120,
            prs={1: 9},
        )
        [row] = self.latest().values()
        self.assertEqual(row["agent"], "codex")

    def test_a_claim_spanning_the_status_change_names_the_new_segment(self) -> None:
        self.see(NOW, {1: "Ready"})
        self.see(NOW + 120, {1: "In progress"})
        views = {
            "claude": [
                tick(NOW + 30, NOW + 200, "claim", f"{ISSUE}/1", "events_unverified")
            ]
        }
        self.update(views, NOW + 240)
        rows = self.latest()
        self.assertEqual(rows[f"1-{NOW}"]["agent"], "")
        self.assertEqual(rows[f"1-{NOW + 120}"]["agent"], "claude")

    def test_review_needs_the_linked_pr(self) -> None:
        self.see(NOW, {1: "In review"})
        views = {"codex": [tick(NOW, NOW + 60, "review", f"{PULL}/9")]}
        self.update(views, NOW + 120)
        self.assertEqual(self.latest()[f"1-{NOW}"]["agent"], "")
        self.update(views, NOW + 180, prs={1: 9})
        self.assertEqual(self.latest()[f"1-{NOW}"]["agent"], "codex")

    def test_late_finish_corrects_within_a_day_then_freezes(self) -> None:
        self.see(NOW, {1: "In review"})
        self.see(NOW + 120, {1: "Done"})
        first = {"codex": [tick(NOW, NOW + 60, "review", f"{PULL}/9", ident="a")]}
        self.update(first, NOW + 180, prs={1: 9})
        later = {
            "codex": first["codex"],
            "codex-2": [tick(NOW + 30, NOW + 100, "review", f"{PULL}/9", ident="b")],
        }
        self.update(later, NOW + 120 + DAY, prs={1: 9})
        row = self.latest()[f"1-{NOW}"]
        self.assertEqual((row["agent"], row["rev"]), ("codex-2", 2))
        newest = {
            "codex-3": [tick(NOW + 40, NOW + 110, "review", f"{PULL}/9", ident="c")]
        }
        self.update(newest, NOW + 121 + DAY, prs={1: 9})
        self.assertEqual(self.latest()[f"1-{NOW}"]["agent"], "codex-2")

    def test_rotated_ticks_keep_the_agent(self) -> None:
        self.see(NOW, {1: "In progress"})
        self.update({"codex": [tick(NOW, NOW + 60, "claim", f"{ISSUE}/1")]}, NOW + 120)
        # The tick ledger rotated: no ticks left, and a restart.
        self.history = self.fresh(NOW + 240)
        self.update({"codex": []}, NOW + 240)
        self.update(None, NOW + 300)
        self.assertEqual(self.latest()[f"1-{NOW}"]["agent"], "codex")
        self.assertEqual(self.history.summary(NOW + 300).tickets[1].agent, "codex")

    def test_restart_writes_no_second_segment_and_refreshes_hourly(self) -> None:
        self.see(NOW, {1: "Ready"})
        self.update({}, NOW + 60)
        self.history = self.fresh(NOW + 120)
        self.update({}, NOW + 120)
        self.assertEqual(len(self.lines(th.SEGMENTS)), 1)
        self.update({}, NOW + 60 + H)
        rows = self.lines(th.SEGMENTS)
        self.assertEqual([r["rev"] for r in rows], [1, 2])
        self.assertEqual({r["segment_id"] for r in rows}, {f"1-{NOW}"})

    def test_closing_writes_the_end_and_old_segments_are_skipped(self) -> None:
        self.see(NOW - 9 * DAY, {1: "Ready"})
        self.see(NOW - 9 * DAY + 120, {1: "Backlog"})
        self.history.last = NOW - 120
        self.see(NOW, {1: "Backlog"})
        self.update({}, NOW + 60)
        # The Ready segment ended 9 days ago: outside the panel window.
        self.assertEqual(sorted(self.latest()), [f"1-{NOW - 9 * DAY + 120}"])

    def test_a_gap_from_the_segment_start_retires_the_old_segment(self) -> None:
        """Codex review of https://github.com/phaabe/live.moafunk.de/pull/640:
        replay must never hold two open segments of one ticket."""
        self.see(NOW - 120, {1: "Ready"})
        self.see(NOW, {1: "In progress"})
        self.update({}, NOW + 30)
        # No snapshot between NOW and NOW + 1200: the gap starts at NOW.
        self.see(NOW + 1200, {1: "In progress"})
        self.update({}, NOW + 1230)
        rows = self.latest()
        self.assertEqual(rows[f"1-{NOW}"]["status"], th.RETIRED)
        self.assertEqual(rows[f"1-{NOW}"]["end"], NOW)
        self.assertEqual(rows[f"1-{NOW}-gap"]["end"], NOW + 1200)
        history = self.fresh(NOW + 1300)
        still_open = [
            key for key, s in history.segments.items() if s.segment.end is None
        ]
        self.assertEqual(still_open, [f"1-{NOW + 1200}"])
        # Nothing new on the next cycle.
        history.update_segments([1], {}, {}, NOW + 1300)
        self.assertEqual(len(self.lines(th.SEGMENTS)), 5)

    def test_a_newer_tick_of_the_same_agent_is_kept(self) -> None:
        """Codex review of https://github.com/phaabe/live.moafunk.de/pull/640."""
        self.see(NOW, {1: "In progress"})
        first = tick(NOW, NOW + 10, "continue", f"{ISSUE}/1", ident="a")
        self.update({"codex": [first]}, NOW + 20)
        newer = tick(NOW + 60, NOW + 100, "continue", f"{ISSUE}/1", ident="b")
        self.update({"codex": [first, newer]}, NOW + 120)
        self.assertEqual(self.latest()[f"1-{NOW}"]["tick"], [NOW + 100.0, "b"])
        # Rotation and a restart; another agent's tick ends between the two.
        self.history = self.fresh(NOW + 200)
        other = tick(NOW + 20, NOW + 50, "continue", f"{ISSUE}/1", "events", "c")
        self.update({"codex-2": [other]}, NOW + 200)
        self.assertEqual(self.latest()[f"1-{NOW}"]["agent"], "codex")

    def test_compaction_keeps_the_newest_revisions(self) -> None:
        self.see(NOW, {1: "Ready"})
        for i in range(5):
            self.update({}, NOW + 60 + i * H)
        with patch.object(th, "MAX_SEGMENT_FILE", 10):
            self.update({}, NOW + 60 + 5 * H)
        rows = self.lines(th.SEGMENTS)
        self.assertEqual([r["rev"] for r in rows], [6])
        self.assertEqual(self.fresh(NOW + 6 * H).segments[f"1-{NOW}"].rev, 6)


class ChecksTest(Base):
    def summary_for(self, ages: dict[int, tuple[str, float]]) -> th.Summary:
        summary = th.Summary()
        for issue, (status, seconds) in ages.items():
            summary.tickets[issue] = th.TicketTime(
                status, int(NOW - seconds), "1", None, ""
            )
        return summary

    def test_time_checks_are_strict_at_the_boundaries(self) -> None:
        state = snapshot(
            items=[
                ticket(1, "In progress"),
                ticket(2, "In progress"),
                ticket(3, "In progress"),
                ticket(4, "In review"),
                ticket(5, "In review"),
            ]
        )
        summary = self.summary_for(
            {
                1: ("In progress", 24 * H),
                2: ("In progress", 24 * H + 1),
                3: ("In progress", 48 * H),
                4: ("In review", 24 * H),
                5: ("In review", 24 * H + 1),
            }
        )
        sink = Sink()
        tickets.ticket_metrics(sink, state, tickets.Extra({}, {}), set(), NOW, summary)
        members = {
            (labels["check"], labels["issue"])
            for _, labels, _ in sink.rows("ticket_check_member")
        }
        self.assertEqual(
            members,
            {
                ("in_progress_long", "2"),
                ("in_progress_long", "3"),
                ("in_review_long", "5"),
            },
        )
        # 48 h is not more than 48 h: amber, not red.
        self.assertEqual(
            sink.value("ticket_check_severity", check="in_progress_long"), 2
        )
        self.assertEqual(sink.value("ticket_check_severity", check="in_review_long"), 3)
        summary.tickets[3] = th.TicketTime(
            "In progress", NOW - 48 * H - 1, "0", None, ""
        )
        sink = Sink()
        tickets.ticket_metrics(sink, state, tickets.Extra({}, {}), set(), NOW, summary)
        self.assertEqual(
            sink.value("ticket_check_severity", check="in_progress_long"), 3
        )

    def test_ready_and_refinement_turn_amber_by_time(self) -> None:
        state = snapshot(items=[ticket(1, "Ready"), ticket(2, "Refinement")])
        for ready, refinement, expected in (
            (24 * H, 72 * H, (1, 1)),
            (24 * H + 1, 72 * H + 1, (2, 2)),
        ):
            with self.subTest(ready=ready):
                summary = self.summary_for(
                    {1: ("Ready", ready), 2: ("Refinement", refinement)}
                )
                sink = Sink()
                tickets.ticket_metrics(
                    sink, state, tickets.Extra({}, {2: None}), {1}, NOW, summary
                )
                self.assertEqual(
                    (
                        sink.value("ticket_check_severity", check="ready_claimable"),
                        sink.value(
                            "ticket_check_severity", check="refinement_unreviewed"
                        ),
                    ),
                    expected,
                )

    def test_without_history_the_time_checks_are_unknown(self) -> None:
        state = snapshot(items=[ticket(1, "In progress")])
        sink = Sink()
        tickets.ticket_metrics(sink, state, tickets.Extra({}, {}), set(), NOW)
        self.assertEqual(sink.rows("ticket_check_count", check="in_progress_long"), [])
        self.assertEqual(sink.value("ticket_status_code", issue="1"), 4)

    def test_no_not_done_ticket_is_in_more_than_two_checks(self) -> None:
        statuses = ["Backlog", "Refinement", "Ready", "In progress", "In review"]
        state = snapshot(items=[ticket(i + 1, s) for i, s in enumerate(statuses)])
        summary = self.summary_for(
            {i + 1: (s, 30 * DAY) for i, s in enumerate(statuses)}
        )
        hidden = {3: [f"{ISSUE}/99"]}
        sink = Sink()
        tickets.ticket_metrics(
            sink, state, tickets.Extra(hidden, {2: None}), {3}, NOW, summary
        )
        counts: dict[str, int] = {}
        for _, labels, _ in sink.rows("ticket_check_member"):
            counts[labels["issue"]] = counts.get(labels["issue"], 0) + 1
        self.assertEqual(max(counts.values()), 2)
        self.assertEqual(counts["3"], 2)

    def test_info_labels_carry_agent_and_ready_entry(self) -> None:
        state = snapshot(items=[ticket(1, "In progress")])
        summary = th.Summary()
        summary.tickets[1] = th.TicketTime(
            "In progress", NOW - H, "1", NOW - 2 * H, "codex"
        )
        sink = Sink()
        tickets.ticket_metrics(sink, state, tickets.Extra({}, {}), set(), NOW, summary)
        [(_, labels, _)] = sink.rows("ticket_info")
        self.assertEqual(labels["last_agent"], "codex")
        self.assertEqual(labels["ready_entered"], str(NOW - 2 * H))

    def test_a_gap_beats_a_lower_bound(self) -> None:
        """Codex review of https://github.com/phaabe/live.moafunk.de/pull/640:
        a seeded or uncertain age that overlaps a gap shows "?"."""
        self.see(NOW, {1: "In progress", 2: "Ready"})
        self.see(NOW + 1200, {1: "In progress", 2: "In progress"})
        summary = self.history.summary(NOW + 1300)
        self.assertTrue(self.history.entries[2][-1].uncertain)
        self.assertEqual(summary.tickets[1].exact, "gap")  # seed
        # The uncertain entry is at the gap end: its own age has no gap.
        self.assertEqual(summary.tickets[2].exact, "0")
        self.see(NOW + 1320, {1: "In progress", 2: "In progress"})
        self.see(NOW + 3000, {1: "In progress", 2: "In progress"})
        summary = self.history.summary(NOW + 3100)
        self.assertEqual(summary.tickets[2].exact, "gap")

    def test_seeded_age_is_a_lower_bound(self) -> None:
        self.see(NOW - 2 * DAY, {1: "In progress"})
        summary = self.history.summary(NOW)
        self.assertEqual(summary.tickets[1].exact, "0")
        sink = Sink()
        th.history_metrics(sink, summary, [1], NOW)
        self.assertEqual(
            sink.value("ticket_status_entered_seconds", issue="1", exact="0"),
            NOW - 2 * DAY,
        )


class CollectorTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "metrics").mkdir()
        patcher = patch.object(tickets.epic, "shared_reader", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def collect(self, state: monitor.Json, ticks=None) -> bool:  # type: ignore[no-untyped-def]
        def fetch(cache: Path, request: monitor.Json, timeout: float) -> monitor.Json:
            return {
                "deps": {str(n): [] for n in request["ready"]},
                "reviews": {str(n): None for n in request["refinement"]},
            }

        with patch.object(monitor.LATEST, "ticks", ticks):
            return monitor.collect_tickets(
                self.root / "metrics", self.root / "cache", 5, state, fetch
            )

    def health(self, source: str) -> str:
        text = (self.root / "metrics/tickets-health.prom").read_text()
        return "1" if f'epic_ticket_source_ok{{source="{source}"}} 1' in text else "0"

    def test_records_history_and_reports_local_and_ledger(self) -> None:
        state = snapshot(items=[ticket(1, "In progress")])
        state["fetched_at"] = int(time.time())
        self.assertFalse(self.collect(state))  # no ticks: local unknown
        self.assertEqual((self.health("ledger"), self.health("local")), ("1", "0"))
        text = (self.root / "metrics/tickets.prom").read_text()
        self.assertIn('epic_ticket_status_entered_seconds{exact="0",issue="1"}', text)
        self.assertTrue((self.root / th.LEDGER).exists())
        self.assertTrue((self.root / th.SEGMENTS).exists())
        state["fetched_at"] += 120
        self.assertTrue(self.collect(state, (time.time(), {})))

    def test_snapshot_without_time_makes_the_ledger_unknown(self) -> None:
        state = snapshot(items=[ticket(1, "In progress")])
        with self.assertLogs(level="ERROR"):
            self.collect(state, (time.time(), {}))
        self.assertEqual(self.health("ledger"), "0")
        self.assertEqual(self.health("board"), "1")
        text = (self.root / "metrics/tickets.prom").read_text()
        self.assertNotIn('check="in_progress_long"', text)

    def test_broken_ledger_is_unknown_and_appends_nothing(self) -> None:
        (self.root / th.LEDGER).write_text("broken\n{}\n")
        state = snapshot(items=[ticket(1, "In progress")])
        state["fetched_at"] = int(time.time())
        with self.assertLogs(level="ERROR"):
            self.collect(state, (time.time(), {}))
        self.assertEqual(self.health("ledger"), "0")
        self.assertEqual((self.root / th.LEDGER).read_text(), "broken\n{}\n")

    def test_old_ticks_are_not_current(self) -> None:
        state = snapshot(items=[ticket(1, "In progress")])
        state["fetched_at"] = int(time.time())
        self.collect(state, (time.time() - monitor.TICKS_MAX_AGE - 1, {}))
        self.assertEqual(self.health("local"), "0")


class PlumbingTest(unittest.TestCase):
    def test_segment_file_is_listed_for_alloy_only_as_a_regular_file(self) -> None:
        import agents

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = agents.discover(root / "state", NOW)
            segments = root / "ticket-segments.jsonl"
            self.assertEqual(json.loads(monitor.alloy_targets(registry, segments)), [])
            segments.symlink_to(root / "elsewhere")
            self.assertEqual(json.loads(monitor.alloy_targets(registry, segments)), [])
            segments.unlink()
            segments.write_text("")
            [row] = json.loads(monitor.alloy_targets(registry, segments))
            self.assertEqual(
                row["labels"],
                {
                    "__path__": "/targets/ticket-segments.jsonl",
                    "stream": "ticket_segments",
                },
            )

    def test_fetched_at_comes_from_the_shared_snapshot(self) -> None:
        import github_state

        snap = github_state.Snapshot(
            {"items": []}, "2026-10-05T12:00:00Z", 30.0, "cache"
        )
        with (
            patch.object(monitor.epic, "shared_reader", return_value=True),
            patch.object(github_state, "read_snapshot", return_value=snap),
        ):
            state = monitor.fetch_state_with_time()
        self.assertEqual(state["fetched_at"], NOW)


if __name__ == "__main__":
    unittest.main()
