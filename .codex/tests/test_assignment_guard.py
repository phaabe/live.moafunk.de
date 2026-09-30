"""Selected issue writes recheck assignment with either reader switch value."""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/epic"))
import isolated_env  # noqa: E402, F401 (before production modules or fixtures)

import importlib
import importlib.util
import json
import os
import unittest
from unittest.mock import patch

import test_github_state as fixtures
import write_checks
from test_project_items import FIELD_IDS, rest_row

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "codex_assignment_guard", ROOT / ".codex/hooks/scripts/epic_guard.py"
)
assert spec is not None and spec.loader is not None
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class AssignmentGuardTests(fixtures.Env):
    def setUp(self) -> None:
        super().setUp()
        self.repo = fixtures.GitHubRepo(self.gh)
        self.action_file = self.root / "action.json"
        env = patch.dict(
            os.environ,
            {
                "EPIC_ACTION_FILE": str(self.action_file),
                "EPIC_TRUSTED_ROOT": str(ROOT),
                "EPIC_FOCUS_ACTIONS": "",
            },
        )
        env.start()
        self.addCleanup(env.stop)
        sys.path.insert(0, str(ROOT / ".codex"))
        self.addCleanup(sys.path.remove, str(ROOT / ".codex"))
        self.assignment = importlib.import_module("assignment")
        reader = patch.object(
            self.assignment,
            "make_reader",
            side_effect=lambda: fixtures.FakeReader(self.gh),
        )
        self.make_reader = reader.start()
        self.addCleanup(reader.stop)
        self.action()

    def action(self, kind: str = "claim", **fields: object) -> None:
        self.action_file.write_text(
            json.dumps(
                {
                    "action": kind,
                    "reason": "selected earlier",
                    "issue": f"{fixtures.ISSUES}/21",
                    **fields,
                }
            )
        )

    def item(self, status: str, executor: str = "Codex", **fields: str) -> None:
        self.repo.items.clear()
        self.repo.add_item(21, status, executor, **fields)

    def check(self, command: str, shared: str, tool: str = "exec_command") -> None:
        with patch.dict(os.environ, {"EPIC_SHARED_READER": shared}):
            guard.runner_write_check(tool, {"cmd": command}, self.root)
            self.assertEqual(os.environ["EPIC_SHARED_READER"], shared)

    def test_changed_assignment_blocks_claim_comment_and_board_write(self) -> None:
        for shared in ("0", "1"):
            for status, executor in (("Ready", "Claude"), ("Backlog", "Codex")):
                for command in (
                    "gh issue comment 21 --body 'Claim by Codex'",
                    "gh project item-edit --id PVTI_21 --field-id F "
                    "--single-select-option-id O",
                ):
                    with self.subTest(shared=shared, status=status, command=command):
                        self.item(status, executor)
                        with self.assertRaisesRegex(ValueError, "selector now gives"):
                            self.check(command, shared)

    def test_stale_evidence_does_not_override_fresh_status(self) -> None:
        self.action(assignment={"executor": "Codex", "status": "Ready"})
        self.item("Backlog")
        for shared in ("0", "1"):
            with self.subTest(shared=shared):
                with self.assertRaisesRegex(ValueError, "selector now gives"):
                    self.check("gh issue comment 21 --body claim", shared)

    def test_ready_then_owned_transition(self) -> None:
        for shared in ("0", "1"):
            with self.subTest(shared=shared):
                self.item("Ready")
                self.check("gh issue comment 21 --body claim", shared)
                with self.assertRaisesRegex(ValueError, "not In progress"):
                    self.check("gh pr create --draft -t title -b body", shared)
                self.item("In progress")
                self.check("gh issue comment 21 --body working", shared)
                self.check("gh pr create --draft -t title -b body", shared)
        self.assertTrue(self.gh.calls)
        self.assertTrue(all(etag is None for _, etag in self.gh.calls))

    def test_continue_requires_current_ownership(self) -> None:
        self.action("continue")
        for shared in ("0", "1"):
            for status, executor in (("Ready", "Codex"), ("In progress", "Claude")):
                with self.subTest(shared=shared, status=status, executor=executor):
                    self.item(status, executor)
                    with self.assertRaisesRegex(
                        ValueError, "not In progress for Codex"
                    ):
                        self.check("gh issue comment 21 --body working", shared)

    def test_failed_read_blocks_without_using_selected_evidence(self) -> None:
        self.item("Ready")
        for shared in ("0", "1"):
            with self.subTest(shared=shared):
                self.gh.fail(f"{fixtures.na.PROJECT_API}/fields?per_page=100", "502")
                with self.assertRaisesRegex(ValueError, "read|502"):
                    self.check("gh issue comment 21 --body claim", shared)

    def test_readiness_and_slot_are_rechecked(self) -> None:
        self.item("Ready", readiness="Ready. Start after A9.9.9.")
        for shared in ("0", "1"):
            with self.subTest(shared=shared):
                with self.assertRaisesRegex(ValueError, "selector now gives"):
                    self.check("gh issue comment 21 --body claim", shared)
        self.item("Ready")
        self.repo.add_pr(fixtures.pull(5, fixtures.A, "Executor: Codex"))
        self.repo.add_pr(fixtures.pull(6, fixtures.A, "Executor: Codex"))
        for shared in ("0", "1"):
            with self.subTest(shared=shared):
                with self.assertRaisesRegex(ValueError, "selector now gives"):
                    self.check("gh issue comment 21 --body claim", shared)

    def test_off_mode_other_actions_and_independent_sessions_stay_inactive(
        self,
    ) -> None:
        self.action("review", issue=None, pr=5)
        self.check("git push origin feat/5-x", "0")
        with patch.dict(os.environ, {"EPIC_ACTION_FILE": ""}):
            self.check("git push origin feat/5-x", "1")
        self.make_reader.assert_not_called()

    def test_shared_nonissue_action_retains_existing_guard(self) -> None:
        self.action("review", issue=None, pr=5)
        with patch.object(write_checks, "guard", return_value="head moved"):
            with self.assertRaisesRegex(ValueError, "head moved"):
                self.check("gh pr comment 5 --body reviewed", "1")
        self.make_reader.assert_not_called()

    def test_assignment_module_must_come_from_trusted_checkout(self) -> None:
        with patch.object(
            self.assignment, "__file__", str(self.root / "assignment.py")
        ):
            for shared in ("0", "1"):
                with self.subTest(shared=shared):
                    with self.assertRaisesRegex(ValueError, "trusted checkout"):
                        self.check("gh issue comment 21 --body claim", shared)


class AssignmentClientIntegrationTests(fixtures.Env):
    """Only HTTP is fake: the hook, reader and write checker run together."""

    def test_real_reader_rechecks_assignment_before_claim_writes_in_both_modes(
        self,
    ) -> None:
        repo = fixtures.GitHubRepo(self.gh)
        repo.add_item(21, "Ready", "Codex")
        fields = f"{fixtures.na.PROJECT_API}/fields?per_page=100"
        items = f"{fixtures.na.PROJECT_API}/items?per_page=100&" + "&".join(
            f"fields[]={FIELD_IDS[name]}" for name in fixtures.na.PROJECT_FIELDS
        )
        self.gh.set(
            fields,
            [
                {"id": FIELD_IDS[name], "name": name}
                for name in fixtures.na.PROJECT_FIELDS
            ],
        )
        action_file = self.root / "action.json"
        action_file.write_text(
            json.dumps({"action": "claim", "issue": f"{fixtures.ISSUES}/21"})
        )
        with (
            patch.dict(
                os.environ,
                {
                    "EPIC_ACTION_FILE": str(action_file),
                    "EPIC_TRUSTED_ROOT": str(ROOT),
                    "EPIC_FOCUS_ACTIONS": "",
                },
            ),
            patch.object(fixtures.gs, "gh_http", self.gh),
        ):
            for shared in ("0", "1"):
                with patch.dict(os.environ, {"EPIC_SHARED_READER": shared}):
                    for command in (
                        "gh issue comment 21 --body claim",
                        "gh project item-edit --id PVTI_21 --field-id F --single-select-option-id O",
                    ):
                        with self.subTest(shared=shared, command=command):
                            self.gh.set(
                                items, [rest_row(21, status="Ready", executor="Codex")]
                            )
                            guard.runner_write_check(
                                "exec_command", {"cmd": command}, self.root
                            )
                            self.gh.set(
                                items, [rest_row(21, status="Ready", executor="Claude")]
                            )
                            with self.assertRaisesRegex(
                                ValueError, "selector now gives"
                            ):
                                guard.runner_write_check(
                                    "exec_command", {"cmd": command}, self.root
                                )
                            self.gh.fail(items, "502")
                            with self.assertRaisesRegex(ValueError, "502"):
                                guard.runner_write_check(
                                    "exec_command", {"cmd": command}, self.root
                                )
                            duplicate = rest_row(21, status="Ready", executor="Claude")
                            duplicate.update(id=9999, node_id="PVTI_duplicate")
                            self.gh.set(
                                items,
                                [
                                    rest_row(21, status="Ready", executor="Codex"),
                                    duplicate,
                                ],
                            )
                            with self.assertRaisesRegex(ValueError, "duplicate issue"):
                                guard.runner_write_check(
                                    "exec_command", {"cmd": command}, self.root
                                )


if __name__ == "__main__":
    unittest.main()
