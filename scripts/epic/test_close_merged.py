"""Tests for close_merged.py: closing tickets whose implementation merged.

Run: python3 -m unittest discover -s scripts/epic
"""

from __future__ import annotations

import isolated_env  # noqa: F401  (first: hides live runner state)

import json
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import close_merged
import next_action
from github_quota import parse_iso

REPO = next_action.REPO
BOARD = next_action.PROJECT_API
STATUS_FIELD = 417511516
DONE = "edaabf77"
NOW = parse_iso("2026-10-05T12:00:00Z")
SHA = "a" * 40


def issue(n: int) -> str:
    return f"https://github.com/{REPO}/issues/{n}"


def pr(
    number: int, ticket: int | None, merged_at: str = "2026-10-04T10:00:00Z",
    base: str = "dev/312-interim", extra: str = "",
) -> dict[str, Any]:  # fmt: skip
    body = f"Change.\n\nIssue: {issue(ticket)}\n" if ticket else "Change.\n"
    return {
        "number": number,
        "body": body + "Validation: 10 tests OK.\n" + extra,
        "merged_at": merged_at,
        "updated_at": merged_at,
        "merge_commit_sha": SHA,
        "base": base,
    }


class FakeGh:
    """GitHub state behind `gh api`, projected the way the jq filters do."""

    def __init__(self) -> None:
        self.merged: list[dict[str, Any]] = []
        self.open_prs: list[str] = []  # bodies
        self.tickets: dict[int, dict[str, Any]] = {}
        self.reopened: dict[int, list[str]] = {}
        self.comments: dict[int, list[str]] = {}
        self.writes: list[tuple[str, int]] = []
        self.pages: list[int] = []  # page numbers of the recent-merge reads
        self.fail_writes = False
        self.board: dict[int, str] = {}  # ticket -> Status option id
        self.fail_board = False
        self.not_planned: set[int] = set()  # closed tickets not completed

    def ticket(self, n: int, body: str = "Do it.", sub_issues: int = 0) -> None:
        self.tickets[n] = {"number": n, "body": body, "sub_issues": sub_issues}

    def __call__(self, args: list[str]) -> str:
        if any(a.startswith(f"{BOARD}/") for a in args):
            return self.project(args)
        url = next(a for a in args if a.startswith("repos/"))
        path = url.removeprefix(f"repos/{REPO}/")
        if "-X" in args:
            return self.write(args[args.index("-X") + 1], path, args)
        if path.startswith("pulls?state=closed"):
            base = re.search(r"base=([^&]+)", path).group(1)
            prs = [p for p in self.merged if p["base"] == base]
            if "--paginate" in args:  # Leaf IDs lines
                found = [
                    m for p in prs
                    for m in re.findall(r"(?m)^Leaf IDs:.*$", p["body"])
                ]  # fmt: skip
                return out(found)
            page = int(re.search(r"&page=(\d+)", path).group(1))
            self.pages.append(page)
            size = close_merged.PER_PAGE
            prs.sort(key=lambda p: p["updated_at"], reverse=True)
            return out(prs[(page - 1) * size : page * size])
        if path.startswith("pulls?state=open"):
            return out(
                [m for b in self.open_prs for m in re.findall(r"(?m)^Issue:.*$", b)]
            )
        if path.startswith("issues?state=open"):
            return out(list(self.tickets.values()))
        n = int(re.match(r"issues/(\d+)", path).group(1))
        if path == f"issues/{n}":
            if n not in self.tickets:
                reason = "not_planned" if n in self.not_planned else "completed"
                return out(
                    [
                        {
                            "number": n,
                            "body": "",
                            "state": "closed",
                            "state_reason": reason,
                        }
                    ]
                )
            return out([{**self.tickets[n], "state": "open", "state_reason": None}])
        if path.endswith("events?per_page=100"):
            return out(self.reopened.get(n, []))
        if "/comments" in path:
            return out(
                [
                    m.group(0)
                    for body in self.comments.get(n, [])
                    for m in close_merged.MARKERS.finditer(body)
                ]
            )
        raise AssertionError(f"unexpected call {args}")

    def write(self, method: str, path: str, args: list[str]) -> str:
        n = int(re.match(r"issues/(\d+)", path).group(1))
        if self.fail_writes:
            raise subprocess.CalledProcessError(1, ["gh"], "", "HTTP 502")
        if method == "POST":
            body = next(a for a in args if a.startswith("body="))[5:]
            self.comments.setdefault(n, []).append(body)
            self.writes.append(("comment", n))
        else:
            self.assertion_close(args)
            self.tickets.pop(n)
            self.writes.append(("close", n))
        return ""

    def project(self, args: list[str]) -> str:
        """The board REST API, projected the way the jq filters do."""
        if self.fail_board:
            raise subprocess.CalledProcessError(1, ["gh"], "", "HTTP 503")
        path = next(a for a in args if a.startswith(f"{BOARD}/"))
        if "-X" in args:
            item = int(path.rsplit("/", 1)[1])
            assert args[args.index("-X") + 1] == "PATCH", args
            assert f"fields[][id]={STATUS_FIELD}" in args, args
            value = next(a for a in args if a.startswith("fields[][value]="))
            n = item - 70_000
            self.board[n] = value.split("=", 1)[1]
            self.writes.append(("done", n))
            return ""
        if "/fields?" in path:
            return out([{"id": STATUS_FIELD, "done": DONE}])
        assert f"fields[]={STATUS_FIELD}" in path, path
        return out(
            [
                {
                    "id": 70_000 + n,
                    "number": n,
                    "repo": f"https://api.github.com/repos/{REPO}",
                    "state": "open" if n in self.tickets else "closed",
                    "reason": None
                    if n in self.tickets
                    else "not_planned"
                    if n in self.not_planned
                    else "completed",
                    "status": status,
                }
                for n, status in self.board.items()
            ]
        )

    @staticmethod
    def assertion_close(args: list[str]) -> None:
        assert "state=closed" in args and "state_reason=completed" in args, args


def out(values: list[Any]) -> str:
    return "".join(json.dumps(v) + "\n" for v in values)


class CloseMergedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.gh = FakeGh()
        self.log: list[str] = []

    def run_once(self, dry_run: bool = False) -> int:
        return close_merged.run(self.gh, NOW, dry_run, self.log.append)

    def test_ticket_of_takes_exactly_one_plain_issue_line(self) -> None:
        cases = {
            f"Issue: {issue(5)}": 5,
            f"Issue: {issue(5)}  ": 5,
            f"Issue: {issue(5)} (partial)": None,
            f"Issue: {issue(next_action.EPIC)}": None,
            f"Issue: {issue(5)}\nIssue: {issue(6)}": None,
            f"See {issue(5)}": None,
            "Issue: https://github.com/other/repo/issues/5": None,
        }
        for body, expected in cases.items():
            with self.subTest(body=body):
                self.assertEqual(close_merged.ticket_of(body), expected)

    def test_merged_ticket_gets_one_comment_then_closes(self) -> None:
        self.gh.merged = [pr(612, 521)]
        self.gh.ticket(521)
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(self.gh.writes, [("comment", 521), ("close", 521)])
        text = self.gh.comments[521][0]
        self.assertIn(f"https://github.com/{REPO}/pull/612", text)
        self.assertIn(f"`{SHA}` into `dev/312-interim`", text)
        self.assertIn("Tests: 10 tests OK.", text)
        self.assertTrue(text.endswith("<!-- epic-close-merged close pr=612 -->"))

    def test_rerun_after_a_failed_close_only_closes(self) -> None:
        self.gh.merged = [pr(612, 521)]
        self.gh.ticket(521)
        self.gh.comments[521] = [close_merged.close_comment(self.gh.merged[0])]
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(self.gh.writes, [("close", 521)])

    def test_second_run_does_nothing(self) -> None:
        self.gh.merged = [pr(612, 521)]
        self.gh.ticket(521)
        self.run_once()
        self.gh.writes.clear()
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(self.gh.writes, [])

    def test_closed_ticket_old_merge_and_other_prs_are_left_alone(self) -> None:
        self.gh.merged = [
            pr(1, 10),  # ticket 10 is closed: not in the open list
            pr(2, 11, merged_at="2026-09-01T00:00:00Z"),  # outside the window
            pr(3, None),  # no Issue: line
        ]
        self.gh.ticket(11)
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(self.gh.writes, [])

    def test_merge_on_page_two_closes(self) -> None:
        # 100 newer updates fill page one: 60 merges without a ticket and 40
        # closed unmerged PRs. The eligible merge is on page two.
        newer = [pr(n, None, merged_at="2026-10-05T09:00:00Z") for n in range(60)]
        for n in range(60, 100):
            newer.append({**pr(n, None), "merged_at": None})
            newer[-1]["updated_at"] = "2026-10-05T09:00:00Z"
        self.gh.merged = [*newer, pr(612, 521, merged_at="2026-10-04T10:00:00Z")]
        self.gh.ticket(521)
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(self.gh.writes, [("comment", 521), ("close", 521)])
        self.assertEqual(self.gh.pages, [1, 2, 1])  # two bases

    def test_paging_stops_at_an_update_before_the_window(self) -> None:
        self.gh.merged = [
            pr(n, None, merged_at="2026-09-01T00:00:00Z") for n in range(250)
        ]
        self.gh.merged[0] = pr(612, 521, merged_at="2026-10-04T10:00:00Z")
        self.gh.ticket(521)
        self.run_once()
        self.assertEqual(self.gh.writes, [("comment", 521), ("close", 521)])
        self.assertEqual(self.gh.pages, [1, 1])

    def test_overlapping_runs_post_one_comment(self) -> None:
        # Run A holds the ticket while it reads the markers; run B starts then.
        self.gh.merged = [pr(612, 521)]
        self.gh.ticket(521)
        inside, release = threading.Event(), threading.Event()
        plain = self.gh.__call__

        def gh(args: list[str]) -> str:
            result = plain(args)
            if any("/comments?" in a for a in args) and not inside.is_set():
                inside.set()
                release.wait(10)
            return result

        first = threading.Thread(
            target=close_merged.run, args=(gh, NOW), kwargs={"log": lambda _: None}
        )
        first.start()
        try:
            self.assertTrue(inside.wait(10))
            self.assertEqual(close_merged.run(gh, NOW, log=self.log.append), 0)
            self.assertEqual(self.gh.writes, [])
            self.assertIn("locked by another runner", self.log[-1])
        finally:
            release.set()
            first.join(10)
        self.assertFalse(first.is_alive())
        self.assertEqual(self.gh.writes, [("comment", 521), ("close", 521)])

    def test_ticket_locked_by_a_running_tick_is_skipped(self) -> None:
        self.gh.merged = [pr(612, 521)]
        self.gh.ticket(521)
        with close_merged.ticket_lock(521) as locked:
            self.assertTrue(locked)
            self.assertEqual(self.run_once(), 0)
        self.assertEqual(self.gh.writes, [])
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(self.gh.writes, [("comment", 521), ("close", 521)])

    def test_change_made_before_the_lock_is_seen_under_it(self) -> None:
        # Another runner holds the ticket's lock right after the open lists
        # are read, changes the ticket, then lets go. Closure must not use
        # the old lists.
        def leaf(gh: FakeGh) -> None:
            gh.tickets[521]["body"] = "- [ ] **B1.1.9** new\n"

        def partial_pr(gh: FakeGh) -> None:
            gh.open_prs.append(f"Next.\n\nIssue: {issue(521)} (partial)\n")

        def sub_issue(gh: FakeGh) -> None:
            gh.tickets[521]["sub_issues"] = 1

        def closed(gh: FakeGh) -> None:
            gh.tickets.pop(521)

        cases = {
            "leaf": (leaf, [("comment", 521)]),
            "partial PR": (partial_pr, []),
            "sub-issue": (sub_issue, []),
            "closed": (closed, []),
        }
        for name, (change, writes) in cases.items():
            with self.subTest(name):
                self.gh = FakeGh()
                self.gh.merged = [pr(612, 521)]
                self.gh.ticket(521)
                fake, pending = self.gh, [change]

                def gh(args: list[str], fake=fake, pending=pending) -> str:
                    result = fake(args)
                    reads_open_prs = f"repos/{REPO}/pulls?state=open" in " ".join(args)
                    if reads_open_prs and pending:
                        with close_merged.ticket_lock(521) as locked:
                            self.assertTrue(locked)
                            pending.pop()(fake)
                    return result

                self.assertEqual(close_merged.run(gh, NOW, log=self.log.append), 0)
                self.assertEqual(self.gh.writes, writes)
                if writes:
                    self.assertIn("pr=612", self.gh.comments[521][0])
                    self.assertIn("note", self.gh.comments[521][0])

    def test_umbrella_and_open_pr_keep_the_ticket_open(self) -> None:
        self.gh.merged = [pr(1, 20), pr(2, 21)]
        self.gh.ticket(20, sub_issues=3)
        self.gh.ticket(21)
        self.gh.open_prs = [f"Next part.\n\nIssue: {issue(21)} (partial)\n"]
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(self.gh.writes, [])

    def test_reopened_after_the_merge_stays_open(self) -> None:
        self.gh.merged = [pr(1, 30, merged_at="2026-10-04T10:00:00Z")]
        self.gh.ticket(30)
        self.gh.reopened[30] = ["2026-10-04T11:00:00Z"]
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(self.gh.writes, [])
        self.assertIn("reopened", self.log[0])

    def test_reopened_before_the_merge_closes(self) -> None:
        self.gh.merged = [pr(1, 30, merged_at="2026-10-04T10:00:00Z")]
        self.gh.ticket(30)
        self.gh.reopened[30] = ["2026-10-03T09:00:00Z"]
        self.run_once()
        self.assertEqual(self.gh.writes, [("comment", 30), ("close", 30)])

    def test_newest_merge_counts_for_the_reopen_check(self) -> None:
        # Reopened after the first PR, then a second PR merged: close again.
        self.gh.merged = [
            pr(2, 30, merged_at="2026-10-04T12:00:00Z"),
            pr(1, 30, merged_at="2026-10-02T10:00:00Z"),
        ]
        self.gh.ticket(30)
        self.gh.reopened[30] = ["2026-10-03T09:00:00Z"]
        self.run_once()
        self.assertEqual(self.gh.writes, [("comment", 30), ("close", 30)])
        self.assertIn("pull/2", self.gh.comments[30][0])

    def test_uncovered_leaves_get_one_note(self) -> None:
        body = (
            "- [ ] **B1.1.1** first\n- [ ] **B1.1.2** second\n- [x] **B1.1.3** done\n"
        )
        self.gh.merged = [pr(410, 350, extra="Leaf IDs: B1.1.2\n")]
        self.gh.ticket(350, body=body)
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(self.gh.writes, [("comment", 350)])
        self.assertIn("leaves in `Leaf IDs:`: B1.1.1.", self.gh.comments[350][0])
        self.gh.writes.clear()
        self.run_once()
        self.assertEqual(self.gh.writes, [])

    def test_leaves_listed_by_any_merged_pr_count(self) -> None:
        body = "- [ ] **B1.1.1** first\n"
        self.gh.merged = [
            pr(410, 350),
            # Old and naming the epic, but it lists the leaf.
            pr(
                300,
                next_action.EPIC,
                "2026-08-01T00:00:00Z",
                extra="Leaf IDs: B1.1.1\n",
            ),
        ]
        self.gh.ticket(350, body=body)
        self.run_once()
        self.assertEqual(self.gh.writes, [("comment", 350), ("close", 350)])

    def test_example_leaf_in_inline_code_does_not_keep_the_ticket_open(
        self,
    ) -> None:
        # Issue 453: the leaf format example, not a leaf.
        body = (
            "Mark each leaf like `- [ ] **X1.2.3**` in the list.\n"
            "Example: `- [ ] **X1.2.4**`\n"
        )
        self.gh.merged = [pr(410, 453)]
        self.gh.ticket(453, body=body)
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(self.gh.writes, [("comment", 453), ("close", 453)])

    def test_indented_unchecked_leaf_still_keeps_the_ticket_open(self) -> None:
        body = "Leaves:\n  - [ ] **B1.1.1** first\n\t- [ ] **B1.1.2** second\n"
        self.gh.merged = [pr(410, 350)]
        self.gh.ticket(350, body=body)
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(self.gh.writes, [("comment", 350)])
        self.assertIn("B1.1.1, B1.1.2.", self.gh.comments[350][0])

    def test_closed_ticket_on_the_board_gets_status_done(self) -> None:
        self.gh.merged = [pr(612, 521)]
        self.gh.ticket(521)
        self.gh.board[521] = "3a81da46"  # In progress
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(
            self.gh.writes, [("comment", 521), ("close", 521), ("done", 521)]
        )
        self.assertEqual(self.gh.board[521], DONE)
        self.gh.writes.clear()
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(self.gh.writes, [])

    def test_closed_ticket_not_on_the_board_is_fine(self) -> None:
        self.gh.merged = [pr(612, 521)]
        self.gh.ticket(521)
        self.gh.board[999] = "3a81da46"  # another ticket, not merged here
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(self.gh.writes, [("comment", 521), ("close", 521)])
        self.assertEqual(self.gh.board[999], "3a81da46")

    def test_open_ticket_keeps_its_board_status(self) -> None:
        self.gh.merged = [pr(612, 521)]
        self.gh.ticket(521)
        self.gh.open_prs = [f"Issue: {issue(521)}"]
        self.gh.board[521] = "3a81da46"
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(self.gh.writes, [])

    def test_board_error_leaves_the_close_and_retries_next_run(self) -> None:
        self.gh.merged = [pr(612, 521)]
        self.gh.ticket(521)
        self.gh.board[521] = "3a81da46"
        self.gh.fail_board = True
        self.assertEqual(self.run_once(), 1)
        self.assertEqual(self.gh.writes, [("comment", 521), ("close", 521)])
        self.assertNotIn(521, self.gh.tickets)  # still closed
        self.assertTrue(any("board read failed: HTTP 503" in x for x in self.log))
        # Issue 655: closed earlier, Status left behind. The next run fixes it.
        self.gh.fail_board = False
        self.gh.writes.clear()
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(self.gh.writes, [("done", 521)])

    def test_board_retry_ends_when_the_merge_leaves_the_window(self) -> None:
        self.gh.merged = [pr(612, 521, merged_at="2026-09-01T00:00:00Z")]
        self.gh.board[521] = "3a81da46"  # closed, Status left behind
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(self.gh.writes, [])

    def test_board_skips_a_ticket_locked_by_a_running_tick(self) -> None:
        self.gh.merged = [pr(612, 521)]
        self.gh.board[521] = "3a81da46"  # closed, Status left behind
        with close_merged.ticket_lock(521) as locked:
            self.assertTrue(locked)
            self.assertEqual(self.run_once(), 0)
        self.assertEqual(self.gh.writes, [])
        self.assertTrue(any("locked by another runner" in x for x in self.log))
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(self.gh.writes, [("done", 521)])

    def test_board_change_after_the_board_read_is_seen_under_the_lock(
        self,
    ) -> None:
        # Another runner reopens (and claims) or closes the ticket as not
        # planned right after the board list is read. Status must stay.
        def reopened(gh: FakeGh) -> None:
            gh.ticket(521)

        def not_planned(gh: FakeGh) -> None:
            gh.not_planned.add(521)

        for name, change in {"reopened": reopened, "not planned": not_planned}.items():
            with self.subTest(name):
                self.gh = FakeGh()
                self.gh.merged = [pr(612, 521)]
                self.gh.board[521] = "3a81da46"  # In progress
                fake, pending = self.gh, [change]

                def gh(args: list[str], fake=fake, pending=pending) -> str:
                    result = fake(args)
                    if any("/items?" in a for a in args) and pending:
                        with close_merged.ticket_lock(521) as locked:
                            self.assertTrue(locked)
                            pending.pop()(fake)
                    return result

                self.assertEqual(close_merged.run(gh, NOW, log=self.log.append), 0)
                self.assertEqual(self.gh.writes, [])
                self.assertEqual(self.gh.board[521], "3a81da46")
                self.assertIn("no longer closed as completed", self.log[-1])

    def test_dry_run_sets_no_board_status(self) -> None:
        self.gh.merged = [pr(612, 521)]
        self.gh.board[521] = "3a81da46"
        self.assertEqual(self.run_once(dry_run=True), 0)
        self.assertEqual(self.gh.writes, [])
        self.assertTrue(any("board Status to Done" in x for x in self.log))

    def test_dry_run_writes_nothing(self) -> None:
        self.gh.merged = [pr(612, 521)]
        self.gh.ticket(521)
        self.assertEqual(self.run_once(dry_run=True), 0)
        self.assertEqual(self.gh.writes, [])
        self.assertIn("closing", self.log[0])

    def test_failed_write_is_logged_and_the_rest_continues(self) -> None:
        self.gh.merged = [pr(1, 40), pr(2, 41)]
        self.gh.ticket(40)
        self.gh.ticket(41)
        self.gh.fail_writes = True
        self.assertEqual(self.run_once(), 1)
        failures = [line for line in self.log if "GitHub call failed: HTTP 502" in line]
        self.assertEqual(len(failures), 2)

    def test_pause_file_stops_before_any_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pause = Path(tmp) / "pause"
            pause.touch()
            calls: list[list[str]] = []
            with patch.object(next_action, "PAUSE_FILE", pause):
                gh = close_merged.guarded(lambda args: calls.append(args) or "")
                with self.assertRaises(close_merged.Deferred):
                    close_merged.run(gh, NOW, log=self.log.append)
            self.assertEqual(calls, [])

    def test_main_reports_a_deferred_run(self) -> None:
        with (
            patch.object(close_merged, "run", side_effect=close_merged.Deferred("p")),
            patch("builtins.print") as printed,
        ):
            self.assertEqual(close_merged.main([]), close_merged.DEFERRED)
        printed.assert_called_once_with("close: deferred: p")


@unittest.skipUnless(shutil.which("jq"), "needs jq")
class JqFilterTest(unittest.TestCase):
    """The filters on raw REST shapes. `gh --jq` prints strings raw, like `jq -r`."""

    def jq(self, program: str, data: Any) -> list[Any]:
        result = subprocess.run(
            ["jq", "-r", "-c", program], input=json.dumps(data),
            capture_output=True, text=True, check=True,
        )  # fmt: skip
        return close_merged.lines(lambda _: result.stdout, [])

    def test_filters(self) -> None:
        body = f"Text\nIssue: {issue(5)} (partial)\nLeaf IDs: B1.1.1, B1.1.2\nmore"
        pulls = [
            {"number": 1, "body": body, "merged_at": "2026-10-04T10:00:00Z",
             "updated_at": "2026-10-04T10:00:01Z",
             "merge_commit_sha": SHA, "base": {"ref": "dev/312-interim"}},
            {"number": 2, "body": None, "merged_at": None, "merge_commit_sha": None,
             "updated_at": "2026-10-03T00:00:00Z",
             "base": {"ref": "dev/312-interim"}},
        ]  # fmt: skip
        self.assertEqual(
            self.jq(close_merged.CLOSED_JQ, pulls),
            [{"number": 1, "body": body, "merged_at": "2026-10-04T10:00:00Z",
              "updated_at": "2026-10-04T10:00:01Z",
              "merge_commit_sha": SHA, "base": "dev/312-interim"},
             {"number": 2, "body": None, "merged_at": None,
              "updated_at": "2026-10-03T00:00:00Z",
              "merge_commit_sha": None, "base": "dev/312-interim"}],
        )  # fmt: skip
        self.assertEqual(
            self.jq(close_merged.LEAVES_JQ, pulls), ["Leaf IDs: B1.1.1, B1.1.2"]
        )
        self.assertEqual(
            self.jq(close_merged.OPEN_PR_JQ, pulls), [f"Issue: {issue(5)} (partial)"]
        )
        issues = [
            {"number": 7, "body": "b", "sub_issues_summary": {"total": 2}},
            {"number": 8, "body": "c"},
            {"number": 9, "body": "d", "pull_request": {"url": "x"}},
        ]
        self.assertEqual(
            self.jq(close_merged.OPEN_ISSUES_JQ, issues),
            [{"number": 7, "body": "b", "sub_issues": 2},
             {"number": 8, "body": "c", "sub_issues": 0}],
        )  # fmt: skip
        self.assertEqual(
            self.jq(close_merged.TICKET_JQ, issues[0] | {"state": "open"}),
            [{"number": 7, "body": "b", "state": "open", "sub_issues": 2}],
        )
        events = [
            {"event": "closed", "created_at": "2026-10-01T00:00:00Z"},
            {"event": "reopened", "created_at": "2026-10-02T00:00:00Z"},
        ]
        self.assertEqual(
            self.jq(close_merged.REOPENED_JQ, events), ["2026-10-02T00:00:00Z"]
        )
        fields = [
            {"id": 1, "name": "Wave", "options": [{"id": "w", "name": {"raw": "1"}}]},
            {"id": STATUS_FIELD, "name": "Status", "options": [
                {"id": "3a81da46", "name": {"raw": "In progress"}},
                {"id": DONE, "name": {"raw": "Done"}}]},
        ]  # fmt: skip
        self.assertEqual(
            self.jq(close_merged.STATUS_JQ, fields),
            [{"id": STATUS_FIELD, "done": DONE}],
        )
        rows = [
            {"id": 5, "content_type": "Issue",
             "content": {"number": 521, "state": "closed",
                         "state_reason": "completed",
                         "repository_url": f"https://api.github.com/repos/{REPO}"},
             "fields": [{"name": "Status", "value": {"id": "3a81da46"}}]},
            {"id": 6, "content_type": "PullRequest", "content": {"number": 7}},
            {"id": 8, "content_type": "Issue",
             "content": {"number": 9, "state": "open", "state_reason": None,
                         "repository_url": "u"}, "fields": []},
        ]  # fmt: skip
        self.assertEqual(
            self.jq(close_merged.BOARD_JQ, rows),
            [{"id": 5, "number": 521, "repo": f"https://api.github.com/repos/{REPO}",
              "state": "closed", "reason": "completed", "status": "3a81da46"},
             {"id": 8, "number": 9, "repo": "u", "state": "open", "reason": None,
              "status": None}],
        )  # fmt: skip
        comments = [
            {"body": "Closed.\n\n<!-- epic-close-merged close pr=612 -->"},
            {"body": None},
            {"body": "no marker"},
        ]
        self.assertEqual(
            self.jq(close_merged.MARKERS_JQ, comments),
            ["<!-- epic-close-merged close pr=612 -->"],
        )


if __name__ == "__main__":
    unittest.main()
