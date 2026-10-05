"""Blocked-target retry policy and its command-line protocol."""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/epic"))
import isolated_env  # noqa: E402, F401 (before production modules or fixtures)

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("tick_backoff", ROOT / "tick_backoff.py")
assert SPEC and SPEC.loader
backoff = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backoff)


class EscalationGitHub:
    """Stateful REST boundary, including a lost write response."""

    def __init__(self) -> None:
        self.head = "a" * 40
        self.labels: list[dict[str, str]] = []
        self.comments: list[dict[str, str]] = []
        self.writes: list[str] = []
        self.fail: str | None = None

    def __call__(self, args: list[str], timeout: int) -> str:
        endpoint = args[1]
        method = args[args.index("--method") + 1] if "--method" in args else "GET"
        if self.fail == "read" and method == "GET":
            raise subprocess.CalledProcessError(1, args)
        if method == "POST" and endpoint.endswith("/labels"):
            if self.fail == "label":
                raise subprocess.CalledProcessError(1, args)
            self.labels = [{"name": "needs-anton"}]
            self.writes.append("label")
        elif method == "POST" and endpoint.endswith("/comments"):
            self.comments.append({"body": args[-1].removeprefix("body=")})
            self.writes.append("comment")
            if self.fail == "comment-response":
                raise subprocess.TimeoutExpired(args, timeout)
        elif method == "DELETE":
            self.labels = []
            self.writes.append("remove-label")
        elif method == "GET" and "/comments?" in endpoint:
            return json.dumps([self.comments])
        elif method == "GET":
            return json.dumps(
                {"state": "open", "head": {"sha": self.head}, "labels": self.labels}
            )
        else:
            raise AssertionError(args)
        return "{}"


class BackoffTests(unittest.TestCase):
    def setUp(self) -> None:
        environment = patch.dict(os.environ, {"EPIC_SHARED_READER": "0"})
        environment.start()
        self.addCleanup(environment.stop)
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

    def test_three_blocks_escalate_once_and_stay_suppressed_after_ttl(self) -> None:
        github = EscalationGitHub()
        with patch.object(backoff.github_quota, "run_gh", side_effect=github):
            for count in range(1, 4):
                self.result.write_text(
                    json.dumps({"status": "blocked", "summary": f"Reason {count}"})
                )
                self.assertEqual(self.record(), 3)
                self.assertEqual(
                    next(iter(self.entries().values()))["blocked_count"], count
                )
                self.assertEqual(backoff.check(self.action, self.state, 900, 1899), 3)
                if count < 3:
                    self.assertEqual(
                        backoff.check(self.action, self.state, 900, 1900), 0
                    )
                    self.assertEqual(github.writes, [])
            for _ in range(2):
                backoff.reconcile(self.state, self.state)
                self.assertEqual(backoff.check(self.action, self.state, 900, 99999), 3)
            self.assertEqual(github.writes, ["label", "comment"])
            self.assertIn("Reason 3", github.comments[0]["body"])
            self.assertNotIn("Reason 2", github.comments[0]["body"])

    def test_new_head_and_label_removal_reset_escalation(self) -> None:
        self.action = {"action": "continue", "pr": 410, "sha": "a" * 40}
        github = EscalationGitHub()
        with patch.object(backoff.github_quota, "run_gh", side_effect=github):
            for reset in ("head", "label"):
                with self.subTest(reset=reset):
                    for _ in range(3):
                        self.assertEqual(self.record(), 3)
                    if reset == "head":
                        github.head = "b" * 40
                        self.action["sha"] = github.head
                    else:
                        github.labels = []
                    backoff.reconcile(self.state, self.state)
                    self.assertEqual(self.entries(), {})
                    self.assertEqual(github.labels, [])
                    self.assertEqual(
                        backoff.check(self.action, self.state, 900, 1001), 0
                    )
            self.assertEqual(self.record(), 3)
            self.assertEqual(next(iter(self.entries().values()))["blocked_count"], 1)
            self.assertEqual(github.writes.count("label"), 2)
            self.assertEqual(github.writes.count("comment"), 2)

    def test_failed_notification_retries_without_model_or_duplicate_comment(
        self,
    ) -> None:
        github = EscalationGitHub()
        with patch.object(backoff.github_quota, "run_gh", side_effect=github):
            self.record()
            self.record()
            github.fail = "label"
            with self.assertRaises(subprocess.CalledProcessError):
                self.record()
            self.assertEqual(backoff.check(self.action, self.state, 900, 99999), 3)
            github.fail = "comment-response"
            with self.assertRaises(subprocess.TimeoutExpired):
                backoff.reconcile(self.state, self.state)
            self.assertEqual(backoff.check(self.action, self.state, 900, 99999), 3)
            github.fail = None
            backoff.reconcile(self.state, self.state)
            self.assertEqual(github.writes, ["label", "comment"])
            self.assertEqual(next(iter(self.entries().values()))["blocked_count"], 3)

    def test_quota_wait_preserves_limit_and_delays_notification(self) -> None:
        github = EscalationGitHub()
        with patch.object(backoff.github_quota, "run_gh", side_effect=github):
            self.record()
            self.record()
            with patch.object(backoff.github_quota, "check", return_value=(3, "later")):
                with self.assertRaises(backoff.QuotaWait):
                    self.record()
            self.assertEqual(github.writes, [])
            self.assertEqual(backoff.check(self.action, self.state, 900, 99999), 3)
            backoff.reconcile(self.state, self.state)
            self.assertEqual(github.writes, ["label", "comment"])

    def test_failed_result_preserves_count_but_does_not_increment_it(self) -> None:
        self.record()
        for _ in range(3):
            self.assertEqual(self.record(code=17), 75)
        self.assertEqual(next(iter(self.entries().values()))["blocked_count"], 1)
        self.assertEqual(self.record(), 3)
        self.assertEqual(next(iter(self.entries().values()))["blocked_count"], 2)

    def test_reconcile_read_failure_cannot_reset_escalation(self) -> None:
        github = EscalationGitHub()
        with patch.object(backoff.github_quota, "run_gh", side_effect=github):
            for _ in range(3):
                self.record()
            before = self.entries()
            github.fail = "read"
            with self.assertRaises(subprocess.CalledProcessError):
                backoff.reconcile(self.state, self.state)
            self.assertEqual(self.entries(), before)
            self.assertEqual(backoff.check(self.action, self.state, 900, 99999), 3)

    def test_new_head_does_not_remove_preexisting_label(self) -> None:
        self.action = {"action": "continue", "pr": 410, "sha": "a" * 40}
        github = EscalationGitHub()
        github.labels = [{"name": "needs-anton"}]
        with patch.object(backoff.github_quota, "run_gh", side_effect=github):
            for _ in range(3):
                self.record()
            github.head = "b" * 40
            backoff.reconcile(self.state, self.state)
            self.assertEqual(github.labels, [{"name": "needs-anton"}])
            self.assertEqual(github.writes, ["comment"])

    def test_invalid_blocked_count_fails_closed(self) -> None:
        self.record()
        entries = self.entries()
        entry = next(iter(entries.values()))
        for count in (-1, True, "3", 4, 3):
            with self.subTest(count=count):
                entry["blocked_count"] = count
                backoff.save_entries(self.state, entries)
                with self.assertRaises(ValueError):
                    backoff.load_entries(self.state)

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

    def test_conflict_cooldown_is_scoped_to_head_and_target_tip(self) -> None:
        action = {
            "action": "resolve-conflict",
            "pr": 410,
            "sha": "a" * 40,
            "target_tip": "b" * 40,
        }
        self.assertEqual(self.record(action), 3)
        self.assertEqual(backoff.check(action, self.state, 900, 1001), 3)
        self.assertEqual(backoff.check(action, self.state, 900, 1900), 0)
        for changed in ({"target_tip": "c" * 40}, {"sha": "d" * 40}):
            self.assertEqual(
                backoff.check({**action, **changed}, self.state, 900, 1001), 0
            )
        # An older head-only cooldown must not suppress a newly pinned base.
        self.record({**action, "action": "fix"})
        self.assertEqual(
            backoff.check({**action, "target_tip": "c" * 40}, self.state, 900, 1001), 0
        )

    def test_conflict_without_a_valid_pin_fails_closed(self) -> None:
        for tip in (None, "", "bad", True):
            with self.subTest(tip=tip), self.assertRaises(ValueError):
                backoff.target_key(
                    {
                        "action": "resolve-conflict",
                        "pr": 410,
                        "sha": "a" * 40,
                        "target_tip": tip,
                    }
                )

    def test_issue_cooldown_transfers_once_without_extending_expiry(self) -> None:
        self.record()
        action = {"action": "continue", "pr": 410, "sha": "a" * 40}
        original = backoff.load_entries(self.state)[backoff.target_key(self.action)]
        original["blocked_count"] = 0
        metadata = {
            "body": f"Executor: Codex\r\nIssue: {self.action['issue']}\r\n",
            "headRefOid": action["sha"],
        }
        response = subprocess.CompletedProcess([], 0, stdout=json.dumps(metadata))
        with (
            patch.object(backoff.subprocess, "run", return_value=response) as github,
            patch.object(
                backoff.github_quota, "resolve_gh", return_value="/test/bin/gh"
            ) as resolve,
        ):
            self.assertEqual(backoff.check(action, self.state, 900, 1200), 3)
            resolve.assert_called_once_with()
            github.assert_called_once_with(
                [
                    "/test/bin/gh",
                    "pr",
                    "view",
                    "410",
                    "--repo",
                    "phaabe/live.moafunk.de",
                    "--json",
                    "body,headRefOid",
                ],
                check=False,
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

    def fresh_cli(
        self, payload: object, status: int = 200, auth_context: bool = True
    ) -> subprocess.CompletedProcess[str]:
        """Exercise the real CLI and shared reader with only gh replaced."""
        self.action = {"action": "continue", "pr": 410, "sha": "a" * 40}
        cache = self.root / "cache" / "github-cache"
        cache.mkdir(parents=True, exist_ok=True)
        if auth_context:
            (cache / "auth-context").write_text("test-backoff")
        bin_dir = self.root / "bin"
        bin_dir.mkdir(exist_ok=True)
        gh = bin_dir / "gh"
        gh.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "assert sys.argv[1:3] == ['api', '-i'], sys.argv\n"
            "if sys.argv[-1] == 'https://api.github.com/user':\n"
            "    print('HTTP/2.0 200 OK\\n\\n' + json.dumps({'login': 'test'}))\n"
            "else:\n"
            "    assert sys.argv[-1].endswith('/pulls/410'), sys.argv\n"
            "    print(os.environ['TEST_GITHUB_RESPONSE'])\n"
        )
        gh.chmod(0o755)
        response = f"HTTP/2.0 {status} Response\n\n{json.dumps(payload)}"
        with patch.dict(
            os.environ,
            {
                "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                "EPIC_SHARED_READER": "1",
                "EPIC_CACHE_DIR": str(cache.parent),
                "TEST_GITHUB_RESPONSE": response,
            },
        ):
            return self.cli("check")

    def fresh_cooldown(self) -> tuple[Path, bytes]:
        # A fixed future expiry keeps real CLI tests independent of wall time.
        key = backoff.target_key(self.action)
        backoff.save_entries(
            self.state,
            {key: {"at": 1000, "until": 4102444800, "reason": "Waiting"}},
        )
        path = self.state / "codex-backoff.json"
        return path, path.read_bytes()

    def test_fresh_cli_transfers_cooldown_for_selected_head(self) -> None:
        self.fresh_cooldown()
        original = next(iter(backoff.load_entries(self.state).values()))
        result = self.fresh_cli(
            {"body": f"Issue: {self.action['issue']}", "head": {"sha": "a" * 40}}
        )
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(
            backoff.load_entries(self.state),
            {backoff.target_key(self.action): original},
        )
        calls = self.root / "cache" / "github-cache" / "calls.jsonl"
        records = [json.loads(line) for line in calls.read_text().splitlines()]
        self.assertEqual([row["purpose"] for row in records], ["identity", "backoff"])

    def test_fresh_cli_changed_head_skips_without_mutation(self) -> None:
        path, original = self.fresh_cooldown()
        result = self.fresh_cli(
            {"body": f"Issue: {self.action['issue']}", "head": {"sha": "b" * 40}}
        )
        self.assertEqual(result.returncode, 6, result.stderr)
        self.assertEqual(path.read_bytes(), original)

    def test_fresh_cli_read_failures_leave_cooldown_and_quota_unchanged(self) -> None:
        path, original = self.fresh_cooldown()
        for status, payload in (
            (429, {"message": "rate limit exceeded"}),
            (403, {"message": "secondary rate limit"}),
            (500, {"message": "server failure"}),
            (200, []),
            (200, {"head": {"sha": "a" * 40}}),
            (200, {"body": "", "head": {"sha": "invalid"}}),
        ):
            with self.subTest(status=status, payload=payload):
                result = self.fresh_cli(payload, status)
                self.assertEqual(result.returncode, 5, result.stderr)
                self.assertEqual(path.read_bytes(), original)
                self.assertFalse((self.state / "github-quota-wait.json").exists())
                self.assertFalse(list((self.root / "cache").rglob("snapshot.json")))
                self.assertFalse(list((self.root / "cache").rglob("etags")))

    def test_fresh_cli_missing_auth_context_is_config_error(self) -> None:
        path, original = self.fresh_cooldown()
        result = self.fresh_cli({}, auth_context=False)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(path.read_bytes(), original)

    def test_issue_count_does_not_transfer_to_new_pr_head(self) -> None:
        self.record()
        self.record()
        action = {"action": "continue", "pr": 410, "sha": "a" * 40}
        with patch.object(backoff, "pr_issue", return_value=self.action["issue"]):
            self.assertEqual(backoff.check(action, self.state, 900, 1001), 3)
        self.assertEqual(backoff.check(action, self.state, 900, 1900), 0)
        self.assertEqual(self.record(action), 3)
        self.assertEqual(self.entries()[backoff.target_key(action)]["blocked_count"], 1)

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

    def test_quota_result_preserves_target_state_and_uses_retry_time_once(self) -> None:
        self.record()
        path = self.state / "codex-backoff.json"
        before = path.read_bytes()
        self.result.write_text(
            json.dumps(
                {
                    "status": "blocked",
                    "summary": "GitHub quota exhausted",
                    "reason_code": "github_rate_limit",
                    "retry_at": "1970-01-01T01:00:00Z",
                }
            )
        )
        self.assertEqual(self.record(), 4)
        self.assertEqual(path.read_bytes(), before)
        wait = json.loads((self.state / "github-quota-wait.json").read_text())
        self.assertEqual(wait["retry_at"], "1970-01-01T01:00:00Z")
        self.assertEqual(wait["reset_at"], "1970-01-01T00:59:00Z")
        self.assertEqual(wait["source"], "caller")
        self.assertEqual(wait["provenance"]["origin"], "model-result")
        self.assertEqual(wait["provenance"]["reset_lookup"], {"attempted": False})

    def test_quota_result_validation_does_not_trust_summary_text(self) -> None:
        for result in (
            {
                "status": "completed",
                "summary": "rate limit",
                "reason_code": "github_rate_limit",
                "retry_at": None,
            },
            {
                "status": "blocked",
                "summary": "rate limit",
                "reason_code": "other",
                "retry_at": None,
            },
            {
                "status": "blocked",
                "summary": "rate limit",
                "reason_code": "github_rate_limit",
                "retry_at": "not a date",
            },
            {
                "status": "blocked",
                "summary": "rate limit",
                "reason_code": "github_rate_limit",
                "retry_at": "2030-01-01T01:00:00",
            },
            {
                "status": "blocked",
                "summary": "rate limit",
                "reason_code": "github_rate_limit",
                "retry_at": "2030-01-01T01:00:00+02:00",
            },
            {
                "status": "blocked",
                "summary": "rate limit",
                "reason_code": None,
                "retry_at": "2030-01-01T01:00:00Z",
            },
        ):
            with self.subTest(result=result):
                self.result.write_text(json.dumps(result))
                self.assertEqual(self.record(), 75)
                self.assertFalse((self.state / "github-quota-wait.json").exists())
        self.result.write_text(
            json.dumps({"status": "blocked", "summary": "GitHub rate limit"})
        )
        self.assertEqual(self.record(), 3)

    def test_nullable_metadata_preserves_normal_results(self) -> None:
        for status, expected in (("blocked", 3), ("completed", 0)):
            with self.subTest(status=status):
                self.result.write_text(
                    json.dumps(
                        {
                            "status": status,
                            "summary": "Normal result",
                            "reason_code": None,
                            "retry_at": None,
                        }
                    )
                )
                self.assertEqual(self.record(), expected)

    def test_quota_result_with_nonzero_exit_preserves_target_state(self) -> None:
        self.record()
        path = self.state / "codex-backoff.json"
        before = path.read_bytes()
        self.result.write_text(
            json.dumps(
                {
                    "status": "blocked",
                    "summary": "Quota exhausted before exit",
                    "reason_code": "github_rate_limit",
                    "retry_at": "1970-01-01T01:00:00Z",
                }
            )
        )
        self.assertEqual(self.record(code=17), 4)
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue((self.state / "github-quota-wait.json").exists())

    def test_unknown_reset_uses_shared_lookup_and_fallback(self) -> None:
        self.result.write_text(
            json.dumps(
                {
                    "status": "blocked",
                    "summary": "Quota exhausted",
                    "reason_code": "github_rate_limit",
                    "retry_at": None,
                }
            )
        )
        response = subprocess.CompletedProcess([], 1, stdout="unavailable", stderr="")
        with patch.object(
            backoff.github_quota.subprocess, "run", return_value=response
        ) as request:
            self.assertEqual(self.record(), 4)
        self.assertEqual(request.call_count, 1)
        wait = json.loads((self.state / "github-quota-wait.json").read_text())
        self.assertEqual(wait["source"], "fallback")
        self.assertEqual(wait["provenance"]["origin"], "model-result")
        self.assertEqual(backoff.github_quota.parse_iso(wait["retry_at"]), 1900)
        self.assertFalse((self.state / "codex-backoff.json").exists())

    def test_far_future_model_time_uses_authoritative_reset(self) -> None:
        self.result.write_text(
            json.dumps(
                {
                    "status": "blocked",
                    "summary": "Quota exhausted",
                    "reason_code": "github_rate_limit",
                    "retry_at": "2027-12-31T00:00:00Z",
                }
            )
        )
        response = subprocess.CompletedProcess(
            [],
            0,
            stdout=json.dumps(
                {"data": {"rateLimit": {"resetAt": "1970-01-01T00:30:00Z"}}}
            ),
            stderr="",
        )
        with patch.object(
            backoff.github_quota.subprocess, "run", return_value=response
        ) as request:
            self.assertEqual(self.record(), 4)
        self.assertEqual(request.call_count, 1)
        wait = json.loads((self.state / "github-quota-wait.json").read_text())
        self.assertEqual(wait["retry_at"], "1970-01-01T00:31:00Z")
        self.assertEqual(wait["source"], "rateLimit")
        self.assertEqual(wait["provenance"]["origin"], "model-result")
        self.assertTrue(wait["provenance"]["reset_lookup"]["attempted"])
        self.assertFalse((self.state / "codex-backoff.json").exists())

    def test_model_quota_preserves_active_wait_with_or_without_provenance(self) -> None:
        wait = backoff.github_quota.record(self.state, 1000, "1970-01-01T00:30:00Z")
        path = self.state / backoff.github_quota.WAIT_FILE
        self.result.write_text(
            json.dumps(
                {
                    "status": "blocked",
                    "summary": "Quota exhausted",
                    "reason_code": "github_rate_limit",
                    "retry_at": "1970-01-01T01:00:00Z",
                }
            )
        )
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                if legacy:
                    del wait["provenance"]
                path.write_text(json.dumps(wait))
                original = path.read_bytes()
                with patch.object(backoff.github_quota, "record") as writer:
                    self.assertEqual(self.record(), 4)
                writer.assert_not_called()
                self.assertEqual(path.read_bytes(), original)

    def test_past_model_retry_keeps_origin_in_fallback(self) -> None:
        self.result.write_text(
            json.dumps(
                {
                    "status": "blocked",
                    "summary": "Quota exhausted",
                    "reason_code": "github_rate_limit",
                    "retry_at": "1970-01-01T00:00:00Z",
                }
            )
        )
        with patch.object(backoff.github_quota.subprocess, "run") as request:
            self.assertEqual(self.record(), 4)
        request.assert_not_called()
        wait = backoff.github_quota.read_wait(self.state)
        self.assertEqual(wait["source"], "fallback")
        self.assertEqual(wait["provenance"]["origin"], "model-result")
        self.assertEqual(backoff.github_quota.parse_iso(wait["retry_at"]), 1900)

    def test_model_time_at_upper_bound_needs_no_reset_query(self) -> None:
        self.result.write_text(
            json.dumps(
                {
                    "status": "blocked",
                    "summary": "Quota exhausted",
                    "reason_code": "github_rate_limit",
                    "retry_at": backoff.github_quota.iso(4660),
                }
            )
        )
        with patch.object(backoff.github_quota.subprocess, "run") as request:
            self.assertEqual(self.record(), 4)
        request.assert_not_called()
        wait = json.loads((self.state / "github-quota-wait.json").read_text())
        self.assertEqual(backoff.github_quota.parse_iso(wait["retry_at"]), 4660)

    def test_invalid_result_with_nonzero_exit_keeps_exit_reason(self) -> None:
        for value in (
            "",
            "not json",
            "[]",
            '{"status":"completed"}',
            json.dumps(
                {
                    "status": "blocked",
                    "summary": "Quota",
                    "reason_code": "github_rate_limit",
                    "retry_at": "bad",
                }
            ),
            json.dumps(
                {
                    "status": "blocked",
                    "summary": "Quota",
                    "reason_code": "bad",
                    "retry_at": None,
                }
            ),
        ):
            with self.subTest(value=value):
                self.result.write_text(value)
                self.assertEqual(
                    backoff.result_outcome(self.result, 124), (75, "model exited 124")
                )
        self.result.unlink()
        self.assertEqual(
            backoff.result_outcome(self.result, 137), (75, "model exited 137")
        )

    def test_quota_during_pr_lookup_cannot_transfer_cooldown(self) -> None:
        self.record()
        path = self.state / "codex-backoff.json"
        before = path.read_bytes()
        response = subprocess.CompletedProcess(
            [],
            1,
            stdout="",
            stderr="GraphQL: API rate limit already exceeded",
        )
        with patch.object(backoff.subprocess, "run", return_value=response):
            with self.assertRaises(backoff.github_quota.QuotaExhausted):
                backoff.check(
                    {"action": "continue", "pr": 410, "sha": "a" * 40},
                    self.state,
                    900,
                    1001,
                )
        self.assertEqual(path.read_bytes(), before)

    def test_legacy_cli_quota_propagates_stub_path_to_shared_wait(self) -> None:
        path, original = self.fresh_cooldown()
        self.action = {"action": "continue", "pr": 410, "sha": "a" * 40}
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        calls = self.root / "gh-calls.jsonl"
        quota_dir = self.root / "shared-quota"
        for code, stdout, stderr in (
            (1, "", "GraphQL: API rate limit already exceeded"),
            (0, '{"errors":[{"type":"RATE_LIMITED"}]}', ""),
        ):
            with self.subTest(code=code):
                calls.write_text("")
                gh.write_text(
                    f"#!{sys.executable}\n"
                    "import json, sys\n"
                    f"with open({str(calls)!r}, 'a') as out:\n"
                    "    out.write(json.dumps(sys.argv) + '\\n')\n"
                    "if sys.argv[1:3] == ['pr', 'view']:\n"
                    f"    print({stdout!r})\n"
                    f"    print({stderr!r}, file=sys.stderr)\n"
                    f"    sys.exit({code})\n"
                    "assert sys.argv[1:] == "
                    "['api', 'graphql', '-f', 'query=query{rateLimit{resetAt}}']\n"
                    "print(json.dumps({'data': {'rateLimit': "
                    "{'resetAt': '2100-01-01T00:00:00Z'}}}))\n"
                )
                gh.chmod(0o755)
                with patch.dict(os.environ, {"PATH": str(bin_dir)}):
                    result = self.cli("check", "--quota-dir", str(quota_dir))
                self.assertEqual(result.returncode, 4, result.stderr)
                wait = backoff.github_quota.read_wait(quota_dir)
                prov = wait["provenance"]
                self.assertEqual(wait["source"], "rateLimit")
                self.assertEqual(prov["origin"], "code")
                self.assertEqual(prov["quota_call"], {"gh_path": str(gh)})
                self.assertEqual(
                    prov["reset_lookup"],
                    {"attempted": True, "gh_path": str(gh), "result": "reset"},
                )
                self.assertEqual(
                    prov["writer"]["script"], str(ROOT / "tick_backoff.py")
                )
                invoked = [json.loads(line) for line in calls.read_text().splitlines()]
                self.assertEqual(len(invoked), 2)
                self.assertEqual([args[0] for args in invoked], [str(gh), str(gh)])
                self.assertEqual(path.read_bytes(), original)
                self.assertFalse((self.state / backoff.github_quota.WAIT_FILE).exists())

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
