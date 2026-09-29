"""Fixture tool tests. Run: python3 -m unittest discover -s scripts/epic."""

from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import fixtures
import monitor


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

    @unittest.skipUnless(shutil.which("docker"), "needs docker for promtool")
    def test_promtool_accepts_the_history(self) -> None:
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
