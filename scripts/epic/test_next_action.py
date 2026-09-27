"""Tests for next_action.decide. Run: python3 -m unittest discover -s scripts/epic"""

from __future__ import annotations

import unittest

from next_action import MAX_ROUNDS, decide

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
        "statusCheckRollup": [{"conclusion": "SUCCESS"}],
        "comments": comments or [],
    }
    base.update(kw)
    return base


def item(number: int, executor: str, status: str, wave: str = "0") -> dict:
    return {
        "executor": executor,
        "status": status,
        "wave": wave,
        "labels": [],
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


if __name__ == "__main__":
    unittest.main()
