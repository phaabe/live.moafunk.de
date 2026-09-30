"""Close helper tests. Run: python3 -m unittest discover -s scripts/epic."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

import close_issue
from github_quota import QuotaExhausted

R = "https://github.com/phaabe/live.moafunk.de/issues"
SHA = "c" * 40


def _quiet() -> contextlib.redirect_stderr:
    """Hide the helper's stderr retry lines in test output."""
    return contextlib.redirect_stderr(io.StringIO())


def pull(
    number: int = 530, body: str = f"Issue: {R}/453\nLeaf IDs: setup", merged=True
) -> dict:
    return {
        "number": number,
        "body": body,
        "merged_at": "2026-09-30T10:00:00Z" if merged else None,
        "merge_commit_sha": SHA if merged else None,
        "base": {"ref": "dev/312-interim"},
    }


def ticket(state: str = "open", body: str = "", sub: int = 0, **extra) -> dict:
    return {
        "state": state,
        "body": body,
        "sub_issues_summary": {"total": sub, "completed": 0},
        **extra,
    }


class GitHub:
    """Fake REST reads and writes. `fail` names calls that raise once each."""

    def __init__(self, pulls: dict[int, dict], issues: dict[int, dict]) -> None:
        self.pulls, self.issues = pulls, issues
        self.comments: list[tuple[int, str]] = []
        self.closed: list[int] = []
        self.fail: list[str] = []

    def _maybe_fail(self, what: str) -> None:
        if what in self.fail:
            self.fail.remove(what)
            raise subprocess.CalledProcessError(1, ["gh"], "", f"{what}: HTTP 502")

    def fetch(self, args: list[str]) -> Any:
        path = args[1]
        self._maybe_fail("read")
        if "/pulls/" in path:
            return self.pulls[int(path.rsplit("/", 1)[1])]
        if path.endswith("/sub_issues?per_page=1"):
            return []
        return self.issues[int(path.rsplit("/", 1)[1])]

    def comment(self, number: int, text: str) -> None:
        self._maybe_fail("comment")
        self.comments.append((number, text))

    def close(self, number: int) -> None:
        self._maybe_fail("close")
        self.closed.append(number)
        self.issues[number]["state"] = "closed"


class QueueCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())

    def record(self, pr: int) -> bool:
        data = close_issue.load(self.dir)
        added = close_issue.enqueue(data, pr)
        close_issue.save(self.dir, data)
        return added

    def run_queue(self, gh: GitHub) -> int:
        return close_issue.run_queue(
            self.dir, gh.fetch, gh.comment, gh.close, now=lambda: 0.0
        )

    def queue(self) -> dict:
        return json.loads((self.dir / close_issue.QUEUE_FILE).read_text())


class CloseTest(QueueCase):
    def test_merge_closes_the_ticket_with_one_evidence_comment(self) -> None:
        body = f"Fix it.\n\nIssue: {R}/453\n\nValidation: `pytest`: 12 pass.\n\nLeaf IDs: setup"
        gh = GitHub({530: pull(body=body)}, {453: ticket()})
        self.record(530)
        self.assertEqual(self.run_queue(gh), 0)
        self.assertEqual(gh.closed, [453])
        self.assertEqual(len(gh.comments), 1)
        number, text = gh.comments[0]
        self.assertEqual(number, 453)
        for part in (
            "https://github.com/phaabe/live.moafunk.de/pull/530",
            SHA,
            "dev/312-interim",
            "`pytest`: 12 pass.",
        ):
            self.assertIn(part, text)
        self.assertEqual(self.queue()["pending"], [])
        self.assertEqual(self.queue()["done"][0]["result"], "close")

    def test_tests_fall_back_to_the_pr(self) -> None:
        body = f"Issue: {R}/453\nValidation: <!-- Commands and results -->\n"
        self.assertEqual(close_issue.tests_text(body), "see the PR")

    def test_partial_pr_keeps_the_ticket_open(self) -> None:
        gh = GitHub({530: pull(body=f"Issue: {R}/453 (partial)")}, {453: ticket()})
        self.record(530)
        self.assertEqual(self.run_queue(gh), 0)
        self.assertEqual((gh.closed, gh.comments), ([], []))
        self.assertEqual(self.queue()["done"][0]["result"], "skip")

    def test_uncovered_leaves_keep_the_ticket_open_and_say_so(self) -> None:
        issue = ticket(
            body="- [x] **B1.1.5** a\n- [ ] **B1.1.6** b\n- [ ] **B1.1.7** c"
        )
        gh = GitHub({530: pull(body=f"Issue: {R}/453\nLeaf IDs: B1.1.6")}, {453: issue})
        self.record(530)
        self.assertEqual(self.run_queue(gh), 0)
        self.assertEqual(gh.closed, [])
        self.assertEqual(len(gh.comments), 1)
        self.assertIn("B1.1.7", gh.comments[0][1])
        self.assertNotIn("B1.1.6", gh.comments[0][1])
        self.assertEqual(self.queue()["done"][0]["result"], "note")

    def test_all_leaves_covered_closes(self) -> None:
        issue = ticket(body="- [x] **B1.1.5** a\n- [ ] **B1.1.6** b")
        gh = GitHub({530: pull(body=f"Issue: {R}/453\nLeaf IDs: B1.1.6")}, {453: issue})
        self.record(530)
        self.run_queue(gh)
        self.assertEqual(gh.closed, [453])

    def test_never_closes_epic_umbrella_or_pr(self) -> None:
        cases = {
            "epic": (f"Issue: {R}/312", {312: ticket()}),
            "umbrella": (f"Issue: {R}/420", {420: ticket(sub=3)}),
            "umbrella, children closed": (
                f"Issue: {R}/420",
                {
                    420: {
                        "state": "open",
                        "body": "",
                        "sub_issues_summary": {"total": 2, "completed": 2},
                    }
                },
            ),
            "pull request": (f"Issue: {R}/9", {9: ticket(pull_request={"url": "x"})}),
            "two tickets": (f"Issue: {R}/453\nIssue: {R}/454", {}),
            "no ticket": ("Leaf IDs: setup", {}),
        }
        for name, (body, issues) in cases.items():
            with self.subTest(name):
                self.setUp()
                gh = GitHub({530: pull(body=body)}, issues)
                self.record(530)
                self.assertEqual(self.run_queue(gh), 0)
                self.assertEqual((gh.closed, gh.comments), ([], []))

    def test_sub_issues_read_when_summary_is_missing(self) -> None:
        gh = GitHub({530: pull()}, {453: {"state": "open", "body": ""}})
        self.record(530)
        self.run_queue(gh)
        self.assertEqual(gh.closed, [453])

    def test_already_closed_ticket_is_left_alone(self) -> None:
        gh = GitHub({530: pull()}, {453: ticket(state="closed")})
        self.record(530)
        self.assertEqual(self.run_queue(gh), 0)
        self.assertEqual((gh.closed, gh.comments), ([], []))


class OncePerMergeTest(QueueCase):
    def test_same_pr_is_queued_once(self) -> None:
        gh = GitHub({530: pull()}, {453: ticket()})
        self.assertTrue(self.record(530))
        self.assertFalse(self.record(530))
        self.run_queue(gh)
        # Reopened later: recording or retrying the old merge closes nothing.
        gh.issues[453]["state"] = "open"
        self.assertFalse(self.record(530))
        self.assertEqual(self.run_queue(gh), 0)
        self.assertEqual(gh.closed, [453])
        self.assertEqual(gh.issues[453]["state"], "open")

    def test_next_merge_closes_a_reopened_ticket(self) -> None:
        gh = GitHub({530: pull(), 531: pull(531)}, {453: ticket()})
        self.record(530)
        self.run_queue(gh)
        gh.issues[453]["state"] = "open"
        self.record(531)
        self.run_queue(gh)
        self.assertEqual(gh.closed, [453, 453])

    def test_unmerged_pr_can_be_queued_again_after_merge(self) -> None:
        gh = GitHub({530: pull(merged=False)}, {453: ticket()})
        self.record(530)
        self.assertEqual(self.run_queue(gh), 0)
        self.assertEqual(self.queue(), {"pending": [], "done": []})
        gh.pulls[530] = pull()
        self.assertTrue(self.record(530))
        self.run_queue(gh)
        self.assertEqual(gh.closed, [453])


class RetryTest(QueueCase):
    def test_failed_read_keeps_the_entry_and_retries(self) -> None:
        gh = GitHub({530: pull()}, {453: ticket()})
        gh.fail = ["read"]
        self.record(530)
        with _quiet():
            self.assertEqual(self.run_queue(gh), 1)
        pending = self.queue()["pending"]
        self.assertEqual([(e["pr"], e["attempts"]) for e in pending], [(530, 1)])
        self.assertIn("HTTP 502", pending[0]["last_error"])
        self.assertEqual(self.run_queue(gh), 0)
        self.assertEqual(gh.closed, [453])

    def test_failed_close_after_comment_does_not_comment_twice(self) -> None:
        gh = GitHub({530: pull()}, {453: ticket()})
        gh.fail = ["close"]
        self.record(530)
        with _quiet():
            self.assertEqual(self.run_queue(gh), 1)
        self.assertTrue(self.queue()["pending"][0]["commented"])
        self.assertEqual(self.run_queue(gh), 0)
        self.assertEqual((len(gh.comments), gh.closed), (1, [453]))

    def test_one_failure_does_not_stop_other_entries(self) -> None:
        gh = GitHub(
            {530: pull(), 531: pull(531, f"Issue: {R}/454")},
            {453: ticket(), 454: ticket()},
        )
        gh.fail = ["read"]
        self.record(530)
        self.record(531)
        with _quiet():
            self.assertEqual(self.run_queue(gh), 1)
        self.assertEqual(gh.closed, [454])
        self.assertEqual([e["pr"] for e in self.queue()["pending"]], [530])

    def test_quota_keeps_the_entry(self) -> None:
        gh = GitHub({530: pull()}, {453: ticket()})

        def quota(_: list[str]) -> Any:
            raise QuotaExhausted("RATE_LIMITED")

        self.record(530)
        with self.assertRaises(QuotaExhausted):
            close_issue.run_queue(self.dir, quota, gh.comment, gh.close)
        self.assertEqual([e["pr"] for e in self.queue()["pending"]], [530])

    def test_bad_queue_file_is_refused(self) -> None:
        for text in ("{", "[]", '{"pending": [{"pr": "1"}], "done": []}'):
            with self.subTest(text=text):
                (self.dir / close_issue.QUEUE_FILE).write_text(text)
                with self.assertRaises(close_issue.BadQueue):
                    close_issue.load(self.dir)


class VerifyTest(unittest.TestCase):
    def test_open_ticket_after_merge_fails(self) -> None:
        gh = GitHub({530: pull()}, {453: ticket()})
        ok, reason = close_issue.verify(530, gh.fetch)
        self.assertFalse(ok)
        self.assertIn(f"{R}/453", reason)

    def test_closed_or_not_closable_passes(self) -> None:
        for body, issues in (
            (f"Issue: {R}/453", {453: ticket(state="closed")}),
            (f"Issue: {R}/453 (partial)", {453: ticket()}),
            (
                f"Issue: {R}/453\nLeaf IDs: B1.1.6",
                {453: ticket(body="- [ ] **B1.1.7** x")},
            ),
        ):
            with self.subTest(body=body):
                gh = GitHub({530: pull(body=body)}, issues)
                self.assertTrue(close_issue.verify(530, gh.fetch)[0])


if __name__ == "__main__":
    unittest.main()
