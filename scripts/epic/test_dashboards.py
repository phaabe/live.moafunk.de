"""Dashboard generator tests. Run: python3 -m unittest discover -s scripts/epic."""

from __future__ import annotations

import json
import unittest

import dashboards


class DashboardTest(unittest.TestCase):
    def test_committed_json_matches_generator(self) -> None:
        for name, page in dashboards.build().items():
            with self.subTest(name=name):
                committed = (dashboards.OUT / name).read_text()
                self.assertEqual(
                    committed,
                    dashboards.render(page),
                    "run python3 scripts/epic/dashboards.py",
                )

    def test_one_overview_and_one_page_per_agent(self) -> None:
        uids = [page["uid"] for page in dashboards.build().values()]
        # The overview keeps the URL of the first dashboard.
        self.assertEqual(uids, ["epic-agents", "epic-agent-claude", "epic-agent-codex"])

    def test_agent_pages_show_only_their_agent_and_include_logs(self) -> None:
        for agent in dashboards.AGENTS:
            page = dashboards.build()[f"{agent}.json"]
            other = next(a for a in dashboards.AGENTS if a != agent)
            exprs = [t["expr"] for p in page["panels"] for t in p.get("targets", [])]
            with self.subTest(agent=agent):
                self.assertFalse(any(f'agent="{other}"' in e for e in exprs))
                logs = [p for p in page["panels"] if p["type"] == "logs"]
                self.assertEqual(len(logs), 2)
                for panel in logs:
                    self.assertEqual(panel["datasource"]["uid"], "agent-loki")
                    # Runner logs only: the permission gate has its own stream.
                    self.assertIn(
                        f'{{agent="{agent}", stream!="permissions"}}',
                        panel["targets"][0]["expr"],
                    )

    def test_panels_have_unique_ids_and_known_datasources(self) -> None:
        for name, page in dashboards.build().items():
            with self.subTest(name=name):
                ids = [p["id"] for p in page["panels"]]
                self.assertEqual(len(ids), len(set(ids)))
                for panel in page["panels"]:
                    if "targets" in panel:
                        self.assertIn(
                            panel["datasource"]["uid"],
                            ("agent-prometheus", "agent-loki"),
                        )

    def test_link_columns_point_at_hidden_url_fields(self) -> None:
        panel = dashboards.path_table("Now", "epic_current_action_info", "")
        overrides = {
            o["matcher"]["options"]: o["properties"]
            for o in panel["fieldConfig"]["overrides"]
        }
        self.assertEqual(
            overrides["Path"][0]["value"][0]["url"], "${__data.fields.url}"
        )
        self.assertEqual(overrides["url"], [{"id": "custom.hidden", "value": True}])
        self.assertIn("epic_task_level_info", json.dumps(panel["targets"]))


if __name__ == "__main__":
    unittest.main()
