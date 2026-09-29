"""Trusted-base loading and status publication tests; no network or credentials."""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from functools import partial
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

import run
from test_check import HEAD, OLD, FakeGitHub, policy, snapshot


def blob(content: bytes) -> dict:
    return {
        "type": "file",
        "encoding": "base64",
        "content": base64.b64encode(content).decode(),
        "sha": hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest(),
    }


class BaseAPI(FakeGitHub):
    def __init__(self) -> None:
        super().__init__(snapshot())
        self.data["pr"]["number"] = 406
        self.pulls = [self.data["pr"]]
        self.contents = {
            "scripts/epic_guard/check.py": blob(
                Path(__file__).with_name("check.py").read_bytes()
            ),
            ".github/epic-lanes.yml": blob(json.dumps(policy()).encode()),
        }

    def __call__(self, endpoint: str) -> dict | list:
        if "/contents/" in endpoint:
            self.calls.append(endpoint)
            path, sha = endpoint.split("/contents/")[1].split("?ref=")
            if sha != OLD:
                raise AssertionError("checker must be loaded from immutable base SHA")
            return copy.deepcopy(self.contents[path])
        if "/pulls?" in endpoint:
            self.calls.append(endpoint)
            return copy.deepcopy(self.pulls)
        return super().__call__(endpoint)


class RunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.api = BaseAPI()
        self.statuses: list[tuple[str, str, str]] = []

    def verify(self, gh: run.Api | None = None) -> list[str]:
        return run.verify(
            406, gh or self.api, lambda *args: self.statuses.append(args), HEAD
        )

    def main(self, *args: str) -> int:
        with (
            patch("sys.argv", ["run.py", *args]),
            patch("sys.stdout", new_callable=io.StringIO),
            patch.dict(
                run.os.environ,
                {
                    "GITHUB_ACTIONS": "true",
                    "GITHUB_REPOSITORY": run.REPO,
                    "GITHUB_RUN_ID": "123",
                },
            ),
            patch.object(run, "api", self.api),
            patch.object(run, "verify", partial(run.verify, gh=self.api)),
        ):
            return run.main()

    def test_main_publish_returns_zero_for_guard_errors(self) -> None:
        self.api.data["comments"][0]["body"] = self.api.data["comments"][0][
            "body"
        ].replace("APPROVED", "CHANGES REQUESTED")
        with patch.object(
            run, "publish", side_effect=lambda *s: self.statuses.append(s)
        ):
            self.assertEqual(self.main("--pr", "406", "--publish"), 0)
        self.assertEqual([s[1] for s in self.statuses], ["pending", "failure"])

    def test_review_and_draft_waiting_publish_pending_but_block_local_mode(
        self,
    ) -> None:
        for reason in ("draft", "missing verdict", "stale verdict", "all"):
            with self.subTest(reason=reason):
                self.api = BaseAPI()
                if reason in {"draft", "all"}:
                    self.api.data["pr"]["draft"] = True
                if reason in {"missing verdict", "all"}:
                    self.api.data["comments"] = []
                    self.api.data["pr"]["comments"] = 0
                if reason == "stale verdict":
                    self.api.data["comments"][0]["body"] = self.api.data["comments"][0][
                        "body"
                    ].replace(HEAD, OLD)
                if reason == "all":
                    self.api.data["check_runs"][0].update(
                        status="queued", conclusion=None
                    )
                with patch.object(
                    run, "publish", side_effect=lambda *s: self.statuses.append(s)
                ):
                    self.assertEqual(self.main("--pr", "406", "--publish"), 0)
                self.assertEqual(self.statuses[-1][:2], (HEAD, "pending"))
                self.assertIn("waiting for PR readiness", self.statuses[-1][2])
                self.assertEqual(self.main("--pr", "406"), 1)

    def test_running_check_publishes_pending_and_blocks_local_mode(self) -> None:
        for status in ("queued", "in_progress"):
            with self.subTest(status=status):
                self.api.data["check_runs"][0].update(status=status, conclusion=None)
                self.assertTrue(self.verify())
                self.assertEqual(self.statuses[-1][:2], (HEAD, "pending"))
                self.assertIn("waiting for", self.statuses[-1][2])
                self.assertIn("test", self.statuses[-1][2])
                self.assertEqual(self.main("--pr", "406"), 1)

    def test_pending_status_publishes_pending_and_blocks_local_mode(self) -> None:
        self.api.data["statuses"] = [
            {"id": 1, "context": "deployment", "state": "pending"}
        ]
        with patch.object(
            run, "publish", side_effect=lambda *s: self.statuses.append(s)
        ):
            self.assertEqual(self.main("--pr", "406", "--publish"), 0)
        self.assertEqual(self.statuses[-1][:2], (HEAD, "pending"))
        self.assertIn("waiting for", self.statuses[-1][2])
        self.assertIn("deployment", self.statuses[-1][2])
        self.assertEqual(self.main("--pr", "406"), 1)

    def test_missing_required_check_publishes_pending_and_blocks_local_mode(
        self,
    ) -> None:
        self.api.data["check_runs"] = []
        self.assertTrue(self.verify())
        self.assertEqual(self.statuses[-1][:2], (HEAD, "pending"))
        self.assertIn("waiting for", self.statuses[-1][2])
        self.assertIn("test", self.statuses[-1][2])
        self.assertEqual(self.main("--pr", "406"), 1)

    def test_waiting_with_real_error_publishes_failure_and_blocks_local_mode(
        self,
    ) -> None:
        for error in (
            "check",
            "status",
            "verdict",
            "malformed verdict",
            "lane",
            "metadata",
        ):
            with self.subTest(error=error):
                self.api = BaseAPI()
                self.api.data["pr"]["draft"] = True
                self.api.data["check_runs"][0].update(
                    status="in_progress", conclusion=None
                )
                if error == "check":
                    self.api.data["check_runs"].append(
                        dict(
                            self.api.data["check_runs"][0],
                            id=20,
                            name="failed-test",
                            status="completed",
                            conclusion="failure",
                        )
                    )
                elif error == "status":
                    self.api.data["statuses"] = [
                        {"id": 1, "context": "deployment", "state": "failure"}
                    ]
                elif error == "verdict":
                    self.api.data["comments"][0]["body"] = self.api.data["comments"][0][
                        "body"
                    ].replace("APPROVED", "CHANGES REQUESTED")
                elif error == "malformed verdict":
                    self.api.data["comments"][0]["body"] += " trailing text"
                elif error == "lane":
                    self.api.data["comments"][0]["body"] = self.api.data["comments"][0][
                        "body"
                    ].replace(HEAD, OLD)
                    self.api.data["files"][0]["filename"] = "unassigned.txt"
                else:
                    self.api.data["comments"] = []
                    self.api.data["pr"]["comments"] = 0
                    self.api.data["pr"]["body"] += "\nExecutor: Codex"
                self.assertTrue(self.verify())
                self.assertEqual(self.statuses[-1][:2], (HEAD, "failure"))
                self.assertNotIn("not completed", self.statuses[-1][2])
                self.assertEqual(self.main("--pr", "406"), 1)

    def test_main_publish_returns_one_for_publication_failures(self) -> None:
        for failed_write in (0, 1):
            with self.subTest(failed_write=failed_write):
                failure = subprocess.CalledProcessError(1, ["gh", "api"])
                with patch.object(
                    run.subprocess, "run", side_effect=[None] * failed_write + [failure]
                ) as command:
                    self.assertEqual(self.main("--pr", "406", "--publish"), 1)
                self.assertEqual(command.call_count, failed_write + 1)

    def test_main_publish_returns_one_for_api_failure(self) -> None:
        with (
            patch.object(
                self, "api", side_effect=subprocess.CalledProcessError(1, ["gh", "api"])
            ),
            patch.object(run, "publish") as publish,
        ):
            self.assertEqual(self.main("--pr", "406", "--publish"), 1)
        publish.assert_not_called()

    def test_main_publish_returns_one_for_bad_event(self) -> None:
        with (
            patch.object(run.Path, "read_text", return_value='{"repository": {}}'),
            patch.object(run, "publish") as publish,
        ):
            self.assertEqual(self.main("--event", "event.json", "--publish"), 1)
        publish.assert_not_called()

    def test_main_publish_returns_zero_for_late_api_failures(self) -> None:
        api = self.api
        for failure in (
            subprocess.CalledProcessError(1, ["gh", "api"]),
            subprocess.TimeoutExpired(["gh", "api"], 60),
            json.JSONDecodeError("invalid response", "", 0),
            OSError("API unavailable"),
        ):
            with self.subTest(failure=type(failure).__name__):
                self.statuses.clear()

                def gh(endpoint: str) -> dict | list:
                    if "/contents/" in endpoint:
                        raise failure
                    return api(endpoint)

                with (
                    patch.object(self, "api", gh),
                    patch.object(
                        run, "publish", side_effect=lambda *s: self.statuses.append(s)
                    ),
                ):
                    self.assertEqual(self.main("--pr", "406", "--publish"), 0)
                self.assertEqual([s[1] for s in self.statuses], ["pending", "failure"])

    def test_main_publish_returns_zero_for_graphql_error_response(self) -> None:
        api = self.api

        def gh(endpoint: str) -> dict | list:
            if endpoint.startswith("graphql?"):
                return {"errors": [{"message": "API unavailable"}]}
            return api(endpoint)

        with (
            patch.object(self, "api", gh),
            patch.object(
                run, "publish", side_effect=lambda *s: self.statuses.append(s)
            ),
        ):
            self.assertEqual(self.main("--pr", "406", "--publish"), 0)
        self.assertEqual([s[1] for s in self.statuses], ["pending", "failure"])

    def test_main_publish_refreshes_all_prs_despite_guard_errors(self) -> None:
        for error in (
            "missing verdict",
            "untrusted base",
            "PR changed during collection; retry",
            "comment changed between REST and GraphQL reads; retry",
            "missing comment edit evidence",
        ):
            with self.subTest(error=error):
                self.statuses.clear()
                failing = BaseAPI()
                failing.data["comments"] = []
                failing.data["pr"]["comments"] = 0
                if error == "untrusted base":
                    failing.data["pr"]["base"]["ref"] = "unsupported"
                passing = BaseAPI()
                passing.data["pr"]["number"] = 407
                passing.data["pr"]["head"]["sha"] = "c" * 40
                passing.data["comments"][0]["body"] = passing.data["comments"][0][
                    "body"
                ].replace(HEAD, "c" * 40)
                passing.data["check_runs"][0]["head_sha"] = "c" * 40
                pulls = [failing.data["pr"], passing.data["pr"]]
                active = failing

                def gh(endpoint: str) -> dict | list:
                    nonlocal active
                    if "/pulls?" in endpoint:
                        return copy.deepcopy(pulls)
                    if endpoint.endswith("/pulls/407"):
                        active = passing
                        endpoint = endpoint.replace("/pulls/407", "/pulls/406")
                    if (
                        active is failing
                        and "/comments?" in endpoint
                        and error not in {"missing verdict", "untrusted base"}
                    ):
                        raise ValueError(error)
                    return active(endpoint)

                event = json.dumps({"repository": {"full_name": run.REPO}})
                with (
                    tempfile.TemporaryDirectory() as directory,
                    patch.object(self, "api", gh),
                    patch.object(
                        run, "publish", side_effect=lambda *s: self.statuses.append(s)
                    ),
                ):
                    event_path = Path(directory) / "event.json"
                    event_path.write_text(event)
                    self.assertEqual(
                        self.main("--event", str(event_path), "--publish"), 0
                    )
                if "retry" in error or "edit evidence" in error:
                    self.assertEqual(self.statuses[1][2], error)
                self.assertEqual(
                    [s[:2] for s in self.statuses],
                    [
                        (HEAD, "pending"),
                        (HEAD, "pending" if error == "missing verdict" else "failure"),
                        ("c" * 40, "pending"),
                        ("c" * 40, "success"),
                    ],
                )

    def test_main_publish_returns_one_when_event_listing_fails(self) -> None:
        event = json.dumps({"repository": {"full_name": run.REPO}})
        with (
            patch.object(run.Path, "read_text", return_value=event),
            patch.object(
                self, "api", side_effect=subprocess.CalledProcessError(1, ["gh", "api"])
            ),
            patch.object(run, "publish") as publish,
        ):
            self.assertEqual(self.main("--event", "event.json", "--publish"), 1)
        publish.assert_not_called()

    def test_main_publish_returns_one_when_failure_status_cannot_be_published(self) -> None:
        api = self.api

        def gh(endpoint: str) -> dict | list:
            if "/comments?" in endpoint:
                raise ValueError("PR changed during collection; retry")
            return api(endpoint)

        with (
            patch.object(self, "api", gh),
            patch.object(
                run,
                "publish",
                side_effect=[None, subprocess.CalledProcessError(1, ["gh", "api"])],
            ) as publish,
        ):
            self.assertEqual(self.main("--pr", "406", "--publish"), 1)
        self.assertEqual(publish.call_count, 2)
        self.assertEqual(publish.call_args.args[:2], (HEAD, "failure"))

    def test_main_local_exit_code_reports_guard_errors(self) -> None:
        with patch.object(run, "publish") as publish:
            self.assertEqual(self.main("--pr", "406"), 0)
            self.api.data["comments"] = []
            self.api.data["pr"]["comments"] = 0
            self.assertEqual(self.main("--pr", "406"), 1)
        publish.assert_not_called()

    def test_loads_only_base_blobs_and_publishes_exact_head(self) -> None:
        self.assertEqual(self.verify(), [])
        self.assertEqual(
            [s[:2] for s in self.statuses], [(HEAD, "pending"), (HEAD, "success")]
        )
        self.assertEqual(sum("/contents/" in call for call in self.api.calls), 2)

    def test_read_only_mode_never_publishes(self) -> None:
        with patch.object(run, "publish") as publish:
            self.assertEqual(run.verify(406, self.api, expected_head=HEAD), [])
            publish.assert_not_called()

    def test_graphql_transport_only_executes_queries(self) -> None:
        endpoint = "graphql?" + urlencode({"query": "query { viewer { login } }"})
        with patch.object(run.subprocess, "run") as command:
            command.return_value.stdout = '{"data": {}}'
            self.assertEqual(run.api(endpoint), {"data": {}})
            self.assertEqual(command.call_args.args[0][2], "graphql")
        with self.assertRaisesRegex(ValueError, "only anonymous"):
            run.api("graphql?" + urlencode({"query": "mutation { wrong }"}))

    def test_missing_bootstrap_or_invalid_blob_fails_closed(self) -> None:
        self.api.contents["scripts/epic_guard/check.py"] = {"type": "symlink"}
        self.assertIn("install setup", self.verify()[0])
        self.assertEqual(self.statuses[-1][1], "failure")
        self.api = BaseAPI()
        self.api.contents["scripts/epic_guard/check.py"]["sha"] = "f" * 40
        self.assertIn("invalid Git blob", self.verify()[0])

    def test_untrusted_base_and_wrong_expected_head(self) -> None:
        self.api.data["pr"]["base"]["ref"] = "attacker-branch"
        self.assertIn("untrusted base", self.verify()[0])
        self.assertFalse(any("/contents/" in call for call in self.api.calls))
        self.api = BaseAPI()
        self.assertIn("expected head", run.verify(406, self.api, expected_head=OLD)[0])

    def test_duplicate_open_pr_at_head_refused(self) -> None:
        duplicate = copy.deepcopy(self.api.data["pr"])
        duplicate["number"] = 407
        self.api.pulls.append(duplicate)
        self.assertIn("exactly one", self.verify()[0])

    def test_changes_requested_during_collection_prevents_success(self) -> None:
        comments = 0

        def gh(endpoint: str) -> dict | list:
            nonlocal comments
            if "/comments?" in endpoint:
                comments += 1
                if comments == 2:
                    self.api.data["comments"][0]["body"] = self.api.data["comments"][0][
                        "body"
                    ].replace("APPROVED", "CHANGES REQUESTED")
            return self.api(endpoint)

        self.assertIn("requests changes", " ".join(self.verify(gh)))
        self.assertEqual(self.statuses[-1][1], "failure")

    def test_check_turning_red_before_publication_prevents_success(self) -> None:
        checks = 0

        def gh(endpoint: str) -> dict | list:
            nonlocal checks
            if "/check-runs?" in endpoint:
                checks += 1
                if checks == 2:
                    self.api.data["check_runs"][0]["conclusion"] = "failure"
            return self.api(endpoint)

        self.assertIn("not successful", " ".join(self.verify(gh)))
        self.assertEqual(self.statuses[-1][1], "failure")

    def test_head_advance_before_publish_never_grants_success(self) -> None:
        reads = 0

        def gh(endpoint: str) -> dict | list:
            nonlocal reads
            if endpoint.endswith("/pulls/406"):
                reads += 1
                if reads == 6:
                    self.api.data["pr"]["head"]["sha"] = "c" * 40
            return self.api(endpoint)

        self.assertIn("before status", " ".join(self.verify(gh)))
        self.assertEqual(self.statuses[-1][:2], (HEAD, "failure"))

    def test_base_advance_rejects_old_policy(self) -> None:
        reads = 0

        def gh(endpoint: str) -> dict | list:
            nonlocal reads
            if endpoint.endswith("/pulls/406"):
                reads += 1
                if reads == 2:
                    self.api.data["pr"]["base"]["sha"] = "d" * 40
            return self.api(endpoint)

        self.assertIn("base changed", " ".join(self.verify(gh)))

    def test_api_failure_after_pending_publishes_failure(self) -> None:
        def gh(endpoint: str) -> dict | list:
            if "/contents/" in endpoint:
                raise subprocess.CalledProcessError(1, ["gh", "api"])
            return self.api(endpoint)

        self.assertTrue(self.verify(gh))
        self.assertEqual([s[1] for s in self.statuses], ["pending", "failure"])

    def test_scope_and_manual_input_validation(self) -> None:
        event = {"repository": {"full_name": run.REPO}}
        self.assertEqual(run.event_numbers(event, self.api), [406])
        self.api.data["pr"]["base"]["ref"] = "main"
        self.api.data["pr"]["body"] = "Ordinary fix"
        self.assertEqual(run.event_numbers(event, self.api), [])
        event["inputs"] = {"pr_number": "406; echo wrong"}
        with self.assertRaisesRegex(ValueError, "invalid PR"):
            run.event_numbers(event, self.api)
        event["inputs"] = {"pr_number": "406"}
        self.assertEqual(run.event_numbers(event, self.api), [406])
        event["repository"]["full_name"] = "another/repo"
        with self.assertRaisesRegex(ValueError, "repository mismatch"):
            run.event_numbers(event, self.api)

    def test_leaving_scope_invalidates_previous_green_status(self) -> None:
        self.api.data["pr"]["base"]["ref"] = "main"
        self.api.data["pr"]["body"] = "Ordinary fix"
        self.api.data["statuses"] = [
            {"id": i, "context": f"other-{i}", "state": "success"} for i in range(100)
        ] + [{"id": 101, "context": "epic-guard", "state": "success"}]
        event = {"repository": {"full_name": run.REPO}}
        self.assertEqual(run.event_numbers(event, self.api), [406])
        self.assertTrue(self.verify())
        self.assertEqual(self.statuses[-1][1], "failure")

    def test_new_pr_sharing_head_before_publication_blocks_success(self) -> None:
        reads = 0

        def gh(endpoint: str) -> dict | list:
            nonlocal reads
            if "/pulls?" in endpoint:
                reads += 1
                if reads == 2:
                    duplicate = copy.deepcopy(self.api.data["pr"])
                    duplicate["number"] = 407
                    self.api.pulls.append(duplicate)
            return self.api(endpoint)

        self.assertIn("sharing this head changed", " ".join(self.verify(gh)))
        self.assertEqual(self.statuses[-1][1], "failure")


if __name__ == "__main__":
    unittest.main()
