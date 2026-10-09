"""Tests for close_merged.py: closing tickets whose implementation merged.

Run: python3 -m unittest discover -s scripts/epic
"""

from __future__ import annotations

import isolated_env  # noqa: F401  (first: hides live runner state)

import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from typing import Any
from unittest.mock import patch
from urllib.parse import unquote

import close_merged
import next_action
import set_status
import target_lock
import ticket_history as th
from github_quota import QuotaExhausted, parse_iso

REPO = next_action.REPO
BOARD = next_action.PROJECT_API
STATUS_FIELD = 417511516
DONE = "edaabf77"
IN_PROGRESS = "3a81da46"
OPTIONS = {
    "f75ad846": "Backlog",
    "61e4505c": "Ready",
    IN_PROGRESS: "In progress",
    "df73e18b": "In review",
    DONE: "Done",
}
NAMES = {name: option for option, name in OPTIONS.items()}
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
        # The status helper's side (set_status.Board over close_merged's gh).
        self.labels: dict[int, list[str]] = {}
        self.events: dict[int, list[dict[str, Any]]] = {}  # REST label events
        self.clock = int(NOW)
        self.calls: list[list[str]] = []
        self.label_calls: dict[int, int] = {}
        # op -> "error" (nothing written), "timeout" (written, then the call
        # fails) or "moved" (board only: another Status lands). Ops: status,
        # scan, labels, add:<label>, remove:<label>.
        self.fail: dict[str, str] = {}
        self.after: Any = None  # called with the args after every call

    def put(self, n: int, status: str, labels: list[str] | None = None) -> None:
        """A board item with Status `status` and, by default, its one label."""
        self.board[n] = NAMES[status]
        self.labels[n] = (
            [set_status.label_for(status)] if labels is None else list(labels)
        )

    def ticket(self, n: int, body: str = "Do it.", sub_issues: int = 0) -> None:
        self.tickets[n] = {"number": n, "body": body, "sub_issues": sub_issues}

    def __call__(self, args: list[str]) -> str:
        self.calls.append(args)
        try:
            return self.answer(args)
        finally:
            if self.after:
                self.after(args)

    def answer(self, args: list[str]) -> str:
        if "--slurp" in args:
            return self.raw(args[-1])
        if "--method" in args:
            return self.helper_write(args)
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

    def failing(self, op: str, write: Any = None) -> str:
        """Run `write` unless op fails with "error"; raise after a timeout."""
        mode = self.fail.get(op)
        if mode == "error":
            raise subprocess.CalledProcessError(1, ["gh"], "", f"HTTP 502 {op}")
        if write:
            write()
        if mode == "timeout":
            raise subprocess.TimeoutExpired(["gh"], 120)
        return ""

    def raw(self, endpoint: str) -> str:
        """`gh api --paginate --slurp`: the helper's reads, raw REST pages."""
        if endpoint == f"{BOARD}/fields?per_page=100":
            options = [{"id": k, "name": {"raw": v}} for k, v in OPTIONS.items()]
            field = {"id": STATUS_FIELD, "name": "Status", "options": options}
            return json.dumps([[{"id": 1, "name": "Title"}, field]])
        if endpoint.startswith(f"{BOARD}/items?"):
            assert f"fields[]={STATUS_FIELD}" in endpoint, endpoint
            if self.fail_board:
                raise subprocess.CalledProcessError(1, ["gh"], "", "HTTP 503")
            self.failing("scan")
            rows = [
                {
                    "id": 70_000 + n,
                    "content_type": "Issue",
                    "content": {"number": n, "html_url": issue(n)},
                    "fields": [
                        {"name": "Status", "value": {"name": {"raw": OPTIONS[o]}}}
                    ]
                    if o
                    else [],
                }
                for n, o in self.board.items()
            ]
            return json.dumps([rows])
        n = int(re.fullmatch(rf"repos/{REPO}/issues/(\d+)/labels\?per_page=100",
                             endpoint).group(1))  # fmt: skip
        self.label_calls[n] = self.label_calls.get(n, 0) + 1
        self.failing("labels")
        return json.dumps([[{"name": x} for x in self.labels.get(n, [])]])

    def label_event(self, n: int, kind: str, name: str) -> None:
        self.clock += 5
        events = self.events.setdefault(n, [])
        events.append(
            {
                "id": 1000 + sum(len(e) for e in self.events.values()),
                "event": kind,
                "label": {"name": name},
                "created_at": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock)
                ),
            }
        )

    def helper_write(self, args: list[str]) -> str:
        """The helper's writes; their bodies come from the --input file."""
        method, path = args[args.index("--method") + 1 : args.index("--method") + 3]
        body = {}
        if "--input" in args:
            source = args[args.index("--input") + 1]
            assert source != "-", "stdin must go through a file"
            body = json.loads(Path(source).read_text())
        if path.startswith(f"{BOARD}/items/"):
            assert method == "PATCH", args
            n = int(path.rsplit("/", 1)[1]) - 70_000
            (field,) = body["fields"]
            assert field["id"] == STATUS_FIELD, body
            value = field["value"]

            def write() -> None:
                moved = self.fail.get("status") == "moved"
                self.board[n] = NAMES["In progress"] if moved else value
                if not moved:
                    self.writes.append(("done" if value == DONE else "status", n))

            return self.failing("status", write)
        n = int(re.match(rf"repos/{REPO}/issues/(\d+)/labels", path).group(1))
        self.label_calls[n] = self.label_calls.get(n, 0) + 1
        labels = self.labels.setdefault(n, [])
        if method == "POST":
            (name,) = body["labels"]

            def add() -> None:
                if name not in labels:
                    labels.append(name)
                    self.label_event(n, "labeled", name)

            return self.failing(f"add:{name}", add)
        assert method == "DELETE", args
        name = unquote(path.rsplit("/", 1)[1])

        def remove() -> None:
            if name not in labels:
                raise subprocess.CalledProcessError(1, ["gh"], "", "HTTP 404")
            labels.remove(name)
            self.label_event(n, "unlabeled", name)

        return self.failing(f"remove:{name}", remove)

    def status_events(self, n: int) -> list[str]:
        """`+label` / `-label` in event order."""
        return [
            ("+" if e["event"] == "labeled" else "-") + e["label"]["name"]
            for e in self.events.get(n, [])
        ]

    def project(self, args: list[str]) -> str:
        """The board REST API, projected the way the jq filters do."""
        if self.fail_board:
            raise subprocess.CalledProcessError(1, ["gh"], "", "HTTP 503")
        path = next(a for a in args if a.startswith(f"{BOARD}/"))
        assert "-X" not in args, "board writes go through the status helper"
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
                    "status_name": OPTIONS[status],
                    "labels": list(self.labels.get(n, [])),
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
        self.gh.put(521, "In progress")  # In progress
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
        self.gh.put(999, "In progress")  # another ticket, not merged here
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(self.gh.writes, [("comment", 521), ("close", 521)])
        self.assertEqual(self.gh.board[999], "3a81da46")

    def test_open_ticket_keeps_its_board_status(self) -> None:
        self.gh.merged = [pr(612, 521)]
        self.gh.ticket(521)
        self.gh.open_prs = [f"Issue: {issue(521)}"]
        self.gh.put(521, "In progress")
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(self.gh.writes, [])

    def test_board_error_leaves_the_close_and_retries_next_run(self) -> None:
        self.gh.merged = [pr(612, 521)]
        self.gh.ticket(521)
        self.gh.put(521, "In progress")
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
        self.gh.put(521, "In progress")  # closed, Status left behind
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(self.gh.writes, [])

    def test_board_skips_a_ticket_locked_by_a_running_tick(self) -> None:
        self.gh.merged = [pr(612, 521)]
        self.gh.put(521, "In progress")  # closed, Status left behind
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
                self.gh.put(521, "In progress")  # In progress
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
        self.gh.put(521, "In progress")
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


class StatusLabelTest(unittest.TestCase):
    """Step 5 through the status helper: board Done and status::done together.
    https://github.com/phaabe/live.moafunk.de/issues/686"""

    def setUp(self) -> None:
        self.gh = FakeGh()
        self.log: list[str] = []
        self.gh.merged = [pr(612, 521)]
        self.gh.ticket(521)

    def run_once(self, gh: Any = None, dry_run: bool = False) -> int:
        return close_merged.run(gh or self.gh, NOW, dry_run, self.log.append)

    def assert_done(self, n: int = 521) -> None:
        self.assertEqual(self.gh.board[n], DONE)
        self.assertEqual(self.gh.labels[n], ["status::done"])

    def changes(self, n: int = 521) -> list[tuple[str, str]]:
        return [(c.frm, c.to) for c in th.label_changes(self.gh.events.get(n, []))]

    def assert_locks_free(self, n: int = 521) -> None:
        with close_merged.ticket_lock(n) as locked:
            self.assertTrue(locked)
        fd = set_status.lock(target_lock.lock_dir() / f"status-{n}.lock")
        self.assertIsNotNone(fd)
        os.close(fd)

    def test_close_then_set_records_a_change(self) -> None:
        self.gh.put(521, "In review")
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(
            self.gh.writes, [("comment", 521), ("close", 521), ("done", 521)]
        )
        self.assert_done()
        self.assertEqual(
            self.gh.status_events(521), ["+status::done", "-status::in-review"]
        )
        self.assertEqual(self.changes(), [("In review", "Done")])
        self.assertTrue(any('"mode": "set"' in x for x in self.log))
        self.gh.writes.clear()
        self.assertEqual(self.run_once(), 0)  # clean now: no call for it
        self.assertEqual(self.gh.writes, [])
        self.assertEqual(len(self.gh.status_events(521)), 2)

    def test_not_clean_is_repaired_then_set(self) -> None:
        cases = {
            "wrong label": ["status::ready"],
            "no label": [],
            "two labels": ["status::in-review", "status::ready"],
            "sync marker": ["status::in-review", "status::sync"],
        }
        for name, labels in cases.items():
            with self.subTest(name):
                self.setUp()
                self.gh.put(521, "In review", labels)
                self.assertEqual(self.run_once(), 0)
                self.assert_done()
                events = self.gh.status_events(521)
                self.assertIn("-status::sync", events)
                self.assertEqual(events[-2:], ["+status::done", "-status::in-review"])
                # The repair is no change; the set after it is one.
                self.assertEqual(self.changes(), [("In review", "Done")])

    def test_failed_repair_does_not_set(self) -> None:
        self.gh.put(521, "In review", ["status::ready"])
        self.gh.fail["add:status::sync"] = "error"
        self.assertEqual(self.run_once(), 1)
        self.assertEqual(self.gh.board[521], NAMES["In review"])
        self.assertNotIn(("done", 521), self.gh.writes)
        self.assertNotIn(521, self.gh.tickets)  # stays closed
        self.assertTrue(any("repair exit 1, no set" in x for x in self.log))
        # The next tick retries.
        self.gh.fail.clear()
        self.assertEqual(self.run_once(), 0)
        self.assert_done()
        self.assertEqual(self.changes(), [("In review", "Done")])

    def test_failed_label_add_is_logged_and_repaired_next_tick(self) -> None:
        self.gh.put(521, "In review")
        self.gh.fail["add:status::done"] = "error"
        self.assertEqual(self.run_once(), 1)
        self.assertEqual(self.gh.board[521], DONE)
        self.assertEqual(self.gh.labels[521], [])  # the old one went
        self.assertTrue(any("status helper exit 1" in x for x in self.log))
        self.gh.fail.clear()
        self.assertEqual(self.run_once(), 0)
        self.assert_done()
        self.assertEqual(self.changes(), [])

    def test_half_done_is_only_repaired(self) -> None:
        # An earlier run wrote board Done but no label.
        self.gh.tickets.pop(521)
        self.gh.put(521, "Done", ["status::in-review"])
        self.assertEqual(self.run_once(), 0)
        self.assert_done()
        self.assertEqual(self.gh.writes, [])  # no board write
        self.assertEqual(
            self.gh.status_events(521),
            ["+status::sync", "+status::done", "-status::in-review",
             "-status::sync"],
        )  # fmt: skip
        self.assertEqual(self.changes(), [])  # no Done time

    def test_failed_old_label_removal_is_repaired_without_a_time(self) -> None:
        self.gh.put(521, "In review")
        self.gh.fail["remove:status::in-review"] = "error"
        self.assertEqual(self.run_once(), 1)
        self.assertEqual(self.gh.labels[521], ["status::in-review", "status::done"])
        self.gh.fail.clear()
        self.assertEqual(self.run_once(), 0)
        self.assert_done()
        self.assertEqual(self.changes(), [])

    def test_board_without_a_status_gets_done_and_its_label(self) -> None:
        self.gh.put(521, "In review", [])
        self.gh.board.pop(521)
        self.gh.board[521] = None  # on the board, Status empty
        OPTIONS[None] = None
        self.addCleanup(OPTIONS.pop, None)
        self.assertEqual(self.run_once(), 0)
        self.assert_done()
        self.assertEqual(self.changes(), [])

    def test_conflict_unknown_and_timeout_results(self) -> None:
        cases = {
            # op -> mode, close step exit, log text
            "moved": ({"status": "moved"}, 1, "status helper exit 3"),
            "unknown": (
                {"status": "error", "scan": "error"},
                1,
                "status helper exit 4",
            ),
            "timeout": ({"status": "timeout"}, 0, '"board": "ok"'),
            "unreadable labels": ({"labels": "error"}, 1, "status helper failed"),
        }
        for name, (fail, code, text) in cases.items():
            with self.subTest(name):
                self.setUp()
                self.gh.put(521, "In review")
                self.gh.fail.update(fail)
                self.assertEqual(self.run_once(), code)
                self.assertTrue(any(text in x for x in self.log), self.log)
                self.assertNotIn(521, self.gh.tickets)  # never reopened
                if code == 0:
                    self.assert_done()
                    self.assertEqual(self.changes(), [("In review", "Done")])

    def deferred_between_board_and_labels(self, stop: Any, extra: int) -> None:
        """`stop(fake)` runs right after the board write; `extra` calls (the
        one that hit the quota) still reach GitHub."""
        self.gh.put(521, "In review")
        seen: list[int] = []

        def after(args: list[str]) -> None:
            if "--method" in args and f"{BOARD}/items/70521" in args and not seen:
                seen.append(len(self.gh.calls))
                stop(self.gh)

        self.gh.after = after
        with tempfile.TemporaryDirectory() as tmp:
            pause = Path(tmp) / "pause"
            self.pause = pause
            with (
                patch.object(next_action, "PAUSE_FILE", pause),
                patch.object(close_merged, "run_gh", self.gh),
                redirect_stdout(io.StringIO()) as printed,
            ):
                self.assertEqual(close_merged.main([]), close_merged.DEFERRED)
        self.assertEqual(len(self.gh.calls), seen[0] + extra)
        self.assertEqual(self.gh.board[521], DONE)
        self.assertEqual(self.gh.labels[521], ["status::in-review"])
        said = printed.getvalue()
        self.assertIn("close: deferred" if not extra else "close: GitHub quota", said)
        self.assertNotIn("board Status Done:", said)  # no false success
        self.assert_locks_free()
        # The next tick finishes it (repair only) and never reopens.
        self.gh.after = None
        self.gh.__dict__.pop("answer", None)
        self.assertEqual(self.run_once(), 0)
        self.assert_done()
        self.assertNotIn(521, self.gh.tickets)

    def test_pause_between_board_and_label_defers(self) -> None:
        self.deferred_between_board_and_labels(lambda gh: self.pause.touch(), 0)

    def test_quota_between_board_and_label_defers(self) -> None:
        def exhausted(gh: FakeGh) -> None:
            def quota(args: list[str]) -> str:
                raise QuotaExhausted("GraphQL RATE_LIMITED")

            gh.answer = quota  # type: ignore[method-assign]

        self.deferred_between_board_and_labels(exhausted, 1)

    def test_busy_helper_lock_skips_until_the_next_tick(self) -> None:
        self.gh.put(521, "In review")
        fd = set_status.lock(target_lock.lock_dir() / "status-521.lock")
        try:
            self.assertEqual(self.run_once(), 0)
        finally:
            os.close(fd)
        self.assertEqual(self.gh.board[521], NAMES["In review"])
        self.assertTrue(any("locked by the status helper" in x for x in self.log))
        self.assertEqual(self.run_once(), 0)
        self.assert_done()

    def test_dry_run_writes_nothing_for_a_half_done_ticket(self) -> None:
        self.gh.tickets.pop(521)
        self.gh.put(521, "Done", ["status::in-review"])
        self.assertEqual(self.run_once(dry_run=True), 0)
        self.assertEqual(self.gh.writes, [])
        self.assertEqual(self.gh.events, {})
        self.assertFalse(any("--method" in c for c in self.gh.calls))

    def test_call_budget_over_three_tickets(self) -> None:
        self.gh.merged = [pr(1, 521), pr(2, 522), pr(3, 523)]
        for n in (522, 523):
            self.gh.ticket(n)
        self.gh.put(521, "In review")  # clean: set
        self.gh.put(522, "In review", ["status::ready"])  # repair, then set
        self.gh.put(523, "Done", ["status::in-review"])  # half-done: repair
        self.assertEqual(self.run_once(), 0)
        for n in (521, 522, 523):
            self.assert_done(n)
        joined = [" ".join(c) for c in self.gh.calls]
        self.assertFalse(any("graphql" in c for c in joined))
        scans = [c for c in joined if f"{BOARD}/items?" in c]
        self.assertEqual(sum("--slurp" not in c for c in scans), 1)  # the run's list
        self.assertEqual(sum("--slurp" in c for c in scans), 2 + 4 + 2)
        self.assertEqual(sum(f"{BOARD}/fields" in c for c in joined), 2)
        # Reads + writes: set 2+2; repair 4+4 (marker, its check, swap, end).
        self.assertEqual(self.gh.label_calls, {521: 4, 522: 4 + 4 + 4, 523: 8})


class StatusHistoryTest(unittest.TestCase):
    """The close step's label events as the Tickets history reads them."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.gh = FakeGh()
        self.gh.merged = [pr(612, 521)]
        self.gh.ticket(521)
        self.gh.put(521, "In review")
        self.t0 = int(NOW) - 4 * 3600
        self.history = self.fresh()
        self.history.observe({521: ("In review", "Claude")}, self.t0)

    def fresh(self) -> th.History:
        history = th.History(self.root)
        history.load(NOW)
        return history

    def close(self) -> None:
        close_merged.run(self.gh, NOW, log=lambda _: None)

    def done_times(self, history: th.History) -> list[tuple[int, bool]]:
        """(time, from a label) of each Done entry."""
        return [
            (e.seen_at, e.event is not None)
            for e in history.entries[521]
            if e.to == "Done"
        ]

    def merged(self) -> th.History:
        """Merge the events, then again and after a restart: no change."""
        self.history.merge_labels({521: self.gh.events.get(521, [])})
        rows = self.history.entries
        self.assertFalse(self.history.merge_labels({521: self.gh.events[521]}))
        replayed = self.fresh()
        self.assertEqual(replayed.entries, rows)
        self.assertFalse(replayed.merge_labels({521: self.gh.events[521]}))
        return replayed

    def test_stack_off_during_the_close_keeps_the_label_time(self) -> None:
        self.gh.clock = self.t0 + 3600
        self.close()  # the collector is off
        added = th.label_changes(self.gh.events[521])[0].at
        self.history.observe({521: ("Done", "Claude")}, self.t0 + 3 * 3600)
        history = self.merged()
        self.assertEqual(self.done_times(history), [(added, True)])
        self.assertEqual(self.gh.labels[521], ["status::done"])

    def test_failed_label_writes_leave_the_snapshot_time(self) -> None:
        for op in ("add:status::done", "remove:status::in-review"):
            with self.subTest(op):
                self.setUp()
                self.gh.fail[op] = "error"
                self.gh.clock = self.t0 + 300
                self.close()
                seen = self.t0 + 600
                self.history.observe({521: ("Done", "Claude")}, seen)
                self.gh.fail.clear()
                self.close()  # the next tick repairs
                self.assertEqual(self.gh.labels[521], ["status::done"])
                history = self.merged()
                self.assertEqual(self.done_times(history), [(seen, False)])


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
                         "labels": [{"name": "type::ci"},
                                    {"name": "status::in-progress"}],
                         "repository_url": f"https://api.github.com/repos/{REPO}"},
             "fields": [{"name": "Status", "value": {
                 "id": "3a81da46", "name": {"raw": "In progress"}}}]},
            {"id": 6, "content_type": "PullRequest", "content": {"number": 7}},
            {"id": 8, "content_type": "Issue",
             "content": {"number": 9, "state": "open", "state_reason": None,
                         "repository_url": "u"}, "fields": []},
        ]  # fmt: skip
        self.assertEqual(
            self.jq(close_merged.BOARD_JQ, rows),
            [{"id": 5, "number": 521, "repo": f"https://api.github.com/repos/{REPO}",
              "state": "closed", "reason": "completed", "status": "3a81da46",
              "status_name": "In progress",
              "labels": ["type::ci", "status::in-progress"]},
             {"id": 8, "number": 9, "repo": "u", "state": "open", "reason": None,
              "status": None, "status_name": None, "labels": []}],
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
