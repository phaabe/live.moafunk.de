"""Delivery and handoff tests. Run: python3 -m unittest discover -s scripts/epic."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

import delivery
import monitor
from test_monitor import HEAD, URL, issue, pull_request, snapshot

NOW = datetime(2026, 9, 29, 12, tzinfo=timezone.utc).timestamp()
DAY = 86400.0
OTHER = "b" * 40


def verdict(state: str, by: str, sha: str = HEAD, at: str = "2026-09-28T10:00:00Z"):
    return {
        "body": f"Review: {state} by {by} at {sha}",
        "createdAt": at,
        "includesCreatedEdit": False,
    }


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Sink(monitor.Metrics):
    def value(self, name: str, **labels: str) -> float | None:
        wanted = [f'{k}="{v}"' for k, v in sorted(labels.items())]
        for line in self.render().splitlines():
            if line.startswith(f"epic_{name}") and all(w in line for w in wanted):
                head = line.split(" ")[0]
                if head == f"epic_{name}" or head.startswith(f"epic_{name}{{"):
                    return float(line.rsplit(" ", 1)[1])
        return None


class HandoffClockTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "runtime/handoff.json"

    def observe(self, prs: list[monitor.Json], now: float) -> delivery.Handoff:
        return delivery.HandoffClock(self.path).observe(snapshot(prs=prs), now)

    def test_both_directions_wait_for_the_other_kind(self) -> None:
        handoff = self.observe(
            [pull_request(7, "Claude"), pull_request(8, "Codex", headRefOid=OTHER)],
            NOW,
        )
        self.assertEqual(
            {(w.target, w.waiter_kind, w.waits_for_kind) for w in handoff.waits},
            {
                (f"{URL}/pull/7", "claude", "codex"),
                (f"{URL}/pull/8", "codex", "claude"),
            },
        )

    def test_clock_keeps_its_start_across_polls_and_restarts(self) -> None:
        self.observe([pull_request(7, "Claude")], NOW)
        # A new clock object reads the saved state, as after a restart.
        [wait] = self.observe([pull_request(7, "Claude")], NOW + 600).waits
        self.assertEqual(wait.since, NOW)

    def test_a_new_head_restarts_the_clock(self) -> None:
        self.observe([pull_request(7, "Claude")], NOW)
        [wait] = self.observe(
            [pull_request(7, "Claude", headRefOid=OTHER)], NOW + 600
        ).waits
        self.assertEqual((wait.head, wait.since), (OTHER, NOW + 600))

    def test_draft_escalation_or_verdict_clears_the_wait(self) -> None:
        for change in (
            {"isDraft": True},
            {"labels": [{"name": "needs-anton"}]},
            {"comments": [verdict("APPROVED", "Codex")]},
        ):
            with self.subTest(change=change):
                self.path.unlink(missing_ok=True)
                self.observe([pull_request(7, "Claude")], NOW)
                self.assertEqual(
                    self.observe([pull_request(7, "Claude", **change)], NOW + 60).waits,
                    (),
                )
                # Waiting again later starts a new clock.
                [wait] = self.observe([pull_request(7, "Claude")], NOW + 120).waits
                self.assertEqual(wait.since, NOW + 120)

    def test_failed_poll_changes_no_clock(self) -> None:
        clock = delivery.HandoffClock(self.path)
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            state = snapshot(prs=[pull_request(7, "Claude")])
            self.addCleanup(setattr, monitor.LATEST, "handoff", None)
            self.assertTrue(
                monitor.collect_github(out, 5, lambda _: state, clock=clock)
            )
            saved = self.path.read_text()
            before = monitor.LATEST.handoff

            def fail(_: float) -> monitor.Json:
                raise RuntimeError("down")

            with self.assertLogs(level="ERROR"):
                self.assertFalse(monitor.collect_github(out, 5, fail, clock=clock))
        self.assertEqual(self.path.read_text(), saved)
        self.assertIs(monitor.LATEST.handoff, before)

    def test_invalid_saved_state_starts_new_clocks(self) -> None:
        self.path.parent.mkdir(parents=True)
        for text in (
            "{",
            json.dumps(
                {
                    "v": 1,
                    "observed_at": NOW,
                    "waits": [{"target": "https://evil/pull/1"}],
                }
            ),
        ):
            with self.subTest(text=text):
                self.path.write_text(text)
                with self.assertLogs(level="WARNING"):
                    [wait] = self.observe([pull_request(7, "Claude")], NOW).waits
                self.assertEqual(wait.since, NOW)


class StalledTest(unittest.TestCase):
    def handoff(
        self, *waiters: str, age: float = 3600, observed: float = NOW
    ) -> delivery.Handoff:
        return delivery.Handoff(
            observed,
            tuple(
                delivery.Wait(
                    f"{URL}/pull/{n}",
                    HEAD,
                    kind,
                    "codex" if kind == "claude" else "claude",
                    NOW - age,
                )
                for n, kind in enumerate(waiters, start=1)
            ),
        )

    def stalled(
        self,
        handoff: delivery.Handoff | None,
        presence: list[tuple[str, str]],
        paused: bool = False,
    ) -> float | None:
        sink = Sink()
        delivery.handoff_metrics(sink, handoff, presence, paused, NOW)
        return sink.value("handoff_stalled")

    def test_stalled_only_when_both_kinds_wait_long_and_nobody_acts(self) -> None:
        idle = [("claude", "idle"), ("codex", "late")]
        both = self.handoff("claude", "codex")
        self.assertEqual(self.stalled(both, idle), 1)
        for name, handoff, presence, paused in (
            ("one direction", self.handoff("claude"), idle, False),
            ("short wait", self.handoff("claude", "codex", age=600), idle, False),
            ("paused", both, idle, True),
            (
                "stale observation",
                self.handoff("claude", "codex", observed=NOW - 600),
                idle,
                False,
            ),
            ("no observation", None, idle, False),
        ):
            with self.subTest(name):
                self.assertEqual(self.stalled(handoff, presence, paused), 0)

    def test_one_running_new_or_unknown_agent_stops_the_flag(self) -> None:
        both = self.handoff("claude", "codex")
        for state in ("running", "new", "unknown"):
            with self.subTest(state=state):
                presence = [("claude", "idle"), ("codex", "idle"), ("codex", state)]
                self.assertEqual(self.stalled(both, presence), 0)

    def test_waits_are_published_with_their_age(self) -> None:
        sink = Sink()
        delivery.handoff_metrics(sink, self.handoff("claude", age=90), [], False, NOW)
        self.assertEqual(
            sink.value(
                "handoff_wait_seconds",
                waiter_kind="claude",
                waits_for_kind="codex",
                target=f"{URL}/pull/1",
            ),
            90,
        )

    def test_runner_metrics_publish_the_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for agent in ("claude", "codex"):
                (root / f"{agent}.log").write_text("")
            text = monitor.runner_metrics(
                root,
                False,
                NOW,
                alive=lambda _: False,
                handoff=self.handoff("claude", "codex"),
            )
        self.assertIn('epic_handoff_wait_seconds{target="', text)
        self.assertIn("epic_handoff_stalled ", text)


def rest_pr(
    number: int,
    executor: str | None,
    created: float,
    merged: float | None,
    **extra: object,
) -> monitor.Json:
    return {
        "number": number,
        "body": f"Executor: {executor}" if executor else "",
        "created_at": iso(created),
        "merged_at": iso(merged) if merged is not None else None,
        "updated_at": iso(merged or created),
        "draft": False,
        **extra,
    }


def rest_comment(body: str, at: str = "2026-09-28T10:00:00Z") -> monitor.Json:
    return {"id": hash(body + at), "body": body, "created_at": at, "updated_at": at}


class FakeGitHub:
    def __init__(
        self,
        open_prs: list[monitor.Json],
        closed: list[monitor.Json],
        comments: dict[int, list[monitor.Json]],
        counts: dict[int, int] | None = None,
    ) -> None:
        self.open, self.closed, self.comments = open_prs, closed, comments
        self.counts = counts or {}
        self.calls: list[str] = []

    def __call__(self, args: list[str]) -> object:
        path = next(a for a in args if a.startswith("repos/"))
        self.calls.append(path)
        if "state=open" in path:
            return [self.open] if "base=dev%2F312-interim" in path else [[]]
        if "state=closed" in path:
            return (
                self.closed
                if "base=dev%2F312-interim" in path and "page=1" in path
                else []
            )
        number = int(path.split("/issues/")[1].split("/")[0].split("?")[0])
        if path.endswith("/comments?per_page=100"):
            return [self.comments.get(number, [])]
        return {"comments": self.counts.get(number, len(self.comments.get(number, [])))}


class FetchDeliveryTest(unittest.TestCase):
    def github(self, **counts: int) -> FakeGitHub:
        return FakeGitHub(
            [rest_pr(9, "Codex", NOW - 3600, None)],
            [
                rest_pr(7, "Claude", NOW - 2 * DAY, NOW - DAY),
                rest_pr(
                    6, "Codex", NOW - 20 * DAY, NOW - 15 * DAY
                ),  # outside the window
                rest_pr(5, "Claude", NOW - 3 * DAY, None),  # closed, not merged
            ],
            {
                7: [
                    rest_comment(f"Review: CHANGES REQUESTED by Codex at {OTHER}"),
                    rest_comment(
                        f"Review: APPROVED by Codex at {HEAD}", "2026-09-28T11:00:00Z"
                    ),
                ]
            },
            {int(k[1:]): v for k, v in counts.items()},
        )

    def test_merged_prs_in_the_window_with_review_rounds(self) -> None:
        data = delivery.fetch_delivery(None, NOW, self.github())
        self.assertTrue(delivery.valid_delivery(data))
        self.assertEqual(list(data["merged"]), ["7"])
        entry = data["merged"]["7"]
        self.assertEqual(
            (entry["rounds"], entry["complete"], entry["executor"]), (2, True, "claude")
        )
        self.assertEqual([row["number"] for row in data["open"]], [9])

    def test_incomplete_history_is_reported_not_guessed(self) -> None:
        data = delivery.fetch_delivery(None, NOW, self.github(n7=5))
        self.assertEqual(
            (data["merged"]["7"]["rounds"], data["merged"]["7"]["complete"]),
            (None, False),
        )
        sink = Sink()
        delivery.delivery_metrics(sink, data, [], NOW)
        self.assertEqual(sink.value("delivery_complete"), 0)
        self.assertIsNone(sink.value("review_rounds_median"))

    def test_cached_comments_are_revalidated_after_30_minutes(self) -> None:
        first = delivery.fetch_delivery(None, NOW, self.github())
        github = self.github()
        delivery.fetch_delivery(first, NOW + 600, github)
        self.assertFalse(any("/issues/7" in call for call in github.calls))
        # A comment edit changes nothing GitHub reports on the PR list.
        github.comments[7][1]["updated_at"] = "2026-09-29T11:59:00Z"
        again = delivery.fetch_delivery(first, NOW + delivery.REVALIDATE + 1, github)
        self.assertTrue(any("/issues/7" in call for call in github.calls))
        self.assertEqual(
            again["merged"]["7"]["rounds"], 1
        )  # the edited verdict is ignored

    def test_a_changed_pr_is_fetched_again_at_once(self) -> None:
        first = delivery.fetch_delivery(None, NOW, self.github())
        github = self.github()
        github.closed[0]["updated_at"] = iso(NOW - 60)
        delivery.fetch_delivery(first, NOW + 600, github)
        self.assertTrue(any("/issues/7" in call for call in github.calls))


class DeliveryMetricsTest(unittest.TestCase):
    def data(
        self, *merged: monitor.Json, open_prs: list[monitor.Json] | None = None
    ) -> monitor.Json:
        return {
            "v": 1,
            "fetched_at": NOW,
            "open": open_prs or [],
            "merged": {str(m["number"]): m for m in merged},
        }

    def merged(
        self,
        number: int,
        executor: str | None,
        days_ago: float,
        rounds: int | None = 1,
        hours: float = 2,
        complete: bool = True,
    ) -> monitor.Json:
        end = NOW - days_ago * DAY
        return {
            "number": number,
            "created": end - hours * 3600,
            "merged": end,
            "updated": "",
            "executor": executor,
            "rounds": rounds,
            "complete": complete,
            "checked": NOW,
        }

    def test_per_day_counts_are_zero_filled_by_kind(self) -> None:
        sink = Sink()
        delivery.delivery_metrics(
            sink,
            self.data(self.merged(7, "claude", 1), self.merged(8, None, 1)),
            [],
            NOW,
        )
        day = delivery.local_day(NOW - DAY)
        self.assertEqual(sink.value("merged_prs_day", kind="claude", day=day), 1)
        self.assertEqual(sink.value("merged_prs_day", kind="unknown", day=day), 1)
        self.assertEqual(sink.value("merged_prs_day", kind="codex", day=day), 0)
        self.assertEqual(
            sum(
                line.startswith("epic_merged_prs_day{")
                for line in sink.render().splitlines()
            ),
            3 * delivery.WINDOW_DAYS,
        )

    def test_medians_use_the_last_7_days_only(self) -> None:
        sink = Sink()
        delivery.delivery_metrics(
            sink,
            self.data(
                self.merged(1, "claude", 1, rounds=1, hours=1),
                self.merged(2, "codex", 2, rounds=3, hours=3),
                self.merged(3, "codex", 3, rounds=2, hours=2),
                self.merged(4, "codex", 10, rounds=9, hours=90),  # older than 7 days
            ),
            [],
            NOW,
        )
        self.assertEqual(sink.value("review_rounds_median"), 2)
        self.assertEqual(sink.value("time_to_merge_median_seconds"), 7200)

    def test_empty_window_has_no_medians(self) -> None:
        sink = Sink()
        delivery.delivery_metrics(sink, self.data(), None, NOW)
        self.assertEqual(sink.value("delivery_complete"), 1)
        self.assertIsNone(sink.value("time_to_merge_median_seconds"))

    def test_pr_flow_uses_snapshot_comments_for_open_prs(self) -> None:
        open_row = {
            "number": 9,
            "created": NOW - 60,
            "executor": "claude",
            "draft": False,
        }
        pr = pull_request(
            9, "Claude", comments=[verdict("CHANGES REQUESTED", "Codex", OTHER)]
        )
        sink = Sink()
        delivery.delivery_metrics(
            sink,
            self.data(self.merged(7, "codex", 1, rounds=2), open_prs=[open_row]),
            [pr],
            NOW,
        )
        self.assertEqual(
            sink.value(
                "pr_flow_info",
                target=f"{URL}/pull/9",
                state="open",
                executor="claude",
                reviewer="codex",
            ),
            1,
        )
        self.assertEqual(
            sink.value(
                "pr_flow_info",
                target=f"{URL}/pull/7",
                state="merged",
                reviewer="claude",
            ),
            2,
        )


class AreaLeavesTest(unittest.TestCase):
    def test_leaves_count_once_per_area_and_kind(self) -> None:
        first = issue(1, "Claude", "Ready", "- [x] **A1.1.1** a\n- [ ] **A1.1.2** b")
        first["area"] = "Stream"
        second = issue(
            2, "Codex", "Ready", "- [ ] **A1.1.1** a again\n- [ ] **B1.1.1** c"
        )
        parent = issue(3, "", "Backlog", "- [ ] **B1.1.1** c in the parent task")
        parent["area"] = "Ops"
        sink = Sink()
        delivery.area_leaves(sink, [parent, first, second])
        self.assertEqual(
            sink.value("area_leaves", area="Stream", kind="claude", state="done"), 1
        )
        self.assertEqual(
            sink.value("area_leaves", area="Stream", kind="claude", state="open"), 1
        )
        # The parent task came first; the leaf's own issue names the kind.
        self.assertEqual(
            sink.value("area_leaves", area="Ops", kind="codex", state="open"), 1
        )
        self.assertIsNone(sink.value("area_leaves", kind="unknown"))


class CollectDeliveryTest(unittest.TestCase):
    def test_failure_or_invalid_data_keeps_the_last_snapshot(self) -> None:
        good = {"v": 1, "fetched_at": NOW, "open": [], "merged": {}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "delivery.json"
            self.assertTrue(monitor.collect_delivery(root, cache, 5, lambda *_: good))
            previous = (root / "delivery.prom").read_text()

            def fail(*_: object) -> monitor.Json:
                raise RuntimeError("down")

            for fetch in (fail, lambda *_: {"v": 1}):
                with self.subTest(fetch=fetch), self.assertLogs(level="ERROR"):
                    self.assertFalse(monitor.collect_delivery(root, cache, 5, fetch))
                self.assertEqual((root / "delivery.prom").read_text(), previous)
                self.assertEqual(json.loads(cache.read_text()), good)
                self.assertIn(
                    "epic_delivery_collection_success 0\n",
                    (root / "delivery-health.prom").read_text(),
                )


if __name__ == "__main__":
    unittest.main()
