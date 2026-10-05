"""Unit tests for tick_cooldown.py (no GitHub: reads are patched)."""

from __future__ import annotations

import isolated_env  # noqa: F401  (first: hides live runner state)

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import tick_cooldown as tc
import tick_verify

SHA = "a" * 40
BASE = "b" * 40
ISSUE = "https://github.com/phaabe/live.moafunk.de/issues/7"
FIX = {"action": "fix", "reason": "t", "pr": 5, "sha": SHA, "updated_at": "u1"}
CONFLICT = {**FIX, "action": "resolve-conflict"}
NOW = 1_000_000.0


def fake_github(responses: dict[str, str]) -> Any:
    """Answers a read by the first key contained in its argument string."""

    def read(args: list[str]) -> str:
        text = " ".join(args)
        for part, out in responses.items():
            if part in text:
                return out
        raise tc.ReadFailed(text)

    return read


class CooldownTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="cooldown-")
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.seen = self.state / "seen.json"
        self.result = self.state / "result.json"
        self.github = {
            "pulls/5 --jq .base.ref": "dev/312-interim",
            "git/ref/heads/dev/312-interim": BASE,
            "pulls/5 --jq .state": f"open\n{SHA}",
            "pulls/5 --jq .body": f"Issue: {ISSUE}\n",
        }
        patcher = mock.patch.object(tc, "read", side_effect=self.read)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.reads: list[str] = []

    def read(self, args: list[str]) -> str:
        self.reads.append(" ".join(args))
        return fake_github(self.github)(args)

    def check(self, action: dict[str, Any], now: float = NOW) -> int:
        return tc.check(action, self.state, self.seen, now)

    def record(
        self,
        action: dict[str, Any],
        model_exit: int = 0,
        verify_exit: int = 0,
        status: str | None = None,
        now: float = NOW,
    ) -> int:
        if status is None:
            self.result.write_text("")
        else:
            self.result.write_text(
                json.dumps({"structured_output": {"status": status, "summary": "gate"}})
            )
        return tc.record(
            action, self.state, self.seen, self.result, model_exit, verify_exit, now
        )

    def block(self, action: dict[str, Any], now: float = NOW) -> None:
        self.assertEqual(self.check(action, now), tc.RUN)
        self.assertEqual(self.record(action, status="blocked", now=now), tc.BLOCKED)

    def entries(self) -> dict[str, Any]:
        return json.loads((self.state / tc.FILE).read_text())

    # Key

    def test_key_has_agent_action_target_and_head(self) -> None:
        self.assertEqual(tc.key(FIX), f"claude:fix:pr:5:{SHA}")
        issue = {"action": "continue", "issue": ISSUE + "/"}
        self.assertEqual(tc.key(issue), f"claude:continue:issue:{ISSUE}")
        self.assertEqual(self.reads, [])

    def test_resolve_conflict_key_adds_the_base_tip(self) -> None:
        self.assertEqual(
            tc.key(CONFLICT), f"claude:resolve-conflict:pr:5:{SHA}:base:{BASE}"
        )

    def test_bad_targets_are_refused(self) -> None:
        for action in (
            {"action": "fix", "pr": 5, "sha": "short"},
            {"action": "fix", "pr": "5", "sha": SHA},
            {"action": "claim", "issue": "https://example.com/issues/7"},
            {"action": "claim"},
            {"pr": 5, "sha": SHA},
        ):
            with self.subTest(action=action), self.assertRaises(ValueError):
                tc.key(action)

    def test_landing_set_matches_tick_verify(self) -> None:
        self.assertEqual(tc.LANDING, tick_verify.CHECKED)

    # Duration

    def test_duration_default_and_override(self) -> None:
        self.assertEqual(tc.seconds(), 4 * 60 * 60)
        with mock.patch.dict(os.environ, {tc.ENV_SECONDS: "60"}):
            self.assertEqual(tc.seconds(), 60)
        for bad in ("0", "-5", "1.5", "x"):
            with mock.patch.dict(os.environ, {tc.ENV_SECONDS: bad}):
                with self.assertRaises(ValueError):
                    tc.seconds()

    def test_blocked_skips_until_expiry(self) -> None:
        self.block(FIX)
        entry = self.entries()[tc.key(FIX)]
        self.assertEqual(entry["until"], NOW + tc.DEFAULT_SECONDS)
        self.assertIn("model reported blocked: gate", entry["reason"])
        self.assertEqual(self.check(FIX, NOW + 10), tc.SKIP)
        self.assertEqual(self.check(FIX, NOW + tc.DEFAULT_SECONDS), tc.RUN)

    def test_comments_neither_reset_nor_extend(self) -> None:
        self.block(FIX)
        # A peer comment changes updated_at; the key stays the same.
        commented = {**FIX, "updated_at": "u2"}
        self.assertEqual(self.check(commented, NOW + 100), tc.SKIP)
        self.assertEqual(self.entries()[tc.key(FIX)]["until"], NOW + tc.DEFAULT_SECONDS)

    def test_new_head_or_new_base_clears(self) -> None:
        self.block(CONFLICT)
        self.assertEqual(self.check(CONFLICT, NOW + 10), tc.SKIP)
        self.assertEqual(self.check({**CONFLICT, "sha": "c" * 40}, NOW + 10), tc.RUN)
        self.github["git/ref/heads/dev/312-interim"] = "d" * 40
        self.assertEqual(self.check(CONFLICT, NOW + 10), tc.RUN)

    def test_other_action_on_the_same_target_runs(self) -> None:
        self.block(FIX)
        self.assertEqual(self.check({**FIX, "action": "fix-checks"}, NOW + 1), tc.RUN)

    def test_success_clears_and_prunes_expired(self) -> None:
        self.block(FIX)
        other = {**FIX, "pr": 6}
        self.github["pulls/6 --jq .state"] = f"open\n{SHA}"
        self.block(other, now=NOW - tc.DEFAULT_SECONDS + 5)
        after = NOW + tc.DEFAULT_SECONDS + 1
        self.assertEqual(self.check(FIX, after), tc.RUN)
        self.assertEqual(self.record(FIX, now=after), tc.DONE)
        self.assertEqual(self.entries(), {})

    def test_record_needs_the_checked_key(self) -> None:
        self.check(FIX)
        with self.assertRaises(ValueError):
            self.record({**FIX, "pr": 6})

    # Evidence

    def test_exit_paths(self) -> None:
        unchanged, moved = f"open\n{SHA}", f"open\n{'c' * 40}"
        cases = [
            # name, action, model exit, verify exit, status, PR read, outcome
            ("blocked exit 0", FIX, 0, 0, "blocked", None, tc.BLOCKED),
            ("blocked, nonzero", FIX, 1, 1, "blocked", None, tc.BLOCKED),
            ("quota result", FIX, 0, 1, "quota", None, tc.QUOTA),
            ("completed, landed", FIX, 0, 0, "completed", None, tc.DONE),
            ("no result, landed", FIX, 0, 0, None, None, tc.DONE),
            ("nonzero, landed", FIX, 1, 0, None, None, tc.DONE),
            ("verify failed, unchanged", FIX, 0, 1, "completed", unchanged, tc.BLOCKED),
            ("nonzero, unchanged", FIX, 1, 1, None, unchanged, tc.BLOCKED),
            ("timeout, unchanged", FIX, 124, 1, None, unchanged, tc.BLOCKED),
            ("killed, unchanged", FIX, 137, 1, None, unchanged, tc.BLOCKED),
            ("verify failed, head moved", FIX, 0, 1, None, moved, tc.MISSED),
            ("verify failed, closed", FIX, 0, 1, None, f"closed\n{SHA}", tc.MISSED),
            ("verify failed, read failed", FIX, 0, 1, None, "", tc.MISSED),
            ("timeout, head moved", FIX, 124, 1, None, moved, tc.UNKNOWN),
            ("bad verify input", FIX, 0, 2, None, unchanged, tc.MISSED),
            # tick_verify.py exit 5: a GitHub read failed, no evidence.
            ("verify read error", FIX, 0, 5, None, unchanged, tc.MISSED),
            ("timeout, verify read error", FIX, 124, 5, None, unchanged, tc.UNKNOWN),
        ]
        cont = {"action": "continue", "reason": "t", "issue": ISSUE}
        cases += [
            ("continue blocked", cont, 0, 0, "blocked", None, tc.BLOCKED),
            ("continue completed", cont, 0, 0, "completed", None, tc.DONE),
            ("continue timeout", cont, 124, 0, None, None, tc.UNKNOWN),
            ("continue nonzero", cont, 1, 0, None, None, tc.UNKNOWN),
        ]
        for name, action, model, verify, status, pr, outcome in cases:
            with self.subTest(name):
                (self.state / tc.FILE).unlink(missing_ok=True)
                if pr is None:
                    self.github.pop("pulls/5 --jq .state", None)
                else:
                    self.github["pulls/5 --jq .state"] = pr
                self.assertEqual(self.check(action), tc.RUN)
                got = self.record(action, model, verify, status)
                self.assertEqual(got, outcome)
                stored = (self.state / tc.FILE).exists() and self.entries()
                self.assertEqual(bool(stored), outcome == tc.BLOCKED)

    def test_model_api_error_is_retried_without_cooldown(self) -> None:
        # Seen live: 529 Overloaded, no structured output, nothing spent.
        output = {"is_error": True, "terminal_reason": "api_error", "total_cost_usd": 0}
        self.github["pulls/5 --jq .state"] = f"open\n{SHA}"
        for model, verify, outcome in (
            (1, 1, tc.UNKNOWN),
            (0, 1, tc.UNKNOWN),
            (1, 0, tc.DONE),
        ):
            with self.subTest(model=model, verify=verify):
                (self.state / tc.FILE).unlink(missing_ok=True)
                self.assertEqual(self.check(FIX), tc.RUN)
                self.result.write_text(json.dumps(output))
                got = tc.record(
                    FIX, self.state, self.seen, self.result, model, verify, NOW
                )
                self.assertEqual(got, outcome)
                self.assertFalse((self.state / tc.FILE).exists())

    def test_invalid_structured_result_is_no_evidence(self) -> None:
        for output in (
            {"structured_output": {"status": "blocked"}},
            {"structured_output": {"status": "maybe", "summary": "x"}},
            {"structured_output": {"status": "blocked", "summary": "x", "y": 1}},
            {"result": '{"status": "blocked", "summary": "x"}'},
            ["blocked"],
        ):
            with self.subTest(output=output):
                self.result.write_text(json.dumps(output))
                self.assertIsNone(tc.model_result(self.result))
        self.result.write_text("blocked: the gate refused git rebase")
        self.assertIsNone(tc.model_result(self.result))

    # Issue to PR

    def test_issue_cooldown_moves_to_the_pr_continue(self) -> None:
        claim = {"action": "claim", "reason": "t", "issue": ISSUE}
        self.block(claim)
        until = self.entries()[tc.key(claim)]["until"]
        cont = {"action": "continue", "reason": "t", "pr": 5, "sha": SHA}
        self.assertEqual(self.check(cont, NOW + 5), tc.SKIP)
        entries = self.entries()
        self.assertEqual(list(entries), [tc.key(cont)])
        self.assertEqual(entries[tc.key(cont)]["until"], until)
        self.assertEqual(entries[tc.key(cont)]["from"], [tc.key(claim)])
        # A push clears it.
        self.assertEqual(self.check({**cont, "sha": "c" * 40}, NOW + 6), tc.RUN)

    def test_all_entries_of_the_issue_transfer_together(self) -> None:
        # Codex review on PR 544: a blocked claim and a blocked continue on one
        # issue; the second entry blocked the next PR head again.
        claim = {"action": "claim", "reason": "t", "issue": ISSUE}
        issue_continue = {"action": "continue", "reason": "t", "issue": ISSUE}
        self.block(claim)
        self.block(issue_continue, now=NOW + 1)
        cont = {"action": "continue", "reason": "t", "pr": 5, "sha": SHA}
        self.assertEqual(self.check(cont, NOW + 5), tc.SKIP)
        entries = self.entries()
        self.assertEqual(list(entries), [tc.key(cont)])
        self.assertEqual(
            sorted(entries[tc.key(cont)]["from"]),
            sorted([tc.key(claim), tc.key(issue_continue)]),
        )
        self.assertEqual(entries[tc.key(cont)]["until"], NOW + 1 + tc.DEFAULT_SECONDS)
        self.assertEqual(self.check({**cont, "sha": "c" * 40}, NOW + 6), tc.RUN)

    def test_no_transfer_for_another_issue_or_action(self) -> None:
        self.block({"action": "claim", "reason": "t", "issue": ISSUE[:-1] + "8"})
        cont = {"action": "continue", "reason": "t", "pr": 5, "sha": SHA}
        self.assertEqual(self.check(cont, NOW + 5), tc.RUN)
        self.assertEqual(self.check({**cont, "action": "fix"}, NOW + 5), tc.RUN)

    def test_no_pr_body_read_without_issue_cooldowns(self) -> None:
        cont = {"action": "continue", "reason": "t", "pr": 5, "sha": SHA}
        self.assertEqual(self.check(cont), tc.RUN)
        self.assertEqual(self.reads, [])

    def test_ambiguous_issue_line_does_not_transfer(self) -> None:
        self.block({"action": "continue", "reason": "t", "issue": ISSUE})
        self.github["pulls/5 --jq .body"] = f"Issue: {ISSUE}\nIssue: {ISSUE}\n"
        cont = {"action": "continue", "reason": "t", "pr": 5, "sha": SHA}
        self.assertEqual(self.check(cont, NOW + 5), tc.RUN)

    # Reads and state

    def test_base_read_failure_raises(self) -> None:
        del self.github["git/ref/heads/dev/312-interim"]
        with self.assertRaises(tc.ReadFailed):
            self.check(CONFLICT)
        self.assertFalse(self.seen.exists())
        self.assertFalse((self.state / tc.FILE).exists())

    def test_invalid_state_is_refused(self) -> None:
        (self.state / tc.FILE).write_text(json.dumps({"k": {"at": 1}}))
        with self.assertRaises(ValueError):
            self.check(FIX)

    def test_status_lists_active_cooldowns_with_reasons(self) -> None:
        self.assertEqual(tc.status_lines(self.state, NOW), ["  none"])
        self.block(FIX)
        (line,) = tc.status_lines(self.state, NOW + 1)
        self.assertIn(tc.key(FIX), line)
        self.assertIn("model reported blocked: gate", line)
        self.assertEqual(tc.status_lines(self.state, NOW + 10**6), ["  none"])
        (self.state / tc.FILE).write_text("{")
        self.assertIn("cannot read", tc.status_lines(self.state, NOW)[0])

    def test_registered_agent_id_is_stored(self) -> None:
        with mock.patch.dict(os.environ, {"EPIC_AGENT_ID": "claude-2"}):
            self.block(FIX)
        self.assertEqual(self.entries()[tc.key(FIX)]["by"], "claude-2")


if __name__ == "__main__":
    unittest.main()
