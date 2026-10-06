"""Fixture tool tests. Run: python3 -m unittest discover -s scripts/epic."""

from __future__ import annotations

import isolated_env  # noqa: F401  (first: hides live runner state)

from pathlib import Path
import re
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import fixtures
import monitor
from test_dashboards import docker_ready


class FixturesTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def presence(self, scenario: str) -> dict[str, str]:
        specs, paused = fixtures.scenario(scenario)
        state = self.root / scenario
        now = time.time()
        fixtures.build_state(state, specs, now)
        text = monitor.runner_metrics(
            state, paused, now, ledgers=monitor.Ledgers(self.root / f"rt-{scenario}")
        )
        self.text = text
        return dict(
            re.findall(
                r'epic_agent_presence_info\{agent="([^"]+)",presence="(\w+)"', text
            )
        )

    def test_scenarios_show_what_they_are_named_for(self) -> None:
        self.assertEqual(self.presence("single"), {"claude": "running"})
        self.assertEqual(self.presence("late")["codex"], "late")
        self.assertEqual(self.presence("retired")["codex-old"], "retired")
        self.assertEqual(len(self.presence("full")), 12)
        self.presence("collision")
        self.assertEqual(self.text.count("epic_agent_collision{"), 2)
        self.presence("failing")
        self.assertIn('epic_tick_consecutive_failures{agent="codex"} 4', self.text)
        # The design's "normal" frame: 4 running, 3 idle, 1 new.
        self.assertEqual(
            sorted(self.presence("normal").items()),
            sorted(
                {
                    "claude": "running",
                    "claude-2": "running",
                    "codex": "running",
                    "codex-review": "running",
                    "claude-docs": "idle",
                    "codex-2": "idle",
                    "codex-ops": "idle",
                    "claude-3": "new",
                }.items()
            ),
        )
        self.assertIn('epic_backoff_info{agent="codex-ops"', self.text)

    def test_never_deletes_a_dir_it_did_not_make(self) -> None:
        foreign = self.root / "real"
        foreign.mkdir()
        (foreign / "claude.log").write_text("keep me")
        with self.assertRaises(SystemExit):
            fixtures.build_state(foreign, [], time.time())
        self.assertEqual((foreign / "claude.log").read_text(), "keep me")

    def test_refuses_the_runners_default_state_dir(self) -> None:
        home = self.root / "home"
        default = home / ".local/state/epic-loop"
        default.mkdir(parents=True)
        (default / ".fixture").write_text("")  # even when it looks like one
        with patch.object(Path, "home", return_value=home):
            with self.assertRaises(SystemExit):
                fixtures.build_state(default, [], time.time())
        self.assertTrue((default / ".fixture").exists())

    def test_rebuild_keeps_the_dir_for_bind_mounts(self) -> None:
        state = self.root / "state"
        specs, _ = fixtures.scenario("normal")
        fixtures.build_state(state, specs, time.time())
        inode = state.stat().st_ino
        fixtures.build_state(state, fixtures.scenario("single")[0], time.time())
        self.assertEqual(state.stat().st_ino, inode)
        self.assertEqual([p.name for p in (state / "agents").iterdir()], ["claude"])

    def test_a_long_preview_stays_normal(self) -> None:
        """Idle agents tick when due and running ticks end before the
        budget, so after 2 h nobody is late or over budget."""
        specs, _ = fixtures.scenario("normal")
        state = self.root / "state"
        start = time.time()
        fixtures.build_state(state, specs, start)
        starts = fixtures.last_starts(specs, start)
        for minute in range(0, 121, 1):
            fixtures.advance(state, specs, starts, start + 60 * minute)
        later = start + 7200
        text = monitor.runner_metrics(
            state,
            False,
            later,
            ledgers=monitor.Ledgers(self.root / "rt"),
            alive=lambda pid: True,
        )
        presence = dict(
            re.findall(
                r'epic_agent_presence_info\{agent="([^"]+)",presence="(\w+)"', text
            )
        )
        self.assertNotIn("late", presence.values())
        self.assertEqual(presence["codex"], "running")
        # The new agent had its first tick.
        self.assertEqual(presence["claude-3"], "idle")
        for agent, value in re.findall(
            r'epic_tick_elapsed_seconds\{agent="([^"]+)"\} (\S+)', text
        ):
            with self.subTest(agent=agent):
                self.assertLess(float(value), 1800)

    def test_late_and_failing_agents_do_not_recover(self) -> None:
        for name, agent in (("late", "codex"), ("failing", "codex")):
            specs, _ = fixtures.scenario(name)
            [spec] = [s for s in specs if s.id == agent]
            self.assertFalse(spec.live)

    def test_normal_events_are_all_accepted(self) -> None:
        """Codex review round 3: running ticks started before the finished
        ones, so the ledger rejected them."""
        specs, _ = fixtures.scenario("normal")
        state = self.root / "state"
        start = time.time()
        fixtures.build_state(state, specs, start)
        starts = fixtures.last_starts(specs, start)
        ledgers = monitor.Ledgers(self.root / "rt")
        text = ""
        for minute in range(0, 31, 5):
            now = start + 60 * minute
            fixtures.advance(state, specs, starts, now)
            text = monitor.runner_metrics(
                state, False, now, ledgers=ledgers, alive=lambda pid: True
            )
        rejected = re.findall(r"epic_tick_events_rejected_total\{[^}]*\} (\S+)", text)
        self.assertEqual(len(rejected), len(specs))
        self.assertEqual({float(v) for v in rejected}, {0.0})

    def test_a_paused_preview_starts_no_ticks(self) -> None:
        """Codex review round 3."""
        runtime = self.root / "runtime-preview"
        state = self.root / "state"
        start = time.time()
        clock = iter([start, *[start + 3600] * 1000])
        with patch.object(fixtures.time, "time", lambda: next(clock)):
            fixtures.main(
                [
                    "paused",
                    "--state-dir",
                    str(state),
                    "--output",
                    str(runtime / "metrics"),
                    "--once",
                ]
            )
        for events in state.glob("agents/*/*-ticks.jsonl"):
            with self.subTest(agent=events.parent.name):
                # build_state and advance() write different JSON spacing.
                starts = len(re.findall(r'"event":\s*"start"', events.read_text()))
                self.assertEqual(starts, 20)


class HistoryTest(unittest.TestCase):
    def test_history_covers_24_h_per_ticking_agent(self) -> None:
        specs, _ = fixtures.scenario("normal")
        now = 1_790_000_000.0
        text = fixtures.history(specs, now)
        self.assertTrue(text.endswith("# EOF\n"))
        agents_seen = set(re.findall(r'epic_tick_last_outcome\{agent="([^"]+)"', text))
        # claude-3 is new: no ticks yet.
        self.assertEqual(agents_seen, {s.id for s in specs} - {"claude-3"})
        times = [
            int(line.rsplit(" ", 1)[1])
            for line in text.splitlines()
            if line.startswith("epic_local")
        ]
        self.assertEqual(times[-1] - times[0], 24 * 3600)
        self.assertLess(times[-1], now)

    def test_promtool_accepts_the_history(self) -> None:
        if not docker_ready():
            self.skipTest("needs a reachable docker daemon for promtool")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.om"
            path.write_text(
                fixtures.history(fixtures.scenario("single")[0], time.time(), hours=3)
            )
            result = subprocess.run(
                ["docker", "run", "--rm", "-v", f"{directory}:/w", "-w", "/w"]
                + ["--entrypoint", "promtool", "prom/prometheus:v3.15.0"]
                + ["tsdb", "create-blocks-from", "openmetrics", "history.om", "out"],
                capture_output=True,
                text=True,
                timeout=120,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class TicketTimesTest(unittest.TestCase):
    def test_time_columns_have_every_kind_of_value(self) -> None:
        """Exact, lower bound and gap ages, an unverified Claude agent and a
        Ready time, so the preview shows every Tickets column."""
        now = 1_790_000_000.0
        text = fixtures.ticket_metrics(now)
        exact = dict(
            re.findall(
                r'^epic_ticket_status_entered_seconds\{exact="(\w+)",issue="(\d+)"\}',
                text,
                re.M,
            )
        )
        self.assertEqual(set(exact), {"1", "0", "gap"})
        self.assertEqual((exact["0"], exact["gap"]), ("305", "308"))
        self.assertIn('last_agent="Claude (unverified)"', text)
        ready = re.findall(r'ready_entered="(\d+)"', text)
        self.assertTrue(ready)
        self.assertTrue(all(int(t) < now for t in ready))
        # The time checks read the history: the ledger is not unknown.
        self.assertIn('epic_ticket_check_count{check="in_review_long"} 1', text)


class DockerSkipTest(unittest.TestCase):
    """The promtool test skips when the daemon is unreachable (sandboxes)."""

    def test_promtool_test_skips_without_a_reachable_daemon(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.sock"
            with patch.dict("os.environ", {"DOCKER_HOST": f"unix://{missing}"}):
                self.assertFalse(docker_ready())
                result = unittest.TestResult()
                HistoryTest("test_promtool_accepts_the_history").run(result)
        self.assertEqual(result.errors + result.failures, [])
        self.assertEqual(len(result.skipped), 1)


class PreviewRuntimeTest(unittest.TestCase):
    """Codex review of https://github.com/phaabe/live.moafunk.de/pull/479."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_a_stopped_collectors_runtime_keeps_its_checkpoints(self) -> None:
        runtime = self.root / "runtime"
        (runtime / "metrics").mkdir(parents=True)
        (runtime / "ticks-claude.json").write_text("{}")
        (runtime / "metrics" / "runners.prom").write_text("real\n")
        with self.assertRaises(SystemExit):
            fixtures.prepare_runtime(runtime)
        self.assertEqual((runtime / "ticks-claude.json").read_text(), "{}")
        self.assertEqual((runtime / "metrics" / "runners.prom").read_text(), "real\n")

    def test_the_collectors_runtime_path_is_refused(self) -> None:
        runtime = self.root / "tools/agent-monitoring/runtime"
        with patch.object(fixtures, "REAL_RUNTIME", runtime):
            with self.assertRaises(SystemExit):
                fixtures.prepare_runtime(runtime)
        self.assertFalse(runtime.exists())

    def test_a_symlinked_metrics_dir_is_refused(self) -> None:
        """Codex review round 3: a marked preview dir whose metrics dir
        links to the real stack's metrics."""
        real = self.root / "runtime" / "metrics"
        real.mkdir(parents=True)
        (real / "runners.prom").write_text("real\n")
        runtime = self.root / "runtime-preview"
        runtime.mkdir()
        (runtime / ".preview").write_text("")
        (runtime / "metrics").symlink_to(real)
        with self.assertRaises(SystemExit):
            fixtures.prepare_runtime(runtime)
        self.assertEqual((real / "runners.prom").read_text(), "real\n")

    def test_output_must_be_a_metrics_dir(self) -> None:
        with self.assertRaises(SystemExit):
            fixtures.main(
                [
                    "normal",
                    "--state-dir",
                    str(self.root / "s"),
                    "--output",
                    str(self.root / "out"),
                    "--once",
                ]
            )
        self.assertFalse((self.root / "s").exists())

    def test_a_preview_runtime_is_cleaned_and_locked(self) -> None:
        runtime = self.root / "runtime-preview"
        lock = fixtures.prepare_runtime(runtime)
        self.addCleanup(lock.close)
        (runtime / "ticks-claude.json").write_text("{}")
        with self.assertRaises(SystemExit):
            fixtures.prepare_runtime(runtime)  # the lock is held
        lock.close()
        fixtures.prepare_runtime(runtime).close()
        self.assertFalse((runtime / "ticks-claude.json").exists())


if __name__ == "__main__":
    unittest.main()
