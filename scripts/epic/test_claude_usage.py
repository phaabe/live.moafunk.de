"""Claude usage wait (claude_usage.py): classification, the wait record,
admissions, recovery probes, unsafe stores and processes sharing an account.

Fixed UTC clocks, temporary folders and fake CLI results; no model and no
live state. Vectors from the 677 preparation fixtures
(https://github.com/phaabe/live.moafunk.de/issues/677#issuecomment-6037565808).
"""

from __future__ import annotations

import isolated_env  # noqa: F401  (first: hides live runner state)

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

import claude_usage as cu

HELPER = Path(__file__).resolve().parent / "claude_usage.py"
UTC = timezone.utc
RECEIPT = datetime(2026, 10, 7, 11, 42, tzinfo=UTC)
LIMIT = "You've hit your session limit · resets 3pm (Europe/Berlin)"


def at(text: str) -> datetime:
    return cu.parse_stamp(text)


def envelope(session: str, **fields: Any) -> dict[str, Any]:
    """The observed terminal result (sanitized), for this session."""
    result = {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "terminal_reason": "api_error",
        "api_error_status": 429,
        "result": LIMIT,
        "session_id": session,
        "duration_ms": 849,
        "duration_api_ms": 0,
        "num_turns": 1,
        "total_cost_usd": 0,
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
        "permission_denials": [],
    }
    result.update(fields)
    return result


def success(session: str) -> dict[str, Any]:
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "done",
        "session_id": session,
        "total_cost_usd": 1.5,
        "duration_api_ms": 9000,
        "usage": {"input_tokens": 10, "output_tokens": 20},
        "permission_denials": [],
    }


class ClassifyTest(unittest.TestCase):
    S = str(uuid.uuid4())

    def kind(self, data: Any, receipt: datetime | None = RECEIPT) -> cu.Terminal:
        return cu.classify(data, self.S, receipt)

    def limit(self, text: str, receipt: datetime) -> cu.Terminal:
        return self.kind(envelope(self.S, result=text), receipt)

    def test_observed_result_with_its_receipt_is_a_known_reset(self) -> None:
        got = self.kind(envelope(self.S))
        self.assertEqual(got.kind, "usage_quota")
        self.assertEqual(got.reset, at("2026-10-07T13:00:00Z"))
        self.assertIsNone(got.detail)
        self.assertTrue(got.void)

    def test_unknown_resets_name_their_reason(self) -> None:
        cases = (
            # Fixture dst_ambiguous_local_time: 02:30 happens twice that night.
            (
                "You've hit your session limit · resets 2:30am (Europe/Berlin)",
                "2026-10-24T23:00:00Z",
                "ambiguous_local_time",
            ),
            # 02:30 does not exist on the spring night.
            (
                "You've hit your session limit · resets 2:30am (Europe/Berlin)",
                "2026-03-28T23:30:00Z",
                "nonexistent_local_time",
            ),
            # Fixture past_reset_do_not_infer_tomorrow.
            (LIMIT, "2026-10-07T14:00:00Z", "past_reset"),
            # A reset exactly at the receipt is no future reset either.
            (LIMIT, "2026-10-07T13:00:00Z", "past_reset"),
            # 12am is midnight of the receipt's date, never the next one.
            (
                "You've hit your session limit · resets 12am (Europe/Berlin)",
                "2026-10-06T21:30:00Z",
                "past_reset",
            ),
            (
                "You've hit your session limit · resets 3pm (Mars/Olympus)",
                "2026-10-07T11:42:00Z",
                "unknown_zone",
            ),
        )
        for text, receipt, detail in cases:
            with self.subTest(detail=detail, receipt=receipt):
                got = self.limit(text, at(receipt))
                self.assertEqual(
                    (got.kind, got.reset, got.detail), ("usage_quota", None, detail)
                )

    def test_clock_forms(self) -> None:
        for text, receipt, reset in (
            (
                "resets 12pm (Europe/Berlin)",
                "2026-10-07T08:00:00Z",
                "2026-10-07T10:00:00Z",
            ),
            ("resets 2:30pm (UTC)", "2026-10-07T10:00:00Z", "2026-10-07T14:30:00Z"),
            (
                "resets 11:05am (America/New_York)",
                "2026-10-07T12:00:00Z",
                "2026-10-07T15:05:00Z",
            ),
        ):
            with self.subTest(text=text):
                got = self.limit(f"You've hit your session limit · {text}", at(receipt))
                self.assertEqual(got.reset, at(reset))

    def test_missing_receipt_has_no_reset(self) -> None:
        got = self.kind(envelope(self.S), None)
        self.assertEqual(
            (got.kind, got.reset, got.detail),
            ("usage_quota", None, "missing_authoritative_receipt"),
        )

    def test_other_results_are_no_usage_evidence(self) -> None:
        quoted = success(self.S) | {"result": f"The CLI said: {LIMIT}"}
        for name, data, kind in (
            (
                "529",
                envelope(
                    self.S, result="API Error: 529 Overloaded", api_error_status=529
                ),
                "transient",
            ),
            ("other API error", envelope(self.S, result="API Error: 500"), "transient"),
            ("prefix", envelope(self.S, result=f"Note: {LIMIT}"), "transient"),
            ("suffix", envelope(self.S, result=f"{LIMIT}."), "transient"),
            ("quoted by the model", quoted, "ok"),
            ("is_error as text", envelope(self.S, is_error="true"), "inconclusive"),
            ("is_error as 1", envelope(self.S, is_error=1), "inconclusive"),
            (
                "other terminal reason",
                envelope(self.S, terminal_reason="max_turns"),
                "inconclusive",
            ),
            ("other session", envelope(str(uuid.uuid4())), "inconclusive"),
            (
                "no session",
                {k: v for k, v in envelope(self.S).items() if k != "session_id"},
                "inconclusive",
            ),
            ("not a result", envelope(self.S, type="assistant"), "inconclusive"),
            ("not an object", [envelope(self.S)], "inconclusive"),
            ("no file", None, "inconclusive"),
        ):
            with self.subTest(name):
                self.assertEqual(self.kind(data).kind, kind)

    def test_unreadable_result_file_is_inconclusive(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "result.json"
            self.assertIsNone(cu.read_terminal(path))
            path.write_text("")
            self.assertIsNone(cu.read_terminal(path))
            path.write_text("{not json")
            self.assertIsNone(cu.read_terminal(path))

    def test_void_only_without_model_work(self) -> None:
        def usage(**fields: Any) -> dict[str, Any]:
            return {**envelope(self.S)["usage"], **fields}

        self.assertTrue(self.kind(envelope(self.S)).void)
        for name, fields in (
            ("input tokens", {"usage": usage(input_tokens=5)}),
            ("output tokens", {"usage": usage(output_tokens=1)}),
            ("cache tokens", {"usage": usage(cache_read_input_tokens=100)}),
            ("tokens as bool", {"usage": usage(input_tokens=False)}),
            ("no usage", {"usage": None}),
            ("cost", {"total_cost_usd": 0.01}),
            ("API time", {"duration_api_ms": 15}),
            ("denied tool call", {"permission_denials": [{"tool_name": "Bash"}]}),
            ("no denial list", {"permission_denials": None}),
        ):
            with self.subTest(name):
                got = self.kind(envelope(self.S, **fields))
                self.assertEqual((got.kind, got.void), ("usage_quota", False))


class StoreCase(unittest.TestCase):
    """A store in a temporary state root; admissions with real descriptors."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="claude-usage-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.root = self.tmp / "state"
        self.root.mkdir(mode=0o700)
        self.env = {"EPIC_QUOTA_DIR": str(self.root)}
        self.store = cu.Store.from_env(self.env)
        self.store.create()
        self.held: dict[str, int] = {}

    def descriptors(self, admission: str) -> tuple[int, int]:
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        fd = os.open(self.store.lock_file(admission), flags, 0o600)
        probe = os.open(self.store.folder / cu.PROBE_LOCK, flags, 0o600)
        for one in (fd, probe):
            self.addCleanup(self.close, one)
        return fd, probe

    def close(self, fd: int) -> None:
        try:
            os.close(fd)
        except OSError:
            pass

    def admit(self, now: datetime) -> tuple[int, str, str]:
        """(code, word or reason, admission ID). The descriptors stay open
        (the admission lives) until crash() or the end of the test."""
        admission = str(uuid.uuid4())
        fd, probe = self.descriptors(admission)
        code, word = cu.admit(self.store, admission, fd, probe, now)
        if code == cu.OPEN:
            self.held[admission] = fd
            self.held[admission + ":probe"] = probe
        else:
            self.close(fd)
            self.close(probe)
        return code, word, admission

    def crash(self, admission: str) -> None:
        """The wrapper dies: its descriptors close without a result."""
        self.close(self.held.pop(admission))
        self.close(self.held.pop(admission + ":probe"))

    def finish(
        self, admission: str, data: Any, now: datetime, receipt: datetime | None = None
    ) -> cu.Terminal | None:
        def terminal(one: str) -> cu.Terminal:
            if isinstance(data, cu.Terminal):
                return data
            return cu.classify(data, one, receipt or now)

        got = cu.finish(self.store, admission, terminal, receipt or now, now)
        for key in (admission, admission + ":probe"):
            if key in self.held:
                self.close(self.held.pop(key))
        return got

    def quota(
        self, now: datetime, receipt: datetime | None = None, **fields: Any
    ) -> str:
        code, word, admission = self.admit(now)
        self.assertEqual(code, cu.OPEN, word)
        self.finish(admission, envelope(admission, **fields), now, receipt)
        return word

    def state(self) -> dict[str, Any]:
        return self.store.load()

    def wait(self) -> dict[str, Any]:
        wait = self.state()["wait"]
        assert wait is not None
        return wait


class WaitTest(StoreCase):
    def test_known_reset_waits_until_reset_plus_a_minute(self) -> None:
        self.quota(RECEIPT)
        wait = self.wait()
        self.assertEqual(
            {
                k: wait[k]
                for k in ("reason", "reset_at", "retry_at", "source", "fallback_count")
            },
            {
                "reason": "session_limit",
                "reset_at": "2026-10-07T13:00:00Z",
                "retry_at": "2026-10-07T13:01:00Z",
                "source": "cli-clock",
                "fallback_count": 0,
            },
        )
        self.assertEqual(cu.check(self.store, at("2026-10-07T13:00:59Z"))[0], cu.WAIT)
        self.assertEqual(
            cu.check(self.store, at("2026-10-07T13:01:00Z")), (cu.OPEN, None)
        )

    def test_unknown_reset_falls_back_fifteen_minutes_from_the_receipt(self) -> None:
        receipt = at("2026-10-07T14:00:00Z")
        self.quota(receipt + timedelta(seconds=5), receipt)
        wait = self.wait()
        self.assertEqual(
            (wait["retry_at"], wait["source"], wait["detail"], wait["fallback_count"]),
            ("2026-10-07T14:15:00Z", "fallback", "past_reset", 1),
        )

    def test_missing_receipt_falls_back_from_the_store_time(self) -> None:
        now = at("2026-10-07T11:42:00Z")
        code, _, admission = self.admit(now)
        cu.finish(
            self.store,
            admission,
            lambda one: cu.classify(envelope(one), one, None),
            None,
            now,
        )
        wait = self.wait()
        self.assertEqual(
            (wait["retry_at"], wait["detail"]),
            ("2026-10-07T11:57:00Z", "missing_authoritative_receipt"),
        )

    def test_an_unexpired_wait_is_extended_never_shortened(self) -> None:
        now = at("2026-10-07T11:00:00Z")
        first, second = self.admit(now)[2], self.admit(now)[2]
        late = "You've hit your session limit · resets 4pm (Europe/Berlin)"
        self.finish(first, envelope(first, result=late), now)
        self.assertEqual(self.wait()["retry_at"], "2026-10-07T14:01:00Z")
        generation = self.state()["generation"]
        self.finish(second, envelope(second), now)  # an earlier reset
        self.assertEqual(self.wait()["retry_at"], "2026-10-07T14:01:00Z")
        self.assertEqual(self.state()["generation"], generation)
        third = self.admit(now)
        self.assertEqual(third[0], cu.WAIT)

    def test_later_evidence_extends_the_wait(self) -> None:
        now = at("2026-10-07T11:00:00Z")
        first, second = self.admit(now)[2], self.admit(now)[2]
        self.finish(first, envelope(first), now)
        later = "You've hit your session limit · resets 5pm (Europe/Berlin)"
        self.finish(second, envelope(second, result=later), now)
        self.assertEqual(self.wait()["retry_at"], "2026-10-07T15:01:00Z")

    def test_fallback_grows_only_on_recovery_probes(self) -> None:
        past = "You've hit your session limit · resets 1am (Europe/Berlin)"
        now = at("2026-10-07T10:00:00Z")
        early = self.admit(now)[2]  # admitted before the wait: no probe
        self.quota(now, result=past)
        self.assertEqual(
            (self.wait()["retry_at"], self.wait()["fallback_count"]),
            ("2026-10-07T10:15:00Z", 1),
        )
        # More evidence of the same wait does not move to the next step.
        self.finish(early, envelope(early, result=past), now + timedelta(minutes=1))
        self.assertEqual(
            (self.wait()["retry_at"], self.wait()["fallback_count"]),
            ("2026-10-07T10:16:00Z", 1),
        )
        steps = []
        for _ in range(4):
            now = at(self.wait()["retry_at"])
            self.assertEqual(self.quota(now, result=past), "probe")
            wait = self.wait()
            steps.append((at(wait["retry_at"]) - now, wait["fallback_count"]))
        minutes = [(int(d.total_seconds() // 60), n) for d, n in steps]
        self.assertEqual(minutes, [(30, 2), (60, 3), (60, 4), (60, 5)])

    def test_skipped_ticks_change_nothing(self) -> None:
        self.quota(RECEIPT)
        before = self.store.state.read_bytes()
        for minutes in range(0, 60, 5):
            now = RECEIPT + timedelta(minutes=minutes)
            self.assertEqual(cu.check(self.store, now)[0], cu.WAIT)
            self.assertEqual(self.admit(now)[0], cu.WAIT)
        self.assertEqual(self.store.state.read_bytes(), before)

    def test_a_result_is_stored_once(self) -> None:
        code, _, admission = self.admit(RECEIPT)
        self.assertEqual(
            self.finish(admission, envelope(admission), RECEIPT).kind, "usage_quota"
        )
        before = self.store.state.read_bytes()
        self.assertIsNone(
            self.finish(admission, envelope(admission), RECEIPT + timedelta(hours=1))
        )
        self.assertEqual(self.store.state.read_bytes(), before)

    def test_one_recovery_probe_then_a_success_clears_the_wait(self) -> None:
        self.quota(RECEIPT)
        later = at("2026-10-07T13:01:00Z")
        code, word, probe = self.admit(later)
        self.assertEqual((code, word), (cu.OPEN, "probe"))
        other = self.admit(later + timedelta(seconds=30))
        self.assertEqual(other[0], cu.WAIT)
        self.assertIn("recovery probe", other[1])
        self.finish(probe, success(probe), later + timedelta(minutes=5))
        self.assertIsNone(self.state()["wait"])
        self.assertEqual(
            self.admit(later + timedelta(minutes=6))[:2], (cu.OPEN, "normal")
        )

    def test_an_inconclusive_probe_moves_the_next_probe_three_minutes(self) -> None:
        for name, result in (
            ("529", lambda one: envelope(one, result="API Error: 529 Overloaded")),
            ("no result", lambda one: None),
            ("stopped tick", lambda one: cu.Terminal("inconclusive")),
        ):
            with self.subTest(name):
                self.setUp()
                self.quota(RECEIPT)
                start = at("2026-10-07T13:01:00Z")
                code, word, probe = self.admit(start)
                self.assertEqual(word, "probe")
                self.finish(probe, result(probe), start + timedelta(seconds=40))
                state = self.state()
                self.assertEqual(state["probe_retry_at"], "2026-10-07T13:04:40Z")
                self.assertEqual(state["wait"]["fallback_count"], 0)
                self.assertEqual(self.admit(start + timedelta(minutes=3))[0], cu.WAIT)
                self.assertEqual(self.admit(start + timedelta(seconds=220))[1], "probe")

    def test_a_withdrawn_probe_frees_the_probe_at_once(self) -> None:
        self.quota(RECEIPT)
        start = at("2026-10-07T13:01:00Z")
        probe = self.admit(start)[2]
        self.finish(probe, cu.Terminal("withdrawn"), start)
        self.assertIsNone(self.state()["probe_retry_at"])
        self.assertEqual(self.admit(start)[1], "probe")

    def test_a_success_from_before_a_newer_wait_never_clears_it(self) -> None:
        now = at("2026-10-07T11:00:00Z")
        old = self.admit(now)[2]  # generation 0, before the wait
        self.quota(now)  # generation 1
        expired = at("2026-10-07T13:05:00Z")
        self.finish(old, success(old), expired)
        self.assertIsNotNone(self.state()["wait"])
        self.assertEqual(self.admit(expired)[1], "probe")

    def test_quota_during_recovery_keeps_the_probe_from_clearing(self) -> None:
        now = at("2026-10-07T11:00:00Z")
        old = self.admit(now)[2]
        self.quota(now)
        start = at("2026-10-07T13:01:00Z")
        probe = self.admit(start)[2]
        # The old session reports the limit again while the probe runs.
        late = "You've hit your session limit · resets 4pm (Europe/Berlin)"
        self.finish(
            old, envelope(old, result=late), start + timedelta(seconds=10), start
        )
        self.finish(probe, success(probe), start + timedelta(minutes=2))
        self.assertEqual(self.wait()["retry_at"], "2026-10-07T14:01:00Z")

    def test_separate_accounts_do_not_share_a_wait(self) -> None:
        self.quota(RECEIPT)
        other = cu.Store.from_env({**self.env, "EPIC_CLAUDE_ACCOUNT_KEY": "second"})
        self.assertEqual(cu.check(other, RECEIPT), (cu.OPEN, None))
        self.assertEqual(cu.check(self.store, RECEIPT)[0], cu.WAIT)
        self.assertNotEqual(other.folder, self.store.folder)


class AdmissionTest(StoreCase):
    def test_a_running_admission_does_not_block_another(self) -> None:
        first = self.admit(RECEIPT)
        second = self.admit(RECEIPT)
        self.assertEqual(
            (first[:2], second[:2]), ((cu.OPEN, "normal"), (cu.OPEN, "normal"))
        )
        lines = cu.status_lines(self.store, RECEIPT)
        self.assertEqual(sum("running since" in line for line in lines), 2)

    def test_a_crashed_admission_blocks_until_repaired(self) -> None:
        code, _, admission = self.admit(RECEIPT)
        self.crash(admission)
        code, reason = cu.check(self.store, RECEIPT)
        self.assertEqual(code, cu.WAIT)
        self.assertIn(f"unresolved admission {admission}", reason or "")
        self.assertEqual(self.admit(RECEIPT)[0], cu.WAIT)
        lines = cu.status_lines(self.store, RECEIPT)
        self.assertIn(f"claude_usage.py repair --id {admission}", "\n".join(lines))
        inconclusive = lambda one: cu.Terminal("inconclusive")  # noqa: E731
        code, line = cu.repair(self.store, admission, inconclusive, None, RECEIPT)
        self.assertEqual(code, cu.OPEN, line)
        self.assertFalse(self.store.lock_file(admission).exists())
        self.assertEqual(self.admit(RECEIPT)[:2], (cu.OPEN, "normal"))

    def test_repair_refuses_a_running_admission(self) -> None:
        admission = self.admit(RECEIPT)[2]
        code, line = cu.repair(
            self.store,
            admission,
            lambda one: cu.Terminal("inconclusive"),
            None,
            RECEIPT,
        )
        self.assertEqual(code, cu.WAIT)
        self.assertIn(admission, self.state()["admissions"])

    def test_repair_can_use_the_leftover_result(self) -> None:
        admission = self.admit(RECEIPT)[2]
        self.crash(admission)
        result = self.tmp / "result.json"
        result.write_text(json.dumps(envelope(admission)))
        code, _ = cu.repair(
            self.store,
            admission,
            lambda one: cu.classify(cu.read_terminal(result), one, RECEIPT),
            RECEIPT,
            RECEIPT,
        )
        self.assertEqual(code, cu.OPEN)
        self.assertEqual(self.wait()["retry_at"], "2026-10-07T13:01:00Z")

    def test_a_crashed_probe_never_gives_a_second_probe(self) -> None:
        self.quota(RECEIPT)
        start = at("2026-10-07T13:01:00Z")
        probe = self.admit(start)[2]
        self.crash(probe)
        for minutes in (0, 5, 60, 600):
            code, reason, _ = self.admit(start + timedelta(minutes=minutes))
            self.assertEqual(code, cu.WAIT)
            self.assertIn("unresolved admission", reason)
        cu.repair(
            self.store, probe, lambda one: cu.Terminal("inconclusive"), None, start
        )
        self.assertEqual(self.admit(start + timedelta(seconds=60))[0], cu.WAIT)
        self.assertEqual(self.admit(start + timedelta(seconds=180))[1], "probe")

    def test_lock_files_live_only_with_their_admission(self) -> None:
        self.quota(RECEIPT)
        refused = self.admit(RECEIPT)
        self.assertEqual(refused[0], cu.WAIT)
        self.assertFalse(self.store.lock_file(refused[2]).exists())
        start = at("2026-10-07T13:01:00Z")
        probe = self.admit(start)[2]
        self.assertTrue(self.store.lock_file(probe).exists())
        self.finish(probe, success(probe), start)
        self.assertFalse(self.store.lock_file(probe).exists())

    def test_an_admission_ID_is_used_once(self) -> None:
        admission = self.admit(RECEIPT)[2]
        flags = os.O_WRONLY | os.O_APPEND
        fd = os.open(self.store.lock_file(admission), flags)
        probe = os.open(self.store.folder / cu.PROBE_LOCK, flags)
        self.addCleanup(os.close, fd)
        self.addCleanup(os.close, probe)
        with self.assertRaises(cu.BadInput):
            cu.admit(self.store, admission, fd, probe, RECEIPT)

    def test_descriptors_must_be_the_named_files(self) -> None:
        admission = str(uuid.uuid4())
        fd, probe = self.descriptors(admission)
        other = str(uuid.uuid4())
        with self.assertRaises(cu.UsageError):
            cu.admit(self.store, other, fd, probe, RECEIPT)
        # A symlink in the admission's place.
        target = self.tmp / "elsewhere.lock"
        target.write_text("")
        target.chmod(0o600)
        link = self.store.lock_file(other)
        link.symlink_to(target)
        held = os.open(link, os.O_WRONLY | os.O_APPEND)
        self.addCleanup(os.close, held)
        with self.assertRaises(cu.UsageError):
            cu.admit(self.store, other, held, probe, RECEIPT)


class SafetyTest(StoreCase):
    def test_bad_keys_and_roots_are_refused(self) -> None:
        for key in ("", "a b", "../x", "x/y", "k" * 65, "ü"):
            with self.subTest(key=key), self.assertRaises(cu.BadInput):
                cu.Store.from_env({**self.env, "EPIC_CLAUDE_ACCOUNT_KEY": key})
        with self.assertRaises(cu.BadInput):
            cu.Store.from_env({"EPIC_QUOTA_DIR": "relative/state"})
        self.assertEqual(
            cu.Store.from_env({**self.env, "EPIC_CLAUDE_ACCOUNT_KEY": "k" * 64}).key,
            "k" * 64,
        )

    def unusable(self, what: str) -> None:
        with self.subTest(what), self.assertRaises(cu.UsageError):
            cu.check(cu.Store.from_env(self.env), RECEIPT)

    def test_unsafe_folders_are_refused(self) -> None:
        folder = self.store.folder
        folder.chmod(0o755)
        self.unusable("key folder mode")
        folder.chmod(0o700)
        moved = self.tmp / "moved"
        folder.rename(moved)
        folder.symlink_to(moved)
        self.unusable("key folder link")
        folder.unlink()
        moved.rename(folder)
        self.assertEqual(cu.check(self.store, RECEIPT), (cu.OPEN, None))
        self.store.admissions.chmod(0o777)
        self.unusable("admissions mode")
        self.store.admissions.chmod(0o700)
        self.root.chmod(0o770)
        with self.assertRaises(cu.UsageError):
            cu.Store.from_env(self.env)
        self.root.chmod(0o700)

    def test_group_writable_parents_are_refused_unless_sticky(self) -> None:
        shared = self.tmp / "shared"
        shared.mkdir()
        root = shared / "state"
        root.mkdir(mode=0o700)
        shared.chmod(0o777)
        self.addCleanup(shared.chmod, 0o700)
        with self.assertRaises(cu.UsageError):
            cu.Store.from_env({"EPIC_QUOTA_DIR": str(root)})
        shared.chmod(0o1777)
        cu.Store.from_env({"EPIC_QUOTA_DIR": str(root)}).create()

    def test_bad_records_are_refused_and_kept(self) -> None:
        self.quota(RECEIPT)
        path = self.store.state
        good = path.read_bytes()
        for name, content, mode in (
            ("not JSON", b"{", 0o600),
            ("unknown version", good.replace(b'"version": 1', b'"version": 2'), 0o600),
            (
                "extra field",
                good.replace(b'"version": 1', b'"version": 1, "x": 1'),
                0o600,
            ),
            ("bad time", good.replace(b"2026-10-07T13:01:00Z", b"tomorrow"), 0o600),
            ("readable by others", good, 0o644),
        ):
            with self.subTest(name):
                path.write_bytes(content)
                path.chmod(mode)
                with self.assertRaises(cu.UsageError):
                    cu.check(self.store, RECEIPT)
                with self.assertRaises(cu.UsageError):
                    self.admit(RECEIPT)
                self.assertEqual(path.read_bytes(), content)
        path.write_bytes(good)
        path.chmod(0o600)
        link = self.tmp / "second-name"
        os.link(path, link)
        with self.assertRaises(cu.UsageError):
            cu.check(self.store, RECEIPT)
        link.unlink()
        moved = self.tmp / "state.json"
        path.rename(moved)
        path.symlink_to(moved)
        with self.assertRaises(cu.UsageError):
            cu.check(self.store, RECEIPT)

    def test_a_missing_root_has_no_wait_and_creates_nothing(self) -> None:
        missing = self.tmp / "missing"
        store = cu.Store.from_env({"EPIC_QUOTA_DIR": str(missing)})
        self.assertEqual(cu.check(store, RECEIPT), (cu.OPEN, None))
        with self.assertRaises(cu.UsageError):
            store.create()
        self.assertFalse(missing.exists())


class PersistenceTest(StoreCase):
    def test_a_failed_admission_write_starts_no_model(self) -> None:
        admission = str(uuid.uuid4())
        fd, probe = self.descriptors(admission)
        with mock.patch.object(
            cu.os, "replace", side_effect=OSError(30, "Read-only file system")
        ):
            with self.assertRaises(cu.UsageError):
                cu.admit(self.store, admission, fd, probe, RECEIPT)
        self.assertEqual(self.state()["admissions"], {})
        self.assertFalse(self.store.lock_file(admission).exists())
        self.assertEqual(sorted(p.name for p in self.store.folder.glob(".state-*")), [])

    def test_a_failed_result_write_leaves_the_admission_unresolved(self) -> None:
        admission = self.admit(RECEIPT)[2]
        with mock.patch.object(
            cu.os, "replace", side_effect=OSError(28, "No space left on device")
        ):
            with self.assertRaises(cu.UsageError):
                self.finish(admission, envelope(admission), RECEIPT)
        state = self.state()
        self.assertIsNone(state["wait"])
        self.assertIn(admission, state["admissions"])
        # The tick ends with the error. The wait was not written, but the
        # unresolved admission still stops every model.
        self.crash(admission)
        self.assertEqual(cu.check(self.store, RECEIPT)[0], cu.WAIT)
        # After a restart (a new process) it still waits until repaired.
        out = subprocess.run(
            [sys.executable, str(HELPER), "check"], env={**os.environ, **self.env},
            capture_output=True, text=True, timeout=30,
        )  # fmt: skip
        self.assertEqual(out.returncode, cu.WAIT, out.stderr)
        self.assertIn("unresolved admission", out.stdout)


# One bash wrapper per process: open both lock files, admit, then wait for a
# go file and store the result named by the test. Like the model in the tick,
# the waiting child never gets the usage locks (18>&- 19>&-).
WRAPPER = r"""
set -euo pipefail
helper=$1; id=$2; work=$3
dir=$(python3 "$helper" path)
exec 18>> "$dir/probe.lock" 19>> "$dir/admissions/$id.lock"
code=0
python3 "$helper" admit --id "$id" --fd 19 --probe-fd 18 > "$work/$id.admit" || code=$?
echo "$code" > "$work/$id.code"
if [[ "$code" != 0 ]]; then exit 0; fi
while [[ ! -e "$work/$id.go" ]]; do sleep 0.05 18>&- 19>&-; done
python3 "$helper" finish --id "$id" --result-file "$work/$id.result" \
    --receipt "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$work/$id.finish" 2> "$work/$id.log"
"""


class ProcessTest(StoreCase):
    def start(self, admission: str, key: str | None = None) -> subprocess.Popen[bytes]:
        env = {**os.environ, **self.env}
        if key is not None:
            env["EPIC_CLAUDE_ACCOUNT_KEY"] = key
        process = subprocess.Popen(
            ["/bin/bash", "-c", WRAPPER, "_", str(HELPER), admission, str(self.tmp)],
            env=env,
            start_new_session=True,
        )
        self.addCleanup(self.stop, process)
        return process

    def stop(self, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            self.kill(process)

    def kill(self, process: subprocess.Popen[bytes]) -> None:
        """SIGKILL the wrapper's whole process group and return only when every
        member has exited, so no child of it can still hold a usage lock."""
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        process.wait(timeout=10)
        deadline = time.monotonic() + 10
        while True:
            try:
                os.killpg(process.pid, 0)
            except (ProcessLookupError, PermissionError):
                return  # gone; macOS refuses a group of zombies with EPERM
            if time.monotonic() > deadline:
                raise AssertionError(f"process group {process.pid} did not exit")
            time.sleep(0.01)

    def lock_taken(self, path: Path) -> bool:
        with path.open("a") as lock:
            return not cu.take(lock.fileno())  # closing drops the test lock

    def code(self, admission: str) -> int:
        path = self.tmp / f"{admission}.code"
        deadline = time.monotonic() + 30
        while not path.exists() or not path.read_text().strip():
            if time.monotonic() > deadline:
                raise AssertionError(f"{admission} did not admit")
            time.sleep(0.05)
        return int(path.read_text())

    def go(self, admission: str, data: dict[str, Any]) -> None:
        (self.tmp / f"{admission}.result").write_text(json.dumps(data))
        (self.tmp / f"{admission}.go").write_text("")

    def test_runners_sharing_an_account_share_the_wait(self) -> None:
        first, second = str(uuid.uuid4()), str(uuid.uuid4())
        one = self.start(first)
        self.assertEqual(self.code(first), 0)
        # A second runner is admitted while the first model runs.
        two = self.start(second)
        self.assertEqual(self.code(second), 0)
        self.go(
            first,
            envelope(first, result="You've hit your session limit · resets 1am (UTC)"),
        )
        self.assertEqual(one.wait(timeout=30), 0)
        self.assertEqual(
            (self.tmp / f"{first}.finish").read_text().split()[0], "usage_quota"
        )
        third = str(uuid.uuid4())
        self.start(third).wait(timeout=30)
        self.assertEqual(self.code(third), cu.WAIT)
        self.assertIn("session_limit until", (self.tmp / f"{third}.admit").read_text())
        # Another account is not affected.
        fourth = str(uuid.uuid4())
        self.start(fourth, key="second")
        self.assertEqual(self.code(fourth), 0)
        self.go(fourth, success(fourth))
        self.go(second, success(second))
        self.assertEqual(two.wait(timeout=30), 0)
        self.assertIsNotNone(self.state()["wait"])

    def test_concurrent_admissions_and_results_lose_no_update(self) -> None:
        ids = [str(uuid.uuid4()) for _ in range(8)]
        processes = [self.start(one) for one in ids]
        for one in ids:
            self.assertEqual(self.code(one), 0)
            self.go(one, success(one))
        for process in processes:
            self.assertEqual(process.wait(timeout=60), 0)
        self.assertEqual(self.state()["admissions"], {})
        self.assertEqual(sorted(p.name for p in self.store.admissions.iterdir()), [])

    def test_a_killed_probe_holder_blocks_until_repair(self) -> None:
        now = datetime.now(UTC)
        data = cu.empty()
        data["generation"] = 1
        data["wait"] = {
            "reason": "session_limit",
            "observed_at": cu.stamp(now - timedelta(hours=1)),
            "reset_at": None,
            "retry_at": cu.stamp(now - timedelta(minutes=1)),
            "source": "fallback",
            "fallback_count": 1,
            "detail": "past_reset",
        }
        self.store.save(data)
        probe = str(uuid.uuid4())
        holder = self.start(probe)
        self.assertEqual(self.code(probe), 0)
        self.assertIn(
            "as the recovery probe", (self.tmp / f"{probe}.admit").read_text()
        )
        self.kill(holder)
        # Nothing of the killed wrapper runs, so nothing holds its locks.
        self.assertFalse(self.store.alive(probe))
        self.assertFalse(self.lock_taken(self.store.folder / cu.PROBE_LOCK))
        later = str(uuid.uuid4())
        self.start(later).wait(timeout=30)
        self.assertEqual(self.code(later), cu.WAIT)
        self.assertIn("unresolved admission", (self.tmp / f"{later}.admit").read_text())
        env = {**os.environ, **self.env}
        repaired = subprocess.run(
            [sys.executable, str(HELPER), "repair", "--id", probe],
            env=env, capture_output=True, text=True, timeout=30,
        )  # fmt: skip
        self.assertEqual(repaired.returncode, 0, repaired.stderr)
        status = subprocess.run(
            [sys.executable, str(HELPER), "status"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertIn("next probe after", status.stdout)


if __name__ == "__main__":
    unittest.main()
