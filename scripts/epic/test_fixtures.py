"""Fixture tool tests. Run: python3 -m unittest discover -s scripts/epic."""

from __future__ import annotations

from pathlib import Path
import re
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
        self.presence("normal")
        self.assertIn('epic_backoff_info{agent="codex"', self.text)

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


if __name__ == "__main__":
    unittest.main()
