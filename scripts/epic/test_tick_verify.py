"""Landing check tests. Run: python3 -m unittest discover -s scripts/epic."""

from __future__ import annotations

import isolated_env  # noqa: F401  (first: hides live runner state)

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

import tick_verify

OLD = "a" * 40
NEW = "b" * 40
SINCE = "2026-09-28T12:00:00Z"


def github(state: str = "OPEN", head: str = OLD, comments: list[dict] | None = None):
    rows = comments or []

    def fetch(args: list[str]) -> Any:
        if args[:2] == ["pr", "view"]:
            return {"state": state, "headRefOid": head}
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


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_AUTHOR_NAME": "t",
             "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
             "GIT_COMMITTER_EMAIL": "t@t"},
    ).stdout.strip()  # fmt: skip


class WorktreeTest(unittest.TestCase):
    """--worktree: the moved head must be the runner's finished work."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="tick-verify-")
        self.addCleanup(tmp.cleanup)
        self.wt = Path(tmp.name)
        git(self.wt, "init", "-q", "-b", "feat/1-x")
        (self.wt / "f.txt").write_text("one\n")
        git(self.wt, "add", "f.txt")
        git(self.wt, "commit", "-q", "-m", "one")
        self.head = git(self.wt, "rev-parse", "HEAD")

    def verify(self, kind: str, head: str) -> tuple[bool, str]:
        action = {"action": kind, "pr": 410, "sha": OLD}
        return tick_verify.landed(
            "claude", action, SINCE, github(head=head), worktree=str(self.wt)
        )

    def test_own_pushed_head_lands(self) -> None:
        for kind in ("fix-checks", "fix"):
            with self.subTest(kind=kind):
                self.assertTrue(self.verify(kind, self.head)[0])

    def test_moved_head_alone_is_no_resolved_conflict(self) -> None:
        # Proof and record are checked in test_rebase_policy.py.
        ok, reason = self.verify("resolve-conflict", self.head)
        self.assertFalse(ok)
        self.assertIn("attempt pin is missing", reason)

    def test_head_moved_by_another_writer_does_not_land(self) -> None:
        # Review finding: a refused lease push counted as done.
        for kind in ("resolve-conflict", "fix-checks", "fix"):
            with self.subTest(kind=kind):
                ok, reason = self.verify(kind, NEW)
                self.assertFalse(ok)
                self.assertIn("not the runner's work", reason)

    def test_unfinished_rebase_does_not_land(self) -> None:
        git(self.wt, "switch", "-q", "-c", "base", "HEAD")
        (self.wt / "f.txt").write_text("base\n")
        git(self.wt, "commit", "-q", "-am", "base")
        git(self.wt, "switch", "-q", "feat/1-x")
        (self.wt / "f.txt").write_text("feature\n")
        git(self.wt, "commit", "-q", "-am", "feature")
        head = git(self.wt, "rev-parse", "HEAD")
        subprocess.run(["git", "rebase", "base"], cwd=self.wt, capture_output=True)
        ok, reason = self.verify("resolve-conflict", head)
        self.assertFalse(ok)
        self.assertIn("unfinished", reason)

    def test_unmoved_head_still_fails_without_reading_the_worktree(self) -> None:
        ok, _ = tick_verify.landed(
            "claude",
            {"action": "resolve-conflict", "pr": 410, "sha": OLD},
            SINCE,
            github(head=OLD),
            worktree="/nonexistent",
        )
        self.assertFalse(ok)


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

    def test_edited_verdict_does_not_count(self) -> None:
        # The rules say an edited verdict counts as no verdict.
        edited = comment(
            f"Review: APPROVED by Claude at {OLD}", edited_at="2026-09-28T12:06:00Z"
        )
        self.assertFalse(check("review", github(comments=[edited])))

    def test_pushing_actions_need_a_moved_head(self) -> None:
        for kind in ("fix-checks", "resolve-conflict"):
            with self.subTest(kind=kind):
                self.assertFalse(check(kind, github(comments=[comment("done")])))
        self.assertTrue(check("fix-checks", github(head=NEW)))
        # A moved head alone does not resolve a conflict: proof and record too.
        self.assertFalse(check("resolve-conflict", github(head=NEW)))
        # Codex keeps the moved-head rule until issue 537 adds its side.
        action = {"action": "resolve-conflict", "pr": 410, "sha": OLD}
        self.assertTrue(tick_verify.landed("codex", action, SINCE, github(head=NEW))[0])

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
