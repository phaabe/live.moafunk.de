"""Landing check tests. Run: python3 -m unittest discover -s scripts/epic."""

from __future__ import annotations

import isolated_env  # noqa: F401  (first: hides live runner state)

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import git_gate
import rebase_policy as rp
import tick_verify
from test_rebase_policy import ConflictFixture, comment as record_row

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


class CodexResolutionTest(ConflictFixture):
    """Codex resolve-conflict: the shared proof and record checks, real Git."""

    since = "2026-09-30T00:00:00Z"

    def pushed(self) -> tuple[str, str]:
        """(target tip, new head) after a proven conflict push."""
        tip = self.conflict()
        self.resolve()
        self.prove()
        self.assertEqual(self.run_approved(self.lease()).returncode, 0)
        new = self.remote_head()
        self.pr["head"]["sha"] = new
        return tip, new

    def attempt(self) -> dict[str, Any]:
        return json.loads((self.tmp / "attempt.json").read_text())

    def publish(self, agent: str = "codex") -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []

        def post(pr: int, body: str) -> None:
            rows.append(record_row(len(rows) + 100, body))

        with (
            mock.patch.object(rp, "read_pr", self.read_pr),
            mock.patch.object(rp, "comments", lambda n: list(rows)),
            mock.patch.object(rp, "post_comment", post),
        ):
            code, reason = rp.publish(
                agent, self.attempt(), self.wt,
                self.state / git_gate.RECORDS, self.state,
            )  # fmt: skip
        self.assertEqual(code, rp.RUN, reason)
        return rows

    def verify(self, new: str, rows: list[dict[str, Any]]) -> tuple[bool, str]:
        def fetch(args: list[str]) -> Any:
            if args[:2] == ["pr", "view"]:
                return {"state": "OPEN", "headRefOid": new}
            self.assertIn("--paginate", args)
            return [rows]

        return tick_verify.landed(
            "codex", self.action, self.since, fetch,
            worktree=str(self.wt), attempt=self.attempt(),
        )  # fmt: skip

    def test_proven_and_recorded_resolution_lands(self) -> None:
        _, new = self.pushed()
        ok, reason = self.verify(new, self.publish())
        self.assertTrue(ok, reason)

    def test_missing_record_fails(self) -> None:
        _, new = self.pushed()
        ok, reason = self.verify(new, [])
        self.assertFalse(ok)
        self.assertIn("no rebase record", reason)

    def test_invalid_record_fails(self) -> None:
        _, new = self.pushed()
        body = self.publish()[0]["body"]
        # Claude's record, another target tip, and a record from before the tick.
        claude = self.publish("claude")
        tip = [
            record_row(1, "\n".join(
                f"Target tip: {'c' * 40}" if line.startswith("Target tip: ") else line
                for line in body.split("\n")
            ))
        ]  # fmt: skip
        old = [record_row(2, body, at="2026-09-29T10:00:00Z")]
        for name, rows in (("claude", claude), ("tip", tip), ("old", old)):
            with self.subTest(name=name):
                self.assertFalse(self.verify(new, rows)[0])

    def test_missing_proof_fails(self) -> None:
        _, new = self.pushed()
        rows = self.publish()
        rp.proof_path(self.state, 7, new).unlink()
        ok, reason = self.verify(new, rows)
        self.assertFalse(ok)
        self.assertIn("no test proof", reason)

    def test_stale_proof_fails(self) -> None:
        _, new = self.pushed()
        rows = self.publish()
        path = rp.proof_path(self.state, 7, new)
        proof = json.loads(path.read_text())
        proof["onto"] = "c" * 40
        path.write_text(json.dumps(proof))
        ok, reason = self.verify(new, rows)
        self.assertFalse(ok)
        self.assertIn("the test proof is for target tip", reason)


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
        action = {"action": "resolve-conflict", "pr": 410, "sha": OLD}
        self.assertFalse(
            tick_verify.landed("codex", action, SINCE, github(head=NEW))[0]
        )

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


ISSUE = "https://github.com/phaabe/live.moafunk.de/issues/900"
DIGEST = "d" * 64


def issue_rows(*bodies: str, edited: bool = False, labels: tuple[str, ...] = ()):
    """Fetch for an issue: comment rows (REST shape) and its labels."""
    rows = [
        {
            "body": body,
            "created_at": "2026-09-28T12:05:00Z",
            "updated_at": "2026-09-28T12:09:00Z" if edited else "2026-09-28T12:05:00Z",
        }
        for body in bodies
    ]

    def fetch(args: list[str]) -> Any:
        if args[1].endswith("/issues/900"):
            return {"labels": [{"name": n} for n in labels]}
        return rows

    return fetch


def refine_landed(kind: str, fetch, agent: str = "claude", **extra: Any) -> bool:
    action = {"action": kind, "issue": ISSUE, "digest": DIGEST, **extra}
    return tick_verify.landed(agent, action, SINCE, fetch)[0]


class RefinementLandedTest(unittest.TestCase):
    def proposal(self, proposer: str) -> str:
        import refinement

        data = {key: [] for key in refinement.LIST_KEYS}
        data.update(request="> x", scope="x", executor=None, proposer=proposer)
        return f"{refinement.PROPOSAL_MARKER}\n```json\n{json.dumps(data)}\n```"

    def test_refine_needs_own_unedited_proposal(self) -> None:
        self.assertTrue(refine_landed("refine", issue_rows(self.proposal("Claude"))))
        self.assertFalse(refine_landed("refine", issue_rows(self.proposal("Codex"))))
        edited = issue_rows(self.proposal("Claude"), edited=True)
        self.assertFalse(refine_landed("refine", edited))
        self.assertFalse(refine_landed("refine", issue_rows("progress")))

    def test_refine_with_questions_for_anton(self) -> None:
        asked = issue_rows("Questions for Anton: ...", labels=("needs-anton",))
        self.assertTrue(refine_landed("refine", asked))
        # The label alone, without a new comment, is no refine run.
        self.assertFalse(refine_landed("refine", issue_rows(labels=("needs-anton",))))

    def test_review_refinement_needs_own_verdict_for_the_digest(self) -> None:
        mine = f"Refinement: APPROVED by Claude at {DIGEST}"
        self.assertTrue(refine_landed("review-refinement", issue_rows(mine)))
        for other in (
            f"Refinement: APPROVED by Codex at {DIGEST}",
            f"Refinement: APPROVED by Claude at {'e' * 64}",
            mine + "\n",
        ):
            with self.subTest(body=other):
                self.assertFalse(refine_landed("review-refinement", issue_rows(other)))
        self.assertFalse(
            refine_landed("review-refinement", issue_rows(mine, edited=True))
        )

    def test_set_ready_reads_the_board_status(self) -> None:
        def board(status: str | None):
            return lambda: [{"content": {"url": ISSUE}, "status": status}]

        action = {"action": "set-ready", "issue": ISSUE}
        landed = tick_verify.landed("claude", action, SINCE, items=board("Ready"))
        self.assertTrue(landed[0])
        for status in ("Backlog", None):
            with self.subTest(status=status):
                self.assertFalse(
                    tick_verify.landed("claude", action, SINCE, items=board(status))[0]
                )
        self.assertFalse(tick_verify.landed("claude", action, SINCE, items=list)[0])

    def test_issue_url_is_required(self) -> None:
        with self.assertRaises(ValueError):
            tick_verify.landed("claude", {"action": "refine"}, SINCE, issue_rows())
