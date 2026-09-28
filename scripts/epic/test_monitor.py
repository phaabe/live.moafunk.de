"""Collector regression tests. Run: python3 -m unittest discover -s scripts/epic."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

import agents
import monitor


NOW = 2_000.0
LEGACY = ("claude", "codex")
HEAD = "a" * 40
URL = "https://github.com/phaabe/live.moafunk.de"


def issue(number: int, agent: str, status: str, body: str = "") -> monitor.Json:
    return {
        "executor": agent,
        "status": status,
        "content": {
            "type": "Issue",
            "number": number,
            "url": f"{URL}/issues/{number}",
            "body": body,
        },
    }


def pull_request(number: int, agent: str, **overrides: object) -> monitor.Json:
    result = {
        "number": number,
        "body": f"Executor: {agent}",
        "title": f"Task {number}",
        "headRefOid": HEAD,
        "baseRefName": "dev/312-interim",
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "statusCheckRollup": [{"conclusion": "SUCCESS"}],
        "comments": [],
        "labels": [],
    }
    result.update(overrides)
    return result


def snapshot(**overrides: object) -> monitor.Json:
    result = {"items": [], "prs": [], "merged_prs": []}
    result.update(overrides)
    return result


class RunnerMetricsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        # The monitor finds legacy agents by their log, as on the runner host.
        for agent in LEGACY:
            (self.root / f"{agent}.log").write_text("")

    def write_json(self, name: str, value: object) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        return path

    def owner(self, **overrides: object) -> Path:
        value = {"pid": 42, "started_at": 1_900, "max_age": 200}
        value.update(overrides)
        return self.write_json("claude.lock/owner.json", value)

    def test_live_dead_and_overdue_locks(self) -> None:
        for live, start, expected in (
            (True, 1_900, "running"),
            (False, 1_900, "orphaned"),
            (True, 1_000, "overdue"),
            (False, 1_000, "orphaned"),
        ):
            with self.subTest(live=live, start=start):
                self.owner(started_at=start)
                text = monitor.runner_metrics(
                    self.root, False, NOW, alive=lambda _: live
                )
                self.assertIn(
                    f'epic_runner_state{{agent="claude",state="{expected}"}} 1\n', text
                )
                self.assertIn(
                    f'epic_tick_elapsed_seconds{{agent="claude"}} {int(NOW - start)}\n',
                    text,
                )

    def test_inactive_runner_is_not_an_error(self) -> None:
        text = monitor.runner_metrics(self.root, False, NOW)
        for agent in LEGACY:
            self.assertIn(
                f'epic_runner_state{{agent="{agent}",state="inactive"}} 1\n', text
            )
            self.assertIn(f'epic_runner_read_success{{agent="{agent}"}} 1\n', text)
        self.assertNotIn("epic_tick_elapsed_seconds", text)

    def test_pause_does_not_hide_running_process(self) -> None:
        self.owner()
        text = monitor.runner_metrics(self.root, True, NOW, alive=lambda _: True)
        self.assertIn("epic_pause_requested 1\n", text)
        self.assertIn('epic_runner_state{agent="claude",state="running"} 1\n', text)

    def test_lock_without_owner_is_unknown(self) -> None:
        (self.root / "claude.lock").mkdir()
        text = monitor.runner_metrics(self.root, False, NOW)
        self.assertIn('epic_runner_state{agent="claude",state="unknown"} 1\n', text)

    def test_malformed_owner_does_not_hide_other_agent(self) -> None:
        for owner in (
            {},
            [],
            {"pid": True},
            {"pid": 1.5, "started_at": 10, "max_age": 20},
            {"pid": 42, "started_at": NOW + 10, "max_age": 20},
        ):
            with self.subTest(owner=owner):
                self.write_json("claude.lock/owner.json", owner)
                with self.assertLogs(level="WARNING"):
                    text = monitor.runner_metrics(self.root, False, NOW)
                self.assertIn('epic_runner_read_success{agent="claude"} 0\n', text)
                self.assertIn('epic_runner_read_success{agent="codex"} 1\n', text)
                self.assertNotIn('epic_runner_state{agent="claude"', text)

    def test_invalid_json_is_reported(self) -> None:
        path = self.owner()
        path.write_text("{partial")
        with self.assertLogs(level="WARNING"):
            text = monitor.runner_metrics(self.root, False, NOW)
        self.assertIn('epic_runner_read_success{agent="claude"} 0\n', text)

    def test_gate_keeps_session_timestamp_and_safe_action_only(self) -> None:
        self.write_json(
            "codex-gate.json",
            {
                "at": 1_500,
                "action": {"action": "review", "pr": 77, "reason": "private prompt"},
            },
        )
        text = monitor.runner_metrics(self.root, False, NOW)
        self.assertIn(
            'epic_last_session_success_timestamp_seconds{agent="codex"} 1500\n', text
        )
        self.assertIn(
            f'epic_last_successful_action_info{{action="review",agent="codex",target="{URL}/pull/77"}} 1\n',
            text,
        )
        self.assertNotIn("private prompt", text)

    def test_malformed_gate_action_is_reported(self) -> None:
        self.write_json("claude-gate.json", {"at": 1_500, "action": []})
        with self.assertLogs(level="WARNING"):
            text = monitor.runner_metrics(self.root, False, NOW)
        self.assertIn('epic_runner_read_success{agent="claude"} 0\n', text)
        self.assertIn('epic_runner_read_success{agent="codex"} 1\n', text)

    def test_invalid_gate_timestamps_are_not_exported(self) -> None:
        for at in (True, 0, -1, "1500", float("nan"), float("inf")):
            with self.subTest(at=at):
                self.write_json(
                    "claude-gate.json", {"at": at, "action": {"action": "idle"}}
                )
                with self.assertLogs(level="WARNING"):
                    text = monitor.runner_metrics(self.root, False, NOW)
                self.assertIn('epic_runner_read_success{agent="claude"} 0\n', text)
                self.assertNotIn("epic_last_session_success_timestamp_seconds", text)

    def test_action_removed_during_collection_is_tolerated(self) -> None:
        self.owner()
        action = self.write_json(
            "claude.lock/action.json", {"action": "review", "pr": 77}
        )

        def complete_tick(_: int) -> bool:
            action.unlink()
            return True

        text = monitor.runner_metrics(self.root, False, NOW, alive=complete_tick)
        self.assertIn('epic_runner_read_success{agent="claude"} 1\n', text)
        self.assertNotIn("epic_current_action_info", text)

    def test_empty_action_during_selection_keeps_running_state(self) -> None:
        self.owner()
        action = self.root / "claude.lock/action.json"
        for content in ("", " \t\n"):
            with self.subTest(content=content):
                action.write_text(content)
                text = monitor.runner_metrics(
                    self.root, False, NOW, alive=lambda _: True
                )
                self.assertIn(
                    'epic_runner_state{agent="claude",state="running"} 1\n', text
                )
                self.assertIn('epic_tick_elapsed_seconds{agent="claude"} 100\n', text)
                self.assertIn('epic_runner_read_success{agent="claude"} 1\n', text)
                self.assertNotIn("epic_current_action_info", text)

    def test_failed_metadata_read_discards_partial_agent_sample(self) -> None:
        self.owner()
        self.write_json("claude.lock/action.json", [])
        with self.assertLogs(level="WARNING"):
            text = monitor.runner_metrics(self.root, False, NOW, alive=lambda _: True)
        self.assertNotIn("epic_tick_elapsed_seconds", text)
        self.assertIn('epic_runner_read_success{agent="claude"} 0\n', text)

    def test_logs_export_only_last_exit_and_modified_time(self) -> None:
        log = self.root / "codex.log"
        log.write_text(
            "private model output\ntick: finished exit=1\ntick: finished exit=0\n"
        )
        os.utime(log, (1_400, 1_400))
        text = monitor.runner_metrics(self.root, False, NOW)
        self.assertIn('epic_last_observed_exit_code{agent="codex"} 0\n', text)
        self.assertIn('epic_log_modified_timestamp_seconds{agent="codex"} 1400\n', text)
        self.assertNotIn("private model output", text)

    def test_running_tick_exports_its_whole_budget(self) -> None:
        self.owner(max_age=1930)
        text = monitor.runner_metrics(self.root, False, NOW, alive=lambda _: True)
        self.assertIn('epic_tick_budget_seconds{agent="claude"} 1930\n', text)

    def test_outcome_severity_is_published_once(self) -> None:
        text = monitor.runner_metrics(self.root, False, NOW)
        self.assertEqual(text.count("# TYPE epic_outcome_severity gauge"), 1)
        self.assertIn('epic_outcome_severity{outcome="error"} 6\n', text)

    def test_ledgers_publish_ticks_for_both_agents(self) -> None:
        for agent in LEGACY:
            (self.root / f"{agent}.log").write_text(
                "\ntick: started 1970-01-01T00:10:00Z repo=/x\ntick: finished exit=0\n"
            )
        ledgers = monitor.Ledgers(self.root / "runtime")
        text = monitor.runner_metrics(self.root, False, NOW, ledgers=ledgers)
        self.assertEqual(text.count("# TYPE epic_ticks_total counter"), 1)
        for agent in LEGACY:
            self.assertIn(f'epic_tick_ledger_read_success{{agent="{agent}"}} 1\n', text)
            self.assertIn(f'epic_tick_last_outcome{{agent="{agent}"}} 1\n', text)
        self.assertTrue((self.root / "runtime/ticks-codex.json").exists())

    def test_missing_log_is_reported_not_zeroed(self) -> None:
        home = agents.register(self.root, "codex-2", 1.0)
        log = home / "codex.log"
        log.write_text(
            "\ntick: started 1970-01-01T00:10:00Z repo=/x\ntick: finished exit=1\n"
        )
        ledgers = monitor.Ledgers(self.root / "runtime")
        monitor.runner_metrics(self.root, False, NOW, ledgers=ledgers)
        log.unlink()
        with self.assertLogs(level="WARNING"):
            text = monitor.runner_metrics(self.root, False, NOW + 5, ledgers=ledgers)
        self.assertIn('epic_tick_ledger_read_success{agent="codex-2"} 0\n', text)
        self.assertIn('epic_tick_last_outcome{agent="codex-2"} 6\n', text)
        # A broken ledger never hides the runner state.
        self.assertIn('epic_runner_state{agent="codex-2",state="inactive"} 1\n', text)


def tick_log(*ticks: tuple[float, int, str]) -> str:
    """Log text for ticks given as (start, exit code, first JSON line)."""
    return "".join(
        "\ntick: started "
        + time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start))
        + f" repo=/x\n{action}\ntick: finished exit={code}\n"
        for start, code, action in ticks
    )


class AgentRowsTest(unittest.TestCase):
    """Presence, row text, collisions and the recent strip per agent."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.ledgers = monitor.Ledgers(self.root / "runtime")

    def agent(self, agent_id: str, *ticks: tuple[float, int, str], **kw: float) -> Path:
        home = agents.register(self.root, agent_id, kw.pop("at", 1.0), **kw)
        (home / f"{agents.kind_of(agent_id)}.log").write_text(tick_log(*ticks))
        return home

    def run_at(self, now: float, *, paused: bool = False, alive: bool = True) -> str:
        return monitor.runner_metrics(
            self.root, paused, now, alive=lambda _: alive, ledgers=self.ledgers
        )

    def running(self, home: Path, kind: str, started: float, action: str) -> None:
        lock = home / f"{kind}.lock"
        lock.mkdir()
        (lock / "owner.json").write_text(
            json.dumps({"pid": 42, "started_at": started, "max_age": 300})
        )
        (lock / "action.json").write_text(action)

    def test_presence_follows_interval_budget_and_pause(self) -> None:
        # Late after 2 x interval + budget = 2 x 60 + 100 = 220 s without a start.
        self.agent("claude-a", (1_000, 0, "{}"), interval=60, budget=100)
        self.agent("codex-b", interval=60, budget=100, at=1_000)
        agents.register(self.root, "codex-c", 1_000)
        agents.retire(self.root, "codex-c", 1_100)
        text = self.run_at(1_200)
        self.assertIn(
            'epic_agent_presence_info{agent="claude-a",presence="idle"} 1', text
        )
        self.assertIn(
            'epic_agent_presence_info{agent="codex-b",presence="new"} 1', text
        )
        self.assertIn('epic_agent_presence{agent="codex-c"} 0', text)
        self.assertNotIn('epic_runner_state{agent="codex-c"', text)
        self.assertIn('epic_agent_next_tick_seconds{agent="claude-a"} -140', text)
        text = self.run_at(1_221)
        self.assertIn('epic_agent_presence{agent="claude-a"} 4', text)
        self.assertIn(
            'epic_agent_presence_info{agent="codex-b",presence="late"} 1', text
        )
        self.assertIn('epic_agents_registered{presence="late"} 2', text)
        self.assertIn('epic_agents_registered_kind{kind="codex"} 1', text)
        # A pause stops ticks on purpose: nobody is late and no tick is due.
        text = self.run_at(1_221, paused=True)
        self.assertIn(
            'epic_agent_presence_info{agent="claude-a",presence="idle"} 1', text
        )
        self.assertIn(
            'epic_agent_presence_info{agent="codex-b",presence="new"} 1', text
        )
        self.assertNotIn("epic_agent_next_tick_seconds", text)

    def test_running_agent_row_and_collision(self) -> None:
        pr = '{"action": "review", "pr": 7}'
        first = self.agent("claude", (100, 1, "{}"))
        second = self.agent("claude-2")
        self.agent("codex", (200, 75, '{"action": "claim", "issue": "x"}'))
        self.running(first, "claude", 1_900, pr)
        self.running(second, "claude", 1_950, pr)
        text = self.run_at(NOW)
        target = f"{URL}/pull/7"
        for name in ("claude", "claude-2"):
            self.assertIn(
                f'epic_agent_collision{{agent="{name}",target="{target}"}} 1', text
            )
            self.assertIn(
                f'epic_agent_row_info{{action_text="collision · review PR 7",agent="{name}"',
                text,
            )
        self.assertIn('epic_agent_budget_seconds{agent="claude"} 300', text)
        self.assertIn(
            'epic_agent_last_start_timestamp_seconds{agent="claude"} 1900', text
        )
        self.assertNotIn('epic_agent_next_tick_seconds{agent="claude"}', text)
        # Idle rows show the last tick's action; exit 75 without a blocked line is error.
        self.assertIn(
            'epic_agent_row_info{action_text="last: claim",agent="codex",'
            'outcome_text="error 75 · unknown",target=""} 1',
            text,
        )
        # Order: running first, then kind, then id.
        order = [
            line.split('"')[1]
            for line in text.splitlines()
            if line.startswith("epic_agent_order{")
        ]
        self.assertEqual(order, ["claude", "claude-2", "codex"])

    def test_one_running_agent_on_a_target_is_no_collision(self) -> None:
        home = self.agent("claude")
        self.running(home, "claude", 1_900, '{"action": "fix", "pr": 9}')
        self.agent("codex", (1_800, 0, '{"action": "fix", "pr": 9}'))
        text = self.run_at(NOW)
        self.assertNotIn("epic_agent_collision", text)
        self.assertIn('action_text="fix PR 9",agent="claude"', text)
        self.assertIn('action_text="last: fix PR 9",agent="codex"', text)

    def test_recent_strip_is_right_aligned(self) -> None:
        self.agent("codex", *[(100 + i, (0, 1, 124)[i % 3], "{}") for i in range(3)])
        text = self.run_at(NOW)
        self.assertIn('epic_agent_recent{agent="codex",slot="20"} 3', text)
        self.assertIn('epic_agent_recent{agent="codex",slot="19"} 6', text)
        self.assertIn('epic_agent_recent{agent="codex",slot="18"} 1', text)
        self.assertNotIn('slot="17"', text)

    def test_agents_come_and_go_between_cycles(self) -> None:
        self.run_at(NOW)
        home = self.agent("codex-new", (1_000, 0, "{}"))
        self.assertIn('epic_tick_last_outcome{agent="codex-new"} 1', self.run_at(NOW))
        checkpoint = self.root / "runtime/ticks-agents-codex-new.json"
        self.assertTrue(checkpoint.exists())
        shutil.rmtree(home)
        text = self.run_at(NOW + 5)
        self.assertNotIn("codex-new", text)
        self.assertEqual(self.ledgers.ledgers, {})
        self.assertFalse(checkpoint.exists())

    def test_returning_agent_does_not_count_old_ticks_again(self) -> None:
        # Codex review: a restored log with a new inode was read as rotated.
        home = self.agent("codex-2")
        log = home / "codex.log"
        self.run_at(NOW)
        log.write_text(tick_log((1_000, 0, "{}")))
        ok = 'epic_ticks_total{agent="codex-2",outcome="ok"}'
        self.assertIn(f"{ok} 1\n", self.run_at(NOW + 5))
        content = log.read_text()
        shutil.rmtree(home)
        self.run_at(NOW + 10)
        home = self.agent("codex-2")
        (home / "codex.log").write_text(content)
        self.assertIn(f"{ok} 0\n", self.run_at(NOW + 15))

    def test_left_over_checkpoints_go_after_a_week(self) -> None:
        # From an agent that vanished while the collector was stopped.
        runtime = self.root / "runtime"
        runtime.mkdir()
        old = runtime / "ticks-agents-codex-gone.json"
        old.write_text("{}")
        self.run_at(NOW)
        self.assertTrue(old.exists())
        os.utime(old, (NOW, NOW))
        self.run_at(NOW + monitor.CHECKPOINT_KEEP + 1)
        self.assertFalse(old.exists())

    def test_running_tick_without_an_action_has_no_old_target(self) -> None:
        # Codex review: the last target of a selecting agent made a collision.
        pr = '{"action": "review", "pr": 7}'
        first = self.agent("claude", (1_000, 0, pr))
        second = self.agent("claude-2")
        self.running(first, "claude", 1_900, "")
        self.running(second, "claude", 1_950, pr)
        text = self.run_at(NOW)
        self.assertNotIn("epic_agent_collision", text)
        self.assertIn(
            'epic_agent_row_info{action_text="selecting",agent="claude",'
            'outcome_text="ok 0",target=""} 1',
            text,
        )

    def test_linked_lock_file_is_a_read_failure(self) -> None:
        home = self.agent("claude-2")
        secret = self.root / "secret.json"
        secret.write_text('{"pid": 42, "started_at": 1900, "max_age": 300}')
        (home / "claude.lock").mkdir()
        (home / "claude.lock/owner.json").symlink_to(secret)
        with self.assertLogs(level="WARNING"):
            text = self.run_at(NOW)
        self.assertIn('epic_runner_read_success{agent="claude-2"} 0\n', text)
        self.assertNotIn('epic_tick_elapsed_seconds{agent="claude-2"}', text)

    def test_each_metric_family_is_contiguous(self) -> None:
        # Codex review: the second agent's samples followed other families.
        for name in ("claude", "codex", "codex-2"):
            self.agent(name, (1_000, 1, '{"action": "fix", "pr": 3}'))
        text = self.run_at(NOW)
        seen: list[str] = []
        for line in text.splitlines():
            family = line.split()[2] if line.startswith("#") else line.split("{")[0]
            family = family.split()[0]
            if not seen or seen[-1] != family:
                self.assertNotIn(family, seen, f"{family} is split")
                seen.append(family)
        self.assertEqual(text.count("# TYPE epic_agent_info gauge"), 1)

    def test_registry_problems_are_published(self) -> None:
        (self.root / "claude.log").write_text("")
        self.agent("claude")
        (self.root / "agents/codex-x").mkdir()
        (self.root / "agents/codex-x/agent.json").write_text("[]")
        text = self.run_at(NOW)
        self.assertIn('epic_agent_conflict{agent="claude"} 1', text)
        self.assertIn('epic_agent_registry_rejected{reason="invalid"} 1', text)
        self.assertIn('epic_agent_registry_rejected{reason="limit"} 0', text)
        self.assertIn(
            'epic_agent_info{agent="claude",kind="claude",label="",layout="registered"} 1',
            text,
        )


# Samples per scrape at the fetch limits; guards against unbounded label growth.
SCRAPE_BUDGET = 10_000


class ScrapeSizeTest(unittest.TestCase):
    def test_max_size_snapshot_and_full_ledgers_stay_under_budget(self) -> None:
        """Fetch limits: 500 project items; 100 open and 300 merged PRs per base
        (two bases). Full ledgers: 2 000 ticks for each of the 12 agents over the
        7-day window, one of them running."""
        body = "\n".join(f"- [ ] **B1.{i}.1** Leaf {i}." for i in range(5))
        items = [
            plan_issue(
                1000 + i,
                f"[B1.{i}] Task {i}",
                "Task",
                body,
                area="Backend",
                executor="Claude" if i % 2 else "Codex",
            )
            for i in range(500)
        ]
        prs = [
            pull_request(
                2000 + i,
                "Claude" if i % 2 else "Codex",
                body=f"Executor: Claude\nLeaf IDs: B1.{i % 500}.1\n"
                f"Issue: {URL}/issues/{1000 + i % 500}",
                baseRefName=("dev/312-interim", "dev/streaming-architecture")[i % 2],
            )
            for i in range(200)
        ]
        merged = [
            {
                "number": 3000 + i,
                "body": f"Executor: Codex\nLeaf IDs: B1.{i % 500}.1\n"
                f"Issue: {URL}/issues/{1000 + i % 500}",
            }
            for i in range(600)
        ]
        github = monitor.github_metrics(
            snapshot(items=items, prs=prs, merged_prs=merged), NOW
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            logs = [root / f"{agent}.log" for agent in LEGACY]
            for i in range(agents.MAX_AGENTS - len(LEGACY)):
                home = agents.register(root, f"claude-{i}", 1.0, label="x" * 40)
                logs.append(home / "claude.log")
            owner = root / "claude.lock/owner.json"
            owner.parent.mkdir()
            owner.write_text('{"pid": 1, "started_at": 599990, "max_age": 1930}')
            for log in logs:
                log.write_text(
                    "".join(
                        "\ntick: started "
                        + time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(i * 300))
                        + ' repo=/x\n{"action": "review", "pr": 7}\n'
                        f"tick: finished exit={(0, 1, 124)[i % 3]}\n"
                        for i in range(2000)
                    )
                )
            ledgers = monitor.Ledgers(root / "runtime")
            runners = monitor.runner_metrics(root, False, 2000 * 300, ledgers=ledgers)
        samples = [
            line for line in (github + runners).splitlines() if not line.startswith("#")
        ]
        self.assertLess(len(samples), SCRAPE_BUDGET)


class GithubMetricsTest(unittest.TestCase):
    def test_review_status_matches_current_head_and_ignores_edited_comments(
        self,
    ) -> None:
        for head, edited, expected in (
            (HEAD, False, "approved"),
            ("b" * 40, False, "waiting"),
            (HEAD, True, "waiting"),
        ):
            with self.subTest(head=head, edited=edited):
                comment = {
                    "body": f"Review: APPROVED by Claude at {HEAD}",
                    "createdAt": "2026-09-28T12:00:00Z",
                    "includesCreatedEdit": edited,
                }
                text = monitor.github_metrics(
                    snapshot(
                        prs=[
                            pull_request(
                                77, "Codex", headRefOid=head, comments=[comment]
                            ),
                        ]
                    ),
                    NOW,
                )
                self.assertIn(f'review="{expected}",target="{URL}/pull/77"', text)

    def test_issue_counts_and_queue_follow_executor(self) -> None:
        text = monitor.github_metrics(
            snapshot(
                items=[
                    issue(401, "Claude", "Ready"),
                    issue(402, "Codex", "In progress"),
                    issue(403, "Codex", "Unexpected"),
                ]
            ),
            NOW,
        )
        self.assertIn('epic_issues{agent="claude",status="Ready"} 1\n', text)
        self.assertIn('epic_issues{agent="codex",status="Unknown"} 1\n', text)
        self.assertIn(
            f'action="claim",agent="claude",reason="Ready leaf assigned to me",target="{URL}/issues/401"',
            text,
        )
        self.assertIn(
            f'action="continue",agent="codex",reason="my In progress leaf has no PR yet",target="{URL}/issues/402"',
            text,
        )

    def test_empty_checks_are_unknown(self) -> None:
        text = monitor.github_metrics(
            snapshot(prs=[pull_request(77, "Codex", statusCheckRollup=[])]), NOW
        )
        self.assertIn('epic_open_prs{agent="codex",checks="unknown"} 1\n', text)
        self.assertIn('epic_open_prs{agent="codex",checks="green"} 0\n', text)
        self.assertIn('agent="codex",checks="unknown",draft="false"', text)

    def test_pr_author_is_body_marker_not_shared_github_account(self) -> None:
        text = monitor.github_metrics(
            snapshot(
                prs=[
                    pull_request(77, "Codex", author={"login": "shared"}),
                    pull_request(78, "Claude", author={"login": "shared"}),
                    pull_request(79, "unknown", author={"login": "shared"}),
                ]
            ),
            NOW,
        )
        self.assertIn('epic_open_prs{agent="codex",checks="green"} 1\n', text)
        self.assertIn('epic_open_prs{agent="claude",checks="green"} 1\n', text)
        self.assertIn("epic_unattributed_prs 1\n", text)

    def test_foreign_issues_and_non_epic_prs_are_excluded(self) -> None:
        foreign = issue(401, "Claude", "Ready")
        foreign["content"]["url"] = "https://github.com/another/repo/issues/401"
        text = monitor.github_metrics(
            snapshot(
                items=[foreign],
                prs=[
                    pull_request(77, "Codex", baseRefName="main"),
                ],
            ),
            NOW,
        )
        self.assertIn('epic_issues{agent="claude",status="Ready"} 0\n', text)
        self.assertIn('epic_open_prs{agent="codex",checks="green"} 0\n', text)
        self.assertNotIn("another/repo", text)
        self.assertNotIn("/pull/77", text)

    def test_checked_leaves_are_distinct_from_merged_prs(self) -> None:
        body = "- [x] **O1.1.1** done\n- [ ] **O1.1.2** pending\n- [x] **O1.1.1** duplicate"
        text = monitor.github_metrics(
            snapshot(
                items=[issue(401, "Codex", "In progress", body)],
                merged_prs=[
                    pull_request(77, "Codex", body="Executor: Codex\nLeaf IDs: O1.1.2")
                ],
            ),
            NOW,
        )
        self.assertIn('epic_checklist_leaves{agent="codex",state="total"} 2\n', text)
        self.assertIn('epic_checklist_leaves{agent="codex",state="checked"} 1\n', text)
        self.assertIn('epic_merged_prs_in_window{agent="codex"} 1\n', text)

    def test_blocked_leaf_and_operator_requests_remain_visible(self) -> None:
        blocked = issue(401, "Codex", "Ready")
        blocked["readiness"] = "Start after B1.1.1."
        escalated = issue(402, "Codex", "In progress")
        escalated["labels"] = ["needs-anton"]
        text = monitor.github_metrics(snapshot(items=[blocked, escalated]), NOW)
        self.assertIn('epic_needs_operator{agent="codex"} 1\n', text)
        self.assertIn(
            f'action="wait",agent="codex",reason="starts after B1.1.1",target="{URL}/issues/401"',
            text,
        )


def plan_issue(
    number: int, title: str, level: str, body: str = "", **fields: str
) -> monitor.Json:
    item = issue(number, fields.pop("executor", "Unassigned"), "Backlog", body)
    item["content"]["title"] = title
    item["level"] = level
    item.update(fields)
    return item


class TaskContextTest(unittest.TestCase):
    """Epic > area > task > subtask > leaf > PR, as the dashboards show it."""

    def setUp(self) -> None:
        subtask_body = (
            f"Parent: {URL}/issues/331\n\n"
            "- [ ] **O1.2.1** Change the workflow. Then more text.\n"
            "- [ ] **O1.2.4** **Wave 0.** v3: CI must run for PRs; and more.\n"
            "- [x] **O1.2.2** Done already.\n"
        )
        self.state = snapshot(
            items=[
                plan_issue(312, "Epic: Continuous playback", "Epic"),
                plan_issue(331, "[O1] Establish the baseline", "Task", area="Ops"),
                plan_issue(
                    381,
                    "[O1.2] Remove the unsafe trigger",
                    "Subtask",
                    subtask_body,
                    area="Ops",
                    executor="Codex",
                ),
            ],
            prs=[
                pull_request(
                    417,
                    "Codex",
                    title="ci: run checks: backend",
                    body=f"Executor: Codex\nLeaf IDs: O1.2.4\nIssue: {URL}/issues/381",
                )
            ],
            merged_prs=[
                {"number": 415, "body": f"Leaf IDs: setup\nIssue: {URL}/issues/312"}
            ],
            batch_order=["| Codex | O1.2.4 (x) first; then O1.2.1 |"],
        )

    def rows(self) -> dict[str, dict[str, str]]:
        return {row["target"]: row for row in monitor.task_contexts(self.state)}

    def test_issue_path_uses_parent_and_batch_ordered_open_leaf(self) -> None:
        row = self.rows()[f"{URL}/issues/381"]
        self.assertEqual(row["epic"], "Continuous playback")
        self.assertEqual(row["area"], "Ops")
        self.assertEqual(row["task"], "O1 · Establish the baseline")
        self.assertEqual(row["task_url"], f"{URL}/issues/331")
        self.assertEqual(row["subtask"], "O1.2 · Remove the unsafe trigger")
        # O1.2.4 comes first in the batch table; markers and later text are cut.
        self.assertEqual(row["leaf"], "O1.2.4 · CI must run for PRs (+1 more)")
        self.assertEqual(row["leaves_done"], "1/3")

    def test_pr_path_uses_issue_and_leaf_lines_and_keeps_full_title(self) -> None:
        row = self.rows()[f"{URL}/pull/417"]
        self.assertEqual(row["subtask_url"], f"{URL}/issues/381")
        self.assertEqual(row["leaf"], "O1.2.4 · CI must run for PRs")
        self.assertEqual(row["pr"], "PR 417 · ci: run checks: backend")

    def test_merged_setup_pr_sits_directly_under_the_epic(self) -> None:
        row = self.rows()[f"{URL}/pull/415"]
        self.assertEqual(row["epic"], "Continuous playback")
        self.assertEqual(row["task"], "")
        self.assertEqual(row["leaf"], "setup (loop and rule files)")

    def test_levels_form_an_indented_linked_tree(self) -> None:
        levels = monitor.task_levels(self.rows()[f"{URL}/pull/417"])
        self.assertEqual(
            [level["level"] for level in levels],
            ["Epic", "Area", "Task", "Subtask", "Leaf", "PR"],
        )
        self.assertEqual([level["depth"] for level in levels], list("123456"))
        self.assertTrue(levels[2]["text"].endswith("└ O1 · Establish the baseline"))
        # The leaf links to the issue holding its checkbox.
        self.assertEqual(levels[4]["url"], f"{URL}/issues/381")
        self.assertEqual(levels[5]["url"], f"{URL}/pull/417")

    def test_github_metrics_publish_context_rows(self) -> None:
        text = monitor.github_metrics(self.state, NOW)
        self.assertIn(f'level="Subtask",target="{URL}/pull/417"', text)
        self.assertIn("epic_task_context_info{", text)


class PublicationTest(unittest.TestCase):
    def test_current_epoch_timestamp_keeps_second_precision(self) -> None:
        metrics = monitor.Metrics()
        metrics.add("local_snapshot_timestamp_seconds", 1_800_000_001.25)
        sample = metrics.render().splitlines()[-1]
        self.assertEqual(float(sample.split()[-1]), 1_800_000_001.25)

    def test_action_links_reject_foreign_or_arbitrary_values(self) -> None:
        for action in (
            {"action": "secret", "issue": "https://example.com/private"},
            {"action": "secret", "pr": True},
            {"action": "secret", "pr": -1},
            {"action": "secret", "issue": f"{URL}/issues/7?secret=value"},
        ):
            with self.subTest(action=action):
                self.assertEqual(
                    monitor.action_labels(action), {"action": "unknown", "target": ""}
                )
        self.assertEqual(
            monitor.action_labels({"action": "claim", "issue": f"{URL}/issues/7"}),
            {"action": "claim", "target": f"{URL}/issues/7"},
        )

    def test_prometheus_labels_escape_newlines_quotes_and_backslashes(self) -> None:
        metrics = monitor.Metrics()
        metrics.add("example", 1, title='a\\b\n"c"')
        self.assertIn('epic_example{title="a\\\\b\\n\\"c\\""} 1\n', metrics.render())
        self.assertEqual(len(metrics.render().splitlines()), 3)
        for value in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                metrics.add("invalid", value)

    def test_atomic_write_publishes_complete_file_and_cleans_temporary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metrics.prom"
            path.write_text("old\n")
            replace = os.replace

            def inspect_replace(source: Path, destination: Path) -> None:
                self.assertEqual(path.read_text(), "old\n")
                self.assertEqual(Path(source).read_text(), "new\n")
                replace(source, destination)

            with patch("monitor.os.replace", side_effect=inspect_replace):
                monitor.atomic_write(path, "new\n")
            self.assertEqual(path.read_text(), "new\n")
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_atomic_write_failure_preserves_previous_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metrics.prom"
            path.write_text("old\n")
            with patch("monitor.os.replace", side_effect=OSError("disk failure")):
                with self.assertRaises(OSError):
                    monitor.atomic_write(path, "new\n")
            self.assertEqual(path.read_text(), "old\n")
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_github_failure_preserves_snapshot_and_publishes_failed_health(
        self,
    ) -> None:
        def fail(_: float) -> monitor.Json:
            raise RuntimeError("private response")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertTrue(monitor.collect_github(root, 1, fetch=lambda _: snapshot()))
            previous = (root / "github.prom").read_text()
            with self.assertLogs(level="ERROR") as logs:
                self.assertFalse(monitor.collect_github(root, 1, fetch=fail))
            self.assertEqual((root / "github.prom").read_text(), previous)
            health = (root / "github-health.prom").read_text()
            self.assertIn("epic_github_collection_success 0\n", health)
            self.assertNotIn("private response", health + "".join(logs.output))


if __name__ == "__main__":
    unittest.main()
