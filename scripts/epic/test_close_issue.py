"""Close helper tests. Run: python3 -m unittest discover -s scripts/epic."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import close_issue
import github_quota
import next_action
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
    """Fake REST reads and writes. `fail` names calls that raise once each;
    `comment-landed` and `close-landed` do the write and then time out."""

    def __init__(self, pulls: dict[int, dict], issues: dict[int, dict]) -> None:
        self.pulls, self.issues = pulls, issues
        self.comments: list[tuple[int, str]] = []
        self.closed: list[int] = []
        self.fail: list[str] = []
        self.searches = 0
        self.event_reads = 0
        self.clock = 0.0
        self.events: list[tuple[int, float]] = []
        self.on_read: Any = None

    def _maybe_fail(self, what: str) -> None:
        if what in self.fail:
            self.fail.remove(what)
            raise subprocess.CalledProcessError(1, ["gh"], "", f"{what}: HTTP 502")

    def fetch(self, args: list[str]) -> Any:
        path = args[1]
        if self.on_read:
            hook, self.on_read = self.on_read, None
            hook()
        self._maybe_fail("read")
        if "/pulls/" in path:
            return self.pulls[int(path.rsplit("/", 1)[1])]
        if path.endswith("/sub_issues?per_page=1"):
            return []
        return self.issues[int(path.rsplit("/", 1)[1])]

    def comment(self, number: int, text: str) -> None:
        self._maybe_fail("comment")
        self.comments.append((number, text))
        if "comment-landed" in self.fail:
            self.fail.remove("comment-landed")
            raise subprocess.TimeoutExpired(["gh"], 120)

    def has_comment(self, number: int, mark: str) -> bool:
        self.searches += 1
        return any(n == number and mark in text for n, text in self.comments)

    def close(self, number: int) -> None:
        self._maybe_fail("close")
        self.closed.append(number)
        self.issues[number]["state"] = "closed"
        self.events.append((number, self.clock))
        if "close-landed" in self.fail:
            self.fail.remove("close-landed")
            raise subprocess.TimeoutExpired(["gh"], 120)

    def closed_since(self, number: int, since: str) -> bool:
        self.event_reads += 1
        floor = github_quota.parse_iso(since) - close_issue.CLOCK_SLACK
        return any(n == number and at >= floor for n, at in self.events)


class QueueCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())

    def record(self, pr: int) -> bool:
        data = close_issue.load(self.dir)
        added = close_issue.enqueue(data, pr)
        close_issue.save(self.dir, data)
        return added

    def run_queue(self, gh: GitHub, blocked=lambda: None) -> int:
        return close_issue.run_queue(
            self.dir,
            gh.fetch,
            gh.comment,
            gh.close,
            gh.has_comment,
            now=lambda: gh.clock,
            blocked=blocked,
            find_close=gh.closed_since,
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
        # The failed close never reached GitHub: one event read, one more close.
        self.assertEqual(gh.event_reads, 1)

    def test_landed_close_then_reopen_is_not_closed_again(self) -> None:
        gh = GitHub({530: pull()}, {453: ticket()})
        gh.fail = ["close-landed"]
        self.record(530)
        with _quiet():
            self.assertEqual(self.run_queue(gh), 1)
        self.assertEqual(gh.closed, [453])
        gh.issues[453]["state"] = "open"  # the operator reopens it
        gh.clock = 3600.0
        self.assertEqual(self.run_queue(gh), 0)
        self.assertEqual(gh.closed, [453])
        self.assertEqual(gh.issues[453]["state"], "open")
        self.assertEqual(len(gh.comments), 1)
        self.assertEqual(self.queue()["pending"], [])
        self.assertEqual(self.queue()["done"][0]["result"], "reopened")

    def test_close_before_the_attempt_does_not_count(self) -> None:
        # Closed and reopened long before this merge: the close still runs.
        gh = GitHub({530: pull()}, {453: ticket()})
        gh.events.append((453, -3600.0))
        gh.fail = ["close"]
        self.record(530)
        with _quiet():
            self.assertEqual(self.run_queue(gh), 1)
        self.assertEqual(self.run_queue(gh), 0)
        self.assertEqual(gh.closed, [453])

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
            close_issue.run_queue(
                self.dir, quota, gh.comment, gh.close, blocked=lambda: None
            )
        self.assertEqual([e["pr"] for e in self.queue()["pending"]], [530])

    def test_timed_out_comment_that_landed_is_not_posted_again(self) -> None:
        cases = {
            "close": (f"Issue: {R}/453", ticket(), [453]),
            "note": (
                f"Issue: {R}/453\nLeaf IDs: B1.1.6",
                ticket(body="- [ ] **B1.1.7** x"),
                [],
            ),
        }
        for name, (body, issue, closed) in cases.items():
            with self.subTest(name):
                self.setUp()
                gh = GitHub({530: pull(body=body)}, {453: issue})
                gh.fail = ["comment-landed"]
                self.record(530)
                with _quiet():
                    self.assertEqual(self.run_queue(gh), 1)
                self.assertEqual(self.run_queue(gh), 0)
                self.assertEqual((len(gh.comments), gh.closed), (1, closed))
                self.assertEqual(gh.searches, 1)
                self.assertIn(
                    f"<!-- epic-close {name} pr=530 merge={SHA} -->", gh.comments[0][1]
                )

    def test_timed_out_comment_that_did_not_land_is_posted_once(self) -> None:
        gh = GitHub({530: pull()}, {453: ticket()})
        gh.fail = ["comment"]
        self.record(530)
        with _quiet():
            self.assertEqual(self.run_queue(gh), 1)
        self.assertEqual(self.run_queue(gh), 0)
        self.assertEqual((len(gh.comments), gh.closed), (1, [453]))

    def test_clean_run_searches_no_comments(self) -> None:
        gh = GitHub({530: pull()}, {453: ticket()})
        self.record(530)
        self.run_queue(gh)
        self.assertEqual((gh.searches, gh.event_reads), (0, 0))

    def test_bad_queue_file_is_refused(self) -> None:
        for text in ("{", "[]", '{"pending": [{"pr": "1"}], "done": []}'):
            with self.subTest(text=text):
                (self.dir / close_issue.QUEUE_FILE).write_text(text)
                with self.assertRaises(close_issue.BadQueue):
                    close_issue.load(self.dir)


class ClosedSinceTest(unittest.TestCase):
    def test_reads_closed_events_with_clock_slack(self) -> None:
        out = "2026-10-04T09:56:00Z\n"
        with mock.patch.object(close_issue, "run_gh", return_value=out) as gh:
            self.assertTrue(close_issue.closed_since(453, "2026-10-04T10:00:00Z"))
            self.assertFalse(close_issue.closed_since(453, "2026-10-04T10:06:00Z"))
        args = gh.call_args.args[0]
        self.assertIn("--paginate", args)
        self.assertIn(
            "repos/phaabe/live.moafunk.de/issues/453/events?per_page=100", args
        )

    def test_no_closed_event(self) -> None:
        with mock.patch.object(close_issue, "run_gh", return_value=""):
            self.assertFalse(close_issue.closed_since(453, "2026-10-04T10:00:00Z"))


class LockTest(QueueCase):
    """Every command holds the queue lock while it reads and writes the queue."""

    def start_during_read(self, gh: GitHub, work: Any) -> threading.Thread:
        """Run `work` in a thread while the retry reads its first PR; it must
        still wait for the lock when that read ends."""
        thread = threading.Thread(target=work)

        def hook() -> None:
            thread.start()
            thread.join(0.5)
            self.assertTrue(thread.is_alive(), "ran without the queue lock")

        gh.on_read = hook
        return thread

    def test_queue_during_retry_is_kept(self) -> None:
        gh = GitHub({530: pull(), 531: pull(531)}, {453: ticket()})
        self.record(530)
        thread = self.start_during_read(gh, lambda: close_issue.add(self.dir, 531))
        self.assertEqual(self.run_queue(gh), 0)
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual([e["pr"] for e in self.queue()["pending"]], [531])
        self.assertEqual([e["pr"] for e in self.queue()["done"]], [530])

    def test_two_retries_post_and_close_once(self) -> None:
        gh = GitHub({530: pull()}, {453: ticket()})
        self.record(530)
        codes: list[int] = []
        thread = self.start_during_read(gh, lambda: codes.append(self.run_queue(gh)))
        self.assertEqual(self.run_queue(gh), 0)
        thread.join(5)
        self.assertEqual(codes, [0])
        self.assertEqual((len(gh.comments), gh.closed), (1, [453]))

    def test_busy_lock_changes_nothing(self) -> None:
        gh = GitHub({530: pull()}, {453: ticket()})
        gh.fetch = mock.Mock(side_effect=AssertionError("GitHub read"))
        self.record(530)
        before = self.queue()
        with close_issue.queue_lock(self.dir):
            with self.assertRaises(close_issue.Locked):
                close_issue.add(self.dir, 531, wait=0.2)
            with self.assertRaises(close_issue.Locked):
                close_issue.run_queue(self.dir, gh.fetch, wait=0.2)
        gh.fetch.assert_not_called()
        self.assertEqual(self.queue(), before)


class DeferTest(QueueCase):
    """Pause or a quota wait that comes up between two calls stops the run."""

    def blocked_after(self, calls: int) -> Any:
        count = [0]

        def blocked() -> str | None:
            count[0] += 1
            return "pause file exists" if count[0] > calls else None

        return blocked

    def test_block_before_any_call_makes_no_call(self) -> None:
        gh = GitHub({530: pull()}, {453: ticket()})
        gh.fetch = mock.Mock(side_effect=AssertionError("GitHub read"))
        self.record(530)
        with self.assertRaises(close_issue.Deferred):
            self.run_queue(gh, lambda: "GitHub quota wait until later")
        gh.fetch.assert_not_called()
        self.assertEqual(self.queue()["pending"][0]["attempts"], 0)

    def test_block_between_comment_and_close_keeps_progress(self) -> None:
        gh = GitHub({530: pull()}, {453: ticket()})
        self.record(530)
        # Calls: read PR, read ticket, comment; then blocked before the close.
        with self.assertRaises(close_issue.Deferred):
            self.run_queue(gh, self.blocked_after(3))
        self.assertEqual((len(gh.comments), gh.closed), (1, []))
        entry = self.queue()["pending"][0]
        self.assertTrue(entry["commented"])
        self.assertEqual(entry["attempts"], 0)
        self.assertEqual(self.run_queue(gh), 0)
        self.assertEqual((len(gh.comments), gh.closed), (1, [453]))

    def test_block_before_second_entry_stops_the_run(self) -> None:
        gh = GitHub(
            {530: pull(), 531: pull(531, f"Issue: {R}/454")},
            {453: ticket(), 454: ticket()},
        )
        self.record(530)
        self.record(531)
        with self.assertRaises(close_issue.Deferred):
            self.run_queue(gh, self.blocked_after(4))
        self.assertEqual((gh.closed, len(gh.comments)), ([453], 1))
        self.assertEqual([e["pr"] for e in self.queue()["pending"]], [531])


class BlockedTest(unittest.TestCase):
    """github_blocked() reads the pause file and the shared quota wait."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        for patch in (
            mock.patch.object(next_action, "PAUSE_FILE", self.dir / "pause"),
            mock.patch.object(github_quota, "STATE_DIR", self.dir),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def test_open(self) -> None:
        self.assertIsNone(close_issue.github_blocked())

    def test_pause_file(self) -> None:
        (self.dir / "pause").touch()
        self.assertIn("pause file", close_issue.github_blocked() or "")

    def test_quota_wait(self) -> None:
        (self.dir / github_quota.WAIT_FILE).write_text(
            '{"retry_at": "2099-01-01T00:00:00Z"}'
        )
        self.assertIn("quota wait", close_issue.github_blocked() or "")

    def test_bad_quota_file_blocks(self) -> None:
        (self.dir / github_quota.WAIT_FILE).write_text("{")
        self.assertIn("unreadable", close_issue.github_blocked() or "")


class MainTest(unittest.TestCase):
    def main(self, *args: str) -> int:
        self.state = Path(tempfile.mkdtemp())
        argv = ["close_issue.py", "--state-dir", str(self.state), *args]
        with mock.patch.object(sys, "argv", argv):
            return close_issue.main()

    def test_queue_makes_no_github_call(self) -> None:
        with mock.patch.object(close_issue, "run_queue") as run:
            self.assertEqual(self.main("queue", "--pr", "530"), 0)
        run.assert_not_called()
        queue = json.loads((self.state / close_issue.QUEUE_FILE).read_text())
        self.assertEqual([e["pr"] for e in queue["pending"]], [530])

    def test_busy_queue_exits_5(self) -> None:
        busy = close_issue.Locked("held")
        with mock.patch.object(close_issue, "add", side_effect=busy):
            with _quiet():
                self.assertEqual(self.main("queue", "--pr", "530"), close_issue.LOCKED)

    def test_deferred_exits_3(self) -> None:
        deferred = close_issue.Deferred("pause file exists")
        with mock.patch.object(close_issue, "run_queue", side_effect=deferred):
            with _quiet():
                self.assertEqual(self.main("retry"), close_issue.DEFERRED)


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
