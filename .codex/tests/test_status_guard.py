"""Status helper enforcement through the Codex hook in both reader modes."""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/epic"))
import isolated_env  # noqa: E402, F401

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
    "codex_status_guard", ROOT / ".codex/hooks/scripts/epic_guard.py"
)
assert spec is not None and spec.loader is not None
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class StatusGuardTests(fixtures.Env):
    """Use the actual checker and REST reader; only HTTP is fake."""

    def setUp(self) -> None:
        super().setUp()
        self.repo = fixtures.GitHubRepo(self.gh)
        self.action_file = self.root / "action.json"
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "EPIC_ACTION_FILE": str(self.action_file),
                    "EPIC_TRUSTED_ROOT": str(ROOT),
                    "EPIC_FOCUS_ACTIONS": "",
                },
            )
        )
        self.enterContext(patch.object(fixtures.gs, "gh_http", self.gh))
        self.items_url = f"{fixtures.na.PROJECT_API}/items?per_page=100&" + "&".join(
            f"fields[]={FIELD_IDS[name]}" for name in fixtures.na.PROJECT_FIELDS
        )
        self.action()
        self.item()

    def action(self, kind: str = "continue", *, pr: bool = True) -> None:
        for pulls in self.repo.open.values():
            pulls.clear()
        if pr:
            self.repo.add_pr(
                fixtures.pull(
                    5,
                    fixtures.A,
                    f"Executor: Codex\nIssue: {fixtures.ISSUES}/21\n",
                    draft=True,
                )
            )
        target = (
            {"pr": 5, "sha": fixtures.A} if pr else {"issue": f"{fixtures.ISSUES}/21"}
        )
        self.action_file.write_text(json.dumps({"action": kind, **target}))

    def item(self, status: str = "In progress", executor: str = "Codex") -> None:
        self.repo.items.clear()
        self.repo.add_item(21, status, executor)
        self.gh.set(
            f"{fixtures.na.PROJECT_API}/fields?per_page=100",
            [
                {"id": FIELD_IDS[name], "name": name}
                for name in fixtures.na.PROJECT_FIELDS
            ],
        )
        self.gh.set(self.items_url, [rest_row(21, status=status, executor=executor)])

    def helper(self, args: str) -> str:
        return f"python3 '{write_checks.STATUS_HELPER}' {args}"

    def check(self, command: str, shared: str, tool: str = "exec_command") -> None:
        key = "command" if tool == "Bash" else "cmd"
        with patch.dict(os.environ, {"EPIC_SHARED_READER": shared}):
            guard.runner_write_check(tool, {key: command}, self.root)
            self.assertEqual(os.environ["EPIC_SHARED_READER"], shared)

    def test_raw_writes_block_before_reads_for_issue_and_pr_ticks(self) -> None:
        labels = f"{fixtures.REPO}/issues/21/labels"
        commands = (
            "gh project item-edit --id PVTI_21 --field-id F --single-select-option-id O",
            "gh api --method PATCH users/anneoneone/projectsV2/2/items/50021 --input -",
            "gh api graphql -f 'query=mutation { updateProjectV2ItemFieldValue(input: {}) { clientMutationId } }'",
            "gh issue edit 21 --add-label status::done",
            "gh issue edit 21 --remove-label bug,status::ready",
            f"gh api {labels} -f 'labels[]=status::done'",
            f"gh api --method DELETE {labels}/status%3A%3Aready",
            f"gh api --method PUT {labels} -f 'labels[]=bug'",
            f"gh api --method DELETE {labels}",
            f"gh api --method PATCH {fixtures.REPO}/issues/21 -f 'labels[]=bug'",
        )
        for shared in ("0", "1"):
            for pr in (False, True):
                self.action(pr=pr)
                for tool in ("exec_command", "shell_command", "Bash"):
                    for command in commands:
                        with self.subTest(
                            shared=shared, pr=pr, tool=tool, command=command
                        ):
                            with self.assertRaisesRegex(
                                ValueError, "no raw.*(helper|set_status)"
                            ):
                                self.check(command, shared, tool)
        self.assertEqual(self.gh.calls, [])

    def test_claim_and_continue_set_recheck_assignment(self) -> None:
        for shared in ("0", "1"):
            for kind, pr, status in (
                ("claim", False, "Ready"),
                ("continue", False, "In progress"),
                ("continue", True, "In progress"),
            ):
                with self.subTest(shared=shared, kind=kind, pr=pr):
                    self.action(kind, pr=pr)
                    self.item(status)
                    self.check(self.helper("set 21 'In progress'"), shared)
                    for args in ("set 22 Done", "repair 22"):
                        with self.assertRaisesRegex(
                            ValueError, "not this tick's issue"
                        ):
                            self.check(self.helper(args), shared)
                    self.item(status, "Claude")
                    with self.assertRaisesRegex(
                        ValueError, "selector now gives|not In progress"
                    ):
                        self.check(self.helper("set 21 Done"), shared)
                    self.item("Backlog")
                    with self.assertRaisesRegex(
                        ValueError, "selector now gives|not In progress"
                    ):
                        self.check(self.helper("set 21 Done"), shared)

    def test_repair_own_issue_in_any_status(self) -> None:
        for shared in ("0", "1"):
            for kind, pr in (("continue", False), ("continue", True), ("fix", True)):
                self.action(kind, pr=pr)
                for status in ("Ready", "In progress", "In review", "Done"):
                    with self.subTest(shared=shared, kind=kind, pr=pr, status=status):
                        self.item(status)
                        self.check(self.helper("repair 21"), shared)
                        self.item(status, "Claude")
                        with self.assertRaisesRegex(ValueError, "Executor is Claude"):
                            self.check(self.helper("repair 21"), shared)

    def test_missing_assignment_and_failed_reads_refuse_helper(self) -> None:
        for shared in ("0", "1"):
            for args in ("set 21 Done", "repair 21"):
                with self.subTest(shared=shared, args=args):
                    self.gh.set(self.items_url, [])
                    with self.assertRaisesRegex(ValueError, "not on the board"):
                        self.check(self.helper(args), shared)
                    self.gh.fail(self.items_url, "502")
                    with self.assertRaisesRegex(ValueError, "502"):
                        self.check(self.helper(args), shared)

    def test_sync_malformed_and_untrusted_helpers_refused_without_reads(self) -> None:
        for shared in ("0", "1"):
            for args in (
                "sync 21",
                "set 21",
                "set 21 Finished",
                "repair 21 22",
                "move 21",
            ):
                with self.subTest(shared=shared, args=args):
                    with self.assertRaisesRegex(ValueError, "backfill|malformed"):
                        self.check(self.helper(args), shared)
            with self.assertRaisesRegex(ValueError, "trusted status helper"):
                self.check("python3 scripts/epic/set_status.py repair 21", shared)
            self.action("fix")
            with self.assertRaisesRegex(ValueError, "does not change the board"):
                self.check(self.helper("set 21 Done"), shared)
            self.action()
        self.assertEqual(self.gh.calls, [])

    def test_reader_off_preserves_unrelated_commands(self) -> None:
        for command in (
            "gh issue edit 21 --add-label bug",
            "git push --all",
            "echo $(gh pr comment 5 --body hi)",
        ):
            self.check(command, "0")
        self.assertEqual(self.gh.calls, [])

    def test_unclear_commands_cannot_hide_status_writes(self) -> None:
        writes = (
            "gh issue edit 21 --add-label status::done",
            self.helper("set 22 Done"),
            self.helper("repair 22"),
        )
        for shared in ("0", "1"):
            for write in writes:
                for separator in ("; ", "\n", " && ", " || ", " & ", " | "):
                    for commands in (
                        ("git push --all", write),
                        (write, "git push --all"),
                    ):
                        command = separator.join(commands)
                        with self.subTest(shared=shared, command=command):
                            with self.assertRaises(ValueError):
                                self.check(command, shared)
                for command in (
                    f"git push --all > >({write})",
                    f"git push --all < <({write})",
                ):
                    with self.subTest(shared=shared, command=command):
                        with self.assertRaises(ValueError):
                            self.check(command, shared)
        self.assertEqual(self.gh.calls, [])

    def test_unclear_label_values_are_refused_without_reads(self) -> None:
        for shared in ("0", "1"):
            for flag in ("--add-label", "--remove-label"):
                command = f'gh issue edit 21 {flag} "$(cat /tmp/label)"'
                with self.subTest(shared=shared, flag=flag):
                    with self.assertRaises(ValueError):
                        self.check(command, shared)
        self.assertEqual(self.gh.calls, [])

    def test_mcp_label_replacement_refused_in_both_modes(self) -> None:
        for shared in ("0", "1"):
            with patch.dict(os.environ, {"EPIC_SHARED_READER": shared}):
                with self.assertRaisesRegex(ValueError, "no raw status::"):
                    guard.runner_write_check(
                        "mcp__github__update_issue",
                        {
                            "issue_number": 21,
                            "labels": ["bug"],
                        },
                        self.root,
                    )
        self.assertEqual(self.gh.calls, [])


if __name__ == "__main__":
    unittest.main()
