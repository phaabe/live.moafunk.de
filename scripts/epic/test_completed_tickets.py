"""Ticket dependencies need a closed-as-completed issue (EPIC_REQUIRE_COMPLETED_TICKETS).

https://github.com/phaabe/live.moafunk.de/issues/520. Each rule is tested with
the switch off (old behavior) and on.

Run: python3 -m unittest discover -s scripts/epic
"""

from __future__ import annotations

import isolated_env  # noqa: F401  (first: hides live runner state)

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import github_state as gs
import monitor
import next_action as na
from test_github_state import (
    ISSUES,
    REPO,
    Env,
    FakeReader,
    GitHubRepo,
    fixture,
)

HERE = Path(__file__).resolve().parent
PREREQ = f"{ISSUES}/488"
SUCCESSOR = f"{ISSUES}/503"


def item(
    n: int,
    status: str | None,
    executor: str = "Claude",
    state: str | None = "open",
    reason: str | None = None,
    readiness: str = "",
) -> dict[str, Any]:
    content: dict[str, Any] = {
        "type": "Issue",
        "number": n,
        "url": f"{ISSUES}/{n}",
        "updated_at": "2026-09-29T09:00:00Z",
    }
    if state is not None:
        content.update({"state": state, "state_reason": reason})
    return {
        "content": content,
        "status": status,
        "executor": executor,
        "wave": "1",
        "labels": [],
        "readiness": readiness,
    }


def successor(**kw: Any) -> dict[str, Any]:
    return item(503, "Ready", readiness=f"**Ready:** Start after {PREREQ}.", **kw)


def state(
    *items: dict[str, Any],
    merged: tuple[str, ...] = (),
    tickets: dict[str, Any] | None = None,
) -> dict[str, Any]:
    found: dict[str, Any] = {
        "prs": [],
        "items": list(items),
        "linked_labels": {},
        "merged_prs": [{"number": 600 + i, "body": b} for i, b in enumerate(merged)],
        "batch_order": [],
    }
    if tickets is not None:
        found["tickets"] = tickets
    return found


def with_tickets(s: dict[str, Any], read: Any = None) -> dict[str, Any]:
    """The `tickets` map both readers build, from the state's own board items."""
    s["tickets"] = na.read_tickets(
        s["items"], read or (lambda _: {"problem": "not stubbed"})
    )
    return s


def first(s: dict[str, Any], on: bool, agent: str = "Claude") -> na.Action:
    return na.decide(agent, s, include_waiting=True, completed_tickets=on)[0]


def kinds(s: dict[str, Any], on: bool) -> list[tuple[str, str | None]]:
    acts = na.decide("Claude", s, include_waiting=True, completed_tickets=on)
    return [(a.action, a.issue) for a in acts]


class Switch(unittest.TestCase):
    def test_values(self) -> None:
        self.assertFalse(na.completed_tickets({}))
        self.assertFalse(na.completed_tickets({na.COMPLETED_TICKETS_ENV: "0"}))
        self.assertTrue(na.completed_tickets({na.COMPLETED_TICKETS_ENV: "1"}))
        for bad in ("", "true", "yes", " 1", "2"):
            with self.assertRaises(na.SettingError, msg=bad):
                na.completed_tickets({na.COMPLETED_TICKETS_ENV: bad})

    def run_main(self, value: str, *args: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            s = state(
                item(488, "Done", state="closed", reason="completed"), successor()
            )
            path.write_text(json.dumps(s))
            env = {**os.environ, na.COMPLETED_TICKETS_ENV: value, "HOME": tmp}
            env.pop("EPIC_SHARED_READER", None)
            return subprocess.run(
                [sys.executable, str(HERE / "next_action.py"), *args]
                + ["--state-file", str(path)],
                capture_output=True,
                text=True,
                env=env,
                cwd=HERE,
            )

    def test_bad_value_exits_2_before_selection(self) -> None:
        out = self.run_main("yes", "--agent", "claude")
        self.assertEqual(out.returncode, 2, out.stderr)
        self.assertIn(na.COMPLETED_TICKETS_ENV, out.stderr)
        self.assertEqual(out.stdout, "")

    def test_status_names_the_mode(self) -> None:
        for value, word in (("0", "off"), ("1", "on")):
            out = self.run_main(value, "--status")
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn(
                f"Completed tickets rule ({na.COMPLETED_TICKETS_ENV}): {word}",
                out.stdout,
            )

    def test_on_without_ticket_data_fails_closed(self) -> None:
        # A state file read with the switch off has no `tickets`.
        out = self.run_main("1", "--agent", "claude")
        self.assertEqual(json.loads(out.stdout)["action"], "idle")


class Completion(unittest.TestCase):
    def test_two_prs_one_merged_blocks_only_when_on(self) -> None:
        merged = (f"Executor: Claude\nIssue: {PREREQ}",)
        s = with_tickets(state(item(488, "In review"), successor(), merged=merged))
        self.assertEqual(first(s, on=False).action, "claim")  # old rule
        act = first(s, on=True)
        self.assertEqual(act.action, "wait")
        self.assertIn(f"{PREREQ} (open)", act.reason)

    def test_completed_unlocks_whatever_the_board_says(self) -> None:
        for status in ("Done", "In review", "Ready", None):
            s = with_tickets(
                state(
                    item(488, status, state="closed", reason="completed"), successor()
                )
            )
            self.assertEqual(first(s, on=True).action, "claim", status)

    def test_completed_off_the_board_unlocks(self) -> None:
        read = {488: {"state": "closed", "state_reason": "completed"}}
        s = with_tickets(state(successor()), read.__getitem__)
        self.assertEqual(first(s, on=True).action, "claim")
        # Old rule: no merged PR names it, so it waits.
        self.assertEqual(first(s, on=False).action, "wait")

    def test_open_but_done_stays_blocked_with_a_warning(self) -> None:
        s = with_tickets(state(item(488, "Done"), successor()))
        act = first(s, on=True)
        self.assertEqual(act.action, "wait")
        self.assertEqual(act.warnings, [f"{PREREQ} is open; board Status is Done"])

    def test_other_closure_reasons_block(self) -> None:
        for reason in ("not_planned", "duplicate", "reopened", None):
            s = with_tickets(
                state(item(488, "Done", state="closed", reason=reason), successor())
            )
            act = first(s, on=True)
            self.assertEqual(act.action, "wait", reason)
            self.assertIn(
                f"{PREREQ} (closed as {reason or 'unknown reason'})", act.reason
            )

    def test_reopened_after_merge_blocks_new_claims_only(self) -> None:
        merged = (f"Executor: Codex\nIssue: {PREREQ}",)
        reopened = item(488, "In progress", executor="Codex", reason="reopened")
        ready = with_tickets(state(reopened, successor(), merged=merged))
        self.assertEqual(first(ready, on=True).action, "wait")
        running = item(503, "In progress", readiness=f"Start after {PREREQ}.")
        busy = with_tickets(state(reopened, running, merged=merged))
        self.assertEqual(first(busy, on=True).action, "continue")

    def test_failed_or_missing_reads_never_complete(self) -> None:
        cases = {
            "not read": None,
            "not readable (HTTP 404); fix the dependency": na.missing_ticket(404),
            "not readable (HTTP 410); fix the dependency": na.missing_ticket(410),
            "state unknown": {"state": None},
        }
        for reason, ticket in cases.items():
            tickets = {} if ticket is None else {"488": ticket}
            s = state(successor(), tickets=tickets)
            act = first(s, on=True)
            self.assertEqual(act.action, "wait", reason)
            self.assertIn(f"{PREREQ} ({reason})", act.reason)

    def test_a_missing_prerequisite_blocks_only_its_successors(self) -> None:
        other = item(504, "Ready", readiness="Ready.")
        s = with_tickets(state(successor(), other), lambda _: na.missing_ticket(404))
        got = kinds(s, on=True)
        self.assertIn(("claim", f"{ISSUES}/504"), got)
        self.assertIn(("wait", SUCCESSOR), got)

    def test_moved_issue_never_counts(self) -> None:
        moved = {
            "number": 12,
            "state": "closed",
            "state_reason": "completed",
            "repository_url": "https://api.github.com/repos/phaabe/other",
        }
        ticket = na.ticket_from_issue(488, moved)
        self.assertIn("moved to phaabe/other#12", ticket["problem"])
        s = state(successor(), tickets={"488": ticket})
        self.assertIn("update the dependency URL", first(s, on=True).reason)

    def test_same_repo_in_other_case_is_not_moved(self) -> None:
        issue = {
            "number": 488,
            "state": "closed",
            "state_reason": "completed",
            "repository_url": f"https://api.github.com/repos/{na.REPO.upper()}",
        }
        self.assertEqual(
            na.ticket_from_issue(488, issue),
            {"state": "closed", "state_reason": "completed"},
        )

    def test_completed_but_not_done_claims_with_a_warning(self) -> None:
        s = with_tickets(
            state(
                item(488, "In review", state="closed", reason="completed"), successor()
            )
        )
        act = first(s, on=True)
        self.assertEqual(act.action, "claim")
        self.assertEqual(
            act.warnings,
            [f"{PREREQ} is closed as completed; board Status is In review"],
        )
        # Warnings stay out of the runner's action JSON.
        self.assertNotIn("warnings", json.loads(act.to_json()))

    def test_leaf_ids_keep_their_rule(self) -> None:
        leaf = item(503, "Ready", readiness="**Ready:** Start after B1.1.6.")
        for on in (False, True):
            self.assertEqual(first(state(leaf), on).action, "wait")
            merged = state(leaf, merged=("Leaf IDs: B1.1.6",))
            self.assertEqual(first(merged, on).action, "claim")
            ticked = item(400, "In progress", executor="Codex")
            ticked["content"]["body"] = "- [x] **B1.1.6** done"
            self.assertEqual(first(state(leaf, ticked), on).action, "claim")


class ClosedIssues(unittest.TestCase):
    def test_closed_ready_gets_no_claim_when_on(self) -> None:
        s = with_tickets(state(item(507, "Ready", state="closed", reason="completed")))
        self.assertEqual(first(s, on=False).action, "claim")  # old behavior
        self.assertEqual(first(s, on=True).action, "idle")

    def test_closed_in_progress_does_not_starve_claims(self) -> None:
        stale = item(430, "In progress", state="closed", reason="completed")
        ready = item(521, "Ready", readiness="Ready.")
        s = with_tickets(state(stale, ready))
        self.assertEqual(kinds(s, on=False), [("continue", f"{ISSUES}/430")])
        self.assertEqual(kinds(s, on=True), [("claim", f"{ISSUES}/521")])

    def test_missing_state_is_not_treated_as_closed(self) -> None:
        s = with_tickets(state(item(521, "Ready", state=None, readiness="Ready.")))
        self.assertEqual(first(s, on=True).action, "claim")


class Readers(unittest.TestCase):
    def test_item_from_rest_keeps_state(self) -> None:
        row = {
            "content_type": "Issue",
            "content": {
                "number": 488,
                "html_url": PREREQ,
                "state": "closed",
                "state_reason": "completed",
            },
            "fields": [],
        }
        content = na.item_from_rest(row)["content"]
        self.assertEqual(
            (content["state"], content["state_reason"]), ("closed", "completed")
        )

    def test_read_tickets_reuses_the_board_and_reads_the_rest_once(self) -> None:
        calls: list[int] = []

        def read(n: int) -> dict[str, Any]:
            calls.append(n)
            return {"state": "open", "state_reason": None}

        board = item(488, "Done", state="closed", reason="completed")
        stateless = item(489, "Done", state=None)
        a = item(
            503,
            "Ready",
            readiness=f"Start after {PREREQ}, {ISSUES}/489 and {ISSUES}/490.",
        )
        b = item(504, "Ready", readiness=f"Start after {ISSUES}/490.")
        backlog = item(505, "Backlog", readiness=f"Start after {ISSUES}/491.")
        got = na.read_tickets([board, stateless, a, b, backlog], read)
        self.assertEqual(calls, [489, 490])
        self.assertEqual(got["488"], {"state": "closed", "state_reason": "completed"})
        self.assertEqual(sorted(got), ["488", "489", "490"])

    def gh_error(self, status: int) -> subprocess.CalledProcessError:
        return subprocess.CalledProcessError(
            1, ["gh"], "", f"gh: Something (HTTP {status})\n"
        )

    def test_legacy_reader_404_and_410_block_only_the_ticket(self) -> None:
        for status in (404, 410):
            with patch.object(na, "gh_json", side_effect=self.gh_error(status)):
                self.assertEqual(na.rest_ticket(488), na.missing_ticket(status))

    def test_legacy_reader_other_failures_stop_the_tick(self) -> None:
        for status in (401, 403, 500, 502):
            with patch.object(na, "gh_json", side_effect=self.gh_error(status)):
                with self.assertRaises(subprocess.CalledProcessError, msg=status):
                    na.rest_ticket(488)

    def test_legacy_reader_off_reads_no_ticket(self) -> None:
        with patch.dict(os.environ, {na.COMPLETED_TICKETS_ENV: "0"}):
            with (
                patch.object(na, "gh_json", return_value=[]),
                patch.object(na, "project_items", return_value=[successor()]),
                patch.object(na, "rest_ticket") as read,
            ):
                s = na.fetch_state()
        read.assert_not_called()
        self.assertNotIn("tickets", s)

    def test_legacy_reader_reads_only_readiness_comments(self) -> None:
        rows = [
            {
                "body": "**Ready, executor Claude:** Start after B1.1.6.",
                "html_url": "c1",
            },
            {"body": f"Review: Start after {PREREQ}.", "html_url": "c2"},
        ]

        def gh_json(args: list[str]) -> Any:
            if any("issues/503/comments" in a for a in args):
                return [rows]
            return []

        with (
            patch.object(na, "gh_json", side_effect=gh_json),
            patch.object(na, "project_items", return_value=[successor()]),
        ):
            s = na.fetch_state()
        ready = next(i for i in s["items"] if i["content"]["number"] == 503)
        self.assertEqual(na.dependency_sources(ready), {"B1.1.6": ["c1"]})

    def test_legacy_reader_on_reads_off_board_tickets(self) -> None:
        def gh_json(args: list[str]) -> Any:
            # The readiness comment of 503; every other list is empty.
            if any("issues/503/comments" in a for a in args):
                return [[{"body": f"**Ready:** Start after {PREREQ}."}]]
            return []

        with patch.dict(os.environ, {na.COMPLETED_TICKETS_ENV: "1"}):
            with (
                patch.object(na, "gh_json", side_effect=gh_json),
                patch.object(na, "project_items", return_value=[successor()]),
                patch.object(na, "rest_ticket", return_value={"state": "open"}) as read,
            ):
                s = na.fetch_state()
        read.assert_called_once_with(488)
        self.assertEqual(s["tickets"], {"488": {"state": "open"}})
        self.assertTrue(s["completed_tickets"])


def gone(status: int) -> gs.Response:
    text = fixture("404").replace("404 Not Found", f"{status} Gone", 1)
    parsed = gs.parse_gh_i(text)
    assert parsed is not None
    return parsed


class SharedReader(Env):
    def setUp(self) -> None:
        super().setUp()
        self.repo = GitHubRepo(self.gh)
        self.repo.add_item(
            503, "Ready", "Claude", readiness=f"**Ready:** Start after {PREREQ}."
        )

    def set_issue(self, state: str, reason: str | None, repo: str = na.REPO) -> None:
        self.gh.set(
            f"{REPO}/issues/488",
            {
                "id": 488,
                "number": 488,
                "state": state,
                "state_reason": reason,
                "repository_url": f"https://api.github.com/repos/{repo}",
                "labels": [],
            },
        )

    def build(self, on: bool) -> dict[str, Any]:
        return gs.build_state(self.client(), set(), completed_tickets=on)

    def test_off_reads_no_ticket(self) -> None:
        self.set_issue("closed", "completed")
        s = self.build(on=False)
        self.assertNotIn("tickets", s)
        self.assertEqual(self.gh.count(f"{REPO}/issues/488"), 0)

    def test_on_reads_the_off_board_ticket(self) -> None:
        self.set_issue("closed", "completed")
        s = self.build(on=True)
        self.assertEqual(
            s["tickets"], {"488": {"state": "closed", "state_reason": "completed"}}
        )
        gs.validate_state(s)
        self.assertEqual(first(s, on=True).action, "claim")

    def test_on_uses_board_state_without_a_read(self) -> None:
        self.repo.add_item(488, "Done", "Codex")
        self.repo.items[-1]["content"].update({"state": "open", "state_reason": None})
        self.repo.publish()
        s = self.build(on=True)
        self.assertEqual(self.gh.count(f"{REPO}/issues/488"), 0)
        act = first(s, on=True)
        self.assertEqual(act.action, "wait")
        self.assertEqual(act.warnings, [f"{PREREQ} is open; board Status is Done"])

    def test_404_and_410_block_only_the_successor(self) -> None:
        for status in (404, 410):
            self.gh.errors.clear()
            self.gh.pages.pop(gs.full_url(f"{REPO}/issues/488"), None)
            real = self.gh

            def http(url: str, etag: str | None, timeout: float) -> gs.Response:
                if url == gs.full_url(f"{REPO}/issues/488"):
                    return gone(status)
                return real(url, etag, timeout)

            client = gs.Client(self.ns(), "test", 30, True, http=http)
            s = gs.build_state(client, set(), completed_tickets=True)
            self.assertEqual(s["tickets"]["488"], na.missing_ticket(status))
            self.assertFalse(self.ns().auth_blocked())

    def test_404_after_a_200_is_not_access_loss(self) -> None:
        self.set_issue("open", None)
        self.build(on=True)  # stores an ETag entry for the issue
        self.gh.pages.pop(gs.full_url(f"{REPO}/issues/488"))
        s = self.build(on=True)
        self.assertEqual(s["tickets"]["488"], na.missing_ticket(404))
        self.assertFalse(self.ns().auth_blocked())

    def test_auth_server_and_rate_limit_errors_stop_the_tick(self) -> None:
        for name in ("401", "502", "403-rate-limit"):
            self.gh.fail(f"{REPO}/issues/488", name)
            with self.assertRaises(gs.ReadBlocked, msg=name):
                self.build(on=True)

    def test_moved_issue_blocks_its_successor(self) -> None:
        self.set_issue("closed", "completed", repo="phaabe/other")
        s = self.build(on=True)
        self.assertIn("moved", s["tickets"]["488"]["problem"])
        self.assertEqual(first(s, on=True).action, "wait")

    def test_snapshot_of_the_other_mode_is_not_used(self) -> None:
        self.set_issue("closed", "completed")
        off = gs.read_snapshot(http=self.gh)
        self.assertEqual(off.source, "refresh")
        on = gs.read_snapshot(http=self.gh, completed_tickets=True)
        self.assertEqual(on.source, "refresh")
        self.assertIn("tickets", on.state)
        again = gs.read_snapshot(http=self.gh, completed_tickets=True)
        self.assertEqual(again.source, "cache")
        self.assertEqual(gs.read_snapshot(http=self.gh).source, "refresh")

    def test_validate_state_needs_tickets_when_on(self) -> None:
        s = self.build(on=True)
        del s["tickets"]
        with self.assertRaisesRegex(gs.ReadBlocked, "tickets"):
            gs.validate_state(s)


class FreshValidation(SharedReader):
    def recheck(self, on: bool) -> str | None:
        action = {"action": "claim", "reason": "r", "issue": SUCCESSOR}
        return gs.recheck(
            "Claude",
            action,
            frozenset(),
            frozenset(),
            False,
            FakeReader(self.gh),
            completed_tickets=on,
        )

    def test_claim_valid_while_completed(self) -> None:
        self.set_issue("closed", "completed")
        self.assertIsNone(self.recheck(on=True))

    def test_reopened_prerequisite_makes_the_claim_stale(self) -> None:
        self.set_issue("closed", "completed")
        self.assertIsNone(self.recheck(on=True))
        self.set_issue("open", "reopened")
        self.assertIn("GitHub changed", self.recheck(on=True) or "")

    def test_unconfirmed_completion_makes_the_claim_stale(self) -> None:
        self.gh.pages.pop(gs.full_url(f"{REPO}/issues/488"), None)
        self.assertIn("GitHub changed", self.recheck(on=True) or "")

    def test_failed_prerequisite_read_blocks_the_recheck(self) -> None:
        self.gh.fail(f"{REPO}/issues/488", "502")
        with self.assertRaises(gs.ReadBlocked):
            self.recheck(on=True)

    def test_off_keeps_the_old_rule(self) -> None:
        # Old rule: no merged PR names 488, so the claim waits either way.
        self.set_issue("closed", "completed")
        self.assertIn("GitHub changed", self.recheck(on=False) or "")
        self.assertEqual(self.gh.count(f"{REPO}/issues/488"), 0)


class StatusAndMonitor(unittest.TestCase):
    def sample(self) -> dict[str, Any]:
        done_prereq = item(
            488, "In review", executor="Codex", state="closed", reason="completed"
        )
        blocked = item(504, "Ready", readiness=f"**Ready:** Start after {ISSUES}/498.")
        open_done = item(498, "Done", executor="Codex")
        return with_tickets(state(done_prereq, successor(), blocked, open_done))

    def test_status_explains_blocks_and_warnings(self) -> None:
        text = na.status(self.sample(), False, completed_tickets=True)
        self.assertIn(
            "Completed tickets rule (EPIC_REQUIRE_COMPLETED_TICKETS): on", text
        )
        self.assertIn(f"starts after {ISSUES}/498 (open)", text)
        self.assertIn(f"{ISSUES}/498 is open; board Status is Done", text)
        self.assertIn(
            f"{PREREQ} is closed as completed; board Status is In review", text
        )
        claim = next(
            line for line in text.splitlines() if line.strip().startswith("claim")
        )
        self.assertIn(SUCCESSOR, claim)

    def test_status_off_is_unchanged(self) -> None:
        text = na.status(self.sample(), False)
        self.assertIn(
            "Completed tickets rule (EPIC_REQUIRE_COMPLETED_TICKETS): off", text
        )
        self.assertNotIn("warning", text)
        self.assertIn(f"starts after {PREREQ}", text)

    def test_status_names_closed_focus_issues(self) -> None:
        closed = item(507, "Ready", state="closed", reason="completed")
        closed["labels"] = ["project::AgentSetup"]
        s = with_tickets(state(closed))
        focus = frozenset({"project::AgentSetup"})
        text = na.status(s, False, focus=focus, completed_tickets=True)
        self.assertIn("closed; board Status is Ready", text)

    def test_monitor_reports_mode_and_warnings(self) -> None:
        off = monitor.github_metrics(self.sample(), 1.0)
        self.assertIn("epic_completed_tickets_rule 0", off)
        self.assertNotIn("dependency_warning_info{", off)
        s = {**self.sample(), "completed_tickets": True}
        on = monitor.github_metrics(s, 1.0)
        self.assertIn("epic_completed_tickets_rule 1", on)
        self.assertIn("is open; board Status is Done", on)
        self.assertIn(f'target="{SUCCESSOR}"', on)


if __name__ == "__main__":
    unittest.main()
