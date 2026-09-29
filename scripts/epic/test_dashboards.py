"""Dashboard generator tests. Run: python3 -m unittest discover -s scripts/epic."""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import unittest

import dashboards
import delivery
import fixtures
import monitor


def pages() -> dict[str, dashboards.Json]:
    return dashboards.build()


def panels(page: dashboards.Json) -> list[dashboards.Json]:
    return page["panels"]


def exprs(page: dashboards.Json) -> list[str]:
    return [t["expr"] for p in panels(page) for t in p.get("targets", [])]


class DashboardTest(unittest.TestCase):
    def test_committed_json_matches_generator(self) -> None:
        for name, page in pages().items():
            with self.subTest(name=name):
                committed = (dashboards.OUT / name).read_text()
                self.assertEqual(
                    committed,
                    dashboards.render(page),
                    "run python3 scripts/epic/dashboards.py",
                )
        # No stale page is left in the provisioned folder.
        self.assertEqual(
            sorted(p.name for p in dashboards.OUT.glob("*.json")), sorted(pages())
        )

    def test_three_pages_and_two_redirects(self) -> None:
        uids = {name: page["uid"] for name, page in pages().items()}
        # The cockpit keeps the home URL of the first dashboard.
        self.assertEqual(
            uids,
            {
                "overview.json": "epic-agents",
                "agent.json": "epic-agent",
                "delivery.json": "epic-delivery",
                "claude.json": "epic-agent-claude",
                "codex.json": "epic-agent-codex",
            },
        )
        for agent in dashboards.LEGACY_PAGES:
            text = json.dumps(pages()[f"{agent}.json"])
            self.assertIn(f"/d/epic-agent?var-agent={agent}", text)

    def test_no_page_lists_agent_ids(self) -> None:
        """A new agent appears without a dashboard change."""
        for name in ("overview.json", "agent.json", "delivery.json"):
            for expr in exprs(pages()[name]):
                with self.subTest(name=name, expr=expr[:60]):
                    for value in re.findall(r'\bagent=~?"([^"]*)"', expr):
                        self.assertEqual(value, "$agent")
        variable = pages()["agent.json"]["templating"]["list"][0]
        self.assertEqual(variable["definition"], "label_values(epic_agent_info, agent)")

    def test_panels_have_unique_ids_and_known_datasources(self) -> None:
        for name, page in pages().items():
            with self.subTest(name=name):
                ids = [p["id"] for p in panels(page)]
                self.assertEqual(len(ids), len(set(ids)))
                for panel in panels(page):
                    if "targets" in panel:
                        self.assertIn(
                            panel["datasource"]["uid"],
                            ("agent-prometheus", "agent-loki"),
                        )

    def test_panels_fit_the_24_column_grid_without_overlap(self) -> None:
        for name, page in pages().items():
            taken: set[tuple[int, int]] = set()
            for panel in panels(page):
                g = panel["gridPos"]
                with self.subTest(name=name, panel=panel["title"]):
                    self.assertLessEqual(g["x"] + g["w"], 24)
                    cells = {
                        (x, y)
                        for x in range(g["x"], g["x"] + g["w"])
                        for y in range(g["y"], g["y"] + g["h"])
                    }
                    self.assertFalse(cells & taken)
                    taken |= cells

    def test_links_use_kept_fields_and_link_fields_are_hidden(self) -> None:
        for name, page in pages().items():
            for panel in panels(page):
                text = json.dumps(panel)
                keep = [
                    t["options"]["include"]["names"]
                    for t in panel.get("transformations", [])
                    if t["id"] == "filterFieldsByName"
                ]
                for field in re.findall(r"\$\{__data\.fields\.(\w+)\}", text):
                    with self.subTest(name=name, panel=panel["title"], field=field):
                        self.assertTrue(keep and field in keep[0])
                        self.assertIn(
                            {"id": "custom.hidden", "value": True},
                            [
                                prop
                                for o in panel["fieldConfig"]["overrides"]
                                if o["matcher"]["options"] == field
                                for prop in o["properties"]
                            ],
                        )

    def test_overrides_match_shown_columns(self) -> None:
        """An override for a renamed column must use the new header."""
        for name, page in pages().items():
            for panel in panels(page):
                organize = [
                    t["options"]
                    for t in panel.get("transformations", [])
                    if t["id"] == "organize"
                ]
                if not organize or panel["type"] != "table":
                    continue
                shown = set(organize[0]["renameByName"].values())
                kept = set(organize[0]["indexByName"])
                for o in panel["fieldConfig"]["overrides"]:
                    if o["matcher"]["id"] != "byName":
                        continue
                    with self.subTest(
                        panel=panel["title"], column=o["matcher"]["options"]
                    ):
                        self.assertIn(o["matcher"]["options"], shown | kept)

    def test_unknown_tick_duration_is_not_shown_as_seconds(self) -> None:
        """Codex review round 4: the collector exports -1 for a tick
        without a finish time."""
        [panel] = [
            p for p in panels(pages()["agent.json"]) if p["title"] == "Tick history"
        ]
        [duration] = [
            o
            for o in panel["fieldConfig"]["overrides"]
            if o["matcher"]["options"] == "Duration"
        ]
        mappings = {p["id"]: p["value"] for p in duration["properties"]}["mappings"]
        self.assertEqual(mappings[0]["options"]["-1"]["text"], "unknown")

    def test_log_panels_keep_permission_lines_apart(self) -> None:
        page = pages()["agent.json"]
        logs = {p["title"]: p for p in panels(page) if p["type"] == "logs"}
        self.assertEqual(set(logs), {"Last 10 denials", "Errors only", "Runner log"})
        for title, panel in logs.items():
            expr = panel["targets"][0]["expr"]
            with self.subTest(title=title):
                self.assertEqual(panel["datasource"]["uid"], "agent-loki")
                if title == "Last 10 denials":
                    self.assertIn('stream="permissions", decision="deny"', expr)
                else:
                    self.assertIn('stream!="permissions"', expr)

    def test_every_queried_metric_is_published(self) -> None:
        """Metric names in the pages exist in the collector's output."""
        published: set[str] = set()
        now = time.time()
        for scenario in ("normal", "collision", "retired"):
            specs, _ = fixtures.scenario(scenario)
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "state"
                fixtures.build_state(root, specs, now)
                ledgers = monitor.Ledgers(Path(directory) / "runtime")
                state = fixtures.github_snapshot()
                view = monitor.epic_view(state)
                handoff = delivery.Handoff(
                    now,
                    tuple(
                        delivery.Wait(*wait, now - 60)
                        for wait in delivery.waiting(view).values()
                    ),
                )
                sink = monitor.Metrics()
                delivery.delivery_metrics(
                    sink, fixtures.delivery_data(now), view["prs"], now
                )
                texts = (
                    monitor.runner_metrics(
                        root, False, now, ledgers=ledgers, handoff=handoff
                    ),
                    monitor.github_metrics(state, now),
                    sink.render(),
                )
                for text in texts:
                    published |= set(re.findall(r"^(epic_\w+)", text, re.M))
        # Health files written by the collection loops.
        published |= {
            "epic_github_collection_success",
            "epic_delivery_collection_success",
        }
        for name, page in pages().items():
            for expr in exprs(page):
                if "{stream" in expr or expr.startswith("{"):
                    continue  # Loki
                for metric in re.findall(r"\bepic_\w+", expr):
                    with self.subTest(page=name, metric=metric):
                        self.assertIn(metric, published)


def title_of(panel: dashboards.Json) -> str:
    """The panel title, or the title a canvas tile draws itself."""
    if panel["type"] == "canvas":
        return panel["options"]["root"]["elements"][0]["config"]["text"]["fixed"]
    return panel["title"]


def panel_expr(page: str, title: str, ref: str = "A") -> str:
    # The "Agents" tile and table share a title; their query ids differ.
    [expr] = [
        t["expr"]
        for p in panels(pages()[page])
        if title_of(p) == title
        for t in p.get("targets", [])
        if t["refId"] == ref
    ]
    return expr


def as_range(expr: str) -> str:
    """The last value over 20 min at 1 min steps, like Grafana's "last not
    null" on a range query: "@ end()" is the outer query's time."""
    return f"last_over_time(({expr})[20m:1m])"


LOCAL_FRESH = {
    "series": "epic_local_snapshot_timestamp_seconds",
    "values": "0+60x25",
}


def docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(
                ["docker", "info"], capture_output=True, timeout=20
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


@unittest.skipUnless(docker_ready(), "needs docker for promtool")
class QuerySemanticsTest(unittest.TestCase):
    """Codex review of https://github.com/phaabe/live.moafunk.de/pull/479:
    the generated queries, evaluated by Prometheus's own test tool."""

    def run_promtool(self, tests: list[dashboards.Json]) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "queries.test.yml"
            # JSON is valid YAML.
            path.write_text(json.dumps({"evaluation_interval": "1m", "tests": tests}))
            result = subprocess.run(
                ["docker", "run", "--rm", "-v", f"{directory}:/w:ro", "-w", "/w"]
                + ["--entrypoint", "promtool", "prom/prometheus:v3.15.0"]
                + ["test", "rules", "queries.test.yml"],
                capture_output=True,
                text=True,
                timeout=120,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_timeline_gaps_when_stale_and_hides_retired_agents(self) -> None:
        expr = panel_expr(
            "overview.json", "Tick outcomes · last 24 h · held until the next tick"
        )
        self.run_promtool(
            [
                {
                    "interval": "1m",
                    "input_series": [
                        # The collector stops at 5 min; node-exporter keeps
                        # serving the last file.
                        {
                            "series": "epic_local_snapshot_timestamp_seconds",
                            "values": "0+60x5 300x10",
                        },
                        {
                            "series": 'epic_tick_last_outcome{agent="claude"}',
                            "values": "1x15",
                        },
                        {
                            "series": 'epic_agent_presence{agent="claude"}',
                            "values": "1x15",
                        },
                        {
                            "series": 'epic_tick_last_outcome{agent="codex"}',
                            "values": "6x15",
                        },
                        # codex is retired at 3 min.
                        {
                            "series": 'epic_agent_presence{agent="codex"}',
                            "values": "2 2 2 0x12",
                        },
                    ],
                    # promtool evaluates instant queries, so "@ end()" is the
                    # evaluation time here; in Grafana it is the range end.
                    "promql_expr_test": [
                        {
                            "expr": expr,
                            "eval_time": "2m",
                            "exp_samples": [
                                {
                                    "labels": 'epic_tick_last_outcome{agent="claude"}',
                                    "value": 1,
                                },
                                {
                                    "labels": 'epic_tick_last_outcome{agent="codex"}',
                                    "value": 6,
                                },
                            ],
                        },
                        {
                            "expr": expr,
                            "eval_time": "4m",
                            "exp_samples": [
                                {
                                    "labels": 'epic_tick_last_outcome{agent="claude"}',
                                    "value": 1,
                                }
                            ],
                        },
                        {"expr": expr, "eval_time": "10m", "exp_samples": []},
                    ],
                }
            ]
        )

    def test_medians_show_nothing_once_delivery_data_is_stale(self) -> None:
        expr = panel_expr("delivery.json", "Median review rounds")
        self.run_promtool(
            [
                {
                    "interval": "1m",
                    "input_series": [
                        # Delivery collection stops at 5 min.
                        {
                            "series": "epic_delivery_snapshot_timestamp_seconds",
                            "values": "0+60x5 300x20",
                        },
                        {"series": "epic_review_rounds_median", "values": "2x25"},
                    ],
                    "promql_expr_test": [
                        {
                            "expr": as_range(expr),
                            "eval_time": "5m",
                            "exp_samples": [
                                {"labels": "epic_review_rounds_median", "value": 2}
                            ],
                        },
                        {"expr": as_range(expr), "eval_time": "20m", "exp_samples": []},
                    ],
                }
            ]
        )

    def test_medians_show_nothing_once_the_median_is_gone(self) -> None:
        """Codex review round 2: the 7-day window empties while delivery data
        stays fresh; the collector then stops publishing the median."""
        for title, name in (
            ("Median review rounds", "epic_review_rounds_median"),
            ("Median time to merge", "epic_time_to_merge_median_seconds"),
        ):
            expr = panel_expr("delivery.json", title)
            with self.subTest(title=title):
                self.run_promtool(
                    [
                        {
                            "interval": "1m",
                            "input_series": [
                                {
                                    "series": "epic_delivery_snapshot_timestamp_seconds",
                                    "values": "0+60x25",
                                },
                                {"series": name, "values": "2x5 stale"},
                            ],
                            "promql_expr_test": [
                                {
                                    "expr": as_range(expr),
                                    "eval_time": "5m",
                                    "exp_samples": [{"labels": name, "value": 2}],
                                },
                                {
                                    "expr": as_range(expr),
                                    "eval_time": "15m",
                                    "exp_samples": [],
                                },
                            ],
                        }
                    ]
                )

    def test_failures_count_only_while_the_history_is_read(self) -> None:
        """Codex review round 2: cached failures of an agent whose later
        ticks cannot be read are not "failing"."""
        health = [
            {"series": f'epic_{name}{{agent="codex"}}', "values": value}
            for name, value in (
                ("runner_read_success", "1x10"),
                ("tick_ledger_read_success", "1 1 1 0x8"),
                ("tick_events_read_success", "1x10"),
                ("tick_ledger_export_success", "1x10"),
            )
        ]
        failing = panel_expr("overview.json", "Agents failing")
        late = panel_expr("overview.json", "Agents late")
        needs = panel_expr("overview.json", "Needs Anton")
        column = panel_expr("overview.json", "Agents", ref="G")
        self.run_promtool(
            [
                {
                    "interval": "1m",
                    "input_series": [
                        LOCAL_FRESH,
                        *health,
                        {
                            "series": 'epic_tick_consecutive_failures{agent="codex"}',
                            "values": "3x10",
                        },
                        {
                            "series": 'epic_agent_presence{agent="codex"}',
                            "values": "4x10",
                        },
                        # The value is the registration time, not 1.
                        {
                            "series": 'epic_agent_info{agent="codex"}',
                            "values": "1790000000x10",
                        },
                        # A new agent has no count yet: no value, not unknown.
                        {
                            "series": 'epic_agent_info{agent="claude-3"}',
                            "values": "1790000000x10",
                        },
                        {"series": "epic_pause_requested", "values": "0x10"},
                    ],
                    "promql_expr_test": [
                        {
                            "expr": failing,
                            "eval_time": "2m",
                            "exp_samples": [{"labels": "{}", "value": 1}],
                        },
                        # Failing and late: two reasons.
                        {
                            "expr": needs,
                            "eval_time": "2m",
                            "exp_samples": [{"labels": "{}", "value": 2}],
                        },
                        {
                            "expr": late,
                            "eval_time": "2m",
                            "exp_samples": [{"labels": "{}", "value": 1}],
                        },
                        # Codex review round 4: late from cached timing.
                        {
                            "expr": late,
                            "eval_time": "5m",
                            "exp_samples": [{"labels": "{}", "value": 0}],
                        },
                        # The ledger cannot be read from 3 min on.
                        {
                            "expr": failing,
                            "eval_time": "5m",
                            "exp_samples": [{"labels": "{}", "value": 0}],
                        },
                        {
                            "expr": needs,
                            "eval_time": "5m",
                            "exp_samples": [{"labels": "{}", "value": 0}],
                        },
                        # The table says "unknown" (-1), not a count.
                        {
                            "expr": column,
                            "eval_time": "5m",
                            "exp_samples": [{"labels": '{agent="codex"}', "value": -1}],
                        },
                    ],
                }
            ]
        )

    def test_handoff_waits_hide_while_github_is_not_seen(self) -> None:
        """Codex review round 2: cached waits keep growing when GitHub polls
        fail; they must not raise "Needs Anton" or the waiting tile."""
        wait = (
            'epic_handoff_wait_seconds{target="https://x/pull/1",'
            'waiter_kind="claude",waits_for_kind="codex"}'
        )
        needs = panel_expr("overview.json", "Needs Anton")
        waiting = panel_expr("overview.json", "PRs waiting > 30 min")
        oldest = panel_expr("overview.json", "Handoff · per kind", ref="C")
        reviewers = panel_expr("overview.json", "Handoff · per kind", ref="A")
        self.run_promtool(
            [
                {
                    "interval": "1m",
                    "input_series": [
                        LOCAL_FRESH,
                        # GitHub is last seen at 1 min.
                        {
                            "series": "epic_handoff_observed_timestamp_seconds",
                            "values": "0 60x15",
                        },
                        {"series": wait, "values": "3700+60x15"},
                        {"series": "epic_pause_requested", "values": "0x15"},
                        {
                            "series": 'epic_agents_registered_kind{kind="codex"}',
                            "values": "1x15",
                        },
                    ],
                    "promql_expr_test": [
                        {
                            "expr": needs,
                            "eval_time": "2m",
                            "exp_samples": [{"labels": "{}", "value": 1}],
                        },
                        {
                            "expr": waiting,
                            "eval_time": "2m",
                            "exp_samples": [{"labels": "{}", "value": 1}],
                        },
                        {
                            "expr": needs,
                            "eval_time": "10m",
                            "exp_samples": [{"labels": "{}", "value": 0}],
                        },
                        {
                            "expr": waiting,
                            "eval_time": "10m",
                            "exp_samples": [{"labels": "{}", "value": 0}],
                        },
                        {"expr": oldest, "eval_time": "10m", "exp_samples": []},
                        # No reviewer row that would read as "0 waiting".
                        {"expr": reviewers, "eval_time": "10m", "exp_samples": []},
                    ],
                }
            ]
        )


if __name__ == "__main__":
    unittest.main()
