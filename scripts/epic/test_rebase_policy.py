"""Rebase policy (rebase_policy.py): proof, record, focused scope, attempts.

Real Git with a local bare remote; GitHub reads and writes are fakes. The
runner flow through the real claude-tick.sh is in test_rebase_runner.py.

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
from unittest import mock

import git_gate
import github_quota
import next_action as na
import rebase_policy as rp
from test_completed_tickets import item, state as board_state, with_tickets
from test_git_gate import BASE, BRANCH, ISOLATED, Fixture, sh

ISSUES = "https://github.com/phaabe/live.moafunk.de/issues"
PULL = "https://github.com/phaabe/live.moafunk.de/pull/7"


def lease_sha(command: str) -> str:
    """The expected SHA of a `--force-with-lease=refs/heads/<b>:<sha>` push."""
    word = next(w for w in command.split() if w.startswith("--force-with-lease="))
    return word.rsplit(":", 1)[1]


class ConflictFixture(Fixture):
    """A PR whose rebase conflicts in f.txt, resolved through the gate."""

    def conflict(self) -> str:
        """PR edits f.txt; the base edits the same line. Returns the target tip."""
        (self.wt / "f.txt").write_text("one\nfeature two\n")
        sh(self.wt, "commit", "-q", "-am", "touch f")
        sh(self.wt, "push", "-q", "origin", BRANCH)
        self.head = self.remote_head()
        self.pr["head"]["sha"] = self.head
        self.set_action({**self.action, "sha": self.head})
        self.advance_base("one\nbase two\n")
        out = self.run_approved(f"git -C {self.wt} rebase origin/{BASE}")
        self.assertIn("CONFLICT", out.stdout + out.stderr)
        return self.remote_head(BASE)

    def resolve(self) -> None:
        (self.wt / "f.txt").write_text("one\nresolved\n")
        self.assertEqual(
            self.run_approved(f"git -C {self.wt} add -- f.txt").returncode, 0
        )
        out = self.run_approved(f"git -C {self.wt} rebase --continue")
        self.assertEqual(out.returncode, 0, out.stderr)

    def refused(self, command: str, text: str) -> None:
        ok, reason = self.decide(command)
        self.assertFalse(ok, command)
        self.assertIn(text, reason)


class ProofGateTest(ConflictFixture):
    """The gate refuses the lease push without a valid proof for HEAD."""

    def test_conflicted_rebase_with_proof_pushes_on_the_old_head(self) -> None:
        tip = self.conflict()
        self.resolve()
        self.assertEqual(git_gate.records()[BRANCH]["conflicted"], ["f.txt"])
        self.refused(self.lease(), "no valid test proof")
        proof = self.prove()
        self.assertEqual(proof["onto"], tip)
        self.assertEqual([s["result"] for s in proof["suites"]], ["passed"])
        # The lease expects the old remote PR head, never the target tip.
        self.refused(self.lease(tip), "the lease must pin")
        command = self.lease()
        self.assertEqual(lease_sha(command), self.head)
        self.assertNotEqual(lease_sha(command), tip)
        out = self.run_approved(command)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(
            self.remote_head(), sh(self.wt, "rev-parse", "HEAD").stdout.strip()
        )

    def test_failed_suite_gives_no_proof(self) -> None:
        self.conflict()
        self.resolve()
        self.suite_command = ["python3", "-c", "raise SystemExit(3)"]
        self.write_suites()
        proof = self.prove()
        self.assertEqual(proof["suites"][0]["result"], "failed")
        self.refused(self.lease(), "suite files failed")

    def test_skipped_suite_gives_no_proof(self) -> None:
        self.conflict()
        self.resolve()
        self.suite_command = ["no-such-test-runner-536"]
        self.write_suites()
        proof = self.prove()
        self.assertEqual(proof["suites"][0]["result"], "skipped")
        self.refused(self.lease(), "suite files skipped")

    def test_uncommitted_edit_after_the_proof(self) -> None:
        self.conflict()
        self.resolve()
        self.prove()
        (self.wt / "g.txt").write_text("edited after the proof\n")
        self.refused(self.lease(), "uncommitted")
        sh(self.wt, "add", "g.txt")  # staged is no better
        self.refused(self.lease(), "uncommitted")
        sh(self.wt, "reset", "-q", "--hard")
        (self.wt / "new.txt").write_text("untracked\n")
        self.refused(self.lease(), "uncommitted")
        (self.wt / "new.txt").unlink()
        self.assertTrue(self.allowed(self.lease()))

    def test_new_commit_after_the_proof(self) -> None:
        self.conflict()
        self.resolve()
        self.prove()
        (self.wt / "g.txt").write_text("late change\n")
        sh(self.wt, "commit", "-q", "-am", "after the proof")
        self.refused(self.lease(), "no test proof for")

    def test_required_suite_missing_from_the_proof(self) -> None:
        self.conflict()
        self.resolve()
        self.prove()
        extra = {"name": "more", "paths": ["f.txt"], "cwd": ".",
                 "command": ["python3", "-c", "pass"]}  # fmt: skip
        table = json.loads((self.tmp / "suites.json").read_text()) + [extra]
        (self.tmp / "suites.json").write_text(json.dumps(table))
        self.refused(self.lease(), "required suite more did not run")

    def test_proof_needs_a_clean_tree(self) -> None:
        self.conflict()
        self.resolve()
        (self.wt / "f.txt").write_text("dirty\n")
        with self.assertRaises(rp.Problem):
            self.prove()

    def test_tests_that_leave_files_give_no_proof(self) -> None:
        self.conflict()
        self.resolve()
        self.suite_command = ["python3", "-c", "open('junk.txt', 'w').write('x')"]
        self.write_suites()
        with self.assertRaisesRegex(rp.Problem, "tests left changes"):
            self.prove()

    def test_rebase_only_onto_the_pinned_target_tip(self) -> None:
        pinned = self.remote_head(BASE)
        self.advance_base("zero\none\ntwo\n")
        attempt = json.loads((self.tmp / "attempt.json").read_text())
        (self.tmp / "attempt.json").write_text(json.dumps({**attempt, "tip": pinned}))
        self.refused(
            f"git -C {self.wt} rebase origin/{BASE}", "pinned for this attempt"
        )

    def test_unfinished_rebase_onto_an_older_tip_is_not_resumed(self) -> None:
        # Review finding: a later tick pinned T2 but resumed and pushed the
        # rebase an earlier tick left unfinished on T1.
        self.conflict()
        self.advance_base("one\nbase two\nthree\n")  # the next tick pins T2
        (self.wt / "f.txt").write_text("one\nresolved\n")
        self.assertTrue(self.allowed(f"git -C {self.wt} add -- f.txt"))
        self.refused(f"git -C {self.wt} rebase --continue", "abort it and rebase again")
        self.assertTrue(self.allowed(f"git -C {self.wt} rebase --abort"))

    def test_finished_rebase_onto_an_older_tip_is_not_pushed(self) -> None:
        self.conflict()
        self.resolve()
        self.prove()
        self.advance_base("one\nbase two\nthree\n")  # the next tick pins T2
        self.refused(self.lease(), "abort it and rebase again")

    def test_other_rebasing_actions_need_no_pin(self) -> None:
        self.set_action({"action": "fix", "reason": "t", "pr": 7, "sha": self.head})
        self.advance_base("zero\none\ntwo\n")
        (self.tmp / "attempt.json").unlink()
        self.assertTrue(self.allowed(f"git -C {self.wt} rebase origin/{BASE}"))


class PublishTest(ConflictFixture):
    """The runner's record and verify's checks, after a real lease push."""

    def pushed(self) -> tuple[str, str, str]:
        """(old head, target tip, new head) after a proven conflict push."""
        tip = self.conflict()
        self.resolve()
        self.prove()
        self.assertEqual(self.run_approved(self.lease()).returncode, 0)
        new = self.remote_head()
        self.pr["head"]["sha"] = new
        return self.head, tip, new

    def attempt(self) -> dict[str, Any]:
        return json.loads((self.tmp / "attempt.json").read_text())

    def publish(self, rows: list[dict[str, Any]]) -> tuple[int, str]:
        def post(pr: int, body: str) -> None:
            rows.append(comment(len(rows) + 100, body))

        with (
            mock.patch.object(rp, "read_pr", lambda n: self.read_pr(n)),
            mock.patch.object(rp, "comments", lambda n: list(rows)),
            mock.patch.object(rp, "post_comment", post),
        ):
            return rp.publish(
                "claude", self.attempt(), self.wt,
                self.state / git_gate.RECORDS, self.state,
            )  # fmt: skip

    def test_record_is_posted_once_and_verifies(self) -> None:
        old, tip, new = self.pushed()
        rows: list[dict[str, Any]] = []
        self.assertEqual(self.publish(rows)[0], 0)
        self.assertEqual(
            self.publish(rows), (0, f"record for {new[:7]} already posted")
        )
        self.assertEqual(len(rows), 1)
        fields = rp.parse_record(rows[0]["body"], "Claude")
        assert fields is not None
        self.assertEqual(fields["Old head"], old)
        self.assertEqual(fields["New head"], new)
        self.assertEqual(fields["Target tip"], tip)
        base0 = sh(self.wt, "merge-base", old, tip).stdout.strip()
        self.assertEqual(fields["Old series base"], base0)
        self.assertEqual(fields["Conflicted files"], "f.txt")
        self.assertIn("files=passed", fields["Proof"])
        self.assertFalse(rows[0]["body"].endswith("\n"))
        self.assertNotIn("Review:", rows[0]["body"])
        problem = rp.verify_resolution(
            "claude",
            self.attempt(),
            new,
            self.wt,
            self.state,
            rows,
            "2000-01-01T00:00:00Z",
        )
        self.assertIsNone(problem)

    def test_moved_head_without_a_record_or_proof_does_not_verify(self) -> None:
        _, _, new = self.pushed()
        since = "2000-01-01T00:00:00Z"
        problem = rp.verify_resolution(
            "claude", self.attempt(), new, self.wt, self.state, [], since
        )
        self.assertIn("no rebase record", problem or "")
        rp.proof_path(self.state, 7, new).unlink()
        problem = rp.verify_resolution(
            "claude", self.attempt(), new, self.wt, self.state, [], since
        )
        self.assertIn("no test proof", problem or "")

    def test_mismatched_record_does_not_verify(self) -> None:
        _, _, new = self.pushed()
        rows: list[dict[str, Any]] = []
        self.publish(rows)
        body = rows[0]["body"]
        since = "2000-01-01T00:00:00Z"
        for field, value in (
            ("Target tip", "c" * 40),
            ("PR", "8"),
            ("Old series base", new),
            ("Proof", "x"),
        ):
            with self.subTest(field=field):
                lines = [
                    f"{field}: {value}" if line.startswith(f"{field}: ") else line
                    for line in body.split("\n")
                ]
                bad = [comment(1, "\n".join(lines))]
                self.assertIsNotNone(
                    rp.verify_resolution(
                        "claude", self.attempt(), new, self.wt, self.state, bad, since
                    )
                )

    def test_nothing_to_publish_without_a_push(self) -> None:
        self.conflict()
        self.resolve()
        self.assertEqual(self.publish([])[0], rp.NOT_DONE)


def comment(
    n: int, body: str, at: str | None = None, edited: bool = False
) -> dict[str, Any]:
    created = at or f"2026-09-30T10:{n % 60:02d}:00Z"
    return {
        "id": n,
        "body": body,
        "created_at": created,
        "updated_at": "2026-09-30T23:59:00Z" if edited else created,
        "html_url": f"{PULL}#issuecomment-{n}",
    }


def git(cwd: Path, *args: str) -> str:
    return sh(cwd, *args).stdout.strip()


class ScopeTest(unittest.TestCase):
    """Focused re-review scope on the normal timeline of the ticket.

    Claude reviewed Codex's head H0, built on base B0. The target moves to B1
    before the rebase tick: B0..B1 changes the return value of `check` and
    edits notes.txt, which the PR also edits (a conflict in another file). The
    PR's caller is unchanged, so range-diff marks its commit identical.
    """

    def setUp(self) -> None:
        env = mock.patch.dict(os.environ, ISOLATED)
        env.start()
        self.addCleanup(env.stop)
        tmp = tempfile.TemporaryDirectory(prefix="rebase-scope-")
        self.addCleanup(tmp.cleanup)
        self.repo = Path(os.path.realpath(tmp.name))
        git(self.repo, "init", "-q", "-b", BASE)
        (self.repo / "api.py").write_text("def check(x):\n    return False\n")
        (self.repo / "notes.txt").write_text("one\ntwo\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "B0")
        self.b0 = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "switch", "-q", "-c", BRANCH)
        (self.repo / "caller.py").write_text(
            "from api import check\n\ndef allowed(x):\n    return bool(check(x))\n"
        )
        git(self.repo, "add", "caller.py")
        git(self.repo, "commit", "-q", "-m", "caller")
        (self.repo / "notes.txt").write_text("one\nfeature two\n")
        git(self.repo, "commit", "-q", "-am", "notes")
        self.h0 = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "switch", "-q", BASE)
        (self.repo / "api.py").write_text(
            "def check(x):\n    return {'allowed': False}\n"
        )
        (self.repo / "notes.txt").write_text("one\nbase two\n")
        git(self.repo, "commit", "-q", "-am", "B1")
        self.b1 = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "switch", "-q", BRANCH)
        sh(self.repo, "rebase", BASE, check=False)
        (self.repo / "notes.txt").write_text("one\nresolved two\n")
        git(self.repo, "add", "notes.txt")
        sh(self.repo, "-c", "core.editor=true", "rebase", "--continue")
        self.h1 = git(self.repo, "rev-parse", "HEAD")
        self.rows = [
            comment(1, "Finding: allowed() must not treat a dict as True."),
            comment(2, f"Review: CHANGES REQUESTED by Claude at {self.h0}"),
            comment(3, self.record()),
        ]

    def record(self, **change: str) -> str:
        fields = {
            "Repository": rp.REPO,
            "PR": "7",
            "Old head": self.h0,
            "New head": self.h1,
            "Old series base": self.b0,
            "Target tip": self.b1,
            "Conflicted files": "notes.txt",
            "Proof": f"{self.h1} tree x; codex=passed",
            **change,
        }
        return "\n".join(
            ["Rebase record by Codex"] + [f"{k}: {fields[k]}" for k in rp.RECORD_FIELDS]
        )

    def scope(
        self, rows: list[dict[str, Any]] | None = None, tip: str | None = None
    ) -> dict[str, Any]:
        return rp.scope(
            "claude",
            7,
            self.h1,
            self.repo,
            self.rows if rows is None else rows,
            tip or self.b1,
        )

    def test_semantic_base_change_is_in_the_focused_scope(self) -> None:
        found = self.scope()
        self.assertEqual(found["mode"], "focused", found["reason"])
        # Old series base B0, not the tip seen before the rebase (B1..B1 is empty).
        self.assertEqual(found["old_series_base"], self.b0)
        self.assertEqual(found["base_changes"], f"{self.b0}..{self.b1}")
        self.assertIn("api.py", found["base_changes_in_scope"])
        self.assertIn("'allowed': False", found["base_diff"])
        # range-diff calls the caller commit identical: it cannot see this.
        caller = next(l for l in found["range_diff"].splitlines() if "caller" in l)
        self.assertIn(" = ", caller)

    def test_conflict_in_another_file_is_in_scope(self) -> None:
        found = self.scope()
        self.assertEqual(found["conflicted_files"], ["notes.txt"])
        self.assertIn("notes.txt", found["base_changes_in_scope"])
        self.assertIn("api.py", found["base_changes_in_scope"])

    def test_unresolved_earlier_finding_survives_the_rebase(self) -> None:
        found = self.scope()
        self.assertEqual(found["last_verdict"], "CHANGES REQUESTED")
        self.assertEqual(found["open_findings"], [f"{PULL}#issuecomment-1"])

    def test_findings_stay_open_across_later_reviews(self) -> None:
        # Review finding: a second changes-requested review that does not
        # repeat a finding must not drop it.
        rows = [
            comment(1, "Finding: allowed() must not treat a dict as True."),
            comment(2, f"Review: CHANGES REQUESTED by Claude at {self.b0}"),
            comment(3, f"Review: CHANGES REQUESTED by Claude at {self.h0}"),
            comment(4, self.record()),
        ]
        found = self.scope(rows)
        self.assertEqual(found["mode"], "focused", found["reason"])
        self.assertEqual(found["open_findings"], [f"{PULL}#issuecomment-1"])

    def test_an_approval_closes_earlier_findings(self) -> None:
        rows = [
            comment(1, "Finding: old, fixed."),
            comment(2, f"Review: APPROVED by Claude at {self.b0}"),
            comment(3, "Finding: new."),
            comment(4, f"Review: CHANGES REQUESTED by Claude at {self.h0}"),
            comment(5, self.record()),
        ]
        self.assertEqual(self.scope(rows)["open_findings"], [f"{PULL}#issuecomment-3"])

    def test_approved_last_review_has_no_open_findings(self) -> None:
        rows = [
            comment(1, "Finding: fixed later."),
            comment(2, f"Review: APPROVED by Claude at {self.h0}"),
            comment(3, self.record()),
        ]
        self.assertEqual(self.scope(rows)["open_findings"], [])

    def assert_full(self, found: dict[str, Any], text: str) -> None:
        self.assertEqual(found["mode"], "full")
        self.assertIn(text, found["reason"])

    def test_mismatched_records_mean_a_full_review(self) -> None:
        cases = {
            "old head": (self.record(**{"Old head": self.b1}), "old head is not"),
            "repository": (self.record(Repository="x/y"), "record is for x/y"),
            "pr": (self.record(PR="8"), "record is for PR 8"),
            "old base": (self.record(**{"Old series base": self.b1}), "merge-base"),
        }
        for name, (body, text) in cases.items():
            with self.subTest(name=name):
                rows = [*self.rows[:2], comment(3, body)]
                self.assert_full(self.scope(rows), text)

    def test_record_old_head_must_be_the_last_verdict_head(self) -> None:
        rows = [
            comment(1, f"Review: APPROVED by Claude at {self.b0}"),
            comment(3, self.record()),
        ]
        self.assert_full(self.scope(rows), "old head is not")

    def test_malformed_edited_or_missing_record(self) -> None:
        for name, rows in {
            "malformed": [*self.rows[:2], comment(3, self.record() + "\nextra")],
            "edited": [*self.rows[:2], comment(3, self.record(), edited=True)],
            "missing": self.rows[:2],
            "by the reviewer": [
                *self.rows[:2],
                comment(3, self.record().replace("by Codex", "by Claude")),
            ],
        }.items():
            with self.subTest(name=name):
                self.assert_full(self.scope(rows), "no valid rebase record")

    def test_no_earlier_verdict_means_a_full_review(self) -> None:
        self.assert_full(self.scope([comment(3, self.record())]), "no earlier verdict")

    def test_later_base_advance_means_a_full_review(self) -> None:
        self.assert_full(self.scope(tip="d" * 40), "base advanced")

    def test_missing_old_objects_mean_a_full_review(self) -> None:
        gone = "e" * 40
        body = self.record(**{"Old head": gone})
        rows = [
            comment(2, f"Review: CHANGES REQUESTED by Claude at {gone}"),
            comment(3, body),
        ]
        self.assert_full(self.scope(rows), "missing locally")


class ChangedFilesTest(unittest.TestCase):
    def test_non_ascii_path_selects_its_suite(self) -> None:
        # Review finding: Git quotes non-ASCII names without -z.
        env = mock.patch.dict(os.environ, ISOLATED)
        env.start()
        self.addCleanup(env.stop)
        tmp = tempfile.TemporaryDirectory(prefix="rebase-names-")
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        git(repo, "init", "-q", "-b", BASE)
        (repo / "README").write_text("x\n")
        git(repo, "add", ".")
        git(repo, "commit", "-q", "-m", "base")
        base = git(repo, "rev-parse", "HEAD")
        (repo / "frontend/src").mkdir(parents=True)
        (repo / "frontend/src/über.ts").write_text("export {}\n")
        git(repo, "add", ".")
        git(repo, "commit", "-q", "-m", "feature")
        paths = rp.touched(repo, base, "HEAD")
        self.assertEqual(paths, ["frontend/src/über.ts"])
        self.assertEqual([s["name"] for s in rp.required(paths, rp.suites())], ["frontend"])


class AttemptTest(unittest.TestCase):
    """The shared attempt store: counting, reset rules and escalation."""

    HEAD = "a" * 40
    TIP = "b" * 40

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="rebase-attempts-")
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.tip = self.TIP
        self.posts: list[int] = []
        self.post_fails = False
        for name, value in (
            ("base_ref", lambda pr: BASE),
            ("base_tip", lambda ref: self.tip),
        ):
            patcher = mock.patch.object(rp, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def post(self, pr: int) -> None:
        self.posts.append(pr)
        if self.post_fails:
            raise subprocess.CalledProcessError(1, ["gh"], "", "HTTP 502")

    def action(self, head: str | None = None) -> dict[str, Any]:
        return {"action": "resolve-conflict", "pr": 526, "sha": head or self.HEAD}

    def check(self, head: str | None = None) -> int:
        return rp.attempt_check(
            self.dir, "claude", self.action(head), self.dir / "attempt.json", self.post
        )

    def attempt(self, tick: str, outcome: str | None) -> int:
        """One tick: check, start and (unless it crashed) finish."""
        code = self.check()
        if code != rp.RUN:
            return code
        pin = rp.load_attempt(self.dir / "attempt.json")
        self.assertEqual(rp.attempt_start(self.dir, pin, tick), rp.RUN)
        if outcome:
            rp.attempt_finish(self.dir, pin, tick, outcome, self.post)
        return rp.RUN

    def store(self) -> dict[str, Any]:
        return json.loads((self.dir / rp.ATTEMPTS).read_text())

    def test_two_failures_suppress_the_key_and_post_the_label(self) -> None:
        self.assertEqual(self.attempt("t1", "failed"), rp.RUN)
        self.assertEqual(self.posts, [])
        self.assertEqual(self.attempt("t2", "failed"), rp.RUN)
        self.assertEqual(self.posts, [526])
        self.assertEqual(self.check(), rp.SKIP)
        self.assertEqual(self.posts, [526])  # posted once
        (entry,) = self.store().values()
        self.assertEqual(entry["escalation"], "posted")

    def refine_setup(self) -> list[dict[str, Any]]:
        """Fake issue comments behind rp.comments and rp.post_comment."""
        rows: list[dict[str, Any]] = []
        self.comment_fails = False

        def post_comment(issue: int, body: str) -> None:
            self.assertEqual(issue, 900)
            if self.comment_fails:
                raise subprocess.CalledProcessError(1, ["gh"], "", "HTTP 502")
            n = 7000 + len(rows)
            at = f"2026-10-06T10:00:{len(rows):02d}Z"
            rows.append(
                {
                    "id": n,
                    "body": body,
                    "created_at": at,
                    "updated_at": at,
                    "html_url": f"https://github.com/{rp.REPO}/issues/900#issuecomment-{n}",
                }
            )

        for name, value in (
            ("comments", lambda issue: list(rows)),
            ("post_comment", post_comment),
        ):
            patcher = mock.patch.object(rp, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        return rows

    def refine(self, tick: str, outcome: str | None, key: str) -> int:
        action = {
            "action": "refine",
            "issue": f"https://github.com/{rp.REPO}/issues/900",
            "attempt_key": key,
        }
        out = self.dir / "attempt.json"
        code = rp.attempt_check(self.dir, "claude", action, out, self.post)
        if code != rp.RUN:
            return code
        pin = rp.load_attempt(out)
        self.assertEqual(rp.attempt_start(self.dir, pin, tick), rp.RUN)
        if outcome:
            rp.attempt_finish(self.dir, pin, tick, outcome, self.post)
        return rp.RUN

    @staticmethod
    def as_issue_comments(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {"id": r["id"], "body": r["body"], "createdAt": r["created_at"],
             "url": r["html_url"], "includesCreatedEdit": False}
            for r in rows
        ]  # fmt: skip

    def test_refine_limit_posts_an_escalation_anton_can_reset(self) -> None:
        import refinement as rf

        rows = self.refine_setup()
        key = rf.attempt_key(900, [], None)
        self.assertEqual(key, "refine:900:0:none")
        for tick in ("t1", "t2"):
            self.assertEqual(self.refine(tick, "failed", key), rp.RUN)
        (esc,) = rows
        self.assertEqual(esc["body"].partition("\n")[0], rf.ESCALATION_MARKER)
        self.assertIn(f"`{key}`", esc["body"])
        self.assertEqual(self.posts, [900])
        self.assertEqual(self.store()[key]["escalation_url"], esc["html_url"])
        self.assertEqual(self.refine("t3", "failed", key), rp.SKIP)
        self.assertEqual(len(rows), 1)
        # The selector now sees the escalation; Anton's reset makes a new key.
        issue = self.as_issue_comments(rows)
        self.assertTrue(rf.escalated(issue))
        self.assertEqual(rf.attempt_key(900, issue, None), key)
        issue.append(
            {"id": 7100, "body": f"Refinement reset: Anton for {esc['html_url']}",
             "createdAt": "2026-10-06T11:00:00Z", "url": "u",
             "includesCreatedEdit": False}
        )  # fmt: skip
        self.assertFalse(rf.escalated(issue))
        reset_key = rf.attempt_key(900, issue, None)
        self.assertEqual(reset_key, "refine:900:0:7100")
        self.assertEqual(self.refine("t4", "failed", reset_key), rp.RUN)

    def test_failed_refine_escalation_is_retried_without_duplicates(self) -> None:
        rows = self.refine_setup()
        key = "refine:900:0:none"
        self.comment_fails = True
        for tick in ("t1", "t2"):
            self.assertEqual(self.refine(tick, "failed", key), rp.RUN)
        self.assertEqual((rows, self.posts), ([], []))  # no label without comment
        self.assertEqual(self.store()[key]["escalation"], "pending")
        self.comment_fails = False
        self.post_fails = True
        self.assertEqual(self.refine("t3", None, key), rp.SKIP)
        self.assertEqual((len(rows), self.posts), (1, [900]))
        self.post_fails = False
        self.assertEqual(self.refine("t4", None, key), rp.SKIP)
        self.assertEqual((len(rows), self.posts), (1, [900, 900]))
        self.assertEqual(self.store()[key]["escalation"], "posted")

    def test_refine_escalation_posted_before_a_crash_is_reused(self) -> None:
        rows = self.refine_setup()
        key = "refine:900:0:none"
        rp.post_comment(900, f"<!-- epic-refinement-escalation v1 -->\n`{key}`")
        for tick in ("t1", "t2"):
            self.assertEqual(self.refine(tick, "failed", key), rp.RUN)
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.store()[key]["escalation_url"], rows[0]["html_url"])

    def test_crashed_attempt_counts_after_a_restart(self) -> None:
        self.attempt("t1", None)  # the runner died after the model started
        self.attempt("t2", "failed")
        self.assertEqual(self.check(), rp.SKIP)

    def test_success_and_void_do_not_count(self) -> None:
        self.attempt("t1", "void")
        self.attempt("t2", "succeeded")
        self.attempt("t3", "failed")
        self.assertEqual(self.check(), rp.RUN)

    def test_duplicate_outcome_and_start_count_once(self) -> None:
        self.attempt("t1", "failed")
        pin = rp.load_attempt(self.dir / "attempt.json")
        rp.attempt_start(self.dir, pin, "t1")
        rp.attempt_finish(self.dir, pin, "t1", "succeeded", self.post)
        (entry,) = self.store().values()
        self.assertEqual([a["outcome"] for a in entry["attempts"]], ["failed"])
        self.assertEqual(self.check(), rp.RUN)

    def test_new_head_or_new_base_tip_is_a_new_key(self) -> None:
        self.attempt("t1", "failed")
        self.attempt("t2", "failed")
        self.assertEqual(self.check(), rp.SKIP)
        self.assertEqual(self.check(head="c" * 40), rp.RUN)
        self.tip = "d" * 40
        self.assertEqual(self.check(), rp.RUN)

    def test_failed_label_post_is_retried_without_a_model(self) -> None:
        self.attempt("t1", "failed")
        self.post_fails = True
        self.attempt("t2", "failed")
        (entry,) = self.store().values()
        self.assertEqual(entry["escalation"], "pending")
        # Still suppressed; each check retries only the post.
        self.assertEqual(self.check(), rp.SKIP)
        self.assertEqual(self.posts, [526, 526])
        self.post_fails = False
        self.assertEqual(self.check(), rp.SKIP)
        self.assertEqual(self.check(), rp.SKIP)
        self.assertEqual(self.posts, [526, 526, 526])
        (entry,) = self.store().values()
        self.assertEqual(entry["escalation"], "posted")

    def test_limit_reached_between_check_and_start(self) -> None:
        self.attempt("t1", "failed")
        self.assertEqual(self.check(), rp.RUN)
        pin = rp.load_attempt(self.dir / "attempt.json")
        # The other runner fails the same key meanwhile.
        rp.attempt_start(self.dir, pin, "codex-1")
        rp.attempt_finish(self.dir, pin, "codex-1", "failed", self.post)
        self.assertEqual(rp.attempt_start(self.dir, pin, "t2"), rp.SKIP)

    def test_limit_is_configurable(self) -> None:
        with mock.patch.dict(os.environ, {rp.LIMIT_ENV: "3"}):
            self.attempt("t1", "failed")
            self.attempt("t2", "failed")
            self.assertEqual(self.check(), rp.RUN)
        with mock.patch.dict(os.environ, {rp.LIMIT_ENV: "0"}):
            with self.assertRaises(ValueError):
                self.check()

    def test_status_lines(self) -> None:
        self.attempt("t1", "failed")
        self.attempt("t2", "failed")
        (line,) = rp.attempt_lines(self.dir)
        self.assertIn("failed 2/2 suppressed escalation=posted", line)


class SelectorTest(unittest.TestCase):
    def pr(self, mergeable: str, merge_state: str) -> dict[str, Any]:
        return {
            "number": 7,
            "body": f"Issue: {ISSUES}/907\nExecutor: Claude",
            "baseRefName": BASE,
            "headRefOid": "a" * 40,
            "isDraft": False,
            "labels": [],
            "mergeable": mergeable,
            "mergeStateStatus": merge_state,
            "statusCheckRollup": [{"conclusion": "SUCCESS"}],
            "comments": [],
        }

    def kinds(self, pr: dict[str, Any]) -> list[str]:
        found = na.decide("Claude", {"prs": [pr], "items": []})
        return [a.action for a in found]

    def test_only_behind_is_not_rebased(self) -> None:
        self.assertNotIn("resolve-conflict", self.kinds(self.pr("MERGEABLE", "BEHIND")))

    def test_conflicting_is_rebased(self) -> None:
        self.assertEqual(
            self.kinds(self.pr("CONFLICTING", "DIRTY")), ["resolve-conflict"]
        )


class QuotaDirTest(unittest.TestCase):
    """A quota hit stores the wait where every agent reads it."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="rebase-quota-")
        self.addCleanup(tmp.cleanup)
        self.shared = Path(tmp.name)
        self.agent = self.shared / "agents/claude-2"
        self.agent.mkdir(parents=True)
        self.attempt = self.shared / "attempt.json"
        self.attempt.write_text(json.dumps({"key": "k", "pr": 7}))

    def publish(self, env: dict[str, str], *extra: str) -> int:
        argv = ["rebase_policy.py", "publish", "--agent", "claude",
                "--attempt-file", str(self.attempt), "--worktree", ".",
                "--rebases-file", str(self.shared / "r.json"), *extra]  # fmt: skip

        def hit(*_: Any) -> Any:
            raise rp.QuotaExhausted("GraphQL: API rate limit exceeded")

        with (
            mock.patch.dict(os.environ, env),
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(rp, "publish", hit),
            mock.patch.object(github_quota, "query_reset_at", lambda: (None, None)),
        ):
            return rp.main()

    def test_registered_agent_stores_the_wait_in_the_shared_dir(self) -> None:
        # claude-tick.sh runs publish without --state-dir: EPIC_STATE_DIR is
        # the agent folder, EPIC_QUOTA_DIR the shared one.
        env = {"EPIC_STATE_DIR": str(self.agent), "EPIC_QUOTA_DIR": str(self.shared)}
        for extra in ((), ("--state-dir", str(self.agent))):
            with self.subTest(extra=extra):
                (self.shared / github_quota.WAIT_FILE).unlink(missing_ok=True)
                self.assertEqual(self.publish(env, *extra), github_quota.QUOTA)
                self.assertTrue((self.shared / github_quota.WAIT_FILE).exists())
                self.assertFalse((self.agent / github_quota.WAIT_FILE).exists())

    def test_without_a_quota_dir_the_state_dir_keeps_the_wait(self) -> None:
        env = {"EPIC_STATE_DIR": str(self.agent), "EPIC_QUOTA_DIR": ""}
        self.assertEqual(self.publish(env), github_quota.QUOTA)
        self.assertTrue((self.agent / github_quota.WAIT_FILE).exists())


class CodexDeliveryTest(unittest.TestCase):
    """https://github.com/phaabe/live.moafunk.de/issues/537 becomes eligible
    after this delivery, in both completion modes: no cycle back to 536."""

    def state(self, closed: bool) -> dict[str, Any]:
        reason = "completed" if closed else None
        return board_state(
            item(536, "Done", "Claude", "closed" if closed else "open", reason),
            item(431, "Done", "Codex", "closed", "completed"),
            item(
                537,
                "Ready",
                "Codex",
                readiness=f"Ready. Start after {ISSUES}/536, {ISSUES}/431.",
            ),
            merged=(f"Issue: {ISSUES}/536", f"Issue: {ISSUES}/431"),
        )

    def first(self, s: dict[str, Any], completed: bool) -> na.Action:
        return na.decide("Codex", s, completed_tickets=completed)[0]

    def test_537_is_claimable_after_536_in_both_modes(self) -> None:
        for completed in (False, True):
            with self.subTest(completed=completed):
                s = with_tickets(self.state(closed=True))
                found = self.first(s, completed)
                self.assertEqual(
                    (found.action, found.issue), ("claim", f"{ISSUES}/537")
                )

    def test_537_waits_while_536_is_open_under_completed_mode(self) -> None:
        s = with_tickets(self.state(closed=False))
        self.assertEqual(self.first(s, False).action, "claim")
        waiting = na.decide("Codex", s, include_waiting=True, completed_tickets=True)
        self.assertEqual([a.action for a in waiting], ["wait"])
        self.assertIn("536", waiting[0].reason)


if __name__ == "__main__":
    unittest.main()
