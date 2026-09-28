"""Agent registry tests. Run: python3 -m unittest discover -s scripts/epic."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import re
import tempfile
import unittest

import agents

NOW = 1_800_000_000.0


class RegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def write(self, agent_id: str, **fields: object) -> Path:
        home = self.root / "agents" / agent_id
        home.mkdir(parents=True, exist_ok=True)
        data = {"v": 1, "id": agent_id, "registered_at": NOW - 100}
        data.update(fields)
        (home / "agent.json").write_text(json.dumps(data))
        return home

    def ids(self, now: float = NOW) -> list[str]:
        return [agent.id for agent in agents.discover(self.root, now).agents]

    def test_register_is_idempotent_and_keeps_first_time(self) -> None:
        agents.register(self.root, "claude-2", NOW, label="docs", interval=300)
        agents.register(self.root, "claude-2", NOW + 50, label="docs 2", budget=900)
        agent = agents.load(self.root, "claude-2", NOW + 60)
        self.assertEqual(agent.registered_at, NOW)
        self.assertEqual(
            (agent.label, agent.interval, agent.budget), ("docs 2", 600, 900)
        )
        self.assertEqual(agent.kind, "claude")
        self.assertEqual(agent.log, self.root / "agents/claude-2/claude.log")
        self.assertEqual(agent.lock, self.root / "agents/claude-2/claude.lock")

    def test_register_replaces_a_broken_file(self) -> None:
        home = self.root / "agents/codex"
        home.mkdir(parents=True)
        (home / "agent.json").write_text("{broken")
        agents.register(self.root, "codex", NOW)
        self.assertEqual(self.ids(), ["codex"])

    def test_register_rejects_bad_input(self) -> None:
        for kwargs in (
            {"agent_id": "gemini"},
            {"agent_id": "claude-"},
            {"agent_id": "claude-UPPER"},
            {"agent_id": "../claude"},
            {"agent_id": "claude-2", "label": "x" * 41},
            {"agent_id": "claude-2", "label": "a\nb"},
            {"agent_id": "claude-2", "label": "<b>"},
            {"agent_id": "claude-2", "interval": 5},
            {"agent_id": "claude-2", "budget": float("nan")},
        ):
            with self.subTest(**kwargs), self.assertRaises(ValueError):
                agents.register(self.root, now=NOW, **kwargs)  # type: ignore[arg-type]

    def test_retire_then_hide_after_a_day(self) -> None:
        agents.register(self.root, "codex-review", NOW)
        agents.retire(self.root, "codex-review", NOW + 10)
        [agent] = agents.discover(self.root, NOW + 20).agents
        self.assertEqual(agent.retired_at, NOW + 10)
        self.assertEqual(self.ids(NOW + 10 + agents.RETIRED_KEEP), [])
        # Starting it again clears the retirement.
        agents.register(self.root, "codex-review", NOW + 30)
        self.assertIsNone(agents.discover(self.root, NOW + 40).agents[0].retired_at)

    def test_invalid_entries_are_counted_not_shown(self) -> None:
        self.write("claude-a", id="claude-b")  # folder mismatch
        self.write("codex-x", kind="claude")  # kind mismatch
        self.write("claude-c", label=7)  # bad label: shown with the id only
        self.write("claude-d", interval_seconds=True)
        self.write("claude-e", registered_at=NOW + 3600)  # future
        self.write("claude-f", v=2)
        self.write("gemini", id="gemini")
        (self.root / "agents/claude-g").mkdir()  # no agent.json yet: ignored
        (self.root / "agents/notes.txt").write_text("x")  # ignored
        self.write("claude-ok")
        registry = agents.discover(self.root, NOW)
        self.assertEqual([a.id for a in registry.agents], ["claude-c", "claude-ok"])
        self.assertEqual(registry.agents[0].label, "")
        self.assertEqual(registry.rejected["invalid"], 6)

    def test_oversized_file_is_rejected(self) -> None:
        home = self.write("claude-2")
        (home / "agent.json").write_text(json.dumps({"v": 1, "pad": "x" * 5000}))
        self.assertEqual(agents.discover(self.root, NOW).rejected["invalid"], 1)

    def test_links_are_rejected(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "agent.json").write_text(
            json.dumps({"v": 1, "id": "claude-9", "registered_at": NOW})
        )
        (self.root / "agents").mkdir()
        (self.root / "agents/claude-9").symlink_to(outside)
        home = self.write("claude-2")
        (home / "claude.log").symlink_to(outside / "agent.json")
        registry = agents.discover(self.root, NOW)
        self.assertEqual(registry.agents, [])
        self.assertEqual(registry.rejected["invalid"], 2)
        with self.assertRaises(ValueError):
            agents.register(self.root, "claude-9", NOW)

    def test_linked_agents_folder_is_rejected(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (self.root / "agents").symlink_to(outside)
        self.assertEqual(agents.discover(self.root, NOW).rejected["invalid"], 1)

    def test_legacy_agents_need_a_log_or_lock(self) -> None:
        self.assertEqual(self.ids(), [])
        (self.root / "claude.log").write_text("")
        (self.root / "codex.lock").mkdir()
        registry = agents.discover(self.root, NOW)
        self.assertEqual([a.id for a in registry.agents], ["claude", "codex"])
        claude = registry.agents[0]
        self.assertEqual(
            (claude.layout, claude.interval, claude.budget), ("legacy", 600, 1930)
        )
        self.assertEqual(claude.log, self.root / "claude.log")
        self.assertEqual(claude.checkpoint_name, "ticks-claude.json")

    def test_registered_id_wins_over_legacy_files(self) -> None:
        (self.root / "claude.log").write_text("")
        agents.register(self.root, "claude", NOW)
        registry = agents.discover(self.root, NOW)
        [agent] = registry.agents
        self.assertEqual(agent.layout, "registered")
        self.assertEqual(agent.checkpoint_name, "ticks-agents-claude.json")
        self.assertEqual(registry.conflicts, ["claude"])
        self.assertEqual(registry.rejected["conflict"], 1)

    def test_limit_keeps_legacy_and_oldest_registrations(self) -> None:
        (self.root / "codex.log").write_text("")
        for i in range(13):
            self.write(f"claude-{i:02d}", registered_at=NOW - 100 + i)
        agents.register(self.root, "codex-late", NOW)
        agents.retire(self.root, "codex-late", NOW)
        registry = agents.discover(self.root, NOW)
        ids = [agent.id for agent in registry.agents]
        # A full set of active agents never hides a just-retired one.
        self.assertEqual(len(ids), agents.MAX_AGENTS + 1)
        self.assertEqual(
            (ids[0], ids[-2], ids[-1]), ("codex", "claude-10", "codex-late")
        )
        self.assertEqual(registry.rejected["limit"], 2)

    def test_retired_agents_have_their_own_cap(self) -> None:
        for i in range(agents.MAX_RETIRED + 2):
            self.write(f"codex-{i:02d}", retired_at=NOW - 1000 + i)
        registry = agents.discover(self.root, NOW)
        ids = [agent.id for agent in registry.agents]
        self.assertEqual(len(ids), agents.MAX_RETIRED)
        self.assertEqual(ids[0], "codex-13")  # most recently retired first
        self.assertEqual(registry.rejected["limit"], 2)

    def test_malformed_values_reject_only_their_agent(self) -> None:
        huge = "1" + "0" * 400
        for agent_id, field, raw in (
            ("claude-a", "registered_at", huge),
            ("claude-b", "interval_seconds", huge),
            ("claude-c", "budget_seconds", "-" + huge),
            ("claude-d", "retired_at", "1e999"),
            ("claude-e", "label", '"\\ud800"'),
            ("claude-f", "label", '"a\\u2028b"'),
            ("claude-g", "id", "[" * 3000 + "]" * 3000),
        ):
            home = self.root / "agents" / agent_id
            home.mkdir(parents=True)
            fields = {"v": "1", "id": f'"{agent_id}"', "registered_at": str(NOW - 1)}
            fields[field] = raw
            body = ", ".join(f'"{k}": {v}' for k, v in fields.items())
            (home / "agent.json").write_text("{" + body + "}")
        self.write("codex")
        registry = agents.discover(self.root, NOW)
        ids = [agent.id for agent in registry.agents]
        # Bad labels fall back to the id; everything else rejects the entry.
        self.assertEqual(sorted(ids), ["claude-e", "claude-f", "codex"])
        self.assertEqual([a.label for a in registry.agents], ["", "", ""])
        self.assertEqual(registry.rejected["invalid"], 5)

    def test_files_are_opened_without_following_links(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.json").write_text("{}")
        home = self.write("claude-2")
        lock = home / "claude.lock"
        lock.mkdir()
        (lock / "owner.json").symlink_to(outside / "secret.json")
        [agent] = agents.discover(self.root, NOW).agents
        with self.assertRaises(OSError):
            agent.open("claude.lock", "owner.json")
        (home / "claude-gate.json").mkdir()  # not a regular file
        with self.assertRaises(ValueError):
            agents.open_file(self.root, "agents", "claude-2", "claude-gate.json")
        self.assertIsNone(agent.open("claude.log"))
        # A folder swapped for a link is refused too.
        lock.rename(self.root / "moved")
        lock.symlink_to(outside)
        with self.assertRaises(OSError):
            agent.open("claude.lock", "secret.json")

    def test_cli_register_checks_kind_and_lists(self) -> None:
        base = ["--state-dir", str(self.root)]
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(
                agents.main([*base, "register", "--id", "codex-2", "--kind", "claude"]),
                2,
            )
        self.assertEqual(
            agents.main([*base, "register", "--id", "codex-2", "--kind", "codex"]), 0
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(agents.main([*base, "list"]), 0)
        self.assertEqual(json.loads(out.getvalue())["agents"][0]["id"], "codex-2")


class AlloyRuleTest(unittest.TestCase):
    def test_alloy_keeps_exactly_the_valid_agent_logs(self) -> None:
        config = (
            Path(__file__).resolve().parents[2] / "tools/agent-monitoring/alloy.alloy"
        ).read_text()
        patterns = set(re.findall(r'regex\s*=\s*"([^"]+)"', config))
        self.assertEqual(len(patterns), 1)
        # Prometheus relabel regexes are anchored at both ends.
        rule = re.compile(patterns.pop().replace("\\\\", "\\"))
        for agent_id in (
            "claude",
            "codex",
            "claude-2",
            "codex-review",
            "x",
            "Claude",
            "claude-",
            "claude-a_b",
            "gemini-1",
            "claude-" + "a" * 17,
        ):
            path = f"/logs/agents/{agent_id}/{agents.kind_of(agent_id)}.log"
            match = rule.fullmatch(path)
            self.assertEqual(bool(match), bool(agents.ID.fullmatch(agent_id)), agent_id)
            if match:
                self.assertEqual(match[1], agent_id)


if __name__ == "__main__":
    unittest.main()
