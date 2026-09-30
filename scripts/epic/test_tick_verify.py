"""Landing check tests. Run: python3 -m unittest discover -s scripts/epic."""

from __future__ import annotations

import unittest
from typing import Any

import tick_verify

OLD = "a" * 40
NEW = "b" * 40
SINCE = "2026-09-28T12:00:00Z"


ISSUES = "https://github.com/phaabe/live.moafunk.de/issues"


def github(
    state: str = "OPEN",
    head: str = OLD,
    comments: list[dict] | None = None,
    ticket_state: str = "closed",
    pr_body: str = f"Issue: {ISSUES}/453\nLeaf IDs: setup",
):
    rows = comments or []

    def fetch(args: list[str]) -> Any:
        if args[:2] == ["pr", "view"]:
            return {"state": state, "headRefOid": head}
        if args[0] == "api" and "/pulls/" in args[1]:
            merged = "2026-09-28T12:10:00Z" if state == "MERGED" else None
            return {"number": 410, "body": pr_body, "merged_at": merged}
        if args[0] == "api" and args[1].endswith("/issues/453"):
            return {
                "state": ticket_state,
                "body": "",
                "sub_issues_summary": {"total": 0},
            }
        if args[:2] == ["api", "graphql"]:
            node_id = next(a[3:] for a in args if a.startswith("id="))
            row = next(c for c in rows if c["node_id"] == node_id)
            return {"data": {"node": {"lastEditedAt": row["edited_at"]}}}
        return [{k: v for k, v in c.items() if k != "edited_at"} for c in rows]

    return fetch


def comment(
    body: str,
    created_at: str = "2026-09-28T12:05:00Z",
    edited_at: str | None = None,
) -> dict:
    return {
        "body": body,
        "created_at": created_at,
        "node_id": f"IC_{abs(hash((body, created_at)))}",
        "edited_at": edited_at,
    }


def check(kind: str, fetch) -> bool:
    action = {"action": kind, "pr": 410, "sha": OLD}
    return tick_verify.landed("claude", action, SINCE, fetch)[0]


class LandedTest(unittest.TestCase):
    def test_merge_needs_a_merged_pr(self) -> None:
        self.assertTrue(check("merge", github(state="MERGED")))
        self.assertFalse(check("merge", github(state="OPEN")))

    def test_merge_needs_its_ticket_closed(self) -> None:
        self.assertFalse(check("merge", github(state="MERGED", ticket_state="open")))
        # A partial PR leaves the ticket open on purpose.
        partial = f"Issue: {ISSUES}/453 (partial)"
        self.assertTrue(
            check("merge", github("MERGED", ticket_state="open", pr_body=partial))
        )

    def test_review_needs_own_verdict_for_the_selected_head(self) -> None:
        mine = comment(f"Review: APPROVED by Claude at {OLD}")
        self.assertTrue(check("review", github(comments=[mine])))
        for other in (
            comment(f"Review: APPROVED by Codex at {OLD}"),
            comment(f"Review: APPROVED by Claude at {NEW}"),
            comment(f"Review: APPROVED by Claude at {OLD}", "2026-09-28T11:00:00Z"),
            comment("Finding: something"),
        ):
            with self.subTest(body=other["body"], at=other["created_at"]):
                self.assertFalse(check("review", github(comments=[other])))

    def test_edited_verdict_does_not_count(self) -> None:
        # The rules say an edited verdict counts as no verdict.
        edited = comment(
            f"Review: APPROVED by Claude at {OLD}", edited_at="2026-09-28T12:06:00Z"
        )
        self.assertFalse(check("review", github(comments=[edited])))

    def test_pushing_actions_need_a_moved_head(self) -> None:
        for kind in ("fix-checks", "resolve-conflict"):
            with self.subTest(kind=kind):
                self.assertTrue(check(kind, github(head=NEW)))
                self.assertFalse(check(kind, github(comments=[comment("done")])))

    def test_fix_needs_a_push_or_a_reply_only_marker(self) -> None:
        self.assertTrue(check("fix", github(head=NEW)))
        reply = comment(f"Reply-only fix by Claude at {OLD}\n\nNot a bug: ...")
        self.assertTrue(check("fix", github(comments=[reply])))
        self.assertFalse(check("fix", github()))

    def test_other_comments_do_not_count_as_a_fix(self) -> None:
        # A denied push leaves the head unchanged; its blocker note is no fix.
        for other in (
            comment("Push was denied; fixes remain local."),
            comment("[vc]: #abc\nThe latest updates on your projects."),
            comment(f"Reply-only fix by Codex at {OLD}\n\nreasons"),
            comment(f"Reply-only fix by Claude at {NEW}\n\nreasons"),
            comment(f"Note: Reply-only fix by Claude at {OLD}"),
            comment(f"Reply-only fix by Claude at {OLD}", "2026-09-28T11:59:59Z"),
        ):
            with self.subTest(body=other["body"], at=other["created_at"]):
                self.assertFalse(check("fix", github(comments=[other])))

    def test_unchecked_actions_pass_without_github(self) -> None:
        def fail(_: list[str]) -> Any:
            raise AssertionError("no GitHub read expected")

        for kind in ("claim", "continue", "escalate"):
            with self.subTest(kind=kind):
                self.assertTrue(
                    tick_verify.landed("claude", {"action": kind}, SINCE, fail)[0]
                )

    def test_checked_action_without_pr_and_sha_is_bad_input(self) -> None:
        with self.assertRaises(ValueError):
            tick_verify.landed("claude", {"action": "merge"}, SINCE, github())


if __name__ == "__main__":
    unittest.main()
