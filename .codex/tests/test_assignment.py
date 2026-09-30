"""Fresh assignment evidence uses complete REST board pages in either mode."""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "epic"))
import isolated_env  # noqa: E402, F401

import importlib.util
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import github_state as gs
import next_action as na
from test_github_state import FakeGitHub
from test_project_items import FIELD_IDS, rest_row

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("assignment", ROOT / "assignment.py")
assert SPEC and SPEC.loader
assignment = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(assignment)

FIELDS = f"{na.PROJECT_API}/fields?per_page=100"
ITEMS = f"{na.PROJECT_API}/items?per_page=100&" + "&".join(
    f"fields[]={FIELD_IDS[name]}" for name in na.PROJECT_FIELDS
)
ISSUE = na.issue_url(532)


class AssignmentTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="assignment-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cache = self.root / "cache"
        self.env = patch.dict(
            os.environ,
            {
                "EPIC_SHARED_READER": "0",
                "EPIC_CACHE_DIR": str(self.cache),
                "GH_HOST": "",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        quota_state = patch.object(
            assignment.github_quota, "STATE_DIR", self.root / "quota"
        )
        quota_state.start()
        self.addCleanup(quota_state.stop)
        gs._LOGINS.clear()
        self.addCleanup(gs._LOGINS.clear)
        self.gh = FakeGitHub()
        self.gh.set(
            FIELDS,
            [{"id": FIELD_IDS[name], "name": name} for name in na.PROJECT_FIELDS],
        )
        self.row = rest_row(532, status="Ready", executor="Codex")
        self.gh.set(ITEMS, [self.row])
        self.action = {"action": "claim", "issue": ISSUE}

    def read(self, mode: str, action: dict | None = None) -> dict:
        if mode == "1":
            (self.cache / "github-cache").mkdir(parents=True, exist_ok=True)
            (self.cache / "github-cache" / "auth-context").write_text(
                "assignment-test\n"
            )
        with (
            patch.dict(os.environ, {"EPIC_SHARED_READER": mode}),
            patch.object(gs, "gh_http", self.gh),
        ):
            return assignment.evidence(action or self.action)

    def test_rest_assignment_ignores_empty_graphql_project_items(self) -> None:
        self.gh.set(f"repos/{na.REPO}/issues/532", {"projectItems": {"nodes": []}})
        for mode in ("0", "1"):
            with self.subTest(mode=mode):
                result = self.read(mode)
                self.assertEqual(
                    (result["result"], result["eligible"]), ("confirmed", True)
                )
                self.assertEqual(
                    (result["status"], result["executor"], result["item_id"]),
                    ("Ready", "Codex", "PVTI_532"),
                )
                self.assertEqual(result["project_url"], assignment.PROJECT_URL)
                self.assertEqual(result["source"], "REST")
        self.assertTrue(all("graphql" not in url for url, _ in self.gh.calls))

    def test_off_mode_needs_no_auth_context_or_snapshot_settings(self) -> None:
        with patch.dict(
            os.environ,
            {
                "EPIC_RECHECK_TIMEOUT_SECONDS": "bad",
                "EPIC_SNAPSHOT_MAX_AGE_SECONDS": "bad",
            },
        ):
            self.assertTrue(self.read("0")["eligible"])
        self.assertFalse(self.cache.exists())
        self.assertEqual(self.gh.count("user"), 0)

    def test_assignment_on_second_page_is_found_in_both_modes(self) -> None:
        other = rest_row(531, status="Ready", executor="Claude")
        self.gh.set_pages(ITEMS, [[other], [self.row]])
        for mode in ("0", "1"):
            with self.subTest(mode=mode):
                self.assertTrue(self.read(mode)["eligible"])

    def test_changed_status_executor_and_continue_are_fresh(self) -> None:
        for mode in ("0", "1"):
            for kind, status, executor, eligible in (
                ("claim", "In progress", "Codex", False),
                ("claim", "Ready", "Claude", False),
                ("continue", "In progress", "Codex", True),
                ("continue", "Ready", "Codex", False),
                ("claim", None, None, False),
            ):
                with self.subTest(
                    mode=mode, kind=kind, status=status, executor=executor
                ):
                    self.gh.set(
                        ITEMS, [rest_row(532, status=status, executor=executor)]
                    )
                    result = self.read(mode, {**self.action, "action": kind})
                    self.assertEqual(
                        (result["result"], result["eligible"]), ("confirmed", eligible)
                    )

    def test_absence_needs_a_complete_board_and_exact_repository(self) -> None:
        foreign = rest_row(532, status="Ready", executor="Codex")
        foreign["content"]["html_url"] = "https://github.com/other/repo/issues/532"
        for rows in ([], [foreign]):
            self.gh.set(ITEMS, rows)
            for mode in ("0", "1"):
                with self.subTest(mode=mode, rows=rows):
                    result = self.read(mode)
                    self.assertEqual(
                        (result["result"], result["eligible"]), ("absent", False)
                    )

    def test_failed_partial_and_malformed_reads_are_unknown(self) -> None:
        for mode in ("0", "1"):
            for error in ("403-forbidden", "404", "502", "403-rate-limit"):
                with self.subTest(mode=mode, error=error):
                    self.gh.fail(ITEMS, error)
                    self.assertEqual(self.read(mode)["result"], "unknown")
            self.gh.set(ITEMS, {"items": []})
            self.assertEqual(self.read(mode)["result"], "unknown")
            pages = self.gh.set_pages(ITEMS, [[self.row], []])
            self.gh.fail(pages[1], "404")
            self.assertEqual(self.read(mode)["result"], "unknown")
            self.gh.set(ITEMS, [self.row])

    def test_missing_fields_and_malformed_selects_are_unknown(self) -> None:
        for mode in ("0", "1"):
            for value in ({"name": {"raw": 123}}, {"name": "Codex"}, "Codex"):
                row = rest_row(532, status="Ready", executor="Codex")
                row["fields"][2]["value"] = value
                self.gh.set(ITEMS, [row])
                with self.subTest(mode=mode, value=value):
                    self.assertEqual(self.read(mode)["result"], "unknown")
            row = rest_row(532, status="Ready", executor="Codex")
            row["fields"] = []
            self.gh.set(ITEMS, [row])
            self.assertEqual(self.read(mode)["result"], "unknown")

    def test_foreign_or_malformed_pagination_is_unknown(self) -> None:
        for link in (
            '<https://api.github.com/users/anneoneone/projectsV2/3/items?page=2>; rel="next"',
            '<https://foreign.test/items?page=2>; rel="next"',
            'invalid; rel="next"',
            f'<{gs.full_url(ITEMS)}&page=2>; rel="next last"',
            f'<{gs.full_url(ITEMS)}&page=2>; rel="next", <{gs.full_url(ITEMS)}&page=3>; rel="next"',
            '<https://api.github.com/users/anneoneone/projectsV2/2/items?page=2>; rel="next"',
        ):
            self.gh.set(ITEMS, [self.row], link)
            for mode in ("0", "1"):
                with self.subTest(mode=mode, link=link):
                    self.assertEqual(self.read(mode)["result"], "unknown")

    def cursor_pages(self) -> tuple[str, str]:
        """The REST Link shape observed on the live project, including cursors."""
        self.gh.set(
            f"users/{na.PROJECT_OWNER}", {"id": 49155143, "login": na.PROJECT_OWNER}
        )
        root = f"https://api.github.com/user/49155143/projectsV2/{na.PROJECT_NUMBER}"
        fields = [{"id": FIELD_IDS[name], "name": name} for name in na.PROJECT_FIELDS]
        field_second = f"{root}/fields?per_page=100&after=Y3Vyc29yOjE%3D"
        self.gh.set(FIELDS, fields[:2], f'<{field_second}>; rel="next"')
        self.gh.set(field_second, fields[2:])
        query = ITEMS.split("?", 1)[1].replace("[]", "%5B%5D")
        second = f"{root}/items?{query}&after=Y3Vyc29yOjE%3D"
        third = f"{root}/items?{query}&after=Y3Vyc29yOjI%3D"
        self.gh.set(ITEMS, [], f'<{second}>; rel="next"')
        self.gh.set(second, [], f'<{third}>; rel="next"')
        self.gh.set(third, [self.row])
        return second, third

    def test_verified_numeric_owner_cursor_pages_find_assignment_in_both_modes(
        self,
    ) -> None:
        for mode in ("0", "1"):
            with self.subTest(mode=mode):
                self.cursor_pages()
                before = self.gh.count(f"users/{na.PROJECT_OWNER}")
                self.assertTrue(self.read(mode)["eligible"])
                self.assertEqual(self.gh.count(f"users/{na.PROJECT_OWNER}") - before, 1)

    def test_numeric_pages_cannot_change_owner_project_resource_or_fields(self) -> None:
        for mode in ("0", "1"):
            for change in (
                lambda url: url.replace("/user/49155143/", "/user/99/"),
                lambda url: url.replace("/projectsV2/2/", "/projectsV2/3/"),
                lambda url: url.replace("/items?", "/fields?"),
                lambda url: url.replace("fields%5B%5D=11", "fields%5B%5D=999"),
            ):
                with self.subTest(mode=mode, change=change):
                    second, third = self.cursor_pages()
                    foreign = change(third)
                    self.gh.set(second, [], f'<{foreign}>; rel="next"')
                    self.gh.set(foreign, [])
                    self.assertEqual(self.read(mode)["result"], "unknown")
                    self.assertEqual(self.gh.count(foreign), 0)

    def test_numeric_owner_alias_requires_valid_owner_response(self) -> None:
        for mode in ("0", "1"):
            for owner in (
                {"id": 99, "login": na.PROJECT_OWNER},
                {"id": 49155143, "login": "other"},
                {"id": "49155143", "login": na.PROJECT_OWNER},
            ):
                with self.subTest(mode=mode, owner=owner):
                    self.cursor_pages()
                    self.gh.set(f"users/{na.PROJECT_OWNER}", owner)
                    self.assertEqual(self.read(mode)["result"], "unknown")

    def test_quota_is_unknown_with_a_distinct_reason(self) -> None:
        state = assignment.github_quota.STATE_DIR
        state.mkdir()
        (state / assignment.github_quota.WAIT_FILE).write_text(
            json.dumps({"retry_at": "2099-01-01T00:00:00Z"})
        )
        for mode in ("0", "1"):
            result = self.read(mode)
            self.assertEqual(result["result"], "unknown")
            self.assertEqual(result["reason_code"], "github_rate_limit")
        self.assertEqual(self.gh.calls, [])

    def test_malformed_quota_wait_is_unknown_without_network(self) -> None:
        state = assignment.github_quota.STATE_DIR
        state.mkdir()
        (state / assignment.github_quota.WAIT_FILE).write_text("bad JSON")
        for mode in ("0", "1"):
            self.assertEqual(self.read(mode)["result"], "unknown")
        self.assertEqual(self.gh.calls, [])

    def test_wrong_project_field_ids_and_duplicate_definitions_are_unknown(
        self,
    ) -> None:
        for mode in ("0", "1"):
            for mutate in (
                lambda row: row.update(
                    project_url="https://github.com/users/anneoneone/projects/3"
                ),
                lambda row: row["fields"][0].update(id=999),
            ):
                row = rest_row(532, status="Ready", executor="Codex")
                mutate(row)
                self.gh.set(ITEMS, [row])
                self.assertEqual(self.read(mode)["result"], "unknown")
        fields = [{"id": FIELD_IDS[name], "name": name} for name in na.PROJECT_FIELDS]
        for invalid in ({"id": 99, "name": "Status"}, {"id": "bad", "name": "Other"}):
            self.gh.set(FIELDS, [*fields, invalid])
            for mode in ("0", "1"):
                self.assertEqual(self.read(mode)["result"], "unknown")

    def test_other_actions_do_not_read_github(self) -> None:
        for action in (
            {"action": "review", "pr": 5},
            {"action": "continue", "pr": 5},
            {"action": "idle"},
        ):
            with patch.object(
                assignment, "make_reader", side_effect=AssertionError("unexpected read")
            ):
                result = assignment.evidence(action)
            self.assertEqual(
                (result["result"], result["eligible"]), ("not_applicable", True)
            )

    def test_cli_writes_evidence_and_distinguishes_outcomes(self) -> None:
        action_file = self.root / "action.json"
        output = self.root / "assignment.json"
        action_file.write_text(json.dumps(self.action))
        for status, rows, error, expected in (
            ("Ready", [self.row], None, 0),
            ("Ready", [], None, 6),
            ("Done", [rest_row(532, status="Done", executor="Codex")], None, 6),
            ("Ready", [self.row], "404", 5),
            ("Ready", [self.row], "403-rate-limit", 5),
        ):
            with self.subTest(status=status, error=error):
                self.gh.set(ITEMS, rows)
                if error:
                    self.gh.fail(ITEMS, error)
                with (
                    patch.object(gs, "gh_http", self.gh),
                    patch.object(sys, "stderr", io.StringIO()) as stderr,
                ):
                    code = assignment.main(
                        ["--action-file", str(action_file), "--output", str(output)]
                    )
                self.assertEqual(code, expected)
                self.assertIn("result", json.loads(output.read_text()))
                if expected:
                    self.assertIn("assignment:", stderr.getvalue())
        with (
            patch.object(assignment.github_quota, "check", return_value=(3, "later")),
            patch.object(gs, "gh_http", self.gh),
            patch.object(sys, "stderr", io.StringIO()),
        ):
            self.assertEqual(
                assignment.main(
                    ["--action-file", str(action_file), "--output", str(output)]
                ),
                4,
            )


if __name__ == "__main__":
    unittest.main()
