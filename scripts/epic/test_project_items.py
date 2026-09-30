"""Tests for reading the project board via REST (next_action.project_items).

Run: python3 -m unittest discover -s scripts/epic
"""

from __future__ import annotations

import isolated_env  # noqa: F401  (first: hides live runner state)

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import monitor
import next_action
from github_quota import WAIT_FILE
from next_action import decide, item_from_rest
from test_monitor import TaskContextTest
from test_next_action import item

HERE = Path(__file__).resolve().parent
WEB = "https://github.com/phaabe/live.moafunk.de/issues"
API = "https://api.github.com/repos/phaabe/live.moafunk.de/issues"
FIELD_IDS = {
    "Title": 1,
    "Status": 11,
    "Labels": 12,
    "Area": 13,
    "Wave": 14,
    "Executor": 15,
    "Level": 16,
}


def select(name: str | None) -> dict | None:
    if name is None:
        return None
    return {
        "color": "GRAY",
        "description": {"html": "", "raw": ""},
        "id": "x",
        "name": {"html": name, "raw": name},
    }


def rest_row(
    number: int,
    *,
    status: str | None = None,
    wave: str | None = None,
    executor: str | None = None,
    area: str | None = None,
    level: str | None = None,
    labels: list[str] | None = None,
    title: str = "",
    body: str | None = "",
) -> dict:
    """A Projects REST item in the shape GitHub returns (checked 2026-09-29)."""
    return {
        "id": 1000 + number,
        "node_id": f"PVTI_{number}",
        "content_type": "Issue",
        "content": {
            "number": number,
            "title": title,
            "body": body,
            "url": f"{API}/{number}",
            "html_url": f"{WEB}/{number}",
        },
        "fields": [
            {
                "id": 11,
                "name": "Status",
                "data_type": "single_select",
                "value": select(status),
            },
            {
                "id": 14,
                "name": "Wave",
                "data_type": "single_select",
                "value": select(wave),
            },
            {
                "id": 15,
                "name": "Executor",
                "data_type": "single_select",
                "value": select(executor),
            },
            {
                "id": 13,
                "name": "Area",
                "data_type": "single_select",
                "value": select(area),
            },
            {
                "id": 16,
                "name": "Level",
                "data_type": "single_select",
                "value": select(level),
            },
            {
                "id": 12,
                "name": "Labels",
                "data_type": "labels",
                "value": [
                    {"id": i, "name": n, "color": "x"}
                    for i, n in enumerate(labels or [])
                ],
            },
        ],
    }


class ItemFromRestTest(unittest.TestCase):
    def test_recorded_item(self) -> None:
        # Trimmed from GET /users/anneoneone/projectsV2/2/items (issue 313).
        row = rest_row(
            313,
            status="Backlog",
            wave="0",
            executor="Unassigned",
            area="Coordination",
            level="Task",
            labels=["project::Infrastructure", "type::ci"],
            title="[P1] Capture the implementation baseline",
            body="Parent: https://github.com/phaabe/live.moafunk.de/issues/312",
        )
        self.assertEqual(
            item_from_rest(row),
            {
                "id": "PVTI_313",
                "title": "[P1] Capture the implementation baseline",
                "content": {
                    "type": "Issue",
                    "number": 313,
                    "title": "[P1] Capture the implementation baseline",
                    "body": "Parent: https://github.com/phaabe/live.moafunk.de/issues/312",
                    "url": f"{WEB}/313",
                },
                "status": "Backlog",
                "wave": "0",
                "executor": "Unassigned",
                "area": "Coordination",
                "level": "Task",
                "labels": ["project::Infrastructure", "type::ci"],
            },
        )

    def test_unset_values_and_text_wave(self) -> None:
        mapped = item_from_rest(rest_row(5, wave="Mixed", body=None))
        self.assertEqual(mapped["wave"], "Mixed")
        for key in ("status", "executor", "area", "level"):
            self.assertIsNone(mapped[key])
        self.assertEqual(mapped["labels"], [])
        self.assertIsNone(mapped["content"]["body"])

    def test_wave_zero_stays_a_string(self) -> None:
        self.assertEqual(item_from_rest(rest_row(5, wave="0"))["wave"], "0")

    def test_missing_labels_field(self) -> None:
        row = rest_row(5)
        row["fields"] = [f for f in row["fields"] if f["name"] != "Labels"]
        self.assertEqual(item_from_rest(row)["labels"], [])

    def test_non_issue_item_has_no_url(self) -> None:
        row = {
            "node_id": "PVTI_d",
            "content_type": "DraftIssue",
            "content": {"title": "Idea", "body": "text"},
            "fields": [],
        }
        mapped = item_from_rest(row)
        self.assertEqual(
            mapped["content"], {"type": "DraftIssue", "title": "Idea", "body": "text"}
        )
        # monitor.py calls content.get("url", "").startswith(...)
        self.assertEqual(mapped["content"].get("url", ""), "")


class SameDecisionTest(unittest.TestCase):
    """REST-mapped items must drive decide() and the monitor like the gh items did."""

    def test_decide_matches_gh_items(self) -> None:
        cases = [
            (
                item(501, "Claude", "Ready"),
                rest_row(501, executor="Claude", status="Ready", wave="0"),
            ),
            (
                item(502, "Codex", "Ready", wave="2"),
                rest_row(502, executor="Codex", status="Ready", wave="2"),
            ),
            (
                item(503, "Claude", "Ready", labels=["needs-anton"]),
                rest_row(
                    503,
                    executor="Claude",
                    status="Ready",
                    wave="0",
                    labels=["needs-anton"],
                ),
            ),
            (
                item(504, "Claude", "Done"),
                rest_row(504, executor="Claude", status="Done", wave="0"),
            ),
        ]
        old = [c[0] for c in cases]
        new = [item_from_rest(c[1]) for c in cases]
        for agent in ("Claude", "Codex"):
            self.assertEqual(
                decide(agent, {"prs": [], "items": old}, False),
                decide(agent, {"prs": [], "items": new}, False),
            )

    def test_monitor_task_contexts_match_gh_items(self) -> None:
        fixture = TaskContextTest()
        fixture.setUp()
        old = fixture.state
        rows = [
            rest_row(
                i["content"]["number"],
                status=i.get("status"),
                executor=i.get("executor"),
                area=i.get("area"),
                level=i.get("level"),
                title=i["content"].get("title", ""),
                body=i["content"].get("body", ""),
            )
            for i in old["items"]
        ]
        new = dict(old, items=[item_from_rest(r) for r in rows])
        self.assertEqual(monitor.task_contexts(new), monitor.task_contexts(old))
        self.assertTrue(any(row["area"] == "Ops" for row in monitor.task_contexts(new)))


class ProjectItemsTest(unittest.TestCase):
    def fake_gh(self, fields: list[dict], pages: list[list[dict]]):
        calls: list[list[str]] = []

        def run(args: list[str], timeout: int = 120) -> str:
            calls.append(args)
            endpoint = args[-1]
            if "/fields?" in endpoint:
                return json.dumps([fields])
            if "/items?" in endpoint:
                return json.dumps(pages)
            raise AssertionError(f"unexpected gh call {args}")

        return calls, run

    def all_fields(self) -> list[dict]:
        return [{"id": i, "name": n} for n, i in FIELD_IDS.items()]

    def test_reads_all_pages_with_the_needed_fields(self) -> None:
        page1 = [rest_row(n, status="Backlog") for n in range(1, 101)]
        page2 = [rest_row(n, status="Ready") for n in range(101, 111)]
        calls, run = self.fake_gh(self.all_fields(), [page1, page2])
        with patch.object(next_action, "run_gh", run):
            items = next_action.project_items()
        self.assertEqual([i["content"]["number"] for i in items], list(range(1, 111)))
        self.assertEqual(
            calls[1],
            [
                "api",
                "--paginate",
                "--slurp",
                "users/anneoneone/projectsV2/2/items?per_page=100"
                "&fields[]=11&fields[]=14&fields[]=15&fields[]=12&fields[]=13&fields[]=16",
            ],
        )
        for args in calls:
            self.assertEqual(args[0], "api")
            self.assertNotIn("graphql", args)

    def test_missing_board_field_stops_before_reading_items(self) -> None:
        fields = [f for f in self.all_fields() if f["name"] != "Wave"]
        calls, run = self.fake_gh(fields, [])
        with patch.object(next_action, "run_gh", run):
            with self.assertRaisesRegex(ValueError, "lacks fields: Wave"):
                next_action.project_items()
        self.assertEqual(len(calls), 1)


FAKE_GH = r"""#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
mode = os.environ["FAKE_GH_MODE"]
with open(os.environ["FAKE_GH_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
endpoint = args[-1]
if args[:2] == ["pr", "list"]:
    print("[]")
elif "/fields?" in endpoint:
    if mode == "fields_fail":
        sys.exit("gh: Server Error (HTTP 502)")
    names = ["Status", "Labels", "Area", "Wave", "Executor", "Level"]
    if mode == "missing_field":
        names.remove("Level")
    print(json.dumps([[{"id": i, "name": n} for i, n in enumerate(names)]]))
elif "/items?" in endpoint:
    page = json.loads(os.environ["FAKE_GH_PAGE"])
    if mode == "ok":
        print(json.dumps([page]))
    elif mode == "page2_fail":
        # gh --paginate prints the pages it got, then fails.
        print(json.dumps([page]))
        sys.exit("gh: Server Error (HTTP 502)")
    elif mode == "http_403":
        sys.exit("gh: Resource not accessible by integration (HTTP 403)")
    elif mode == "http_429":
        sys.exit("gh: You have exceeded a secondary rate limit (HTTP 429)")
elif "/comments?" in endpoint:
    print("[[]]")
else:
    sys.exit(f"fake gh: unexpected call {args}")
"""


class SelectorRunTest(unittest.TestCase):
    """Run the real selector with a fake gh on PATH, like the runners do."""

    def run_selector(self, mode: str) -> tuple[subprocess.CompletedProcess, list, Path]:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(0o755)
        state = tmp / "state"
        state.mkdir()
        log = tmp / "gh.log"
        page = [rest_row(601, status="Ready", executor="Claude", wave="0")]
        env = dict(
            os.environ,
            PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            HOME=str(tmp),
            EPIC_STATE_DIR=str(state),
            EPIC_QUOTA_DIR=str(state),
            FAKE_GH_MODE=mode,
            FAKE_GH_LOG=str(log),
            FAKE_GH_PAGE=json.dumps(page),
        )
        result = subprocess.run(
            [sys.executable, str(HERE / "next_action.py"), "--agent", "claude"],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        return result, calls, state

    def assert_no_graphql_for_project(self, calls: list) -> None:
        for args in calls:
            self.assertNotEqual(args[0], "project")
            self.assertNotIn("graphql", args)

    def test_board_read_via_rest_gives_an_action(self) -> None:
        result, calls, state = self.run_selector("ok")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["action"], "claim")
        self.assert_no_graphql_for_project(calls)
        self.assertFalse((state / WAIT_FILE).exists())

    def test_failed_reads_stop_without_action_or_quota_wait(self) -> None:
        for mode in (
            "fields_fail",
            "missing_field",
            "page2_fail",
            "http_403",
            "http_429",
        ):
            with self.subTest(mode=mode):
                result, calls, state = self.run_selector(mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertFalse((state / WAIT_FILE).exists())
                self.assert_no_graphql_for_project(calls)


if __name__ == "__main__":
    unittest.main()
