"""Tests for next_action.decide. Run: python3 -m unittest discover -s scripts/epic"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import next_action
from next_action import (
    MAX_ROUNDS,
    checks_state,
    comments_from_rest,
    decide,
    priority_rank,
    read_focus,
    status,
)

A = "a" * 40
B = "b" * 40
R = "https://github.com/phaabe/live.moafunk.de/issues"


def verdict(state: str, by: str, sha: str, at: str, edited: bool = False) -> dict:
    return {
        "body": f"Review: {state} by {by} at {sha}",
        "createdAt": at,
        "url": f"c-{at}",
        "includesCreatedEdit": edited,
    }


def note(at: str) -> dict:
    return {"body": "[P2] finding", "createdAt": at, "url": f"c-{at}"}


def pr(
    number: int, author: str, head: str = A, comments: list | None = None, **kw
) -> dict:
    base = {
        "number": number,
        "body": f"Issue: {R}/{900 + number}\nExecutor: {author}",
        "baseRefName": "dev/312-interim",
        "headRefOid": head,
        "isDraft": False,
        "labels": [],
        "mergeable": "MERGEABLE",
        "statusCheckRollup": [
            {"conclusion": "SUCCESS"},
            {"context": "epic-guard", "state": "SUCCESS"},
        ],
        "comments": comments or [],
    }
    base.update(kw)
    return base


def item(
    number: int,
    executor: str,
    status: str,
    wave: str = "0",
    labels: list[str] | None = None,
) -> dict:
    return {
        "executor": executor,
        "status": status,
        "wave": wave,
        "labels": labels or [],
        "content": {"type": "Issue", "number": number, "url": f"{R}/{number}"},
    }


def first(
    agent: str, prs: list | None = None, items: list | None = None, paused: bool = False
):
    return decide(agent, {"prs": prs or [], "items": items or []}, paused)[0]


class DecideTest(unittest.TestCase):
    def test_idle(self) -> None:
        self.assertEqual(first("Claude").action, "idle")

    def test_pause_wins(self) -> None:
        p = pr(1, "Claude", comments=[verdict("APPROVED", "Codex", A, "t1")])
        self.assertEqual(first("Claude", [p], paused=True).action, "stop")

    def test_merge_when_approved_for_head_and_green(self) -> None:
        a = first(
            "Claude",
            [pr(1, "Claude", comments=[verdict("APPROVED", "Codex", A, "t1")])],
        )
        self.assertEqual((a.action, a.pr, a.sha), ("merge", 1, A))

    def test_no_merge_when_approval_is_for_old_head(self) -> None:
        p = pr(1, "Claude", head=B, comments=[verdict("APPROVED", "Codex", A, "t1")])
        self.assertEqual(first("Claude", [p]).action, "idle")

    def test_no_merge_while_checks_pending(self) -> None:
        p = pr(
            1,
            "Claude",
            comments=[verdict("APPROVED", "Codex", A, "t1")],
            statusCheckRollup=[{"status": "IN_PROGRESS"}],
        )
        self.assertEqual(first("Claude", [p]).action, "idle")

    def test_later_changes_requested_overrides_approval(self) -> None:
        p = pr(
            1,
            "Claude",
            comments=[
                verdict("APPROVED", "Codex", A, "t1"),
                verdict("CHANGES REQUESTED", "Codex", A, "t2"),
            ],
        )
        self.assertEqual(first("Claude", [p]).action, "fix")

    def test_self_verdict_is_ignored(self) -> None:
        p = pr(1, "Claude", comments=[verdict("APPROVED", "Claude", A, "t1")])
        self.assertEqual(first("Claude", [p]).action, "idle")

    def test_edited_verdict_is_ignored(self) -> None:
        p = pr(
            1, "Claude", comments=[verdict("APPROVED", "Codex", A, "t1", edited=True)]
        )
        self.assertEqual(first("Claude", [p]).action, "idle")

    def test_malformed_verdict_is_ignored(self) -> None:
        bad = {"body": f"Review: APPROVED at {A}", "createdAt": "t1", "url": "c1"}
        self.assertEqual(
            first("Claude", [pr(1, "Claude", comments=[bad])]).action, "idle"
        )

    def test_fix_lists_findings_since_previous_verdict(self) -> None:
        p = pr(
            1,
            "Claude",
            head=B,
            comments=[
                verdict("CHANGES REQUESTED", "Codex", A, "t1"),
                note("t2"),
                note("t3"),
                verdict("CHANGES REQUESTED", "Codex", B, "t4"),
            ],
        )
        a = first("Claude", [p])
        self.assertEqual((a.action, a.sha, a.comments), ("fix", B, ["c-t2", "c-t3"]))

    def test_waiting_for_review_after_fix_push(self) -> None:
        p = pr(
            1,
            "Claude",
            head=B,
            comments=[verdict("CHANGES REQUESTED", "Codex", A, "t1")],
        )
        self.assertEqual(first("Claude", [p]).action, "idle")

    def test_escalate_after_max_rounds(self) -> None:
        shas = [c * 40 for c in "abcdef"[:MAX_ROUNDS]]
        comments = [
            verdict("CHANGES REQUESTED", "Codex", s, f"t{i}")
            for i, s in enumerate(shas)
        ]
        a = first("Claude", [pr(1, "Claude", head=shas[-1], comments=comments)])
        self.assertEqual(a.action, "escalate")

    def test_needs_anton_label_is_skipped(self) -> None:
        p = pr(
            1,
            "Claude",
            comments=[verdict("APPROVED", "Codex", A, "t1")],
            labels=[{"name": "needs-anton"}],
        )
        self.assertEqual(first("Claude", [p]).action, "idle")

    def test_failed_checks(self) -> None:
        p = pr(1, "Claude", statusCheckRollup=[{"conclusion": "FAILURE"}])
        self.assertEqual(first("Claude", [p]).action, "fix-checks")

    def test_conflict(self) -> None:
        self.assertEqual(
            first("Claude", [pr(1, "Claude", mergeable="CONFLICTING")]).action,
            "resolve-conflict",
        )

    def test_review_counterpart_head_without_my_verdict(self) -> None:
        p = pr(
            2,
            "Codex",
            head=B,
            comments=[verdict("CHANGES REQUESTED", "Claude", A, "t1")],
        )
        a = first("Claude", [p])
        self.assertEqual((a.action, a.pr, a.sha), ("review", 2, B))

    def test_no_review_when_my_verdict_covers_head(self) -> None:
        p = pr(2, "Codex", comments=[verdict("APPROVED", "Claude", A, "t1")])
        self.assertEqual(first("Claude", [p]).action, "idle")

    def test_no_review_of_draft(self) -> None:
        self.assertEqual(first("Claude", [pr(2, "Codex", isDraft=True)]).action, "idle")

    def test_author_from_reviewer_line(self) -> None:
        p = pr(2, "x", body="Lane: setup · Reviewer: Claude")
        self.assertEqual(first("Claude", [p]).action, "review")

    def test_author_line_is_accepted(self) -> None:
        p = pr(2, "x", body="Author: Codex")
        self.assertEqual(first("Claude", [p]).action, "review")

    def test_executor_must_start_a_line(self) -> None:
        p = pr(2, "x", body="see Executor: Codex in the notes")
        self.assertEqual(first("Claude", [p]).action, "idle")

    def test_pr_without_author_is_ignored(self) -> None:
        self.assertEqual(first("Claude", [pr(2, "x", body="no marker")]).action, "idle")

    def test_other_base_is_ignored(self) -> None:
        self.assertEqual(
            first("Claude", [pr(2, "Codex", baseRefName="main")]).action, "idle"
        )

    def test_merge_before_review(self) -> None:
        mine = pr(1, "Claude", comments=[verdict("APPROVED", "Codex", A, "t1")])
        self.assertEqual(first("Claude", [pr(2, "Codex"), mine]).action, "merge")

    def test_review_before_new_work(self) -> None:
        a = first("Claude", [pr(2, "Codex")], [item(10, "Claude", "Ready")])
        self.assertEqual(a.action, "review")

    def test_claim_ready_leaf_lowest_wave_first(self) -> None:
        items = [
            item(20, "Claude", "Ready", "1"),
            item(30, "Claude", "Ready", "0"),
            item(5, "Codex", "Ready"),
        ]
        a = first("Claude", items=items)
        self.assertEqual((a.action, a.issue), ("claim", f"{R}/30"))

    def test_continue_in_progress_before_claim(self) -> None:
        items = [item(10, "Claude", "Ready"), item(11, "Claude", "In progress")]
        a = first("Claude", items=items)
        self.assertEqual((a.action, a.issue), ("continue", f"{R}/11"))
        self.assertNotIn(
            "claim", [x.action for x in decide("Claude", {"items": items})]
        )

    def test_in_progress_leaf_with_open_pr_is_not_continued(self) -> None:
        items = [item(901, "Claude", "In progress")]
        self.assertEqual(first("Claude", [pr(1, "Claude")], items).action, "idle")

    def test_escalated_pr_still_links_its_issue(self) -> None:
        p = pr(1, "Claude", labels=[{"name": "needs-anton"}])
        items = [item(901, "Claude", "In progress"), item(902, "Claude", "Ready")]
        actions = [a.action for a in decide("Claude", {"prs": [p], "items": items})]
        self.assertNotIn("continue", actions)
        self.assertEqual(actions, ["claim"])

    def test_escalated_pr_counts_toward_open_limit(self) -> None:
        prs = [pr(1, "Claude", labels=[{"name": "needs-anton"}]), pr(2, "Claude")]
        self.assertEqual(
            first("Claude", prs, [item(10, "Claude", "Ready")]).action, "idle"
        )

    def test_dependency_link_does_not_block_claim(self) -> None:
        body = f"Issue: {R}/901\nDepends on: {R}/350\nExecutor: Codex"
        p = pr(1, "Codex", body=body, comments=[verdict("APPROVED", "Claude", A, "t1")])
        a = first("Claude", [p], [item(350, "Claude", "Ready")])
        self.assertEqual((a.action, a.issue), ("claim", f"{R}/350"))

    def test_draft_pr_is_continued(self) -> None:
        self.assertEqual(
            first("Claude", [pr(1, "Claude", isDraft=True)]).action, "continue"
        )

    def test_no_claim_at_open_pr_limit(self) -> None:
        prs = [pr(1, "Claude"), pr(2, "Claude")]
        self.assertEqual(
            first("Claude", prs, [item(10, "Claude", "Ready")]).action, "idle"
        )


STREAM = frozenset({"project::Stream"})


class GuardCheckTest(unittest.TestCase):
    # The guard publishes a commit status. Waiting is pending; failure is real.
    FAILED_GUARD = {"context": "epic-guard", "state": "FAILURE"}
    PENDING_GUARD = {"context": "epic-guard", "state": "PENDING"}

    def test_failed_guard_needs_fix_checks(self) -> None:
        # Wrong lane or bad metadata: a real break the author must fix.
        for guard in (self.FAILED_GUARD, {"context": "epic-guard", "state": "ERROR"}):
            with self.subTest(guard=guard):
                p = pr(
                    1,
                    "Claude",
                    statusCheckRollup=[{"conclusion": "SUCCESS"}, guard],
                )
                self.assertEqual(checks_state(p), "failed")
                self.assertEqual(first("Claude", [p]).action, "fix-checks")

    def test_failed_guard_blocks_merge(self) -> None:
        p = pr(
            1,
            "Claude",
            comments=[verdict("APPROVED", "Codex", A, "t1")],
            statusCheckRollup=[self.FAILED_GUARD],
        )
        self.assertEqual(first("Claude", [p]).action, "fix-checks")

    def test_pending_guard_is_no_action(self) -> None:
        # Waiting for the verdict or for running checks: no fix, no merge.
        for comments in ([], [verdict("APPROVED", "Codex", A, "t1")]):
            with self.subTest(comments=comments):
                p = pr(
                    1,
                    "Claude",
                    comments=comments,
                    statusCheckRollup=[{"conclusion": "SUCCESS"}, self.PENDING_GUARD],
                )
                self.assertEqual(checks_state(p), "pending")
                self.assertEqual(first("Claude", [p]).action, "idle")

    def test_green_guard_allows_merge(self) -> None:
        p = pr(
            1,
            "Claude",
            comments=[verdict("APPROVED", "Codex", A, "t1")],
            statusCheckRollup=[{"context": "epic-guard", "state": "SUCCESS"}],
        )
        self.assertEqual(first("Claude", [p]).action, "merge")

    def test_other_failed_check_still_needs_fixing(self) -> None:
        rollup = [self.FAILED_GUARD, {"name": "backend-ci", "conclusion": "FAILURE"}]
        p = pr(1, "Claude", statusCheckRollup=rollup)
        self.assertEqual(first("Claude", [p]).action, "fix-checks")

    def test_no_checks_reported_blocks_merge(self) -> None:
        # Checks that have not started yet are not green.
        p = pr(
            1,
            "Claude",
            comments=[verdict("APPROVED", "Codex", A, "t1")],
            statusCheckRollup=[],
        )
        self.assertEqual(first("Claude", [p]).action, "idle")

    def test_failed_runner_job_does_not_block_merge(self) -> None:
        # The runner fails for other PRs; the epic-guard status is the gate.
        rollup = [
            {"context": "epic-guard", "state": "SUCCESS"},
            {"name": "backend-ci", "conclusion": "SUCCESS"},
            {"name": "epic-guard-runner", "conclusion": "FAILURE"},
        ]
        p = pr(
            1,
            "Claude",
            comments=[verdict("APPROVED", "Codex", A, "t1")],
            statusCheckRollup=rollup,
        )
        self.assertEqual(first("Claude", [p]).action, "merge")

    def test_missing_guard_status_blocks_merge(self) -> None:
        # The publisher has not posted this PR's epic-guard status yet.
        for runner in (
            {"conclusion": "FAILURE"},
            {"status": "QUEUED"},
            {"status": "IN_PROGRESS"},
        ):
            with self.subTest(runner=runner):
                rollup = [
                    {"name": "backend-ci", "conclusion": "SUCCESS"},
                    {"name": "epic-guard-runner", **runner},
                ]
                p = pr(
                    1,
                    "Claude",
                    comments=[verdict("APPROVED", "Codex", A, "t1")],
                    statusCheckRollup=rollup,
                )
                self.assertEqual(checks_state(p), "pending")
                self.assertEqual(first("Claude", [p]).action, "idle")

    def test_missing_guard_keeps_failed_check_actionable(self) -> None:
        rollup = [
            {"name": "backend-ci", "conclusion": "FAILURE"},
            {"name": "epic-guard-runner", "status": "IN_PROGRESS"},
        ]
        p = pr(1, "Claude", statusCheckRollup=rollup)
        self.assertEqual(first("Claude", [p]).action, "fix-checks")

    def test_only_runner_job_is_pending(self) -> None:
        p = pr(
            1,
            "Claude",
            comments=[verdict("APPROVED", "Codex", A, "t1")],
            statusCheckRollup=[{"name": "epic-guard-runner", "conclusion": "SUCCESS"}],
        )
        self.assertEqual(first("Claude", [p]).action, "idle")


class FocusTest(unittest.TestCase):
    def test_claims_only_issues_in_focus(self) -> None:
        items = [
            item(5, "Claude", "Ready", labels=["project::Infrastructure"]),
            item(6, "Claude", "Ready", labels=["project::Stream"]),
        ]
        got = decide("Claude", {"items": items}, focus=STREAM)[0]
        self.assertEqual((got.action, got.issue), ("claim", f"{R}/6"))

    def test_no_focus_keeps_everything(self) -> None:
        items = [item(5, "Claude", "Ready", labels=["project::Infrastructure"])]
        self.assertEqual(decide("Claude", {"items": items})[0].action, "claim")

    def test_pr_follows_its_issue_labels(self) -> None:
        approved = [verdict("APPROVED", "Codex", A, "t1")]
        inside = pr(1, "Claude", comments=approved)  # Issue: .../901
        outside = pr(2, "Claude", comments=approved)  # Issue: .../902
        items = [
            item(901, "Claude", "In review", labels=["project::Stream"]),
            item(902, "Claude", "In review", labels=["project::Backup"]),
        ]
        acts = decide(
            "Claude", {"prs": [outside, inside], "items": items}, focus=STREAM
        )
        self.assertEqual([(a.action, a.pr) for a in acts], [("merge", 1)])

    def test_foreign_issue_with_same_number_does_not_focus_pr(self) -> None:
        # Project boards may hold issues from other repositories.
        approved = [verdict("APPROVED", "Codex", A, "t1")]
        local = item(901, "Claude", "In review", labels=["project::Backup"])
        foreign = item(901, "Claude", "In review", labels=["project::Stream"])
        foreign["content"]["url"] = "https://github.com/other/repo/issues/901"
        for items in ([local, foreign], [foreign, local]):
            p = pr(1, "Claude", comments=approved)  # Issue: .../901
            got = decide("Claude", {"prs": [p], "items": items}, focus=STREAM)[0]
            self.assertEqual(got.action, "idle")

    def test_pr_own_label_counts(self) -> None:
        p = pr(3, "Codex", labels=[{"name": "project::Stream"}])
        got = decide("Claude", {"prs": [p]}, focus=STREAM)[0]
        self.assertEqual((got.action, got.pr), ("review", 3))

    def test_frozen_pr_gets_no_action_but_still_counts(self) -> None:
        # Out-of-focus PRs keep their issue linked and count toward the PR limit.
        frozen = [pr(n, "Claude") for n in (1, 2)]  # issues 901, 902: unlabelled
        items = [item(7, "Claude", "Ready", labels=["project::Stream"])]
        got = decide("Claude", {"prs": frozen, "items": items}, focus=STREAM)[0]
        self.assertEqual(got.action, "idle")
        self.assertIn("project::Stream", got.reason)

    def test_read_focus(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "focus"
            self.assertEqual(read_focus(path), frozenset())
            path.write_text("# stream first\nproject::Stream\n\n  project::Backup \n")
            self.assertEqual(
                read_focus(path), frozenset({"project::Stream", "project::Backup"})
            )
            path.write_text("")
            self.assertEqual(read_focus(path), frozenset())

    def test_status_names_the_focus(self) -> None:
        self.assertIn("Focus: project::Stream", status({}, False, STREAM))
        self.assertIn("Focus: all", status({}, False))


class StartAfterTest(unittest.TestCase):
    def ready(self, number: int, readiness: str) -> dict:
        i = item(number, "Claude", "Ready")
        i["readiness"] = readiness
        return i

    def test_claim_waits_for_start_after_leaf(self) -> None:
        items = [
            self.ready(
                358, "Ready: B3.3.5 may start now.\n\nStart after B1.1.6 (same editor)."
            )
        ]
        self.assertEqual(first("Claude", items=items).action, "idle")
        waiting = decide("Claude", {"items": items}, include_waiting=True)
        self.assertEqual(
            (waiting[0].action, waiting[0].reason), ("wait", "starts after B1.1.6")
        )

    def test_merged_pr_with_leaf_unblocks_claim(self) -> None:
        items = [self.ready(358, "Start after B1.1.6 (same editor).")]
        state = {
            "items": items,
            "merged_prs": [{"body": "Executor: Claude\nLeaf IDs: B1.1.6"}],
        }
        self.assertEqual(decide("Claude", state)[0].action, "claim")

    def test_ticked_leaf_unblocks_claim(self) -> None:
        done = item(350, "Claude", "In review")
        done["content"]["body"] = "- [x] **B1.1.6** Wave 0. Stop ..."
        items = [self.ready(358, "Start after B1.1.6."), done]
        self.assertEqual(first("Claude", items=items).issue, f"{R}/358")

    def test_open_pr_with_leaf_does_not_unblock(self) -> None:
        items = [self.ready(358, "Start after B1.1.6.")]
        open_pr = pr(
            1, "Claude", body=f"Issue: {R}/350\nExecutor: Claude\nLeaf IDs: B1.1.6"
        )
        self.assertEqual(first("Claude", [open_pr], items).action, "idle")

    def test_chained_waits_list_every_missing_leaf(self) -> None:
        items = [self.ready(362, "Start after B3.3.5 and B1.1.6.")]
        waiting = decide("Claude", {"items": items}, include_waiting=True)[0]
        self.assertEqual(waiting.reason, "starts after B1.1.6, B3.3.5")

    def test_claim_waits_for_start_after_ticket(self) -> None:
        items = [self.ready(434, f"Start after {R}/432 is merged: same file.")]
        waiting = decide("Claude", {"items": items}, include_waiting=True)[0]
        self.assertEqual(
            (waiting.action, waiting.reason), ("wait", f"starts after {R}/432")
        )

    def test_merged_pr_for_ticket_unblocks_claim(self) -> None:
        items = [self.ready(434, f"Start after {R}/432.")]
        state = {
            "items": items,
            "merged_prs": [
                {"body": f"Executor: Claude\nIssue: {R}/432\nLeaf IDs: setup"}
            ],
        }
        self.assertEqual(decide("Claude", state)[0].action, "claim")

    def test_open_pr_for_ticket_does_not_unblock(self) -> None:
        items = [self.ready(434, f"Start after {R}/432.")]
        open_pr = pr(1, "Claude", body=f"Issue: {R}/432\nExecutor: Claude")
        self.assertEqual(first("Claude", [open_pr], items).action, "idle")

    def test_ticket_and_leaf_mix(self) -> None:
        items = [self.ready(424, f"Start after {R}/432 and B1.1.6.")]
        state = {"items": items, "merged_prs": [{"body": f"Issue: {R}/432"}]}
        waiting = decide("Claude", state, include_waiting=True)[0]
        self.assertEqual(waiting.reason, "starts after B1.1.6")

    def test_clause_stops_at_sentence_end(self) -> None:
        text = "Start after B3.3.5. Other B5.2 leaves wait for P2.2.2 and B1.2."
        items = [self.ready(362, text)]
        waiting = decide("Claude", {"items": items}, include_waiting=True)[0]
        self.assertEqual(waiting.reason, "starts after B3.3.5")

    def test_no_start_after_claims_normally(self) -> None:
        items = [self.ready(10, "Ready: B9.9.9 may start now.")]
        self.assertEqual(first("Claude", items=items).action, "claim")


# The first batch table from the epic, trimmed to the two rows.
BATCH = f"""**First implementation batch.**

| Executor | Scope, in order |
| --- | --- |
| Claude | B1.1.6 ({R}/350) → B3.3.5 ({R}/358) → B5.2.6 ({R}/362), one after another |
| Codex | O1.2.4 ({R}/381) first; then P1 ({R}/338, {R}/339), O1.1 ({R}/380) and O1.2.2 ({R}/381) |
"""


class BatchOrderTest(unittest.TestCase):
    def state(self, *numbers: int, done: str = "") -> dict:
        return {
            "items": [item(n, "Codex", "Ready") for n in numbers],
            "batch_order": [BATCH],
            "merged_prs": [{"body": f"Leaf IDs: {done}"}] if done else [],
        }

    def test_first_stage_is_claimed_before_lower_numbers(self) -> None:
        # Found live: Codex claimed 338 again and again while O1.2.4 (381) was open.
        got = decide("Codex", self.state(338, 339, 380, 381))
        self.assertEqual([(a.action, a.issue) for a in got], [("claim", f"{R}/381")])

    def test_later_stage_waits_for_earlier_leaves(self) -> None:
        got = decide("Codex", self.state(338), include_waiting=True)[0]
        self.assertEqual((got.action, got.reason), ("wait", "starts after O1.2.4"))

    def test_done_leaf_unblocks_later_stage(self) -> None:
        got = decide("Codex", self.state(338, 339, 380, done="O1.2.4"))[0]
        self.assertEqual((got.action, got.issue), ("claim", f"{R}/338"))

    def test_arrow_chain_waits_for_every_earlier_leaf(self) -> None:
        state = self.state()
        state["items"] = [item(362, "Claude", "Ready")]
        got = decide("Claude", state, include_waiting=True)[0]
        self.assertEqual(got.reason, "starts after B1.1.6, B3.3.5")

    def test_other_agents_row_is_ignored(self) -> None:
        state = self.state()
        state["items"] = [item(338, "Claude", "Ready")]
        self.assertEqual(decide("Claude", state)[0].action, "claim")

    def test_comment_without_order_header_is_ignored(self) -> None:
        state = self.state(338)
        state["batch_order"] = [BATCH.replace("Scope, in order", "Scope")]
        self.assertEqual(decide("Codex", state)[0].action, "claim")


HIGH, MEDIUM, LOW = "priority::high", "priority::medium", "priority::low"


def lbl(*names: str) -> list[dict]:
    return [{"name": n} for n in names]


class PriorityTest(unittest.TestCase):
    """Inside one action: priority (high, medium or none, low), then age."""

    def targets(self, agent: str, prs=(), items=(), **kw) -> list[tuple]:
        state = {"prs": list(prs), "items": list(items), **kw}
        return [(a.action, a.pr or a.issue) for a in decide(agent, state)]

    def test_rank(self) -> None:
        self.assertEqual(priority_rank({HIGH}), 0)
        self.assertEqual(priority_rank({MEDIUM}), 1)
        self.assertEqual(priority_rank(set()), 1)
        self.assertEqual(priority_rank({"project::Stream"}), 1)
        self.assertEqual(priority_rank({LOW}), 2)

    def test_conflicting_labels_take_the_highest(self) -> None:
        self.assertEqual(priority_rank({LOW, HIGH}), 0)
        self.assertEqual(priority_rank({LOW, MEDIUM}), 1)

    def test_older_pr_is_reviewed_first(self) -> None:
        # `gh pr list` returns the newest first.
        prs = [pr(7, "Codex"), pr(3, "Codex")]
        self.assertEqual(self.targets("Claude", prs), [("review", 3), ("review", 7)])

    def test_priority_pr_is_reviewed_before_older_pr(self) -> None:
        prs = [pr(3, "Codex"), pr(7, "Codex", labels=lbl(HIGH))]
        self.assertEqual(self.targets("Claude", prs)[0], ("review", 7))

    def test_three_levels_and_no_label(self) -> None:
        prs = [
            pr(1, "Codex", labels=lbl(LOW)),
            pr(2, "Codex"),
            pr(3, "Codex", labels=lbl(MEDIUM)),
            pr(4, "Codex", labels=lbl(HIGH)),
        ]
        self.assertEqual([t[1] for t in self.targets("Claude", prs)], [4, 2, 3, 1])

    def test_pr_takes_priority_from_its_board_ticket(self) -> None:
        prs = [pr(3, "Codex"), pr(7, "Codex")]  # tickets 903 and 907
        items = [item(907, "Codex", "In progress", labels=[HIGH])]
        self.assertEqual(self.targets("Claude", prs, items)[0], ("review", 7))

    def test_pr_takes_priority_from_linked_ticket_off_the_board(self) -> None:
        prs = [pr(3, "Codex"), pr(7, "Codex")]
        state = {"linked_labels": {"907": [HIGH]}}
        self.assertEqual(self.targets("Claude", prs, **state)[0], ("review", 7))

    def test_ticket_label_beats_low_pr_label(self) -> None:
        prs = [pr(3, "Codex"), pr(7, "Codex", labels=lbl(LOW))]
        items = [item(907, "Codex", "In progress", labels=[HIGH])]
        self.assertEqual(self.targets("Claude", prs, items)[0], ("review", 7))

    def test_low_priority_pr_goes_last(self) -> None:
        prs = [pr(3, "Codex", labels=lbl(LOW)), pr(7, "Codex")]
        self.assertEqual(self.targets("Claude", prs)[0], ("review", 7))

    def test_priority_keeps_action_order(self) -> None:
        # A priority review never goes before a plain merge of my own PR.
        prs = [
            pr(5, "Codex", labels=lbl(HIGH)),
            pr(9, "Claude", comments=[verdict("APPROVED", "Codex", A, "t1")]),
        ]
        self.assertEqual(self.targets("Claude", prs), [("merge", 9), ("review", 5)])

    def test_merges_sorted_by_priority_then_age(self) -> None:
        ok = [verdict("APPROVED", "Codex", A, "t1")]
        prs = [
            pr(9, "Claude", comments=ok),
            pr(5, "Claude", comments=ok),
            pr(8, "Claude", comments=ok, labels=lbl(HIGH)),
        ]
        self.assertEqual([t[1] for t in self.targets("Claude", prs)], [8, 5, 9])

    def test_priority_in_progress_ticket_before_draft_pr(self) -> None:
        prs = [pr(3, "Claude", isDraft=True)]
        items = [item(20, "Claude", "In progress", labels=[HIGH])]
        self.assertEqual(
            self.targets("Claude", prs, items),
            [("continue", f"{R}/20"), ("continue", 3)],
        )

    def test_continue_queue_is_oldest_first_without_priority(self) -> None:
        prs = [pr(30, "Claude", isDraft=True)]
        items = [item(20, "Claude", "In progress")]
        self.assertEqual(
            self.targets("Claude", prs, items),
            [("continue", f"{R}/20"), ("continue", 30)],
        )

    def test_priority_claim_before_older_and_lower_wave(self) -> None:
        items = [
            item(10, "Claude", "Ready", "0"),
            item(20, "Claude", "Ready", "1", labels=[HIGH]),
            item(5, "Claude", "Ready", "0", labels=[LOW]),
        ]
        self.assertEqual(
            [t[1] for t in self.targets("Claude", items=items)],
            [f"{R}/20", f"{R}/10", f"{R}/5"],
        )

    def test_same_priority_claims_by_wave_then_number(self) -> None:
        items = [
            item(10, "Claude", "Ready", "1", labels=[HIGH]),
            item(30, "Claude", "Ready", "0", labels=[HIGH]),
            item(20, "Claude", "Ready", "0", labels=[HIGH]),
        ]
        self.assertEqual(
            [t[1] for t in self.targets("Claude", items=items)],
            [f"{R}/20", f"{R}/30", f"{R}/10"],
        )

    def test_priority_claim_not_before_own_fix_or_review(self) -> None:
        fix = pr(1, "Claude", comments=[verdict("CHANGES REQUESTED", "Codex", A, "t1")])
        items = [item(20, "Claude", "Ready", labels=[HIGH])]
        self.assertEqual(first("Claude", [fix], items).action, "fix")
        self.assertEqual(first("Claude", [pr(2, "Codex")], items).action, "review")

    def test_priority_claim_not_while_work_continues(self) -> None:
        items = [
            item(20, "Claude", "Ready", labels=[HIGH]),
            item(30, "Claude", "In progress"),
        ]
        self.assertEqual(self.targets("Claude", items=items), [("continue", f"{R}/30")])

    def test_priority_claim_waits_for_start_after(self) -> None:
        blocked = item(20, "Claude", "Ready", labels=[HIGH])
        blocked["readiness"] = f"Start after {R}/19."
        items = [blocked, item(30, "Claude", "Ready")]
        self.assertEqual(self.targets("Claude", items=items), [("claim", f"{R}/30")])

    def test_priority_claim_respects_open_pr_limit(self) -> None:
        prs = [pr(1, "Claude"), pr(2, "Claude")]
        items = [item(20, "Claude", "Ready", labels=[HIGH])]
        self.assertEqual(first("Claude", prs, items).action, "idle")

    def test_priority_does_not_bypass_pause(self) -> None:
        items = [item(20, "Claude", "Ready", labels=[HIGH])]
        self.assertEqual(first("Claude", items=items, paused=True).action, "stop")

    def test_priority_does_not_bypass_focus(self) -> None:
        prs = [pr(3, "Codex", labels=lbl(HIGH))]
        items = [item(20, "Claude", "Ready", labels=[HIGH])]
        state = {"prs": prs, "items": items}
        self.assertEqual(decide("Claude", state, focus=STREAM)[0].action, "idle")

    def test_priority_does_not_bypass_escalation(self) -> None:
        prs = [pr(3, "Codex", labels=lbl(HIGH, "needs-anton"))]
        self.assertEqual(first("Claude", prs).action, "idle")

    def test_linked_ticket_off_the_board_counts_for_focus(self) -> None:
        state = {"prs": [pr(3, "Codex")], "linked_labels": {"903": ["project::Stream"]}}
        self.assertEqual(decide("Claude", state, focus=STREAM)[0].action, "review")

    def test_priority_stays_out_of_the_action_json(self) -> None:
        a = first("Claude", [pr(3, "Codex", labels=lbl(HIGH))])
        self.assertEqual(
            json.loads(a.to_json()),
            {"action": "review", "reason": a.reason, "pr": 3, "sha": A},
        )

    def test_status_marks_priority(self) -> None:
        state = {
            "prs": [pr(3, "Codex", labels=lbl(HIGH)), pr(4, "Codex")],
            "items": [item(20, "Claude", "Ready", labels=[LOW])],
        }
        lines = status(state, False).splitlines()
        self.assertTrue(any("high" in x and "PR 3" in x for x in lines))
        self.assertTrue(any("medium" in x and "PR 4" in x for x in lines))
        self.assertTrue(any("low" in x and f"{R}/20" in x for x in lines))
        idle = [x for x in lines if x.strip().startswith("idle")]
        self.assertTrue(idle and not any("medium" in x for x in idle))


class FetchLinkedLabelsTest(unittest.TestCase):
    def test_reads_labels_only_for_linked_tickets_off_the_board(self) -> None:
        calls: list[list[str]] = []
        board = [item(903, "Codex", "In progress", labels=[HIGH])]
        prs = [pr(3, "Codex"), pr(7, "Codex")]  # tickets 903 and 907
        for p in prs:
            p.pop("comments")

        def run(args: list[str], timeout: int = 120) -> str:
            calls.append(args)
            if args[:2] == ["pr", "list"]:
                open_prs = (
                    "--state" in args and args[args.index("--state") + 1] == "open"
                )
                first_base = args[args.index("--base") + 1] == "dev/312-interim"
                return json.dumps(prs if open_prs and first_base else [])
            endpoint = args[-1] if args[-2:-1] != ["--jq"] else args[-3]
            if endpoint.endswith("/comments?per_page=100"):
                return json.dumps([[]])
            if args[-2:] == ["--jq", "{comments}"]:
                return json.dumps({"comments": 0})
            if endpoint == "repos/phaabe/live.moafunk.de/issues/907":
                return json.dumps({"labels": [{"name": LOW}]})
            raise AssertionError(f"unexpected gh call {args}")

        with (
            patch.object(next_action, "run_gh", run),
            patch.object(next_action, "project_items", lambda: board),
        ):
            state = next_action.fetch_state()
        self.assertEqual(state["linked_labels"], {"907": [LOW]})
        issue_reads = [
            a for a in calls if a[-1].endswith(("/issues/903", "/issues/907"))
        ]
        self.assertEqual(
            issue_reads, [["api", "repos/phaabe/live.moafunk.de/issues/907"]]
        )


def rest(i: int, body: str, edited: bool = False) -> dict:
    at = f"2026-09-28T00:{i // 60:02d}:{i % 60:02d}Z"
    return {
        "id": i,
        "body": body,
        "created_at": at,
        "updated_at": "later" if edited else at,
        "html_url": f"u{i}",
    }


class CommentsFromRestTest(unittest.TestCase):
    def test_changes_requested_after_comment_100_wins(self) -> None:
        rows = [rest(i, "discussion") for i in range(99)]
        rows += [
            rest(99, f"Review: APPROVED by Codex at {A}"),
            rest(100, f"Review: CHANGES REQUESTED by Codex at {A}"),
        ]
        p = pr(1, "Claude", comments=comments_from_rest(rows, 101))
        self.assertEqual(first("Claude", [p]).action, "fix")

    def test_partial_history_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            comments_from_rest([rest(0, "x")], 2)

    def test_duplicate_ids_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            comments_from_rest([rest(0, "x"), rest(0, "x")], 2)

    def test_edited_rest_verdict_is_ignored(self) -> None:
        rows = [rest(0, f"Review: APPROVED by Codex at {A}", edited=True)]
        p = pr(1, "Claude", comments=comments_from_rest(rows, 1))
        self.assertEqual(first("Claude", [p]).action, "idle")


if __name__ == "__main__":
    unittest.main()
