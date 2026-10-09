"""Waiting work: label `waiting` plus a `Waiting:` comment.

https://github.com/phaabe/live.moafunk.de/issues/523
Run: python3 -m unittest discover -s scripts/epic
"""

from __future__ import annotations

import isolated_env  # noqa: F401  (first: hides live runner state)

import subprocess
import unittest
from typing import Any
from unittest.mock import MagicMock, patch

import github_state as gs
import next_action as na
from test_github_state import (
    A,
    ISSUES,
    REPO,
    Env,
    FakeReader,
    GitHubRepo,
    comment,
    pull,
)

R = ISSUES
PULLS = f"https://github.com/{na.REPO}/pull"


def dep_record(*numbers: int, actor: str = "Claude", reason: str = "needs 521") -> str:
    urls = ", ".join(f"{R}/{n}" for n in numbers)
    return f"Waiting: {actor}\nReason: {reason}\nResume after: {urls}"


def operator_record(actor: str = "Claude") -> str:
    return f"Waiting: {actor}\nReason: Anton decides the host\nResume: Anton"


def found(body: str, edited: bool = False) -> dict[str, Any]:
    return {"found": True, "body": body, "url": "c-w", "edited": edited}


def item(n: int, status: str, executor: str = "Claude", labels=(), state=None):
    content: dict[str, Any] = {"type": "Issue", "number": n, "url": f"{R}/{n}"}
    if state:
        content["state"], content["state_reason"] = state
    return {
        "executor": executor,
        "status": status,
        "wave": "1",
        "labels": list(labels),
        "content": content,
        "readiness": "",
    }


def draft(n: int, issue: int | None, labels=(), is_draft: bool = True, **kw):
    body = "Executor: Claude" + (f"\nIssue: {R}/{issue}" if issue else "")
    return {
        "number": n,
        "body": body,
        "baseRefName": "dev/312-interim",
        "headRefOid": A,
        "isDraft": is_draft,
        "labels": [{"name": x} for x in labels],
        "mergeable": "MERGEABLE",
        "statusCheckRollup": [],
        "comments": [],
        **kw,
    }


def merged(issue: int) -> dict[str, Any]:
    return {"number": 1, "body": f"Issue: {R}/{issue}"}


def kinds(actions: list[na.Action]) -> list[tuple[str, str]]:
    return [(a.action, a.issue or f"PR {a.pr}") for a in actions]


class Parser(unittest.TestCase):
    def test_dependency_and_operator_records(self) -> None:
        self.assertEqual(
            na.parse_waiting(dep_record(521, 520)),
            ({"actor": "Claude", "reason": "needs 521", "resume": [520, 521]}, None),
        )
        self.assertEqual(
            na.parse_waiting(operator_record("Anton"))[0],
            {"actor": "Anton", "reason": "Anton decides the host", "resume": "Anton"},
        )

    def test_malformed_records(self) -> None:
        bad = [
            "Waiting: Claude\nReason: x",
            f"Waiting: Bob\nReason: x\nResume after: {R}/1",
            f"Waiting: Claude\nReason:\nResume after: {R}/1",
            "Waiting: Claude\nReason: x\nResume after: #521",
            "Waiting: Claude\nReason: x\nResume after: https://github.com/o/r/issues/1",
            "Waiting: Claude\nReason: x\nResume: Codex",
            f"Waiting: Claude\nReason: x\nResume after: {R}/1\nResume: Anton",
        ]
        for body in bad:
            with self.subTest(body=body):
                record, problem = na.parse_waiting(body)
                self.assertIsNone(record)
                self.assertTrue(problem)

    def test_newest_record_wins_even_when_malformed(self) -> None:
        comments = [
            {
                "body": dep_record(521),
                "createdAt": "2026-09-01T00:00:00Z",
                "url": "old",
            },
            {
                "body": "Waiting: typo",
                "createdAt": "2026-09-02T00:00:00Z",
                "url": "new",
            },
            {"body": "unrelated", "createdAt": "2026-09-03T00:00:00Z", "url": "x"},
        ]
        record = na.newest_waiting(comments)
        self.assertEqual((record["url"], record["body"]), ("new", "Waiting: typo"))
        self.assertEqual(na.newest_waiting([]), {"found": False})


class Selector(unittest.TestCase):
    """decide() with free claims (shared reader and fresh recheck on)."""

    def decide(self, state: dict[str, Any], free: bool = True, completed=False):
        state = {"prs": [], "items": [], "merged_prs": [], **state}
        return na.decide(
            "Claude",
            state,
            include_waiting=True,
            completed_tickets=completed,
            free_claims=free,
        )

    def issue_only(self, record: dict[str, Any] | None, **extra: Any):
        state: dict[str, Any] = {
            "items": [
                item(500, "In progress", labels=["waiting"]),
                item(521, "Ready"),
            ],
            **extra,
        }
        if record is not None:
            state["waiting"] = {"500": record}
        return self.decide(state)

    def test_issue_only_wait_frees_the_prerequisite_claim(self) -> None:
        actions = self.issue_only(found(dep_record(521)))
        self.assertEqual(kinds(actions), [("claim", f"{R}/521"), ("wait", f"{R}/500")])
        self.assertIn(f"resume after {R}/521", actions[1].reason)

    def test_wait_reason_codes_follow_the_record(self) -> None:
        # Producer-side codes (issue 681): never parsed from the reason text.
        for record, code in (
            (found(dep_record(521)), "dependency_wait"),
            (found(operator_record()), "operator_wait"),
            (found(dep_record(521), edited=True), "invalid_wait"),
            ({"found": False}, "invalid_wait"),
            (None, "unknown"),  # record not read
        ):
            with self.subTest(code=code):
                wait = self.issue_only(record)[-1]
                self.assertEqual(
                    (wait.action, wait.reason_code, wait.scope),
                    ("wait", code, "target"),
                )

    def test_draft_wait_carries_the_reason_code(self) -> None:
        state: dict[str, Any] = {
            "prs": [draft(7, 500)],
            "items": [item(500, "In progress", labels=["waiting"])],
            "waiting": {"500": found(operator_record())},
        }
        wait = self.decide(state)[0]
        self.assertEqual(
            (wait.action, wait.pr, wait.reason_code, wait.scope),
            ("wait", 7, "operator_wait", "target"),
        )

    def test_without_fresh_recheck_waits_hold_claims(self) -> None:
        state = {
            "items": [
                item(500, "In progress", labels=["waiting"]),
                item(521, "Ready"),
            ],
            "waiting": {"500": found(dep_record(521))},
        }
        actions = self.decide(state, free=False)
        self.assertEqual(kinds(actions), [("wait", f"{R}/500")])
        self.assertIn("claims stay held", actions[0].reason)

    def test_draft_inherits_the_wait_and_is_parked(self) -> None:
        state: dict[str, Any] = {
            "prs": [draft(7, 500)],
            "items": [
                item(500, "In progress", labels=["waiting"]),
                item(521, "Ready"),
            ],
            "waiting": {"500": found(dep_record(521))},
        }
        self.assertEqual(
            kinds(self.decide(state)), [("claim", f"{R}/521"), ("wait", "PR 7")]
        )
        # The parked draft uses no active slot: one ready PR leaves room.
        state["prs"].append(draft(8, None, is_draft=False))
        self.assertIn("claim", [a.action for a in self.decide(state)])
        # A second ready PR fills active capacity.
        state["prs"].append(draft(9, None, is_draft=False))
        actions = self.decide(state)
        self.assertNotIn("claim", [a.action for a in actions])
        held = [a for a in actions if a.issue == f"{R}/521"]
        self.assertEqual(
            held[0].reason,
            "no claim capacity: active 2 of 2 (PR 8, PR 9); parked 1 of 2 (PR 7 waiting)",
        )

    def test_direct_pr_label_and_off_board_linked_issue(self) -> None:
        records = {"7": found(operator_record()), "600": found(dep_record(521))}
        cases = {
            "direct": draft(7, None, labels=["waiting"]),
            "off-board issue": draft(7, 600),
        }
        for name, pr in cases.items():
            with self.subTest(name):
                state = {
                    "prs": [pr],
                    "linked_labels": {"600": ["waiting"]},
                    "items": [item(521, "Ready")],
                    "waiting": records,
                }
                self.assertEqual(
                    kinds(self.decide(state)), [("claim", f"{R}/521"), ("wait", "PR 7")]
                )
        # Two waiting drafts fill the parked limit: waiting is no capacity bypass.
        state = {
            "prs": [cases["direct"], draft(8, 600)],
            "linked_labels": {"600": ["waiting"]},
            "items": [item(521, "Ready")],
            "waiting": records,
        }
        self.assertEqual(
            kinds(self.decide(state)),
            [("wait", "PR 7"), ("wait", "PR 8"), ("wait", f"{R}/521")],
        )

    def test_invalid_record_uses_active_capacity(self) -> None:
        # Not parked: with one ready PR, active capacity is full.
        state = {
            "prs": [draft(8, None, is_draft=False)],
            "items": [
                item(500, "In progress", labels=["waiting"]),
                item(521, "Ready"),
            ],
            "waiting": {"500": found("Waiting: Claude\nReason: x")},
        }
        actions = self.decide(state)
        self.assertEqual(kinds(actions), [("wait", f"{R}/500"), ("wait", f"{R}/521")])
        self.assertEqual(
            actions[1].reason,
            f"no claim capacity: active 2 of 2 (PR 8, {R}/500); parked 0 of 2",
        )

    def test_ready_pr_with_waiting_label_stays_active(self) -> None:
        state = {
            "prs": [
                draft(7, None, labels=["waiting"], is_draft=False),
                draft(8, None, is_draft=False),
            ],
            "items": [item(521, "Ready")],
            "waiting": {"7": found(operator_record())},
        }
        room = na.capacity("Claude", state, set(), free_claims=True)
        self.assertEqual((room.active, room.parked), (["PR 7", "PR 8"], []))
        self.assertNotIn("claim", [a.action for a in self.decide(state)])

    def test_a_wait_parks_only_with_a_fresh_recheck(self) -> None:
        state: dict[str, Any] = {
            "prs": [draft(7, None, labels=["waiting"])],
            "items": [],
            "waiting": {"7": found(operator_record())},
        }
        off = na.capacity("Claude", state, set(), free_claims=False)
        on = na.capacity("Claude", state, set(), free_claims=True)
        self.assertEqual((off.active, off.parked), (["PR 7"], []))
        self.assertEqual((on.active, on.parked), ([], ["PR 7 waiting"]))
        # An unread record is active and still holds claims.
        state["waiting"] = {}
        state["items"] = [item(521, "Ready")]
        unread = na.capacity("Claude", state, set(), free_claims=True)
        self.assertEqual((unread.active, unread.parked), (["PR 7"], []))
        self.assertEqual(kinds(self.decide(state)), [("wait", "PR 7")])

    def test_resumed_draft_over_capacity_continues_without_claims(self) -> None:
        state = {
            "prs": [
                draft(7, None, labels=["waiting"]),
                draft(8, None, is_draft=False),
                draft(9, None, is_draft=False),
            ],
            "items": [item(530, "Ready")],
            "waiting": {"7": found(dep_record(521))},
            "merged_prs": [merged(521)],
        }
        actions = self.decide(state)
        self.assertEqual(kinds(actions), [("continue", "PR 7"), ("wait", f"{R}/530")])
        self.assertIn("active 3 of 2", actions[1].reason)

    def test_every_source_must_resolve(self) -> None:
        state = {
            "prs": [draft(7, 500, labels=["waiting"])],
            "items": [item(500, "In progress", labels=["waiting"])],
            "merged_prs": [merged(521)],
            "waiting": {
                "7": found(dep_record(521)),  # satisfied
                "500": found(operator_record("Anton")),  # still open
            },
        }
        actions = self.decide(state)
        self.assertEqual(kinds(actions), [("wait", "PR 7")])
        self.assertIn("resumes when Anton removes the label", actions[0].reason)
        self.assertNotIn(f"{PULLS}/7", actions[0].reason)

    def test_invalid_records_park_the_target_and_free_claims(self) -> None:
        cases = {
            "missing": {"found": False},
            "edited": found(dep_record(521), edited=True),
            "malformed": found("Waiting: Claude\nReason: x"),
            "other agent": found(dep_record(521, actor="Codex")),
        }
        for name, record in cases.items():
            with self.subTest(name):
                actions = self.issue_only(record)
                self.assertEqual(
                    kinds(actions), [("claim", f"{R}/521"), ("wait", f"{R}/500")]
                )

    def test_unread_record_parks_the_target_and_holds_claims(self) -> None:
        actions = self.issue_only(None)
        self.assertEqual(kinds(actions), [("wait", f"{R}/500")])
        self.assertIn("not read", actions[0].reason)

    def test_ordinary_continuation_still_holds_claims(self) -> None:
        state = {
            "items": [
                item(500, "In progress", labels=["waiting"]),
                item(501, "In progress"),
                item(521, "Ready"),
            ],
            "waiting": {"500": found(dep_record(521))},
        }
        self.assertEqual(
            kinds(self.decide(state)),
            [("continue", f"{R}/501"), ("wait", f"{R}/500")],
        )

    def test_unlabeled_work_is_unchanged(self) -> None:
        state = {"items": [item(500, "In progress"), item(521, "Ready")]}
        self.assertEqual(kinds(self.decide(state)), [("continue", f"{R}/500")])

    def test_labeled_ready_pr_keeps_its_actions_with_a_warning(self) -> None:
        pr = draft(
            7,
            500,
            labels=["waiting"],
            is_draft=False,
            mergeable="CONFLICTING",
        )
        state = {"prs": [pr], "items": [item(500, "In progress", labels=["waiting"])]}
        first = self.decide(state)[0]
        self.assertEqual((first.action, first.pr), ("resolve-conflict", 7))
        self.assertEqual(len(first.warnings), 2)
        self.assertIn("ready PR keeps its actions", first.warnings[0])

    def test_dependency_resolves_with_the_old_rule_and_label_kept(self) -> None:
        actions = self.issue_only(found(dep_record(521)), merged_prs=[merged(521)])
        self.assertEqual(kinds(actions), [("continue", f"{R}/500")])
        self.assertIn("label waiting still present", actions[0].warnings[0])

    def test_dependency_with_the_completed_tickets_rule(self) -> None:
        base = {
            "items": [item(500, "In progress", labels=["waiting"])],
            "waiting": {"500": found(dep_record(521))},
            # A merged PR alone is not enough in this mode.
            "merged_prs": [merged(521)],
            "completed_tickets": True,
        }
        cases = [
            ({"state": "open"}, "wait"),
            ({"state": "closed", "state_reason": "not_planned"}, "wait"),
            ({"state": "closed", "state_reason": "completed"}, "continue"),
        ]
        for ticket, expected in cases:
            with self.subTest(ticket=ticket):
                state = {**base, "tickets": {"521": ticket}}
                actions = self.decide(state, completed=True)
                self.assertEqual(actions[0].action, expected)
        # Reopened: blocks again while the label is still there.
        state = {**base, "tickets": {"521": {"state": "open"}}}
        self.assertEqual(self.decide(state, completed=True)[0].action, "wait")

    def test_label_removal_ends_any_wait(self) -> None:
        state = {
            "items": [item(500, "In progress"), item(521, "Ready")],
            "waiting": {"500": found(operator_record())},
        }
        self.assertEqual(kinds(self.decide(state)), [("continue", f"{R}/500")])

    def test_runner_output_never_holds_a_wait(self) -> None:
        state = {
            "prs": [],
            "items": [item(500, "In progress", labels=["waiting"])],
            "merged_prs": [],
            "waiting": {"500": found(operator_record())},
        }
        actions = na.decide("Claude", state, free_claims=True)
        self.assertEqual([a.action for a in actions], ["idle"])

    def test_status_names_the_wait(self) -> None:
        state = {
            "prs": [],
            "items": [item(500, "In progress", labels=["waiting"])],
            "merged_prs": [],
            "linked_labels": {},
            "waiting": {"500": found(operator_record())},
        }
        text = na.status(state, paused=False, free_claims=True)
        self.assertIn("wait", text)
        self.assertIn("Anton decides the host", text)


class Readers(Env):
    """build_state() and the fresh recheck load the records they need."""

    def setUp(self) -> None:
        super().setUp()
        self.repo = GitHubRepo(self.gh)

    def waiting_issue(self, n: int, body: str, labels=("waiting",)) -> None:
        self.repo.add_item(n, "In progress", "Claude", labels=labels)
        self.gh.set(
            f"{REPO}/issues/{n}/comments?per_page=100",
            [comment(700 + n, body, "2026-09-29T09:30:00Z")],
        )

    def merge_issue_pr(self, issue: int) -> None:
        self.repo.closed["dev/312-interim"] = [
            {
                "id": 1,
                "number": 1,
                "body": f"Issue: {R}/{issue}",
                "merged_at": "2026-09-29T12:00:00Z",
            }
        ]
        self.repo.publish()

    def recheck(self, action: dict[str, Any], completed: bool = False) -> str | None:
        reader = FakeReader(self.gh)
        return gs.recheck(
            "Claude",
            action,
            frozenset(),
            frozenset(),
            False,
            reader,
            completed_tickets=completed,
        )

    def test_build_state_reads_only_labeled_sources(self) -> None:
        self.waiting_issue(500, dep_record(521))
        self.repo.add_item(501, "Backlog", "Claude", labels=("waiting",))
        self.repo.add_item(521, "Ready", "Claude")
        state = gs.build_state(self.client(), set())
        gs.validate_state(state)
        self.assertEqual(list(state["waiting"]), ["500"])
        self.assertEqual(state["waiting"]["500"]["body"], dep_record(521))
        self.assertEqual(self.gh.count(f"{REPO}/issues/501/comments?per_page=100"), 0)
        actions = na.decide("Claude", state, free_claims=True)
        self.assertEqual(kinds(actions), [("claim", f"{R}/521")])

    def test_draft_pr_comments_are_reused(self) -> None:
        self.repo.add_pr(
            pull(7, A, "Executor: Claude", draft=True, labels=[{"name": "waiting"}]),
            [comment(1, operator_record(), "2026-09-29T09:30:00Z")],
        )
        state = gs.build_state(self.client(), set())
        self.assertEqual(state["waiting"]["7"]["body"], operator_record())
        self.assertEqual(self.gh.count(f"{REPO}/issues/7/comments?per_page=100"), 1)

    def test_selection_and_recheck_write_nothing(self) -> None:
        self.waiting_issue(500, dep_record(521))
        self.repo.add_item(521, "Ready", "Claude")
        action = {"action": "claim", "reason": "r", "issue": f"{R}/521"}
        self.assertIsNone(self.recheck(action))
        # FakeGitHub serves GETs only; every call is a read of api.github.com.
        self.assertTrue(all(u.startswith(gs.API) for u, _ in self.gh.calls))

    def test_claim_recheck_sees_a_resumed_issue(self) -> None:
        self.waiting_issue(500, dep_record(522))
        self.repo.add_item(521, "Ready", "Claude")
        action = {"action": "claim", "reason": "r", "issue": f"{R}/521"}
        self.assertIsNone(self.recheck(action))
        # The dependency completes after selection: 500 continues, claim stale.
        self.merge_issue_pr(522)
        self.assertIn("continue", self.recheck(action) or "")

    def test_claim_recheck_sees_a_removed_label(self) -> None:
        self.waiting_issue(500, operator_record())
        self.repo.add_item(521, "Ready", "Claude")
        action = {"action": "claim", "reason": "r", "issue": f"{R}/521"}
        self.assertIsNone(self.recheck(action))
        self.repo.items = [i for i in self.repo.items if i["content"]["number"] != 500]
        self.waiting_issue(500, operator_record(), labels=())
        self.assertIsNotNone(self.recheck(action))

    def test_claim_recheck_sees_a_resumed_draft(self) -> None:
        self.waiting_issue(500, dep_record(522))
        self.repo.add_item(521, "Ready", "Claude")
        self.repo.add_pr(pull(7, A, f"Executor: Claude\nIssue: {R}/500", draft=True))
        action = {"action": "claim", "reason": "r", "issue": f"{R}/521"}
        self.assertIsNone(self.recheck(action))
        self.merge_issue_pr(522)
        self.assertIn("continue PR 7", self.recheck(action) or "")

    def test_draft_continue_recheck_sees_a_new_inherited_wait(self) -> None:
        self.repo.add_item(500, "In progress", "Claude")
        self.repo.add_pr(pull(7, A, f"Executor: Claude\nIssue: {R}/500", draft=True))
        action = {"action": "continue", "reason": "r", "pr": 7, "sha": A}
        self.assertIsNone(self.recheck(action))
        # The linked issue gets the label and a record after selection.
        self.gh.set(
            f"{REPO}/issues/500",
            {"id": 500, "number": 500, "labels": [{"name": "waiting"}]},
        )
        self.gh.set(
            f"{REPO}/issues/500/comments?per_page=100",
            [comment(9, dep_record(522), "2026-09-29T12:00:00Z")],
        )
        # `wait` is status-only: the runner sees idle and starts no model.
        self.assertIn("gives idle", self.recheck(action) or "")
        # Its dependency completes: the draft may continue again, label kept.
        self.merge_issue_pr(522)
        self.assertIsNone(self.recheck(action))

    def test_draft_continue_recheck_with_the_completed_tickets_rule(self) -> None:
        self.repo.add_pr(
            pull(7, A, "Executor: Claude", draft=True, labels=[{"name": "waiting"}]),
            [comment(1, dep_record(522), "2026-09-29T09:30:00Z")],
        )
        issue = {"number": 522, "repository_url": f"https://api.github.com/{REPO}"}
        self.gh.set(f"{REPO}/issues/522", {**issue, "state": "open"})
        action = {"action": "continue", "reason": "r", "pr": 7, "sha": A}
        self.assertIn("gives idle", self.recheck(action, completed=True) or "")
        self.gh.set(
            f"{REPO}/issues/522",
            {**issue, "state": "closed", "state_reason": "completed"},
        )
        self.assertIsNone(self.recheck(action, completed=True))

    def test_issue_continue_recheck_sees_a_new_wait(self) -> None:
        self.repo.add_item(500, "In progress", "Claude")
        action = {"action": "continue", "reason": "r", "issue": f"{R}/500"}
        self.assertIsNone(self.recheck(action))
        self.repo.items.clear()
        self.waiting_issue(500, operator_record())
        self.assertIsNotNone(self.recheck(action))

    def test_failed_record_read_blocks_instead_of_counting_as_missing(self) -> None:
        self.waiting_issue(500, dep_record(521))
        self.repo.add_item(521, "Ready", "Claude")
        self.gh.fail(f"{REPO}/issues/500/comments?per_page=100", "502")
        action = {"action": "claim", "reason": "r", "issue": f"{R}/521"}
        with self.assertRaises(gs.ReadBlocked):
            self.recheck(action)

    def test_snapshot_without_waiting_records_is_rebuilt(self) -> None:
        state = gs.build_state(self.client(), set())
        del state["waiting"]
        with self.assertRaisesRegex(gs.ReadBlocked, "waiting"):
            gs.validate_state(state)


class LegacyReader(unittest.TestCase):
    """fetch_state() without the shared reader reads the same records."""

    def test_fetch_state_reads_labeled_issue_records(self) -> None:
        board = [item(500, "In progress", labels=["waiting"]), item(521, "Ready")]
        record = {
            "id": 1,
            "body": dep_record(521),
            "created_at": "2026-09-29T09:00:00Z",
            "updated_at": "2026-09-29T09:00:00Z",
            "html_url": "c-1",
        }
        calls: list[list[str]] = []

        def gh(args: list[str]) -> Any:
            calls.append(args)
            endpoint = args[-1]
            if args[:2] == ["pr", "list"]:
                return []
            if endpoint.endswith("issues/500/comments?per_page=100"):
                return [[record]]
            return [[]]

        with (
            patch.dict("os.environ", {"EPIC_SHARED_READER": ""}),
            patch.object(na, "gh_json", side_effect=gh),
            patch.object(na, "project_items", return_value=board),
        ):
            state = na.fetch_state()
        self.assertEqual(state["waiting"]["500"]["body"], dep_record(521))
        # Legacy mode has no fresh recheck: waits hold claims.
        self.assertEqual(
            kinds(na.decide("Claude", state, include_waiting=True)),
            [("wait", f"{R}/500")],
        )


def runner_github(agent: str, dependency_merged: bool) -> dict[str, list[Any]]:
    """REST pages: the agent's #500 In progress waits for #522; #521 is Ready."""
    from test_github_state import FakeGitHub

    fake = FakeGitHub()
    repo = GitHubRepo(fake)
    repo.add_item(500, "In progress", agent, labels=("waiting",))
    fake.set(
        f"{REPO}/issues/500/comments?per_page=100",
        [comment(1, dep_record(522, actor=agent), "2026-09-29T09:30:00Z")],
    )
    repo.add_item(521, "Ready", agent)
    if dependency_merged:
        repo.closed["dev/312-interim"] = [
            {
                "id": 1,
                "number": 1,
                "body": f"Issue: {R}/522",
                "merged_at": "2026-09-29T12:00:00Z",
            }
        ]
        repo.publish()
    return {url: [body, link] for url, (body, link, _) in fake.pages.items()}


class ClaudeRunner(unittest.TestCase):
    """The real claude-tick.sh with the real selector and recheck.

    Only the network (a fake `gh` that serves recorded REST pages), git, the
    gate, the worktree step and the model are stubs.
    """

    def setUp(self) -> None:
        import json
        import shutil

        from test_claude_tick import ROOT, ClaudeTickTest

        self.json = json
        helper = ClaudeTickTest("test_reader_off_runs_no_recheck")
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        self.helper = helper
        epic = helper.repo / "scripts/epic"
        for name in ("next_action.py", "github_state.py", "routing.py"):
            shutil.copyfile(ROOT / "scripts/epic" / name, epic / name)
        self.map = helper.root / "gh-map.json"
        self.next_map = helper.root / "gh-map-next.json"
        # Gate stub: as in test_claude_tick, plus GitHub changing after selection.
        (epic / "tick_gate.py").write_text(
            "import json, os, shutil, sys\n"
            "with open(os.environ['TEST_CALLS'], 'a') as f:\n"
            "    f.write(json.dumps(['gate', sys.argv[1]]) + '\\n')\n"
            "if sys.argv[1] == 'check':\n"
            "    nxt = os.environ.get('TEST_GH_MAP_NEXT')\n"
            "    if nxt:\n"
            "        shutil.copyfile(nxt, os.environ['TEST_GH_MAP'])\n"
            "    seen = os.path.join(os.environ['EPIC_STATE_DIR'], 'claude-gate-seen.json')\n"
            "    open(seen, 'w').write('{}')\n"
        )
        gh = helper.root / "bin" / "gh"
        gh.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "args = sys.argv[1:]\n"
            "with open(os.environ['TEST_CALLS'], 'a') as f:\n"
            "    f.write(json.dumps(['gh', ' '.join(args)[-120:]]) + '\\n')\n"
            "if args[:1] != ['api'] or '-i' not in args:\n"
            "    sys.exit(1)\n"
            "pages = json.load(open(os.environ['TEST_GH_MAP']))\n"
            "if args[-1] not in pages:\n"
            "    sys.stdout.write('HTTP/2.0 404 Not Found\\r\\n\\r\\n{}')\n"
            "    sys.exit(1)\n"
            "body, link = pages[args[-1]]\n"
            "head = 'HTTP/2.0 200 OK\\r\\nEtag: \"t\"\\r\\n'\n"
            "head += f'Link: {link}\\r\\n' if link else ''\n"
            "sys.stdout.write(head + '\\r\\n' + body)\n"
        )
        gh.chmod(0o755)
        cache = helper.state / "github-cache"
        cache.mkdir(parents=True)
        (cache / "auth-context").write_text("runner-test\n")

    def github(self, dependency_merged: bool) -> dict[str, list[Any]]:
        return runner_github("Claude", dependency_merged)

    def tick(self, changes_after_selection: bool) -> int:
        self.map.write_text(self.json.dumps(self.github(False)))
        env = {"EPIC_SHARED_READER": "1", "TEST_GH_MAP": str(self.map)}
        if changes_after_selection:
            self.next_map.write_text(self.json.dumps(self.github(True)))
            env["TEST_GH_MAP_NEXT"] = str(self.next_map)
        proc = self.helper.run_tick(**env)
        try:
            return proc.wait(timeout=120)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            self.fail(f"tick did not finish in 120 s\n{self.log()}")

    def calls(self) -> list[list[str]]:
        return self.helper.calls_made()

    def log(self) -> str:
        path = self.helper.state / "claude.log"
        return path.read_text() if path.exists() else "(no runner log)"

    def test_waiting_work_frees_the_claim_and_the_model_starts(self) -> None:
        self.assertEqual(self.tick(changes_after_selection=False), 0, self.log())
        self.assertEqual(len(self.helper.model_targets()), 1, self.log())
        prompt = (self.helper.root / "calls.jsonl.prompt").read_text()
        self.assertIn('"action": "claim"', prompt)
        self.assertIn(f"{R}/521", prompt)
        self.assertIn(["gate", "record"], self.calls())

    def test_resumed_work_rejects_the_claim_before_the_model(self) -> None:
        self.assertEqual(self.tick(changes_after_selection=True), 0, self.log())
        self.assertEqual(self.helper.model_targets(), [], self.log())
        # No success, cooldown or repeat record for the rejected claim.
        self.assertNotIn(["gate", "record"], self.calls())
        self.assertFalse((self.helper.state / "claude-gate-seen.json").exists())
        self.assertFalse((self.helper.root / "calls.jsonl.worktree").exists())

    def test_a_tick_timeout_fails_with_the_runner_log(self) -> None:
        # https://github.com/phaabe/live.moafunk.de/issues/599
        (self.helper.state / "claude.log").write_text("tick: stuck in select\n")
        proc = MagicMock()
        proc.wait.side_effect = [subprocess.TimeoutExpired("claude-tick.sh", 120), 0]
        with patch.object(self.helper, "run_tick", return_value=proc):
            with self.assertRaisesRegex(AssertionError, "(?s)120 s.*stuck in select"):
                self.tick(changes_after_selection=False)
        proc.kill.assert_called_once_with()


class CodexRunner(unittest.TestCase):
    """The real .codex/codex-tick.sh with the real selector and recheck.

    It needs no adapter change: with EPIC_SHARED_READER=1 it rechecks every
    candidate after the gate and before the model. Only the network (a fake
    `gh` with recorded REST pages), `git pull`, the worktree step and the
    model are stubs, from the Codex runner's own test harness.
    """

    def setUp(self) -> None:
        import json
        import shutil
        import sys
        from pathlib import Path

        codex_tests = Path(__file__).resolve().parents[2] / ".codex/tests"
        sys.path.insert(0, str(codex_tests))
        self.addCleanup(sys.path.remove, str(codex_tests))
        from test_codex_tick import ROOT, TickTests

        self.json = json
        helper = TickTests("test_pause_never_calls_selector_or_codex")
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        self.helper = helper
        epic = helper.repo / "scripts/epic"
        # The real selector replaces the harness's selector stub.
        shutil.copyfile(
            ROOT.parent / "scripts/epic/next_action.py", epic / "next_action.py"
        )
        self.map = helper.root / "gh-map.json"
        self.next_map = helper.root / "gh-map-next.json"
        gh = helper.bin / "gh"
        gh.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, shutil, sys\n"
            "args = sys.argv[1:]\n"
            "with open(os.environ['TEST_GH_CALLS'], 'a') as f:\n"
            "    f.write(json.dumps(args) + '\\n')\n"
            "if args[:1] == ['api'] and '-i' in args:\n"
            "    pages = json.load(open(os.environ['TEST_GH_MAP']))\n"
            "    if args[-1] not in pages:\n"
            "        sys.stdout.write('HTTP/2.0 404 Not Found\\r\\n\\r\\n{}')\n"
            "        sys.exit(1)\n"
            "    body, link = pages[args[-1]]\n"
            "    head = 'HTTP/2.0 200 OK\\r\\nEtag: \"t\"\\r\\n'\n"
            "    head += f'Link: {link}\\r\\n' if link else ''\n"
            "    sys.stdout.write(head + '\\r\\n' + body)\n"
            "    sys.exit(0)\n"
            # The gate's target read: GitHub changes after selection here.
            "if args[:1] == ['api'] and '--jq' in args:\n"
            "    nxt = os.environ.get('TEST_GH_MAP_NEXT')\n"
            "    if nxt:\n"
            "        shutil.copyfile(nxt, os.environ['TEST_GH_MAP'])\n"
            "    print('2026-09-29T09:00:00Z open')\n"
            "    sys.exit(0)\n"
            "sys.exit(1)\n"
        )
        cache = helper.state / "github-cache"
        cache.mkdir(parents=True)
        (cache / "auth-context").write_text("runner-test\n")
        for name in (
            "EPIC_QUOTA_DIR",
            "EPIC_CACHE_DIR",
            "EPIC_FOCUS_ACTIONS",
            "EPIC_ACTION_FILE",
            "EPIC_TRUSTED_ROOT",
            "EPIC_WORKTREE",
            "EPIC_REQUIRE_COMPLETED_TICKETS",
        ):
            helper.env.pop(name, None)
        helper.env.update(
            {
                "EPIC_SHARED_READER": "1",
                "TEST_GH_MAP": str(self.map),
            }
        )

    def tick(self, changes_after_selection: bool) -> Any:
        self.map.write_text(self.json.dumps(runner_github("Codex", False)))
        if changes_after_selection:
            self.next_map.write_text(self.json.dumps(runner_github("Codex", True)))
            self.helper.env["TEST_GH_MAP_NEXT"] = str(self.next_map)
        return self.helper.run_tick()

    def model_actions(self) -> list[dict[str, Any]]:
        if not self.helper.calls.exists():
            return []
        return [
            self.json.loads(line)["action"]
            for line in self.helper.calls.read_text().splitlines()
        ]

    def log(self) -> str:
        return (self.helper.state / "codex.log").read_text()

    def test_waiting_work_frees_the_claim_and_the_model_starts(self) -> None:
        self.assertEqual(
            self.tick(changes_after_selection=False).returncode, 0, self.log()
        )
        actions = self.model_actions()
        self.assertEqual([a["action"] for a in actions], ["claim"], self.log())
        self.assertEqual(actions[0]["issue"], f"{R}/521")
        self.assertTrue((self.helper.state / "codex-gate.json").exists())

    def test_resumed_work_rejects_the_claim_before_the_model(self) -> None:
        self.assertEqual(
            self.tick(changes_after_selection=True).returncode, 0, self.log()
        )
        self.assertEqual(self.model_actions(), [], self.log())
        self.assertIn("claim is stale on GitHub", self.log())
        # No success, cooldown or repeat record for the rejected claim.
        self.assertFalse((self.helper.state / "codex-gate.json").exists())
        self.assertFalse((self.helper.state / "codex-gate-seen.json").exists())
        self.assertFalse((self.helper.state / "codex-backoff.json").exists())

    def test_slow_github_reads_still_finish_in_time(self) -> None:
        # A loaded machine, made repeatable: each fake `gh` call takes 0.15s
        # more. With the old 1s refresh budget this tick stopped in the
        # selector with exit 75.
        gh = self.helper.bin / "gh"
        gh.write_text(
            gh.read_text().replace(
                "import json, os, shutil, sys\n",
                "import json, os, shutil, sys, time\ntime.sleep(0.15)\n",
                1,
            )
        )
        self.assertIn("time.sleep(0.15)", gh.read_text())
        self.assertEqual(
            self.tick(changes_after_selection=False).returncode, 0, self.log()
        )
        self.assertNotIn("GitHub reads took too long", self.log())
        self.assertEqual([a["action"] for a in self.model_actions()], ["claim"])


if __name__ == "__main__":
    unittest.main()
