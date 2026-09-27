"""Feature gate tests with a fake GitHub API boundary."""

from __future__ import annotations

import copy
import json
import re
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import check

HEAD = "a" * 40
OLD = "b" * 40
REPO = "phaabe/live.moafunk.de"
EPIC = f"https://github.com/{REPO}/issues/312"


def policy() -> dict:
    return {
        "version": 1,
        "repository": REPO,
        "epic_url": EPIC,
        "feature_bases": ["dev/312-interim", "dev/streaming-architecture"],
        "setup_branch": "ci/312-epic-guard",
        "release_branch": "dev/streaming-architecture",
        "trusted_reviewers": {"Codex": ["anneoneone"], "Claude": ["anneoneone"]},
        "required_checks": ["test"],
        "ignored_checks": ["epic-guard", "epic-guard-runner"],
        "file_rules": [
            {
                "pattern": "scripts/epic_guard/*",
                "owners": ["Codex"],
                "lanes": ["setup"],
            },
            {"pattern": ".github/workflows/*", "owners": ["Codex"], "lanes": ["ops"]},
            {"pattern": "backend/src/*", "owners": ["Claude"], "lanes": ["backend"]},
        ],
    }


def verdict(number: int = 1, state: str = "APPROVED", sha: str = HEAD) -> dict:
    return {
        "id": number,
        "body": f"Review: {state} by Claude at {sha}",
        "user": {"login": "anneoneone"},
        "created_at": f"2026-09-28T10:{number:02d}:00Z",
        "updated_at": f"2026-09-28T10:{number:02d}:00Z",
        "last_edited_at": None,
    }


def snapshot() -> dict:
    return {
        "repository": REPO,
        "pr": {
            "head": {"sha": HEAD, "ref": "ci/312-guard", "repo": {"full_name": REPO}},
            "base": {"sha": OLD, "ref": "dev/312-interim", "repo": {"full_name": REPO}},
            "state": "open",
            "draft": False,
            "comments": 1,
            "changed_files": 1,
            "updated_at": "2026-09-28T10:00:00Z",
            "body": f"{EPIC}\n<details>\nExecutor: Codex\nLane: setup\nReviewer: Claude\nLeaf IDs: setup\n</details>",
        },
        "files": [{"filename": "scripts/epic_guard/check.py", "status": "modified"}],
        "comments": [verdict()],
        "check_runs": [
            {
                "id": 10,
                "name": "test",
                "head_sha": HEAD,
                "app": {"id": 1},
                "status": "completed",
                "conclusion": "success",
            }
        ],
        "statuses": [],
    }


class FakeGitHub:
    def __init__(self, data: dict) -> None:
        self.data = data
        self.calls: list[str] = []

    def __call__(self, endpoint: str) -> dict | list:
        self.calls.append(endpoint)
        parsed = urlsplit(endpoint)
        if parsed.path == "graphql":
            query = parse_qs(parsed.query)["query"][0]
            cursor = json.loads(re.search(r"after: (null|\"[^\"]*\")", query)[1])
            start = int(cursor or 0)
            comments = self.data["comments"]
            nodes = [
                {
                    "databaseId": comment["id"],
                    "lastEditedAt": comment.get("last_edited_at"),
                    "body": comment["body"],
                    "createdAt": comment["created_at"],
                    "updatedAt": comment["updated_at"],
                    "author": comment["user"],
                }
                for comment in comments[start : start + 100]
            ]
            return {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "comments": {
                                "totalCount": len(comments),
                                "nodes": nodes,
                                "pageInfo": {
                                    "hasNextPage": start + 100 < len(comments),
                                    "endCursor": str(start + 100),
                                },
                            }
                        }
                    }
                }
            }
        page = int(parse_qs(parsed.query).get("page", ["1"])[0])
        begin = (page - 1) * 100
        path = parsed.path
        if path.endswith("/pulls/406"):
            return copy.deepcopy(self.data["pr"])
        key = next(
            (
                key
                for ending, key in (
                    ("/files", "files"),
                    ("/comments", "comments"),
                    ("/check-runs", "check_runs"),
                    ("/statuses", "statuses"),
                )
                if path.endswith(ending)
            ),
            None,
        )
        if key is None:
            raise AssertionError(f"unexpected endpoint: {endpoint}")
        rows = copy.deepcopy(self.data[key])
        batch = rows[begin : begin + 100]
        return (
            {"total_count": len(rows), "check_runs": batch}
            if key == "check_runs"
            else batch
        )


class EvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = policy()
        self.data = snapshot()

    def errors(self) -> list[str]:
        return check.evaluate(self.policy, self.data, HEAD)

    def test_valid_feature_and_setup_exception(self) -> None:
        self.assertEqual(self.errors(), [])
        self.data["pr"]["base"]["ref"] = "main"
        self.data["pr"]["head"]["ref"] = "ci/312-epic-guard"
        self.assertEqual(self.errors(), [])

    def test_head_state_draft_and_repository(self) -> None:
        for path, value in [
            (("head", "sha"), OLD),
            (("state",), "closed"),
            (("draft",), True),
            (("head", "repo", "full_name"), "other/fork"),
        ]:
            with self.subTest(path=path):
                self.data = snapshot()
                target = self.data["pr"]
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
                self.assertTrue(self.errors())

    def test_bad_base_release_and_main_feature_blocked(self) -> None:
        for base, branch in [
            ("other", "ci/312-guard"),
            ("main", "ci/312-guard"),
            ("main", "dev/streaming-architecture"),
            ("main", "dev/312-interim"),
        ]:
            with self.subTest(base=base, branch=branch):
                self.data["pr"]["base"]["ref"] = base
                self.data["pr"]["head"]["ref"] = branch
                self.assertTrue(self.errors())

    def test_duplicate_missing_and_invalid_metadata(self) -> None:
        for suffix in [
            "\nExecutor: Codex",
            "\nLane: ops",
            "\nReviewer: Claude",
            "\nLeaf IDs: setup",
        ]:
            with self.subTest(suffix=suffix):
                self.data = snapshot()
                self.data["pr"]["body"] += suffix
                self.assertTrue(self.errors())
        for old, new in [
            ("Executor: Codex", "Executor: Robot"),
            ("Reviewer: Claude", "Reviewer: Codex"),
            ("Lane: setup", "Lane: unknown"),
            ("Leaf IDs: setup", "Leaf IDs: B1.1"),
            (EPIC, EPIC + "0"),
        ]:
            with self.subTest(new=new):
                self.data = snapshot()
                self.data["pr"]["body"] = self.data["pr"]["body"].replace(old, new)
                self.assertTrue(self.errors())

    def test_ops_leaf_ids_and_first_matching_rule(self) -> None:
        self.data["pr"]["body"] = (
            self.data["pr"]["body"]
            .replace("Lane: setup", "Lane: ops")
            .replace("Leaf IDs: setup", "Leaf IDs: O1.2.4, O1.2.2")
        )
        self.data["files"][0]["filename"] = ".github/workflows/build.yml"
        self.assertEqual(self.errors(), [])
        self.policy["file_rules"].insert(
            0,
            {
                "pattern": ".github/workflows/build.yml",
                "owners": ["Claude"],
                "lanes": ["ops"],
            },
        )
        self.assertTrue(self.errors())

    def test_unknown_files_and_rename_crossing_lane(self) -> None:
        self.data["files"][0]["filename"] = "unassigned.txt"
        self.assertTrue(self.errors())
        self.data = snapshot()
        self.data["files"][0].update(
            status="renamed", previous_filename="backend/src/main.rs"
        )
        self.assertTrue(self.errors())
        self.data["files"][0].pop("previous_filename")
        self.assertTrue(self.errors())

    def test_empty_or_incomplete_file_snapshot(self) -> None:
        self.data["files"] = []
        self.assertTrue(self.errors())

    def test_stale_missing_forged_or_nonstandalone_verdict(self) -> None:
        for body in [
            f"Review: APPROVED by Claude at {OLD}",
            f"Review: APPROVED by Codex at {HEAD}",
            f"quoted\nReview: APPROVED by Claude at {HEAD}",
            f"Review: APPROVED by Claude at {HEAD}\n",
        ]:
            with self.subTest(body=body):
                self.data = snapshot()
                self.data["comments"][0]["body"] = body
                self.assertTrue(self.errors())
        self.data = snapshot()
        self.data["comments"][0]["user"]["login"] = "untrusted"
        self.assertTrue(self.errors())

    def test_latest_changes_requested_and_later_other_sha_override(self) -> None:
        for later in [verdict(2, "CHANGES REQUESTED"), verdict(2, sha=OLD)]:
            with self.subTest(later=later):
                self.data["comments"] = [later, verdict()]
                self.assertTrue(self.errors())

    def test_edited_or_malformed_latest_does_not_revive_approval(self) -> None:
        latest = verdict(2)
        latest["updated_at"] = "2026-09-28T11:00:00Z"
        self.data["comments"].append(latest)
        self.assertTrue(self.errors())
        latest["body"] += " edited"
        self.assertTrue(self.errors())

    def test_new_approval_after_changes_requested(self) -> None:
        self.data["comments"] = [verdict(1, "CHANGES REQUESTED"), verdict(2)]
        self.assertEqual(self.errors(), [])

    def test_same_second_edit_rejected_and_missing_marker_fails_closed(self) -> None:
        comment = self.data["comments"][0]
        comment["last_edited_at"] = comment["created_at"]
        self.assertTrue(self.errors())
        comment.pop("last_edited_at")
        self.assertTrue(self.errors())

    def test_verdict_edited_to_unrelated_text_cannot_revive_old_approval(self) -> None:
        edited = verdict(2, "CHANGES REQUESTED")
        edited["body"] = "No longer a verdict"
        edited["updated_at"] = "2026-09-28T10:03:00Z"
        self.data["comments"].append(edited)
        self.assertTrue(self.errors())
        self.data["comments"].append(verdict(4))
        self.assertEqual(self.errors(), [])

    def test_pending_failed_stale_or_missing_check(self) -> None:
        for key, value in [
            ("status", "in_progress"),
            ("conclusion", "failure"),
            ("conclusion", "skipped"),
            ("head_sha", OLD),
        ]:
            with self.subTest(key=key, value=value):
                self.data = snapshot()
                self.data["check_runs"][0][key] = value
                self.assertTrue(self.errors())
        self.data["check_runs"] = []
        self.assertTrue(self.errors())

    def test_latest_check_rerun_wins_but_other_app_failure_blocks(self) -> None:
        failed = dict(self.data["check_runs"][0], id=9, conclusion="failure")
        self.data["check_runs"].append(failed)
        self.assertEqual(self.errors(), [])
        failed["app"] = {"id": 2}
        self.assertTrue(self.errors())

    def test_all_reported_statuses_and_checks_must_succeed(self) -> None:
        self.data["statuses"] = [{"id": 2, "context": "extra", "state": "pending"}]
        self.assertTrue(self.errors())
        self.data["statuses"].append({"id": 3, "context": "extra", "state": "success"})
        self.assertEqual(self.errors(), [])
        self.data["check_runs"].append(
            dict(
                self.data["check_runs"][0],
                id=15,
                name="extra-test",
                conclusion="failure",
            )
        )
        self.assertTrue(self.errors())

    def test_guard_self_contexts_ignored_and_status_can_supply_requirement(
        self,
    ) -> None:
        self.data["check_runs"] = [
            dict(self.data["check_runs"][0], name="epic-guard-runner", conclusion=None)
        ]
        self.data["statuses"] = [
            {"id": 1, "context": "epic-guard", "state": "pending"},
            {"id": 2, "context": "test", "state": "success"},
        ]
        self.assertEqual(self.errors(), [])


class CollectionTests(unittest.TestCase):
    def test_collection_and_pagination_of_every_endpoint(self) -> None:
        data = snapshot()
        data["comments"] = [verdict(i) for i in range(1, 102)]
        data["pr"]["comments"] = 101
        data["files"] = [{"filename": f"scripts/epic_guard/{i}.py"} for i in range(101)]
        data["pr"]["changed_files"] = 101
        data["check_runs"] = [dict(data["check_runs"][0], id=i) for i in range(1, 102)]
        data["statuses"] = [
            {"id": i, "context": "test", "state": "success"} for i in range(1, 102)
        ]
        api = FakeGitHub(data)
        result = check.collect(REPO, 406, gh=api)
        for key in ["comments", "files", "check_runs", "statuses"]:
            self.assertEqual(len(result[key]), 101)
        self.assertEqual(len([url for url in api.calls if "page=2" in url]), 4)
        self.assertEqual(
            len([url for url in api.calls if url.startswith("graphql?")]), 2
        )

    def test_graphql_missing_markers_errors_and_mismatched_ids_fail_closed(
        self,
    ) -> None:
        for damage in [
            "missing_marker",
            "missing_node",
            "wrong_id",
            "wrong_body",
            "api_error",
        ]:
            with self.subTest(damage=damage):
                api = FakeGitHub(snapshot())

                def broken(endpoint: str) -> dict | list:
                    response = api(endpoint)
                    if endpoint.startswith("graphql?"):
                        connection = response["data"]["repository"]["pullRequest"][
                            "comments"
                        ]
                        if damage == "missing_marker":
                            connection["nodes"][0].pop("lastEditedAt")
                        elif damage == "missing_node":
                            connection["nodes"] = []
                        elif damage == "wrong_id":
                            connection["nodes"][0]["databaseId"] += 1
                        elif damage == "wrong_body":
                            connection["nodes"][0]["body"] = "changed"
                        elif damage == "api_error":
                            response["errors"] = [{"message": "query failed"}]
                    return response

                with self.assertRaises(ValueError):
                    check.collect(REPO, 406, gh=broken)

    def test_incomplete_files_comments_and_file_cap_fail_closed(self) -> None:
        for key, value in [
            ("changed_files", 2),
            ("changed_files", 3000),
            ("comments", 2),
        ]:
            with self.subTest(key=key, value=value):
                data = snapshot()
                data["pr"][key] = value
                with self.assertRaises(ValueError):
                    check.collect(REPO, 406, gh=FakeGitHub(data))

    def test_api_error_does_not_become_empty_success(self) -> None:
        def fail(endpoint: str) -> dict:
            raise OSError("GitHub unreachable")

        with self.assertRaises(OSError):
            check.collect(REPO, 406, gh=fail)

    def test_incomplete_check_pages_and_duplicate_ids(self) -> None:
        for response in [
            {"total_count": 2, "check_runs": [{"id": 1}]},
            {"total_count": 2, "check_runs": [{"id": 1}, {"id": 1}]},
        ]:
            with self.subTest(response=response), self.assertRaises(ValueError):
                check.pages(lambda _: response, "test", "check_runs")

    def test_head_or_metadata_changed_during_collection(self) -> None:
        for field, value in [("body", "changed"), ("head", {"sha": OLD})]:
            with self.subTest(field=field):
                api = FakeGitHub(snapshot())
                calls = 0

                def race(endpoint: str) -> dict | list:
                    nonlocal calls
                    result = api(endpoint)
                    if endpoint.endswith("/pulls/406"):
                        calls += 1
                        if calls == 2:
                            result[field] = value
                    return result

                with self.assertRaises(ValueError):
                    check.collect(REPO, 406, gh=race)


class PolicyTests(unittest.TestCase):
    def test_policy_loading_and_rejecting_ignored_required_checks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.yml"
            path.write_text(json.dumps(policy()))
            self.assertEqual(check.load_policy(path), policy())
            malformed = policy()
            malformed["ignored_checks"].append("test")
            path.write_text(json.dumps(malformed))
            with self.assertRaises(ValueError):
                check.load_policy(path)


if __name__ == "__main__":
    unittest.main()
