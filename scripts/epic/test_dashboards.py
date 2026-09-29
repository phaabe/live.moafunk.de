"""Dashboard generator tests. Run: python3 -m unittest discover -s scripts/epic."""

from __future__ import annotations

import json
from pathlib import Path
import re
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


if __name__ == "__main__":
    unittest.main()
