"""Landing check tests. Run: python3 -m unittest discover -s scripts/epic."""

from __future__ import annotations

import unittest
from typing import Any

import tick_verify

OLD = "a" * 40
NEW = "b" * 40
SINCE = "2026-09-28T12:00:00Z"


def github(state: str = "OPEN", head: str = OLD, comments: list[dict] | None = None):
    def fetch(args: list[str]) -> Any:
        if args[:2] == ["pr", "view"]:
            return {"state": state, "headRefOid": head}
        return comments or []

    return fetch


def comment(body: str, created_at: str = "2026-09-28T12:05:00Z") -> dict:
    return {"body": body, "created_at": created_at}


def check(kind: str, fetch) -> bool:
    action = {"action": kind, "pr": 410, "sha": OLD}
    return tick_verify.landed("claude", action, SINCE, fetch)[0]


class LandedTest(unittest.TestCase):
    def test_merge_needs_a_merged_pr(self) -> None:
        self.assertTrue(check("merge", github(state="MERGED")))
        self.assertFalse(check("merge", github(state="OPEN")))

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

    def test_pushing_actions_need_a_moved_head(self) -> None:
        for kind in ("fix-checks", "resolve-conflict"):
            with self.subTest(kind=kind):
                self.assertTrue(check(kind, github(head=NEW)))
                self.assertFalse(check(kind, github(comments=[comment("done")])))

    def test_fix_needs_a_push_or_a_reply(self) -> None:
        self.assertTrue(check("fix", github(head=NEW)))
        self.assertTrue(check("fix", github(comments=[comment("Not a bug: ...")])))
        self.assertFalse(check("fix", github()))
        old_reply = comment("earlier", "2026-09-28T11:59:59Z")
        self.assertFalse(check("fix", github(comments=[old_reply])))

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
