"""Collector regression tests. Run: python3 -m unittest discover -s scripts/epic."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import monitor


NOW = 2_000.0
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
        for agent in monitor.AGENTS:
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
