"""Tickets dashboard collector tests. Run: python3 -m unittest discover -s scripts/epic."""

from __future__ import annotations

import isolated_env  # noqa: F401  (first: hides live runner state)

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import monitor
import tickets
from test_delivery import Sink
from test_monitor import URL, issue, pull_request, snapshot

NOW = datetime(2026, 10, 5, 12, tzinfo=timezone.utc).timestamp()
DAY = 86400.0


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ticket(
    number: int,
    status: str,
    executor: str | None = "Claude",
    *,
    body: str = "",
    level: str = "Task",
    closed_at: float | None = None,
    readiness: str = "",
) -> monitor.Json:
    item = issue(number, executor, status, body)  # type: ignore[arg-type]
    item["level"] = level
    item["area"] = "Coordination"
    item["content"]["title"] = f'Ticket "{number}"'
    if closed_at is not None:
        item["content"]["state"] = "closed"
        item["content"]["closed_at"] = iso(closed_at)
    else:
        item["content"]["state"] = "open"
    if readiness:
        item["readiness"] = readiness
    return item


def comment(
    body: str, at: str = "2026-10-05T10:00:00Z", ident: int = 1, edited: bool = False
) -> monitor.Json:
    return {
        "id": ident,
        "body": body,
        "created_at": at,
        "updated_at": "2026-10-05T11:00:00Z" if edited else at,
    }


def digest(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()[:12]


def render(
    state: monitor.Json,
    extra: tickets.Extra | None = None,
    claims: set[int] | None = None,
) -> Sink:
    sink = Sink()
    tickets.ticket_metrics(
        sink,
        state,
        extra or tickets.Extra({}, {}),
        claims or set(),
        NOW,
    )
    return sink


def info(sink: Sink, number: int) -> dict[str, str]:
    """The labels of one ticket's info row."""
    for line in sink.render().splitlines():
        if line.startswith("epic_ticket_info{") and f'issue="{number}"' in line:
            labels = line[len("epic_ticket_info{") : line.rindex("}")]
            return dict(
                (key, value[1:-1].replace('\\"', '"'))
                for key, value in (part.split("=", 1) for part in split_labels(labels))
            )
    raise AssertionError(f"no info row for {number}")


def split_labels(text: str) -> list[str]:
    parts, current, quoted, escape = [], "", False, False
    for char in text:
        if escape:
            current, escape = current + char, False
        elif char == "\\":
            current, escape = current + char, True
        elif char == '"':
            current, quoted = current + char, not quoted
        elif char == "," and not quoted:
            parts.append(current)
            current = ""
        else:
            current += char
    return [*parts, current] if current else parts


class BodyReviewTest(unittest.TestCase):
    def test_digest_is_raw_sha256_prefix(self) -> None:
        self.assertEqual(tickets.body_digest("a\r\nb "), digest("a\r\nb "))
        self.assertNotEqual(tickets.body_digest("a\nb"), tickets.body_digest("a\nb "))

    def test_newest_unedited_one_line_comment_wins(self) -> None:
        rows = [
            comment("Body review: APPROVED aaaaaaaaaaaa", "2026-10-05T09:00:00Z", 1),
            comment("Body review: CHANGES REQUESTED bbbbbbbbbbbb", ident=2),
            comment(
                "Body review: APPROVED cccccccccccc",
                "2026-10-05T12:00:00Z",
                3,
                edited=True,
            ),
            comment("Body review: APPROVED dddddddddddd\n", "2026-10-05T13:00:00Z", 4),
            comment("x\nBody review: APPROVED eeeeeeeeeeee", "2026-10-05T14:00:00Z", 5),
        ]
        self.assertEqual(
            tickets.body_review(rows),
            {"state": "CHANGES REQUESTED", "digest": "bbbbbbbbbbbb"},
        )
        self.assertIsNone(tickets.body_review(rows[3:]))


class PopulationTest(unittest.TestCase):
    def test_caps_order_and_done_window(self) -> None:
        items = [ticket(1000 + i, "Ready") for i in range(tickets.MAX_OPEN + 3)]
        items += [ticket(10, "Done", closed_at=NOW - 8 * DAY)]  # too old
        items += [ticket(11, "Done")]  # issue open
        items += [
            ticket(100 + i, "Done", closed_at=NOW - i * 60)
            for i in range(tickets.MAX_DONE + 1)
        ]
        items += [ticket(12, "Ready", level="Epic"), ticket(13, "Ready", level="")]
        foreign = ticket(14, "Ready")
        foreign["content"]["url"] = "https://github.com/another/repo/issues/14"
        population = tickets.board_tickets(snapshot(items=[*items, foreign]), NOW)
        numbers = [t.number for t in population.tickets]
        self.assertEqual(numbers[: tickets.MAX_OPEN], list(range(1000, 1100)))
        # Open Done issue first, then newest closed.
        self.assertEqual(
            numbers[tickets.MAX_OPEN : tickets.MAX_OPEN + 3], [11, 100, 101]
        )
        self.assertEqual(len(numbers), tickets.MAX_OPEN + tickets.MAX_DONE)
        self.assertNotIn(10, numbers)
        # 3 not-Done and 2 Done over the caps.
        self.assertEqual(population.dropped, 5)
        self.assertEqual(population.counts["Ready"], tickets.MAX_OPEN + 3)
        self.assertEqual(population.counts["Done"], tickets.MAX_DONE + 3)

    def test_backlog_never_pushes_out_active_work(self) -> None:
        items = [ticket(i, "Backlog") for i in range(1, tickets.MAX_OPEN + 1)]
        items += [
            ticket(900, "Refinement"),
            ticket(901, "In review"),
            ticket(902, "Ready"),
            ticket(903, "In progress"),
        ]
        population = tickets.board_tickets(snapshot(items=items), NOW)
        numbers = [t.number for t in population.tickets]
        self.assertEqual(numbers[:4], [901, 903, 902, 900])
        self.assertEqual(numbers[4:], list(range(1, tickets.MAX_OPEN - 3)))
        self.assertEqual(population.dropped, 4)

    def test_status_counts_publish_zeros_and_unknown(self) -> None:
        sink = render(snapshot(items=[ticket(1, "Ready"), ticket(2, "Odd")]))
        self.assertEqual(sink.value("tickets_by_status", status="Ready"), 1)
        self.assertEqual(sink.value("tickets_by_status", status="Backlog"), 0)
        self.assertEqual(sink.value("tickets_by_status", status="Unknown"), 1)
        self.assertEqual(sink.value("tickets_dropped"), 0)


class ChecksTest(unittest.TestCase):
    def test_ready_ticket_can_be_in_both_ready_checks(self) -> None:
        state = snapshot(
            items=[
                ticket(1, "Ready", readiness=f"**Ready:** Start after {URL}/issues/9."),
                ticket(2, "Ready"),
            ]
        )
        extra = tickets.Extra(
            {1: [f"{URL}/issues/9"], 2: [f"{URL}/issues/9", f"{URL}/issues/8"]}, {}
        )
        sink = render(state, extra, claims={1, 2})
        self.assertEqual(sink.value("ticket_check_count", check="ready_undeclared"), 1)
        self.assertEqual(sink.value("ticket_check_count", check="ready_claimable"), 2)
        self.assertEqual(
            sink.value("ticket_check_member", issue="2", check="ready_undeclared"), 1
        )
        self.assertEqual(
            sink.value("ticket_check_member", issue="2", check="ready_claimable"), 1
        )
        self.assertIsNone(
            sink.value("ticket_check_member", issue="1", check="ready_undeclared")
        )
        self.assertEqual(
            sink.value("ticket_check_severity", check="ready_undeclared"), 3
        )
        self.assertEqual(
            sink.value("ticket_check_severity", check="ready_claimable"), 1
        )
        self.assertEqual(
            info(sink, 2)["note"],
            f"Needs {URL}/issues/8 (open) · not declared (+1 more)",
        )
        self.assertEqual(info(sink, 1)["note"], "May be claimed")

    def test_start_after_note_lists_dependencies(self) -> None:
        state = snapshot(
            items=[
                ticket(
                    1,
                    "Ready",
                    readiness=f"**Ready:** Start after B1.1.6 and {URL}/issues/9.",
                )
            ]
        )
        sink = render(state, tickets.Extra({1: []}, {}))
        self.assertEqual(info(sink, 1)["note"], "Start after 9, B1.1.6")
        self.assertEqual(sink.value("ticket_check_count", check="ready_undeclared"), 0)
        self.assertEqual(
            sink.value("ticket_check_severity", check="ready_undeclared"), 0
        )

    def test_failed_source_makes_its_check_unknown_not_zero(self) -> None:
        state = snapshot(items=[ticket(1, "Ready"), ticket(2, "Refinement")])
        sink = render(state, tickets.Extra(None, None))
        for check in ("ready_undeclared", "refinement_unreviewed"):
            self.assertIsNone(sink.value("ticket_check_count", check=check))
            self.assertIsNone(sink.value("ticket_check_severity", check=check))
        self.assertEqual(sink.value("ticket_check_count", check="done_open"), 0)
        self.assertEqual(info(sink, 1)["note"], "Dependencies unknown")
        self.assertEqual(info(sink, 2)["note"], "Body review unknown")

    def test_body_review_states(self) -> None:
        body = "the body"
        state = snapshot(
            items=[
                ticket(1, "Refinement", body=body),
                ticket(2, "Refinement", body=body),
                ticket(3, "Refinement", body=body + " changed"),
                ticket(4, "Refinement", body=body),
            ]
        )
        reviews: dict[int, monitor.Json | None] = {
            1: {"state": "APPROVED", "digest": digest(body)},
            2: None,
            3: {"state": "APPROVED", "digest": digest(body)},
            4: {"state": "CHANGES REQUESTED", "digest": digest(body)},
        }
        sink = render(state, tickets.Extra({}, reviews))
        self.assertEqual(info(sink, 1)["note"], "Body reviewed")
        self.assertEqual(info(sink, 2)["note"], "Body not reviewed")
        self.assertEqual(info(sink, 3)["note"], "Body changed since review")
        self.assertEqual(info(sink, 4)["note"], "Body not reviewed")
        self.assertEqual(
            sink.value("ticket_check_count", check="refinement_unreviewed"), 3
        )
        self.assertEqual(
            sink.value("ticket_check_severity", check="refinement_unreviewed"), 2
        )

    def test_done_open_check_and_done_sort(self) -> None:
        state = snapshot(
            items=[
                ticket(1, "Done"),
                ticket(2, "Done", closed_at=NOW - DAY),
                ticket(3, "Done", closed_at=NOW - 2 * DAY),
                ticket(4, "Backlog"),
            ],
            merged_prs=[{"number": 50, "body": f"Issue: {URL}/issues/2"}],
        )
        sink = render(state)
        self.assertEqual(sink.value("ticket_check_count", check="done_open"), 1)
        self.assertEqual(
            sink.value("ticket_check_member", issue="1", check="done_open"), 1
        )
        self.assertEqual(info(sink, 1)["note"], "Board Done · issue open")
        self.assertEqual(info(sink, 2)["note"], "Merged")
        self.assertEqual(info(sink, 3)["note"], "")
        sorts = [int(info(sink, n)["done_sort"]) for n in (1, 2, 3)]
        # Descending sort: open issue first, then the newest closed.
        self.assertEqual(sorts, sorted(sorts, reverse=True))
        self.assertEqual(sorts[0], tickets.OPEN_DONE_SORT)
        self.assertEqual(sorts[1], int(NOW - DAY))
        self.assertEqual(info(sink, 4)["done_sort"], "")


class InfoTest(unittest.TestCase):
    def test_pr_notes_and_labels(self) -> None:
        state = snapshot(
            items=[
                ticket(1, "In progress"),
                ticket(2, "In progress"),
                ticket(3, "In review", executor="Codex"),
                ticket(4, "In review", executor=None),
                ticket(5, "In progress", executor="Someone"),
            ],
            prs=[
                pull_request(71, "Claude", body=f"Issue: {URL}/issues/2", isDraft=True),
                pull_request(
                    72,
                    "Codex",
                    body=f"Executor: Codex\nReviewer: Claude\nIssue: {URL}/issues/3",
                ),
            ],
            merged_prs=[{"number": 60, "body": f"Issue: {URL}/issues/3"}],
        )
        sink = render(state)
        self.assertEqual(info(sink, 1)["note"], "No PR yet")
        self.assertEqual(info(sink, 2)["note"], "PR 71 (draft)")
        self.assertEqual(info(sink, 2)["pr"], "71")
        # The open PR wins over the merged one.
        self.assertEqual(info(sink, 3)["note"], "PR 72 · review by claude")
        self.assertEqual(info(sink, 3)["pr"], "72")
        self.assertEqual(info(sink, 4)["note"], "No PR yet")
        self.assertEqual(info(sink, 4)["executor"], "Unassigned")
        self.assertEqual(info(sink, 5)["executor"], "Unknown")
        row = info(sink, 1)
        self.assertEqual(row["url"], f"{URL}/issues/1")
        self.assertEqual(row["title"], 'Ticket "1"')
        self.assertEqual(row["status"], "In progress")
        self.assertEqual(row["area"], "Coordination")
        self.assertEqual(row["level"], "Task")

    def test_claimable_uses_the_selector(self) -> None:
        state = snapshot(
            items=[ticket(1, "Ready"), ticket(2, "Ready", executor="Codex")],
            batch_order=[],
            linked_labels={},
            waiting={},
        )
        with patch.object(tickets.epic, "shared_reader", return_value=False):
            self.assertEqual(tickets.claimable(state), {1, 2})


class FetchTest(unittest.TestCase):
    class Blocked(Exception):
        pass

    def test_each_source_fails_alone(self) -> None:
        def pages(url: str) -> list[monitor.Json]:
            if "blocked_by" in url:
                return [
                    {"id": 1, "state": "open", "html_url": f"{URL}/issues/9"},
                    {"id": 2, "state": "closed", "html_url": f"{URL}/issues/8"},
                ]
            raise self.Blocked()

        request = {"ready": [1], "refinement": [2]}
        data = tickets.fetch_extra(request, pages, (self.Blocked,))
        self.assertEqual(data, {"deps": {"1": [f"{URL}/issues/9"]}, "reviews": None})
        extra = tickets.valid_extra(json.loads(json.dumps(data)), request)
        self.assertEqual(extra.deps, {1: [f"{URL}/issues/9"]})
        self.assertIsNone(extra.reviews)

    def test_malformed_or_partial_answers_are_unknown(self) -> None:
        request = {"ready": [1, 2], "refinement": [3]}
        for data in (
            None,
            {"deps": {"1": [], "2": [""]}, "reviews": {"3": {"state": "OK"}}},
        ):
            with self.subTest(data=data):
                extra = tickets.valid_extra(data, request)
                self.assertIsNone(extra.deps)
                self.assertIsNone(extra.reviews)
        # Each source is checked alone: deps lacks issue 2, reviews is fine.
        partial = tickets.valid_extra(
            {"deps": {"1": []}, "reviews": {"3": None}}, request
        )
        self.assertIsNone(partial.deps)
        self.assertEqual(partial.reviews, {3: None})
        good = tickets.valid_extra(
            {
                "deps": {"1": [], "2": []},
                "reviews": {"3": {"state": "APPROVED", "digest": "a" * 12}},
            },
            request,
        )
        self.assertEqual(good.deps, {1: [], 2: []})
        self.assertEqual(good.reviews, {3: {"state": "APPROVED", "digest": "a" * 12}})

    def test_read_request_refuses_bad_numbers(self) -> None:
        self.assertEqual(
            tickets.read_request('{"ready": [1], "refinement": []}'),
            {"ready": [1], "refinement": []},
        )
        for text in ('{"ready": [0]}', '{"ready": ["1"]}', '{"ready": 1}', "[]"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                tickets.read_request(text)

    def test_wanted_lists_ready_and_refinement_tickets(self) -> None:
        state = snapshot(
            items=[ticket(1, "Ready"), ticket(2, "Refinement"), ticket(3, "Backlog")]
        )
        self.assertEqual(tickets.wanted(state, NOW), {"ready": [1], "refinement": [2]})


class CollectTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.state = snapshot(items=[ticket(1, "Ready"), ticket(2, "Refinement")])
        patcher = patch.object(tickets.epic, "shared_reader", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def health(self) -> str:
        return (self.root / "tickets-health.prom").read_text()

    def collect(self, state: monitor.Json | None, fetch) -> bool:  # type: ignore[no-untyped-def]
        return monitor.collect_tickets(self.root, self.root / "cache", 5, state, fetch)

    def test_success_writes_rows_and_health(self) -> None:
        def fetch(cache: Path, request: monitor.Json, timeout: float) -> monitor.Json:
            self.assertEqual(request, {"ready": [1], "refinement": [2]})
            return {"deps": {"1": []}, "reviews": {"2": None}}

        self.assertTrue(self.collect(self.state, fetch))
        text = (self.root / "tickets.prom").read_text()
        self.assertIn('epic_ticket_check_count{check="refinement_unreviewed"} 1', text)
        for source in tickets.SOURCES:
            self.assertIn(
                f'epic_ticket_source_ok{{source="{source}"}} 1', self.health()
            )

    def test_failed_child_keeps_board_rows_and_marks_sources(self) -> None:
        def fetch(cache: Path, request: monitor.Json, timeout: float) -> monitor.Json:
            raise RuntimeError("child failed")

        self.assertFalse(self.collect(self.state, fetch))
        text = (self.root / "tickets.prom").read_text()
        self.assertIn("epic_ticket_info{", text)
        self.assertNotIn('check="ready_undeclared"', text)
        self.assertIn('epic_ticket_source_ok{source="board"} 1', self.health())
        self.assertIn('epic_ticket_source_ok{source="deps"} 0', self.health())
        self.assertIn('epic_ticket_source_ok{source="review"} 0', self.health())

    def test_no_snapshot_keeps_last_rows(self) -> None:
        (self.root / "tickets.prom").write_text("old\n")
        self.assertFalse(self.collect(None, lambda *_: {}))
        self.assertEqual((self.root / "tickets.prom").read_text(), "old\n")
        for source in tickets.SOURCES:
            self.assertIn(
                f'epic_ticket_source_ok{{source="{source}"}} 0', self.health()
            )

    def test_monitor_keeps_the_last_good_snapshot_only_for_its_cycle(self) -> None:
        self.assertTrue(
            monitor.collect_github(self.root, 1, fetch=lambda _: self.state)
        )
        self.assertIs(monitor.LATEST.state, self.state)

        def fail(_: float) -> monitor.Json:
            raise OSError("down")

        self.assertFalse(monitor.collect_github(self.root, 1, fetch=fail))
        self.assertIsNone(monitor.LATEST.state)


if __name__ == "__main__":
    unittest.main()
