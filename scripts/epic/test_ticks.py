"""Tick ledger tests. Run: python3 -m unittest discover -s scripts/epic."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from collections.abc import Callable
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import monitor
import ticks

URL = "https://github.com/phaabe/live.moafunk.de"
# 2026-09-28 12:00:00 UTC (14:00 in Berlin).
NOON = datetime(2026, 9, 28, 12, tzinfo=timezone.utc).timestamp()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def tick(
    start: float,
    exit_code: int | None = 0,
    *,
    action: str | None = '{"action": "review", "reason": "x", "pr": 7}',
    body: str = "",
) -> str:
    lines = ["", f"tick: started {iso(start)} repo=/tmp/runner"]
    if action is not None:
        lines.append(action)
    if body:
        lines.append(body)
    if exit_code is not None:
        lines.append(f"tick: finished exit={exit_code}")
    return "\n".join(lines) + "\n"


class Metrics(monitor.Metrics):
    def samples(self) -> dict[str, float]:
        return {
            line.rsplit(" ", 1)[0]: float(line.rsplit(" ", 1)[1])
            for line in self.lines
            if not line.startswith("#")
        }


class LedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.log = self.root / "codex.log"
        self.log.write_text("")
        self.checkpoint = self.root / "runtime" / "ticks-codex.json"

    def ledger(self) -> ticks.LogLedger:
        return ticks.LogLedger(
            "codex", self.log, self.checkpoint, monitor.action_labels
        )

    def append(self, text: str | bytes) -> None:
        with self.log.open("ab") as out:
            out.write(text.encode() if isinstance(text, str) else text)

    def run_cycle(self, ledger: ticks.LogLedger, now: float) -> dict[str, float]:
        ledger.update(now)
        ledger.save()
        metrics = Metrics()
        ticks.export(metrics, ledger, now)
        return metrics.samples()

    def last(self, ledger: ticks.LogLedger) -> dict:
        return ledger.ticks[-1]

    # Outcomes

    def test_exit_codes_map_to_named_outcomes(self) -> None:
        cases = {
            0: "ok",
            124: "timeout",
            143: "killed",
            1: "error",
            75: "error",  # missing or invalid result also exits 75
        }
        for code, expected in cases.items():
            with self.subTest(code=code):
                self.log.write_text(tick(NOON, code))
                self.checkpoint.unlink(missing_ok=True)
                ledger = self.ledger()
                ledger.update(NOON)
                self.assertEqual(self.last(ledger)["outcome"], expected)

    def test_exit_75_is_blocked_only_with_the_runner_blocked_line(self) -> None:
        self.log.write_text(
            tick(NOON, 75, body="backoff: model reported blocked: stale PR")
        )
        ledger = self.ledger()
        ledger.update(NOON)
        self.assertEqual(self.last(ledger)["outcome"], "blocked")
        self.assertEqual(self.last(ledger)["phase"], "")

    def test_gh_failure_in_selector_is_phase_select(self) -> None:
        body = (
            "Traceback (most recent call last):\n"
            "subprocess.CalledProcessError: Command '['gh', 'pr', 'list']' "
            "returned non-zero exit status 1."
        )
        self.log.write_text(tick(NOON, 1, action=None, body=body))
        ledger = self.ledger()
        ledger.update(NOON)
        self.assertEqual(
            (self.last(ledger)["outcome"], self.last(ledger)["phase"]),
            ("error", "select"),
        )

    def test_start_without_finish_is_interrupted(self) -> None:
        self.log.write_text(tick(NOON, None) + tick(NOON + 60, 0))
        ledger = self.ledger()
        ledger.update(NOON)
        self.assertEqual([t["outcome"] for t in ledger.ticks], ["interrupted", "ok"])

    def test_open_tick_is_not_in_the_ledger(self) -> None:
        self.log.write_text(tick(NOON, None))
        ledger = self.ledger()
        ledger.update(NOON)
        self.assertEqual(ledger.ticks, [])

    def test_contender_lock_line_does_not_change_the_tick(self) -> None:
        body = "tick: locked; owner pid 28194 is live or within its timeout"
        self.log.write_text(tick(NOON, 0, body=body))
        ledger = self.ledger()
        ledger.update(NOON)
        self.assertEqual(len(ledger.ticks), 1)
        self.assertEqual(self.last(ledger)["outcome"], "ok")

    def test_tokens_accept_commas_and_ignore_non_numbers(self) -> None:
        self.log.write_text(
            tick(NOON, 0, body="tokens used\n89,040")
            + tick(NOON + 60, 0, body="tokens used\n1234")
            + tick(NOON + 120, 0, body="tokens used\nabout 5k")
        )
        ledger = self.ledger()
        ledger.update(NOON)
        self.assertEqual([t["tokens"] for t in ledger.ticks], [89040, 1234, None])

    def test_action_is_read_only_from_the_first_line_and_normalized(self) -> None:
        later = '{"action": "merge", "pr": 9}'
        foreign = '{"action": "hack", "issue": "https://evil.example/issues/1"}'
        self.log.write_text(
            tick(
                NOON,
                0,
                action='{"action": "claim", "issue": "' + URL + '/issues/5"}',
                body=later,
            )
            + tick(NOON + 60, 0, action=foreign)
        )
        ledger = self.ledger()
        ledger.update(NOON)
        first, second = ledger.ticks
        self.assertEqual(
            (first["action"], first["target"]), ("claim", f"{URL}/issues/5")
        )
        self.assertEqual((second["action"], second["target"]), ("unknown", ""))

    # Trust boundary

    def test_prefixed_or_indented_markers_are_ignored(self) -> None:
        body = "\n".join(
            [
                "+tick: finished exit=0",
                "  tick: finished exit=0",
                "> tick: started 2026-09-28T12:30:00Z repo=/x",
                "tick: finished exit=0 extra",
            ]
        )
        self.log.write_text(tick(NOON, 1, body=body))
        ledger = self.ledger()
        ledger.update(NOON)
        self.assertEqual([t["outcome"] for t in ledger.ticks], ["error"])

    def test_exact_forged_marker_is_accepted_which_is_why_log_is_best_effort(
        self,
    ) -> None:
        # Model output that prints an exact runner line cannot be told apart.
        self.log.write_text(tick(NOON, 1, body="tick: finished exit=0"))
        ledger = self.ledger()
        ledger.update(NOON)
        self.assertEqual(ledger.ticks[0]["outcome"], "ok")
        metrics = Metrics()
        ticks.export(metrics, ledger, NOON)
        self.assertIn('source="log"', "\n".join(metrics.lines))

    def test_model_text_never_reaches_labels(self) -> None:
        secret = "SECRET-PROMPT-TEXT git push --force origin main"
        self.log.write_text(
            tick(
                NOON,
                75,
                action='{"action": "review", "reason": "' + secret + '", "pr": 7}',
                body=f"{secret}\nbackoff: model reported blocked: {secret}\n"
                f"tokens used\n{secret}",
            )
        )
        ledger = self.ledger()
        ledger.update(NOON)
        metrics = Metrics()
        ticks.export(metrics, ledger, NOON)
        self.assertNotIn("SECRET", "\n".join(metrics.lines))
        self.assertNotIn("SECRET", json.dumps(ledger.ticks))

    # Incremental reading and counters

    def test_first_read_fills_history_but_counts_nothing(self) -> None:
        self.log.write_text(tick(NOON - 600, 0) + tick(NOON - 300, 1))
        samples = self.run_cycle(self.ledger(), NOON)
        self.assertEqual(samples['epic_ticks_total{agent="codex",outcome="ok"}'], 0)
        self.assertEqual(samples['epic_ticks_today{agent="codex",outcome="error"}'], 1)
        self.assertEqual(
            samples['epic_tick_coverage_start_timestamp_seconds{agent="codex"}'], NOON
        )

    def test_new_ticks_are_counted_once_with_observed_end(self) -> None:
        ledger = self.ledger()
        self.run_cycle(ledger, NOON)
        self.append(tick(NOON + 10, 0))
        samples = self.run_cycle(ledger, NOON + 100)
        self.assertEqual(samples['epic_ticks_total{agent="codex",outcome="ok"}'], 1)
        self.assertEqual(self.last(ledger)["end"], NOON + 100)
        samples = self.run_cycle(ledger, NOON + 105)
        self.assertEqual(samples['epic_ticks_total{agent="codex",outcome="ok"}'], 1)

    def test_every_outcome_series_exists_from_the_first_publish(self) -> None:
        samples = self.run_cycle(self.ledger(), NOON)
        for kind in ticks.SEVERITY:
            self.assertEqual(
                samples[f'epic_ticks_total{{agent="codex",outcome="{kind}"}}'], 0
            )

    def test_counter_has_counter_type(self) -> None:
        ledger = self.ledger()
        ledger.update(NOON)
        metrics = Metrics()
        ticks.export(metrics, ledger, NOON)
        self.assertIn("# TYPE epic_ticks_total counter", metrics.lines)

    def test_partial_line_and_split_utf8_across_reads(self) -> None:
        ledger = self.ledger()
        self.run_cycle(ledger, NOON)
        text = tick(NOON + 10, 0, body="café ✓").encode()
        cut = text.index("✓".encode()) + 1  # inside the 3-byte character
        self.append(text[:cut])
        self.run_cycle(ledger, NOON + 20)
        self.assertEqual(ledger.ticks, [])
        self.append(text[cut:])
        samples = self.run_cycle(ledger, NOON + 30)
        self.assertEqual(samples['epic_ticks_total{agent="codex",outcome="ok"}'], 1)

    def test_restart_between_read_and_checkpoint_does_not_double_count(self) -> None:
        ledger = self.ledger()
        self.run_cycle(ledger, NOON)
        self.append(tick(NOON + 10, 0))
        ledger.update(NOON + 20)  # crash before save()
        again = self.ledger()
        samples = self.run_cycle(again, NOON + 30)
        self.assertEqual(samples['epic_ticks_total{agent="codex",outcome="ok"}'], 1)
        self.assertEqual(len(again.ticks), 1)

    def test_restart_after_checkpoint_keeps_totals(self) -> None:
        ledger = self.ledger()
        self.run_cycle(ledger, NOON)
        self.append(tick(NOON + 10, 0))
        self.run_cycle(ledger, NOON + 20)
        samples = self.run_cycle(self.ledger(), NOON + 30)
        self.assertEqual(samples['epic_ticks_total{agent="codex",outcome="ok"}'], 1)

    def test_corrupt_checkpoint_rebuilds_history_and_resets_counters(self) -> None:
        ledger = self.ledger()
        self.run_cycle(ledger, NOON)
        self.append(tick(NOON + 10, 0))
        self.run_cycle(ledger, NOON + 20)
        self.checkpoint.write_text("{not json")
        with self.assertLogs(level="WARNING"):
            samples = self.run_cycle(self.ledger(), NOON + 30)
        self.assertEqual(samples['epic_ticks_total{agent="codex",outcome="ok"}'], 0)
        self.assertEqual(
            samples['epic_tick_coverage_start_timestamp_seconds{agent="codex"}'],
            NOON + 30,
        )
        self.assertEqual(samples['epic_ticks_today{agent="codex",outcome="ok"}'], 1)

    def test_truncation_reads_the_new_content_as_new(self) -> None:
        ledger = self.ledger()
        self.log.write_text(tick(NOON - 300, 0) + tick(NOON - 200, 0))
        self.run_cycle(ledger, NOON)
        self.log.write_text(tick(NOON + 10, 1))
        samples = self.run_cycle(ledger, NOON + 20)
        self.assertEqual(samples['epic_ticks_total{agent="codex",outcome="error"}'], 1)
        self.assertEqual(len(ledger.ticks), 3)

    def test_rotation_reads_the_new_file_from_the_start(self) -> None:
        ledger = self.ledger()
        self.log.write_text(tick(NOON - 300, 0) + "x" * 500 + "\n")
        self.run_cycle(ledger, NOON)
        rotated = self.root / "new.log"
        rotated.write_text(tick(NOON + 10, 0) + "y" * 900 + "\n")
        os.replace(rotated, self.log)
        samples = self.run_cycle(ledger, NOON + 20)
        self.assertEqual(samples['epic_ticks_total{agent="codex",outcome="ok"}'], 1)

    def test_missing_log_keeps_the_ledger(self) -> None:
        self.log.write_text(tick(NOON - 300, 0))
        ledger = self.ledger()
        self.run_cycle(ledger, NOON)
        self.log.unlink()
        with self.assertRaises(FileNotFoundError):
            ledger.update(NOON + 10)
        self.assertEqual(len(ledger.ticks), 1)

    def test_unchanged_file_does_not_rewrite_the_checkpoint(self) -> None:
        ledger = self.ledger()
        self.run_cycle(ledger, NOON)
        before = self.checkpoint.stat().st_mtime_ns
        os.utime(self.checkpoint, ns=(before - 10**9, before - 10**9))
        self.run_cycle(ledger, NOON + 5)
        self.assertEqual(self.checkpoint.stat().st_mtime_ns, before - 10**9)

    def test_overlong_line_is_skipped(self) -> None:
        ledger = self.ledger()
        self.run_cycle(ledger, NOON)
        with patch.object(ticks, "MAX_LINE", 100):
            self.append(tick(NOON + 10, None) + "z" * 300)
            self.run_cycle(ledger, NOON + 20)
            self.append("tick: finished exit=0\ntick: finished exit=0\n")
            self.run_cycle(ledger, NOON + 30)
        # The overlong line's tail is dropped, the next finish closes the tick.
        self.assertEqual([t["outcome"] for t in ledger.ticks], ["ok"])

    def test_totals_survive_ledger_eviction(self) -> None:
        ledger = self.ledger()
        self.run_cycle(ledger, NOON)
        with patch.object(ticks, "LEDGER_SIZE", 2):
            for i in range(5):
                self.append(tick(NOON + 10 + i * 60, 0))
            samples = self.run_cycle(ledger, NOON + 400)
        self.assertEqual(len(ledger.ticks), 2)
        self.assertEqual(samples['epic_ticks_total{agent="codex",outcome="ok"}'], 5)

    # Review regressions (PR 449)

    def test_checkpoint_with_wrong_types_is_rebuilt_not_a_crash(self) -> None:
        self.log.write_text(tick(NOON - 60, 0))
        ledger = self.ledger()
        self.run_cycle(ledger, NOON)
        good = json.loads(self.checkpoint.read_text())
        broken = [
            {**good, "partial": 7},
            {**good, "offset": -1},
            {**good, "totals": {**good["totals"], "ok": -3}},
            {**good, "totals": {**good["totals"], "ok": True}},
            {**good, "ticks": [{"tick": "x"}]},
            {**good, "ticks": [{**good["ticks"][0], "outcome": "weird"}]},
            {**good, "open": {"tick": 5}},
            {**good, "v": 2},
            {**good, "inode": True},
            {**good, "coverage_start": True},
            {**good, "ticks": [{**good["ticks"][0], "end": True}]},
            {**good, "ticks": [{**good["ticks"][0], "exit": True}]},
            {**good, "ticks": [{**good["ticks"][0], "tokens": True}]},
            {
                **good,
                "open": {
                    "tick": "2026-09-28T12:00:00Z",
                    "first": False,
                    "action": "",
                    "target": "",
                    "blocked": False,
                    "gh_failed": False,
                    "tokens": True,
                    "want_tokens": False,
                },
            },
        ]
        for value in broken:
            with self.subTest(value=value):
                self.checkpoint.write_text(json.dumps(value))
                with self.assertLogs(level="WARNING"):
                    samples = self.run_cycle(self.ledger(), NOON + 10)
                self.assertEqual(
                    samples['epic_ticks_total{agent="codex",outcome="ok"}'], 0
                )
                self.assertEqual(
                    len(json.loads(self.checkpoint.read_text())["ticks"]), 1
                )

    def test_ticks_are_counted_even_if_the_clock_goes_back(self) -> None:
        # Identity is the position in the log, not the wall-clock start time.
        ledger = self.ledger()
        self.run_cycle(ledger, NOON)
        self.append(tick(NOON + 60, 0) + tick(NOON + 60, 0) + tick(NOON - 3600, 1))
        samples = self.run_cycle(ledger, NOON + 120)
        self.assertEqual(samples['epic_ticks_total{agent="codex",outcome="ok"}'], 2)
        self.assertEqual(samples['epic_ticks_total{agent="codex",outcome="error"}'], 1)
        self.assertEqual(len(ledger.ticks), 3)

    def test_replay_of_many_ticks_after_a_crash_is_not_counted_again(self) -> None:
        ledger = self.ledger()
        self.run_cycle(ledger, NOON)
        self.append("".join(tick(NOON + 10 + i * 60, 0) for i in range(60)))
        ledger.update(NOON + 4000)  # crash before save()
        again = self.ledger()
        samples = self.run_cycle(again, NOON + 4100)
        self.assertEqual(samples['epic_ticks_total{agent="codex",outcome="ok"}'], 60)
        self.assertEqual(len(again.ticks), 60)

    def test_finish_line_split_across_the_baseline_is_counted(self) -> None:
        self.log.write_text(tick(NOON, None) + "tick: finished exit=")
        ledger = self.ledger()
        self.run_cycle(ledger, NOON + 10)
        self.append("0\n")
        samples = self.run_cycle(ledger, NOON + 20)
        self.assertEqual(samples['epic_ticks_total{agent="codex",outcome="ok"}'], 1)
        self.assertEqual(self.last(ledger)["end"], NOON + 20)

    def test_open_tick_survives_rotation_and_becomes_interrupted(self) -> None:
        ledger = self.ledger()
        self.run_cycle(ledger, NOON)
        self.append(tick(NOON + 10, None) + "half a li")
        self.run_cycle(ledger, NOON + 20)
        rotated = self.root / "new.log"
        rotated.write_text("ne from the new file\n" + tick(NOON + 100, 0))
        os.replace(rotated, self.log)
        samples = self.run_cycle(ledger, NOON + 200)
        self.assertEqual([t["outcome"] for t in ledger.ticks], ["interrupted", "ok"])
        self.assertEqual(
            samples['epic_ticks_total{agent="codex",outcome="interrupted"}'], 1
        )

    def test_impossible_start_date_is_skipped_and_parsing_continues(self) -> None:
        ledger = self.ledger()
        self.run_cycle(ledger, NOON)
        self.append(
            "tick: started 2026-99-28T12:00:00Z repo=/x\ntick: finished exit=0\n"
            + tick(NOON + 10, 0)
        )
        samples = self.run_cycle(ledger, NOON + 20)
        self.assertEqual(len(ledger.ticks), 1)
        self.assertEqual(samples['epic_ticks_total{agent="codex",outcome="ok"}'], 1)

    def test_parse_failure_mid_read_changes_nothing(self) -> None:
        calls = {"fail": True}

        def normalize(action: dict) -> dict[str, str]:
            if calls["fail"] and action.get("action") == "merge":
                raise RuntimeError("boom")
            return monitor.action_labels(action)

        ledger = ticks.LogLedger("codex", self.log, self.checkpoint, normalize)
        self.run_cycle(ledger, NOON)
        self.append(
            tick(NOON + 10, 0) + tick(NOON + 70, 0, action='{"action": "merge"}')
        )
        with self.assertRaises(RuntimeError):
            ledger.update(NOON + 100)
        self.assertEqual(ledger.ticks, [])
        self.assertEqual(ledger.state["totals"]["ok"], 0)
        calls["fail"] = False
        samples = self.run_cycle(ledger, NOON + 110)
        self.assertEqual(samples['epic_ticks_total{agent="codex",outcome="ok"}'], 2)

    def test_oversized_token_count_is_ignored_not_a_crash(self) -> None:
        self.log.write_text(tick(NOON, 0, body="tokens used\n" + "9" * 5000))
        ledger = self.ledger()
        ledger.update(NOON)
        self.assertEqual(self.last(ledger)["tokens"], None)

    def test_ticks_with_the_same_start_get_unique_labels(self) -> None:
        self.log.write_text(tick(NOON, 0) + tick(NOON, 1) + tick(NOON, 0))
        ledger = self.ledger()
        ledger.update(NOON + 10)
        metrics = Metrics()
        ticks.export(metrics, ledger, NOON + 10)
        rows = [
            line.rsplit(" ", 1)[0]
            for line in metrics.lines
            if line.startswith("epic_tick_info{")
        ]
        self.assertEqual(len(rows), 3)
        self.assertEqual(len(set(rows)), 3)
        self.assertIn('tick="2026-09-28T12:00:00Z#2"', "\n".join(rows))

    def test_labels_stay_unique_when_many_ticks_share_a_start(self) -> None:
        self.log.write_text(tick(NOON, 0) * 45)
        ledger = self.ledger()
        ledger.update(NOON + 10)
        metrics = Metrics()
        ticks.export(metrics, ledger, NOON + 10)
        rows = [
            line.rsplit(" ", 1)[0]
            for line in metrics.lines
            if line.startswith("epic_tick_info{")
        ]
        self.assertEqual(len(rows), 20)
        self.assertEqual(len(set(rows)), 20)

    # Derived gauges

    def test_consecutive_failures_skip_blocked_and_reset_on_ok(self) -> None:
        blocked = "backoff: model reported blocked: x"
        self.log.write_text(
            tick(NOON, 1)
            + tick(NOON + 60, 0)
            + tick(NOON + 120, 124)
            + tick(NOON + 180, 75, body=blocked)
            + tick(NOON + 240, 1)
        )
        samples = self.run_cycle(self.ledger(), NOON + 300)
        self.assertEqual(samples['epic_tick_consecutive_failures{agent="codex"}'], 2)

    def test_today_uses_berlin_local_day(self) -> None:
        # 22:30 UTC on Sep 28 is 00:30 on Sep 29 in Berlin.
        late = datetime(2026, 9, 28, 22, 30, tzinfo=timezone.utc).timestamp()
        self.log.write_text(tick(late - 3600, 0) + tick(late, 1))
        samples = self.run_cycle(self.ledger(), late + 60)
        self.assertEqual(samples['epic_ticks_today{agent="codex",outcome="ok"}'], 0)
        self.assertEqual(samples['epic_ticks_today{agent="codex",outcome="error"}'], 1)

    def test_hour_worst_keeps_the_repeated_autumn_hour(self) -> None:
        # 2026-10-25: 00:30 and 01:30 UTC are both 02:30 local.
        first = datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc).timestamp()
        self.log.write_text(tick(first, 0) + tick(first + 3600, 1))
        samples = self.run_cycle(self.ledger(), first + 7200)
        self.assertEqual(
            samples['epic_tick_hour_worst{agent="codex",day="2026-10-25",hour="02"}'], 1
        )
        self.assertEqual(
            samples['epic_tick_hour_worst{agent="codex",day="2026-10-25",hour="02b"}'],
            6,
        )

    def test_hour_worst_skips_the_missing_spring_hour(self) -> None:
        # 2026-03-29: 00:30 UTC is 01:30 local, 01:30 UTC is 03:30 local.
        first = datetime(2026, 3, 29, 0, 30, tzinfo=timezone.utc).timestamp()
        self.log.write_text(tick(first, 0) + tick(first + 3600, 0))
        samples = self.run_cycle(self.ledger(), first + 7200)
        hours = sorted(k for k in samples if "tick_hour_worst" in k)
        self.assertEqual(
            hours,
            [
                'epic_tick_hour_worst{agent="codex",day="2026-03-29",hour="01"}',
                'epic_tick_hour_worst{agent="codex",day="2026-03-29",hour="03"}',
            ],
        )

    def test_hour_worst_is_the_most_severe_outcome(self) -> None:
        self.log.write_text(tick(NOON, 0) + tick(NOON + 60, 143) + tick(NOON + 120, 0))
        samples = self.run_cycle(self.ledger(), NOON + 300)
        self.assertEqual(
            samples['epic_tick_hour_worst{agent="codex",day="2026-09-28",hour="14"}'],
            ticks.SEVERITY["killed"],
        )

    def test_recent_ticks_are_the_last_twenty_with_fixed_labels(self) -> None:
        self.log.write_text("".join(tick(NOON - 3000 + i * 60, 0) for i in range(25)))
        samples = self.run_cycle(self.ledger(), NOON)
        rows = [k for k in samples if k.startswith("epic_tick_info{")]
        self.assertEqual(len(rows), 20)
        self.assertTrue(all(samples[k] == -1 for k in rows))  # history: end unknown

    def test_duration_quantiles_use_only_known_ends(self) -> None:
        ledger = self.ledger()
        self.run_cycle(ledger, NOON)
        self.append(tick(NOON + 10, 0))
        self.run_cycle(ledger, NOON + 70)
        self.append(tick(NOON + 100, 0))
        samples = self.run_cycle(ledger, NOON + 400)
        self.assertEqual(
            samples['epic_tick_duration_today_seconds{agent="codex",quantile="0.5"}'],
            60,
        )
        self.assertEqual(
            samples['epic_tick_duration_today_seconds{agent="codex",quantile="0.95"}'],
            300,
        )

    def test_tokens_today_only_for_runners_that_report_tokens(self) -> None:
        self.log.write_text(tick(NOON, 0))
        samples = self.run_cycle(self.ledger(), NOON)
        self.assertNotIn('epic_tokens_today{agent="codex"}', samples)
        self.log.write_text(tick(NOON, 0, body="tokens used\n2,000"))
        self.checkpoint.unlink()
        samples = self.run_cycle(self.ledger(), NOON)
        self.assertEqual(samples['epic_tokens_today{agent="codex"}'], 2000)

    def test_deeply_nested_action_line_is_ignored(self) -> None:
        # Model output can print a line that looks like the selector's JSON.
        ledger = self.ledger()
        ledger.update(NOON)
        self.append(
            tick(NOON, 0, action='{"action": ' + "[" * 20_000 + "]" * 20_000 + "}")
        )
        ledger.update(NOON + 5)
        self.assertEqual(
            (self.last(ledger)["outcome"], self.last(ledger)["action"]), ("ok", "")
        )

    def test_v4_checkpoint_keeps_its_counters(self) -> None:
        ledger = self.ledger()
        ledger.update(NOON)
        self.append(tick(NOON, 0))
        ledger.update(NOON + 5)
        ledger.save()
        data = json.loads(self.checkpoint.read_text())
        del data["first_start"]
        data["v"] = 4  # as written before events existed
        self.checkpoint.write_text(json.dumps(data))
        again = self.ledger()
        again.update(NOON + 10)
        self.assertEqual((again.state["v"], again.state["totals"]["ok"]), (5, 1))


def event(kind: str, start: float, **fields: object) -> str:
    data: dict[str, object] = {"v": 1, "event": kind, "tick": iso(start)}
    if kind == "finish":
        data.update(
            at=iso(start + 60),
            exit=0,
            outcome="ok",
            phase="record",
            action="review",
            pr=7,
            issue=None,
            tokens=None,
        )
    data.update(fields)
    return json.dumps(data) + "\n"


class EventLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.file = self.root / "codex-ticks.jsonl"
        self.file.write_text("")
        self.log = self.root / "codex.log"
        self.log.write_text("")

    def events(self) -> ticks.EventLedger:
        return ticks.EventLedger(
            "codex",
            self.file,
            self.root / "runtime/events-codex.json",
            monitor.action_labels,
            source="events",
        )

    def append(self, *lines: str) -> None:
        with self.file.open("a") as out:
            out.write("".join(lines))

    def test_finish_gives_real_end_and_runner_outcome(self) -> None:
        ledger = self.events()
        ledger.update(NOON)  # baseline: counters start here
        self.append(
            event("start", NOON),
            event(
                "finish", NOON, exit=75, outcome="blocked", phase="result", tokens=1234
            ),
        )
        ledger.update(NOON + 300)
        [t] = ledger.ticks
        self.assertEqual(
            (t["end"], t["outcome"], t["phase"], t["exit"]),
            (NOON + 60, "blocked", "result", 75),
        )
        self.assertEqual(
            (t["action"], t["target"], t["tokens"]), ("review", f"{URL}/pull/7", 1234)
        )
        self.assertEqual(ledger.state["totals"]["blocked"], 1)
        self.assertEqual(ledger.rejected, 0)
        self.assertTrue(ledger.active)

    def test_invalid_events_are_rejected_and_counted(self) -> None:
        ledger = self.events()
        ledger.update(NOON)
        self.append(
            "not json\n",
            json.dumps({"v": 2, "event": "start", "tick": iso(NOON)}) + "\n",
            event("finish", NOON),  # orphan finish
            event("start", NOON),
            event("finish", NOON + 5),  # other tick
            event("finish", NOON, at=iso(NOON - 1)),  # before its start
            event("finish", NOON, outcome="interrupted"),
            event("finish", NOON, phase="thinking"),
            event("finish", NOON, exit=True),
            event("finish", NOON, tokens=-1),
            event("start", NOON - 60),  # out of order
            event("pause", NOON),
        )
        ledger.update(NOON + 300)
        self.assertEqual(ledger.rejected, 11)
        self.assertEqual(ledger.ticks, [])
        self.assertIsNotNone(ledger.state["open"])  # the valid start stays open

    def test_start_without_finish_is_interrupted(self) -> None:
        ledger = self.events()
        ledger.update(NOON)
        self.append(event("start", NOON), event("start", NOON + 600))
        ledger.update(NOON + 700)
        [t] = ledger.ticks
        self.assertEqual((t["outcome"], t["end"]), ("interrupted", None))
        self.assertEqual(ledger.state["open"]["tick"], iso(NOON + 600))

    def test_history_before_the_baseline_is_not_counted(self) -> None:
        self.append(event("start", NOON), event("finish", NOON))
        ledger = self.events()
        ledger.update(NOON + 300)
        self.assertEqual(len(ledger.ticks), 1)
        self.assertEqual(sum(ledger.state["totals"].values()), 0)

    def test_restart_keeps_rejections_out_of_the_replay(self) -> None:
        ledger = self.events()
        ledger.update(NOON)
        self.append("bad\n", event("start", NOON), event("finish", NOON))
        ledger.update(NOON + 300)
        ledger.save()
        again = self.events()
        again.update(NOON + 600)
        self.assertEqual((len(again.ticks), again.rejected), (1, 0))
        self.assertEqual(again.state["totals"]["ok"], 1)

    def test_merge_counts_each_tick_once_and_marks_the_source(self) -> None:
        log = ticks.LogLedger(
            "codex",
            self.log,
            self.root / "runtime/ticks-codex.json",
            monitor.action_labels,
        )
        events = self.events()
        log.update(NOON - 3600)
        events.update(NOON - 3600)
        with self.log.open("a") as out:
            out.write(tick(NOON - 1800, 1))  # old runner: log only
        log.update(NOON - 1700)
        # New runner: the tick is in the log and in the events.
        with self.log.open("a") as out:
            out.write(tick(NOON, 0))
        self.append(event("start", NOON), event("finish", NOON))
        events.update(NOON + 300)
        log.count_before = events.peek
        log.update(NOON + 300)
        view = ticks.merge(log, events)
        self.assertEqual(
            [(t["outcome"], t.get("source", "log")) for t in view.state["ticks"]],
            [("error", "log"), ("ok", "events")],
        )
        self.assertEqual(view.state["totals"]["ok"], 1)
        self.assertEqual(view.state["totals"]["error"], 1)
        metrics = Metrics()
        ticks.export(metrics, view, NOON + 300)
        samples = metrics.samples()
        self.assertEqual(samples['epic_ticks_total{agent="codex",outcome="ok"}'], 1)
        self.assertTrue(any('source="events"' in key for key in samples))

    def test_merge_without_events_is_the_log(self) -> None:
        log = ticks.LogLedger(
            "codex", self.log, self.root / "runtime/t.json", monitor.action_labels
        )
        log.update(NOON)
        self.assertIs(ticks.merge(log, self.events()).state, log.state)

    # Codex review of https://github.com/phaabe/live.moafunk.de/pull/469
    def test_deep_nesting_and_oversized_lines_are_rejected(self) -> None:
        ledger = self.events()
        ledger.update(NOON)
        self.append("[" * 2000 + "]" * 2000 + "\n", "[" * 40_000 + "\n")
        ledger.update(NOON + 5)
        self.assertEqual(ledger.rejected, 2)

    def test_events_file_appearing_between_polls_counts_in_full(self) -> None:
        self.file.unlink()
        log = ticks.LogLedger(
            "codex",
            self.log,
            self.root / "runtime/ticks-codex.json",
            monitor.action_labels,
        )
        events = self.events()
        log.update(NOON - 3600)
        with self.assertRaises(FileNotFoundError):
            events.update(NOON - 3600)
        # An old-runner tick finishes after the new runner's first tick.
        with self.log.open("a") as out:
            out.write(tick(NOON - 1800, None) + tick(NOON, 0))
        self.file.write_text("")
        self.append(event("start", NOON), event("finish", NOON))
        events.update(NOON + 300)
        log.count_before = events.peek
        log.update(NOON + 300)
        view = ticks.merge(log, events)
        self.assertEqual(view.state["totals"]["ok"], 1)  # from the events
        self.assertEqual(view.state["totals"]["interrupted"], 1)  # the old tick

    def log_ledger(self) -> ticks.LogLedger:
        return ticks.LogLedger(
            "codex",
            self.log,
            self.root / "runtime/ticks-codex.json",
            monitor.action_labels,
        )

    def run_tick(self, start: float) -> None:
        """One whole tick in the runner's order: start event, log, finish."""
        self.append(event("start", start))
        with self.log.open("a") as out:
            out.write(tick(start, 0))
        self.append(event("finish", start))

    def poll(
        self,
        now: float,
        *,
        between: Callable[[], None] | None = None,
        ledgers: tuple[ticks.LogLedger, ticks.EventLedger] | None = None,
    ) -> ticks.TickView:
        """One collector cycle; without `ledgers` as after a restart.

        `between` runs after the events read, before the log read.
        """
        log, events = ledgers or (self.log_ledger(), self.events())
        if between is not None:
            read = events.update

            def race(at: float) -> None:
                del events.update  # once: kept ledgers read normally later
                try:
                    read(at)
                finally:
                    between()

            events.update = race  # type: ignore[method-assign]
        return monitor.ledger_metrics(monitor.Metrics(), log, events, now)

    def test_first_tick_between_polls_is_counted_once(self) -> None:
        # Codex review rounds 2 and 3: the log counted the tick before the
        # events were read, then the events counted it again, also after a
        # restart between the two polls; with or without an events file.
        for missing in (False, True):
            with self.subTest(missing=missing):
                self.setUp()
                if missing:
                    self.file.unlink()
                self.poll(NOON - 60)
                self.poll(NOON + 5, between=lambda: self.run_tick(NOON))
                view = self.poll(NOON + 10)
                self.assertEqual(view.state["totals"]["ok"], 1)
                self.assertEqual(len(view.state["ticks"]), 1)

    def test_many_ticks_between_polls_are_each_counted_once(self) -> None:
        # Codex review round 3: a fixed list of counted ticks overflowed.
        def many() -> None:
            for i in range(250):
                self.run_tick(NOON - 250 * 60 + i * 60)

        self.poll(NOON - 250 * 60 - 60)
        self.poll(NOON + 5, between=many)
        view = self.poll(NOON + 10)
        self.assertEqual(view.state["totals"]["ok"], 250)

    def test_log_rotation_between_reads_loses_no_count(self) -> None:
        # Codex review round 4: the log never saw tick A, so it must not
        # decide about the events' ticks.
        def rotate() -> None:
            self.run_tick(NOON)
            self.log.rename(self.root / "codex.log.1")
            self.log.write_text("")
            self.run_tick(NOON + 120)

        self.poll(NOON - 60)
        self.poll(NOON + 200, between=rotate)
        view = self.poll(NOON + 300)
        self.assertEqual(view.state["totals"]["ok"], 2)

    def test_failed_log_save_then_restart_loses_no_count(self) -> None:
        # Codex review round 4: the events must not rely on log state that
        # was never saved.
        self.poll(NOON - 60)
        running = (self.log_ledger(), self.events())
        with patch.object(running[0], "save", side_effect=OSError("disk")):
            self.poll(NOON + 5, between=lambda: self.run_tick(NOON), ledgers=running)
            self.poll(NOON + 10, ledgers=running)
        view = self.poll(NOON + 15)
        self.assertEqual(view.state["totals"]["ok"], 1)

    def test_deploy_after_the_old_collector_counted_a_tick(self) -> None:
        # The old collector (v4, no event reader) counted the runner's first
        # event tick from the log; the new one has no event checkpoint yet.
        old = self.log_ledger()
        old.update(NOON - 60)
        self.run_tick(NOON)
        old.update(NOON + 5)
        old.save()
        path = self.root / "runtime/ticks-codex.json"
        data = json.loads(path.read_text())
        del data["first_start"]
        path.write_text(json.dumps(data | {"v": 4}))
        view = self.poll(NOON + 10)
        self.assertEqual(view.state["totals"]["ok"], 1)

    def test_first_start_survives_trimming_and_restarts(self) -> None:
        ledger = self.events()
        ledger.update(NOON)
        for i in range(3):
            self.append(event("start", NOON + i * 60), event("finish", NOON + i * 60))
        with patch.object(ticks, "LEDGER_SIZE", 2):
            ledger.update(NOON + 300)
        ledger.save()
        self.assertEqual(len(ledger.state["ticks"]), 2)
        again = self.events()
        again.update(NOON + 600)
        self.assertEqual(again.first_start(), NOON)

    def test_restart_while_the_events_file_is_missing_keeps_its_state(self) -> None:
        events = self.events()
        events.update(NOON)
        self.append(event("start", NOON), event("finish", NOON))
        events.update(NOON + 300)
        events.save()
        self.file.unlink()
        again = self.events()
        with self.assertRaises(FileNotFoundError):
            again.update(NOON + 600)
        self.assertTrue(again.active)
        self.assertEqual(again.state["totals"]["ok"], 1)


class DecisionLedgerTest(unittest.TestCase):
    def test_counts_survive_a_restart_and_foreign_checkpoints_are_rebuilt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "claude-permissions.log"
            log.write_text("")
            checkpoint = root / "permissions-claude.json"

            def ledger() -> ticks.DecisionLedger:
                return ticks.DecisionLedger(
                    "claude", log, checkpoint, monitor.action_labels
                )

            first = ledger()
            first.update(NOON)
            log.write_text("2026-09-28T11:00:00Z deny 'x' (no)\n")
            first.update(NOON + 5)
            first.save()
            again = ledger()
            again.update(NOON + 10)
            self.assertEqual(again.state["totals"], {"allow": 0, "deny": 1})
            # A tick checkpoint has other totals: rebuilt, not trusted.
            tick_ledger = ticks.LogLedger(
                "claude", log, checkpoint, monitor.action_labels
            )
            with self.assertLogs(level="WARNING"):
                tick_ledger.update(NOON + 15)


if __name__ == "__main__":
    unittest.main()
