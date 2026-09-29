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
            action or self.action, self.state, self.result, code, 1000, 900
        )

    def entries(self) -> dict[str, dict[str, object]]:
        return json.loads((self.state / "codex-backoff.json").read_text())

    def test_record_stores_the_expiry_and_check_obeys_it(self) -> None:
        self.assertEqual(self.record(), 3)
        [entry] = self.entries().values()
        self.assertEqual((entry["at"], entry["until"]), (1000, 1900))
        # A shorter --ttl on a later tick does not end the stored delay early.
        self.assertEqual(backoff.check(self.action, self.state, 60, 1899), 3)
        self.assertEqual(backoff.check(self.action, self.state, 60, 1900), 0)

    def test_old_entries_without_expiry_use_the_ttl(self) -> None:
        self.state.mkdir()
        key = backoff.target_key(self.action)
        (self.state / "codex-backoff.json").write_text(
            json.dumps({key: {"at": 1000, "reason": "old"}})
        )
        self.assertEqual(backoff.check(self.action, self.state, 900, 1899), 3)
        self.assertEqual(backoff.check(self.action, self.state, 900, 1900), 0)

    def test_invalid_expiry_is_rejected(self) -> None:
        self.state.mkdir()
        key = backoff.target_key(self.action)
        for until in ("soon", True, float("inf")):
            with self.subTest(until=until):
                (self.state / "codex-backoff.json").write_text(
                    json.dumps({key: {"at": 1000, "until": until, "reason": "x"}})
                )
                with self.assertRaises(ValueError):
                    backoff.load_entries(self.state)

    def test_claim_block_suppresses_continue_until_expiry(self) -> None:
        self.assertEqual(self.record(), 3)
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
        self.assertEqual(self.record(action), 3)
        self.assertEqual(backoff.check(action, self.state, 900, 1001), 3)
        new_head = {**action, "sha": "b" * 40}
        self.assertEqual(backoff.check(new_head, self.state, 900, 1001), 0)

    def test_completed_target_does_not_clear_other_blocks(self) -> None:
        self.assertEqual(self.record(), 3)
        other = {"action": "review", "pr": 412, "sha": "c" * 40}
        self.assertEqual(backoff.check(other, self.state, 900, 1001), 0)
        self.assertEqual(self.record(other), 3)
        self.result.write_text(json.dumps({"status": "completed", "summary": "Done"}))
        self.assertEqual(self.record(other), 0)
        self.assertEqual(backoff.check(other, self.state, 900, 1001), 0)
        self.assertEqual(backoff.check(self.action, self.state, 900, 1001), 3)

    def test_issue_cooldown_transfers_once_without_extending_expiry(self) -> None:
        self.record()
        action = {"action": "continue", "pr": 410, "sha": "a" * 40}
        original = backoff.load_entries(self.state)[backoff.target_key(self.action)]
        metadata = {
            "body": f"Executor: Codex\r\nIssue: {self.action['issue']}\r\n",
            "headRefOid": action["sha"],
        }
        response = subprocess.CompletedProcess([], 0, stdout=json.dumps(metadata))
        with patch.object(backoff.subprocess, "run", return_value=response) as github:
            self.assertEqual(backoff.check(action, self.state, 900, 1200), 3)
            github.assert_called_once_with(
                [
                    "gh",
                    "pr",
                    "view",
                    "410",
                    "--repo",
                    "phaabe/live.moafunk.de",
                    "--json",
                    "body,headRefOid",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
        self.assertEqual(
            backoff.load_entries(self.state), {backoff.target_key(action): original}
        )
        with patch.object(backoff.subprocess, "run") as github:
            self.assertEqual(backoff.check(action, self.state, 900, 1899), 3)
            self.assertEqual(backoff.check(action, self.state, 900, 1900), 0)
            new_head = {**action, "sha": "b" * 40}
            self.assertEqual(backoff.check(new_head, self.state, 900, 1201), 0)
            github.assert_not_called()

    def test_migration_preserves_unrelated_entries(self) -> None:
        self.record()
        other = {"action": "claim", "issue": self.action["issue"].replace("338", "381")}
        self.record(other)
        before = backoff.load_entries(self.state)
        action = {"action": "continue", "pr": 410, "sha": "a" * 40}
        with patch.object(backoff, "pr_issue", return_value=self.action["issue"]):
            self.assertEqual(backoff.check(action, self.state, 900, 1001), 3)
        after = backoff.load_entries(self.state)
        self.assertNotIn(backoff.target_key(self.action), after)
        self.assertEqual(
            after[backoff.target_key(other)], before[backoff.target_key(other)]
        )
        with patch.object(backoff, "pr_issue", return_value=self.action["issue"]):
            self.assertEqual(
                backoff.check({**action, "sha": "b" * 40}, self.state, 900, 1002), 0
            )
        self.assertEqual(backoff.load_entries(self.state), after)

    def test_healthy_expired_and_non_continue_prs_need_no_metadata(self) -> None:
        action = {"action": "continue", "pr": 410, "sha": "a" * 40}
        with patch.object(backoff.subprocess, "run") as github:
            self.assertEqual(backoff.check(action, self.state, 900, 1000), 0)
            self.record()
            self.assertEqual(backoff.check(action, self.state, 900, 1900), 0)
            self.assertEqual(
                backoff.check({**action, "action": "review"}, self.state, 900, 1001), 0
            )
            self.record(action)
            self.assertEqual(backoff.check(action, self.state, 900, 1001), 3)
            github.assert_not_called()

    def test_missing_or_unrelated_issue_does_not_consume_cooldown(self) -> None:
        self.record()
        action = {"action": "continue", "pr": 410, "sha": "a" * 40}
        path = self.state / "codex-backoff.json"
        original = path.read_bytes()
        for body in (
            "",
            f"Refs: {self.action['issue']}\nCloses: {self.action['issue']}",
            f"This fixes {self.action['issue']}",
            "Issue: https://github.com/phaabe/live.moafunk.de/issues/381",
        ):
            with self.subTest(body=body):
                response = subprocess.CompletedProcess(
                    [],
                    0,
                    stdout=json.dumps({"body": body, "headRefOid": action["sha"]}),
                )
                with patch.object(backoff.subprocess, "run", return_value=response):
                    self.assertEqual(backoff.check(action, self.state, 900, 1001), 0)
                self.assertEqual(path.read_bytes(), original)

    def test_invalid_or_ambiguous_metadata_keeps_state(self) -> None:
        self.record()
        action = {"action": "continue", "pr": 410, "sha": "a" * 40}
        path = self.state / "codex-backoff.json"
        original = path.read_bytes()
        issue_line = f"Issue: {self.action['issue']}"
        for metadata in (
            [],
            {"body": None, "headRefOid": action["sha"]},
            {"body": issue_line, "headRefOid": "b" * 40},
            {"body": "Issue: #338", "headRefOid": action["sha"]},
            {"body": f"{issue_line}\n{issue_line}", "headRefOid": action["sha"]},
            {"body": f"{issue_line} extra", "headRefOid": action["sha"]},
            {
                "body": "Issue: https://github.com/other/repo/issues/338",
                "headRefOid": action["sha"],
            },
        ):
            with self.subTest(metadata=metadata):
                response = subprocess.CompletedProcess(
                    [], 0, stdout=json.dumps(metadata)
                )
                with patch.object(backoff.subprocess, "run", return_value=response):
                    with self.assertRaises(ValueError):
                        backoff.check(action, self.state, 900, 1001)
                self.assertEqual(path.read_bytes(), original)

    def test_github_errors_keep_state_and_fail_cli_closed(self) -> None:
        self.record()
        action = {"action": "continue", "pr": 410, "sha": "a" * 40}
        path = self.state / "codex-backoff.json"
        original = path.read_bytes()
        action_file = self.root / "action.json"
        action_file.write_text(json.dumps(action))
        argv = [
            "tick_backoff.py",
            "check",
            "--action-file",
            str(action_file),
            "--state-dir",
            str(self.state),
            "--ttl",
            "900",
        ]
        for error in (
            subprocess.TimeoutExpired("gh", 30),
            subprocess.CalledProcessError(1, "gh"),
            FileNotFoundError("gh"),
        ):
            with self.subTest(error=error):
                with (
                    patch.object(backoff.subprocess, "run", side_effect=error),
                    patch.object(backoff.time, "time", return_value=1001),
                    patch.object(sys, "argv", argv),
                ):
                    self.assertEqual(backoff.main(), 75)
                self.assertEqual(path.read_bytes(), original)

    def test_nonzero_exit_overrides_completed_output(self) -> None:
        self.result.write_text(json.dumps({"status": "completed", "summary": "Done"}))
        self.assertEqual(self.record(code=17), 75)
        self.assertEqual(backoff.check(self.action, self.state, 900, 1001), 3)

    def test_nonzero_exit_overrides_blocked_output(self) -> None:
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
        self.assertEqual(blocked.returncode, 3)
        self.assertIn("Waiting", blocked.stderr)
        self.assertEqual(self.cli("check").returncode, 3)
        self.result.write_text(json.dumps({"status": "completed", "summary": "Done"}))
        self.assertEqual(self.cli("record", *arguments).returncode, 0)
        self.assertEqual(self.cli("check").returncode, 0)

    def test_cli_failure_does_not_report_valid_blocked_outcome(self) -> None:
        arguments = ("--result-file", str(self.result), "--exit-code", "17")
        self.assertEqual(self.cli("record", *arguments).returncode, 75)
        self.result.write_text("invalid JSON")
        arguments = ("--result-file", str(self.result), "--exit-code", "0")
        self.assertEqual(self.cli("record", *arguments).returncode, 75)
        self.result.unlink()
        self.assertEqual(self.cli("record", *arguments).returncode, 75)

    def test_invalid_state_fails_closed(self) -> None:
        self.state.mkdir()
        for value in ("broken", "[]", '{"issue:338":{"at":"bad"}}'):
            with self.subTest(value=value):
                (self.state / "codex-backoff.json").write_text(value)
                self.assertEqual(self.cli("check").returncode, 75)
                self.assertEqual(
                    self.cli(
                        "record", "--result-file", str(self.result), "--exit-code", "0"
                    ).returncode,
                    75,
                )

    def test_cli_rejects_invalid_ttl_and_missing_record_arguments(self) -> None:
        for ttl in ("0", "-1", "bad"):
            with self.subTest(ttl=ttl):
                self.assertEqual(self.cli("check", "--ttl", ttl).returncode, 2)
        self.assertEqual(self.cli("record").returncode, 2)


if __name__ == "__main__":
    unittest.main()
