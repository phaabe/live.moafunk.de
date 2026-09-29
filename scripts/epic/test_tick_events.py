"""Tick event writer tests. Run: python3 -m unittest discover -s scripts/epic."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

import tick_events

TICK = "2026-09-28T15:39:39Z"


class TickEventsTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.file = self.root / "claude-ticks.jsonl"
        self.log = self.root / "claude.log"

    def events(self) -> list[dict[str, object]]:
        return [json.loads(line) for line in self.file.read_text().splitlines()]

    def run_main(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = tick_events.main(list(args))
        return code, out.getvalue()

    def test_start_prints_the_log_offset(self) -> None:
        self.log.write_text("old output\n")
        code, out = self.run_main(
            "start", "--file", str(self.file), "--tick", TICK, "--log", str(self.log)
        )
        self.assertEqual((code, out.strip()), (0, "11"))
        [event] = self.events()
        self.assertEqual(
            (event["v"], event["event"], event["tick"]), (1, "start", TICK)
        )
        self.assertIsInstance(event["pid"], int)

    def test_finish_names_outcome_and_keeps_only_safe_action_fields(self) -> None:
        action = self.root / "action.json"
        action.write_text(
            json.dumps(
                {
                    "action": "review",
                    "pr": 436,
                    "issue": "https://github.com/o/r/issues/7",
                    "reason": "SECRET prompt text",
                }
            )
        )
        self.log.write_text("before\ntokens used\n9\n")
        offset = len("before\ntokens used\n9\n")
        with self.log.open("a") as out:
            out.write("model text\ntokens used\n89,040\nmore\n")
        code, _ = self.run_main(
            *("finish", "--file", str(self.file), "--tick", TICK, "--exit", "0"),
            *("--phase", "record", "--action-file", str(action)),
            *("--log", str(self.log), "--since", str(offset)),
        )
        self.assertEqual(code, 0)
        [event] = self.events()
        self.assertEqual(
            {
                k: event[k]
                for k in ("exit", "outcome", "phase", "action", "pr", "issue")
            },
            {
                "exit": 0,
                "outcome": "ok",
                "phase": "record",
                "action": "review",
                "pr": 436,
                "issue": "https://github.com/o/r/issues/7",
            },
        )
        self.assertEqual(event["tokens"], 89040)
        self.assertNotIn("SECRET", self.file.read_text())

    def test_tokens_before_the_tick_are_not_used(self) -> None:
        self.log.write_text("tokens used\n500\n")
        self.assertIsNone(tick_events.tokens_since(self.log, self.log.stat().st_size))
        self.assertIsNone(tick_events.tokens_since(None, 0))

    def test_outcome_from_exit_code_unless_named(self) -> None:
        for code, expected in (
            (0, "ok"),
            (1, "error"),
            (75, "error"),
            (124, "timeout"),
            (130, "killed"),
            (143, "killed"),
        ):
            self.assertEqual(tick_events.outcome_of(code), expected)
        self.run_main(
            *("finish", "--file", str(self.file), "--tick", TICK, "--exit", "75"),
            *("--phase", "result", "--outcome", "blocked"),
        )
        self.assertEqual(self.events()[0]["outcome"], "blocked")

    def test_bad_action_values_are_dropped(self) -> None:
        action = self.root / "action.json"
        for data in (
            {"action": "Review; rm -rf", "pr": True, "issue": "https://evil/issues/1"},
            {"action": "x" * 40, "pr": -1},
            ["not", "an", "object"],
        ):
            action.write_text(json.dumps(data))
            self.assertEqual(
                tick_events.action_fields(action),
                {"action": None, "pr": None, "issue": None},
            )
        self.assertEqual(
            tick_events.action_fields(self.root / "missing.json")["action"], None
        )

    def test_invalid_tick_or_phase_is_refused(self) -> None:
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            tick_events.main(["start", "--file", str(self.file), "--tick", "now"])
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            tick_events.main(
                [
                    "finish",
                    "--file",
                    str(self.file),
                    "--tick",
                    TICK,
                    "--exit",
                    "0",
                    "--phase",
                    "thinking",
                ]
            )
        self.assertFalse(self.file.exists())

    def test_unwritable_file_reports_failure(self) -> None:
        code, _ = self.run_main(
            "start", "--file", str(self.root / "missing/dir/x.jsonl"), "--tick", TICK
        )
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
