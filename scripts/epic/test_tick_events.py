"""Tick event writer tests. Run: python3 -m unittest discover -s scripts/epic."""

from __future__ import annotations

import isolated_env  # noqa: F401  (first: hides live runner state)

import contextlib
import io
import json
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import tick_events

TICK = "2026-09-28T15:39:39Z"
FIXTURE = Path(__file__).resolve().parent / "fixtures/claude-transcript.jsonl"
# The sanitized runner transcript: 9 model records of 3 messages.
FIXTURE_USAGE = {
    "input": 6,
    "output": 943,
    "cache_read": 109053,
    "cache_write": 38698,
}
SESSION = "00000000-0000-4000-8000-000000000533"
OTHER = "11111111-1111-4111-8111-111111111111"
LAUNCH = Path("/runner/live.moafunk.de-claude-runner")


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


def record(msg: str | None, output: int, *, tool: str = "Bash", **usage: object) -> str:
    """One model record of a transcript; usage keys override the defaults."""
    counts: dict[str, object] = {
        "input_tokens": 1,
        "output_tokens": output,
        "cache_read_input_tokens": 100,
        "cache_creation_input_tokens": 10,
    }
    counts.update(usage)
    message: dict[str, object] = {
        "model": "claude-opus-5-5",
        "content": [{"type": "tool_use", "id": f"toolu_{msg}_{tool}", "name": tool}],
        "usage": {k: v for k, v in counts.items() if v is not None},
    }
    if msg is not None:
        message["id"] = msg
    return json.dumps({"type": "assistant", "message": message}) + "\n"


class ClaudeUsageTest(unittest.TestCase):
    """Usage from the session transcript, per message, never guessed."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = self.root / "claude-config"
        patcher = patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.config)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.folder = self.config / "projects/-runner-live-moafunk-de-claude-runner"
        self.folder.mkdir(parents=True)

    def transcript(self, text: str, session: str = SESSION) -> Path:
        path = self.folder / f"{session}.jsonl"
        path.write_text(text)
        return path

    def usage(self, model_exit: int | None = 0, session: str = SESSION) -> dict:
        return tick_events.claude_fields(session, LAUNCH, model_exit)["usage"]

    def test_fixture_counts_each_message_once(self) -> None:
        self.transcript(FIXTURE.read_text())
        # Not the cost-state record's session totals, and no total field.
        self.assertEqual(
            self.usage(), {**FIXTURE_USAGE, "complete": True, "reason": None}
        )

    def test_cumulative_records_keep_the_largest_value(self) -> None:
        self.transcript(
            record("m1", 10)
            + record("m1", 50, cache_read_input_tokens=300)
            + record("m1", 30)
            + record("m2", 5)
        )
        usage = self.usage()
        self.assertEqual(
            [usage[k] for k in ("input", "output", "cache_read", "cache_write")],
            [2, 55, 400, 20],
        )
        self.assertTrue(usage["complete"])

    def test_record_without_message_id_uses_the_request_id(self) -> None:
        line = json.loads(record(None, 7))
        line["requestId"] = "req_1"
        self.transcript(json.dumps(line) + "\n" + json.dumps(line) + "\n")
        self.assertEqual(self.usage()["output"], 7)

    def test_missing_data_is_null_with_a_reason_never_zero(self) -> None:
        self.assertEqual(
            self.usage(),
            {
                "input": None,
                "output": None,
                "cache_read": None,
                "cache_write": None,
                "complete": False,
                "reason": "no-transcript",
            },
        )
        self.transcript('{"type":"user","message":{"content":"x"}}\n')
        self.assertEqual(
            (self.usage()["output"], self.usage()["reason"]), (None, "no-usage")
        )
        self.transcript('{"type":"assistant","message":{"id":"m1","usage":{}}}\n')
        self.assertEqual(
            (self.usage()["output"], self.usage()["reason"]), (None, "malformed")
        )

    def test_bad_records_make_usage_partial(self) -> None:
        # The bad record's readable counters still count; the bad one does not.
        for bad, output, cache_read in (
            (record("m2", 5, cache_read_input_tokens=None), 15, 100),  # missing
            (record("m2", 5, output_tokens=-1), 10, 200),
            (record("m2", 5, output_tokens=True), 10, 200),
            ('{"type":"assistant","message":{"id":"m2","usa\n', 10, 100),  # cut off
        ):
            with self.subTest(bad=bad):
                self.transcript(record("m1", 10) + bad)
                usage = self.usage()
                self.assertEqual(
                    (
                        usage["output"],
                        usage["cache_read"],
                        usage["complete"],
                        usage["reason"],
                    ),
                    (output, cache_read, False, "malformed"),
                )

    def test_a_missing_counter_keeps_the_others(self) -> None:
        # Codex review of https://github.com/phaabe/live.moafunk.de/pull/558.
        self.transcript(record("m1", 10, cache_creation_input_tokens=None))
        self.assertEqual(
            self.usage(),
            {
                "input": 1,
                "output": 10,
                "cache_read": 100,
                "cache_write": None,
                "complete": False,
                "reason": "malformed",
            },
        )
        # A later record of the same message can fill the gap; still partial.
        self.transcript(
            record("m1", 10, cache_creation_input_tokens=None) + record("m1", 10)
        )
        usage = self.usage()
        self.assertEqual((usage["cache_write"], usage["complete"]), (10, False))

    def test_synthetic_error_note_is_not_a_model_call(self) -> None:
        note = json.loads(record("m2", 0))
        note["message"]["model"] = "<synthetic>"
        del note["message"]["usage"]
        self.transcript(record("m1", 10) + json.dumps(note) + "\n")
        self.assertEqual((self.usage()["output"], self.usage()["complete"]), (10, True))

    def test_stopped_sessions_keep_their_counts_as_partial(self) -> None:
        self.transcript(record("m1", 10))
        for code in (None, 124, 129, 130, 137, 143):
            with self.subTest(code=code):
                usage = self.usage(code)
                self.assertEqual(
                    (usage["output"], usage["complete"], usage["reason"]),
                    (10, False, "interrupted"),
                )
        # A nonzero exit of a finished session keeps full coverage.
        for code in (0, 1, 75):
            self.assertTrue(self.usage(code)["complete"])

    def test_subagent_usage_is_added_or_marked_missing(self) -> None:
        self.transcript(record("m1", 10, tool="Agent"))
        usage = self.usage()
        self.assertEqual(
            (usage["output"], usage["complete"], usage["reason"]),
            (10, False, "subagents"),
        )
        children = self.folder / SESSION / "subagents"
        children.mkdir(parents=True)
        # The child repeats the parent's message ID once: counted once.
        (children / "agent-a1.jsonl").write_text(record("c1", 4) + record("m1", 10))
        usage = self.usage()
        self.assertEqual(
            (usage["output"], usage["input"], usage["complete"]), (14, 2, True)
        )

    def test_child_transcript_without_usage_is_not_coverage(self) -> None:
        # Codex review of https://github.com/phaabe/live.moafunk.de/pull/558.
        self.transcript(record("m1", 10, tool="Agent"))
        children = self.folder / SESSION / "subagents"
        children.mkdir(parents=True)
        for text in ("", '{"type":"user","message":{"content":"x"}}\n'):
            with self.subTest(text=text):
                (children / "agent-a1.jsonl").write_text(text)
                usage = self.usage()
                self.assertEqual(
                    (usage["output"], usage["complete"], usage["reason"]),
                    (10, False, "subagents"),
                )
        # A second child with usage does not cover the empty one.
        (children / "agent-a2.jsonl").write_text(record("c2", 4))
        self.assertEqual(self.usage()["reason"], "subagents")

    def test_transcript_found_by_session_id_in_a_shortened_folder(self) -> None:
        other = self.config / "projects/-runner-live-moafunk-de-cla-1a2b3c"
        other.mkdir()
        (other / f"{SESSION}.jsonl").write_text(record("m1", 10))
        self.assertEqual(self.usage()["output"], 10)
        # Another session's transcript (the other agent) is never used.
        self.assertEqual(self.usage(session=OTHER)["reason"], "no-transcript")

    def test_limits_and_unreadable_files(self) -> None:
        self.transcript(record("m1", 10) + record("m2", 20))
        with patch.object(tick_events, "MAX_TRANSCRIPT", len(record("m1", 10))):
            usage = self.usage()
        self.assertEqual((usage["output"], usage["reason"]), (10, "too-large"))
        with patch.object(tick_events, "MAX_TRANSCRIPT", 5):
            self.assertEqual(self.usage()["reason"], "too-large")
        path = self.folder / f"{SESSION}.jsonl"
        path.unlink()
        target = self.root / "elsewhere.jsonl"
        target.write_text(record("m1", 10))
        path.symlink_to(target)
        self.assertEqual(self.usage()["reason"], "unreadable")

    def test_no_model_bad_session_and_reader_errors(self) -> None:
        self.assertEqual(
            tick_events.claude_fields("", LAUNCH, None),
            {"session_id": None, "usage": None},
        )
        fields = tick_events.claude_fields("../../etc/passwd", LAUNCH, 0)
        self.assertEqual(
            (fields["session_id"], fields["usage"]["reason"]), (None, "no-transcript")
        )
        with patch.object(tick_events, "usage_of", side_effect=KeyError("bug")):
            fields = tick_events.claude_fields(SESSION, LAUNCH, 0)
        self.assertEqual(
            (fields["session_id"], fields["usage"]["reason"]), (SESSION, "unreadable")
        )

    def test_finish_event_keeps_tokens_for_codex_and_adds_usage(self) -> None:
        self.transcript(FIXTURE.read_text())
        events = self.root / "claude-ticks.jsonl"
        base = ("finish", "--file", str(events), "--tick", TICK, "--exit", "0")
        with contextlib.redirect_stdout(io.StringIO()):
            tick_events.main([*base, "--phase", "record"])
            tick_events.main([*base, "--phase", "record", "--claude-session", ""])
            tick_events.main(
                [*base, "--phase", "record", "--claude-session", SESSION]
                + ["--launch-dir", str(LAUNCH), "--model-exit", "0"]
            )
        codex, idle, claude = [json.loads(x) for x in events.read_text().splitlines()]
        self.assertNotIn("usage", codex)
        self.assertEqual((idle["session_id"], idle["usage"]), (None, None))
        self.assertEqual(
            (claude["tokens"], claude["session_id"], claude["usage"]["output"]),
            (None, SESSION, 943),
        )

    def test_two_runner_instances_read_their_own_sessions(self) -> None:
        self.transcript(FIXTURE.read_text())
        self.transcript(record("m1", 10), session=OTHER)
        runs = []
        for name, session in (("claude", SESSION), ("claude-2", OTHER)):
            events = self.root / name / "claude-ticks.jsonl"
            events.parent.mkdir()
            command = [sys.executable, str(Path(tick_events.__file__)), "finish"]
            command += ["--file", str(events), "--tick", TICK, "--exit", "0"]
            command += ["--phase", "record", "--claude-session", session]
            command += ["--launch-dir", str(LAUNCH), "--model-exit", "0"]
            runs.append((events, subprocess.Popen(command, env=dict(os.environ))))
        outputs = []
        for events, run in runs:
            self.assertEqual(run.wait(timeout=15), 0)
            outputs.append(json.loads(events.read_text())["usage"]["output"])
        self.assertEqual(outputs, [943, 10])


if __name__ == "__main__":
    unittest.main()


class HelperNeverBlocksTest(unittest.TestCase):
    """Codex review of https://github.com/phaabe/live.moafunk.de/pull/469."""

    def run_helper(self, *args: str) -> subprocess.CompletedProcess[str]:
        # Run as a process: a hang would fail the timeout, not freeze the suite.
        return subprocess.run(
            [sys.executable, str(Path(tick_events.__file__)), *args],
            capture_output=True,
            text=True,
            timeout=15,
        )

    def test_fifo_files_fail_fast_instead_of_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            events, action, log = root / "e.jsonl", root / "action.json", root / "l.log"
            for fifo in (events, action, log):
                os.mkfifo(fifo)
            result = self.run_helper(
                "start", "--file", str(events), "--tick", TICK, "--log", str(log)
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("event write failed", result.stderr)
            regular = root / "ok.jsonl"
            result = self.run_helper(
                "finish",
                "--file",
                str(regular),
                "--tick",
                TICK,
                "--exit",
                "0",
                "--phase",
                "model",
                "--action-file",
                str(action),
                "--log",
                str(log),
                "--since",
                "0",
            )
            self.assertEqual(result.returncode, 0)
            finish = json.loads(regular.read_text())
            self.assertEqual((finish["action"], finish["tokens"]), (None, None))

    def test_linked_events_file_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "elsewhere.txt"
            target.write_text("")
            (root / "e.jsonl").symlink_to(target)
            result = self.run_helper(
                "start", "--file", str(root / "e.jsonl"), "--tick", TICK
            )
            self.assertEqual(result.returncode, 1)
            self.assertEqual(target.read_text(), "")
