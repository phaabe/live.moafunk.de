"""Blocked-target retry policy and its command-line protocol."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("tick_backoff", ROOT / "tick_backoff.py")
assert SPEC and SPEC.loader
backoff = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backoff)


class BackoffTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="tick-backoff-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "state"
        self.action = {
            "action": "claim",
            "issue": "https://github.com/phaabe/live.moafunk.de/issues/338",
        }
        self.result = self.root / "result.json"
        self.result.write_text(json.dumps({"status": "blocked", "summary": "Waiting"}))

    def record(self, action: dict[str, object] | None = None, code: int = 0) -> int:
        return backoff.record(
            action or self.action, self.state, self.result, code, 1000
        )

    def test_claim_block_suppresses_continue_until_expiry(self) -> None:
        self.assertEqual(self.record(), 75)
        continuation = {**self.action, "action": "continue"}
        self.assertEqual(backoff.check(continuation, self.state, 900, 1899), 3)
        self.assertEqual(backoff.check(continuation, self.state, 900, 1900), 0)

    def test_healthy_continue_never_skips(self) -> None:
        continuation = {**self.action, "action": "continue"}
        self.assertEqual(backoff.check(continuation, self.state, 900, 1000), 0)
        self.result.write_text(json.dumps({"status": "completed", "summary": "Done"}))
        self.assertEqual(self.record(continuation), 0)
        self.assertEqual(backoff.check(continuation, self.state, 900, 1001), 0)

    def test_new_pr_head_bypasses_previous_block(self) -> None:
        action = {"action": "review", "pr": 410, "sha": "a" * 40}
        self.assertEqual(self.record(action), 75)
        self.assertEqual(backoff.check(action, self.state, 900, 1001), 3)
        new_head = {**action, "sha": "b" * 40}
        self.assertEqual(backoff.check(new_head, self.state, 900, 1001), 0)

    def test_completed_target_does_not_clear_other_blocks(self) -> None:
        self.assertEqual(self.record(), 75)
        other = {"action": "review", "pr": 412, "sha": "c" * 40}
        self.assertEqual(backoff.check(other, self.state, 900, 1001), 0)
        self.assertEqual(self.record(other), 75)
        self.result.write_text(json.dumps({"status": "completed", "summary": "Done"}))
        self.assertEqual(self.record(other), 0)
        self.assertEqual(backoff.check(other, self.state, 900, 1001), 0)
        self.assertEqual(backoff.check(self.action, self.state, 900, 1001), 3)

    def test_nonzero_exit_overrides_completed_output(self) -> None:
        self.result.write_text(json.dumps({"status": "completed", "summary": "Done"}))
        self.assertEqual(self.record(code=17), 75)
        self.assertEqual(backoff.check(self.action, self.state, 900, 1001), 3)

    def test_missing_or_invalid_result_records_backoff(self) -> None:
        self.result.unlink()
        self.assertEqual(self.record(), 75)
        for result in (
            "not json",
            "[]",
            '{"status":"completed"}',
            '{"status":"unknown","summary":"Done"}',
            '{"status":"completed","summary":null}',
            '{"status":"completed","summary":"Done","extra":true}',
        ):
            with self.subTest(result=result):
                self.result.write_text(result)
                self.assertEqual(self.record(), 75)
                self.assertEqual(backoff.check(self.action, self.state, 900, 1001), 3)

    def test_failed_atomic_replace_preserves_old_state(self) -> None:
        self.record()
        path = self.state / "codex-backoff.json"
        before = path.read_bytes()
        self.result.write_text(json.dumps({"status": "completed", "summary": "Done"}))
        with patch.object(backoff.os, "replace", side_effect=OSError("write failed")):
            with self.assertRaises(OSError):
                self.record()
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(self.state.iterdir()), [path])

    def cli(self, command: str, *extra: str) -> subprocess.CompletedProcess[str]:
        action_file = self.root / "action.json"
        action_file.write_text(json.dumps(self.action))
        return subprocess.run(
            [
                sys.executable,
                str(ROOT / "tick_backoff.py"),
                command,
                "--action-file",
                str(action_file),
                "--state-dir",
                str(self.state),
                "--ttl",
                "900",
                *extra,
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )

    def test_cli_protocol(self) -> None:
        self.assertEqual(self.cli("check").returncode, 0)
        arguments = ("--result-file", str(self.result), "--exit-code", "0")
        blocked = self.cli("record", *arguments)
        self.assertEqual(blocked.returncode, 75)
        self.assertIn("Waiting", blocked.stderr)
        self.assertEqual(self.cli("check").returncode, 3)
        self.result.write_text(json.dumps({"status": "completed", "summary": "Done"}))
        self.assertEqual(self.cli("record", *arguments).returncode, 0)
        self.assertEqual(self.cli("check").returncode, 0)

    def test_invalid_state_fails_closed(self) -> None:
        self.state.mkdir()
        for value in ("broken", "[]", '{"issue:338":{"at":"bad"}}'):
            with self.subTest(value=value):
                (self.state / "codex-backoff.json").write_text(value)
                self.assertEqual(self.cli("check").returncode, 75)

    def test_cli_rejects_invalid_ttl_and_missing_record_arguments(self) -> None:
        for ttl in ("0", "-1", "bad"):
            with self.subTest(ttl=ttl):
                self.assertEqual(self.cli("check", "--ttl", ttl).returncode, 2)
        self.assertEqual(self.cli("record").returncode, 2)


if __name__ == "__main__":
    unittest.main()
