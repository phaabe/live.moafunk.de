#!/usr/bin/env python3
from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

import wait_checks as w

SHA = "a" * 40
NEW_SHA = "b" * 40

FRONTEND_YML = """\
name: FRONTEND
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
    paths:
      - frontend/**
      - .github/workflows/frontend.yml  # self
  workflow_dispatch:
jobs:
  build:
    runs-on: ubuntu-latest
"""

BACKEND_YML = """\
on:
  push:
    branches: [main]
    paths:
      - backend/**
"""

GUARD_YML = """\
on:
  pull_request_target:
    types: [opened]
"""


def run(
    path: str,
    status: str = "completed",
    conclusion: str | None = "success",
    id: int = 1,
):
    return {"id": id, "path": path, "status": status, "conclusion": conclusion}


def check(
    name: str,
    status: str = "completed",
    conclusion: str | None = "success",
    id: int = 1,
):
    return {"id": id, "name": name, "status": status, "conclusion": conclusion}


NO_STATUS = {"total_count": 0, "state": "pending", "statuses": []}
VERCEL_OK = {
    "total_count": 1,
    "state": "success",
    "statuses": [{"context": "Vercel", "state": "success"}],
}


class FakeGit:
    def __init__(
        self, files: list[str], workflows: dict[str, str] | None = None
    ) -> None:
        self.files = files
        self.workflows = (
            workflows
            if workflows is not None
            else {
                ".github/workflows/frontend.yml": FRONTEND_YML,
                ".github/workflows/backend.yml": BACKEND_YML,
                ".github/workflows/guard.yml": GUARD_YML,
            }
        )
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> str:
        self.calls.append(args)
        if args[0] == "fetch":
            return ""
        if args[0] == "diff":
            return "\n".join(self.files) + "\n"
        if args[0] == "ls-tree":
            return "\n".join(self.workflows) + "\n"
        if args[0] == "show":
            return self.workflows[args[1].split(":", 1)[1]]
        raise AssertionError(args)


class FakeApi:
    """Each poll reads the PR first; `polls[i]` is the GitHub state during poll i."""

    def __init__(self, polls: list[dict[str, Any]]) -> None:
        self.polls = polls
        self.i = -1
        self.endpoints: list[str] = []

    def __call__(self, endpoint: str, paginate: bool) -> Any:
        self.endpoints.append(endpoint)
        if "/pulls/" in endpoint:
            self.i = min(self.i + 1, len(self.polls) - 1)
        p = self.polls[self.i]
        if "error" in p:
            raise w.ToolError(p["error"])
        if "/pulls/" in endpoint:
            return {
                "state": p.get("state", "open"),
                "mergeable": p.get("mergeable", True),
                "head": {"sha": p.get("sha", SHA)},
                "base": {"ref": "main"},
            }
        if "/actions/runs" in endpoint:
            assert paginate
            return [{"workflow_runs": p.get("runs", [])}]
        if "/check-runs" in endpoint:
            assert paginate
            pages = p.get("check_pages", [p.get("checks", [])])
            return [{"check_runs": page} for page in pages]
        if endpoint.endswith("/status?per_page=100"):
            return p.get("status", NO_STATUS)
        raise AssertionError(endpoint)


def do_wait(
    api: FakeApi, git: FakeGit, timeout: float = 1800
) -> tuple[str, list[float]]:
    now = [0.0]
    sleeps: list[float] = []

    def sleep(s: float) -> None:
        sleeps.append(s)
        now[0] += s

    sha = w.wait(
        api, git, 7, 60, timeout, sleep=sleep, clock=lambda: now[0], log=lambda _m: None
    )
    return sha, sleeps


FRONTEND = ".github/workflows/frontend.yml"


class TriggerParsing(unittest.TestCase):
    def test_block_filters(self) -> None:
        self.assertEqual(
            w.pull_request_filters(FRONTEND_YML),
            {
                "branches": ["main"],
                "paths": ["frontend/**", ".github/workflows/frontend.yml"],
            },
        )

    def test_no_pull_request_trigger(self) -> None:
        self.assertIsNone(w.pull_request_filters(BACKEND_YML))

    def test_pull_request_target_is_not_pull_request(self) -> None:
        self.assertIsNone(w.pull_request_filters(GUARD_YML))

    def test_inline_forms(self) -> None:
        self.assertEqual(w.pull_request_filters("on: pull_request\n"), {})
        self.assertEqual(w.pull_request_filters("on: [push, pull_request]\n"), {})
        self.assertIsNone(w.pull_request_filters("on: [push]\n"))

    def test_bare_pull_request_key(self) -> None:
        self.assertEqual(w.pull_request_filters("on:\n  pull_request:\n  push:\n"), {})

    def test_types_without_code_events(self) -> None:
        self.assertIsNone(
            w.pull_request_filters("on:\n  pull_request:\n    types: [closed]\n")
        )
        self.assertEqual(
            w.pull_request_filters(
                "on:\n  pull_request:\n    types: [opened, labeled]\n"
            ),
            {},
        )

    def test_unsupported_syntax_is_unknown(self) -> None:
        with self.assertRaises(w.Unknown):
            w.pull_request_filters("on:\n  pull_request:\n    paths: *shared\n")
        with self.assertRaises(w.Unknown):
            w.pull_request_filters("name: no trigger\n")


class Matching(unittest.TestCase):
    def test_glob(self) -> None:
        self.assertTrue(w.glob_regex("frontend/**").match("frontend/src/a/b.ts"))
        self.assertFalse(w.glob_regex("frontend/*").match("frontend/src/a.ts"))
        self.assertTrue(w.glob_regex("**/*.md").match("README.md"))
        self.assertTrue(w.glob_regex("**/*.md").match("docs/x/y.md"))
        self.assertTrue(
            w.glob_regex(".github/workflows/epic-guard*.yml").match(
                ".github/workflows/epic-guard-tests.yml"
            )
        )
        self.assertFalse(w.glob_regex("a.b").match("axb"))

    def test_negation_last_match_wins(self) -> None:
        self.assertFalse(w._matches(["docs/**", "!docs/keep.md"], "docs/keep.md"))
        self.assertTrue(w._matches(["docs/**", "!docs/keep.md"], "docs/other.md"))

    def test_filters(self) -> None:
        f = {"branches": ["main"], "paths": ["frontend/**"]}
        self.assertTrue(w.workflow_runs_for(f, "main", ["frontend/x.ts", "README.md"]))
        self.assertFalse(w.workflow_runs_for(f, "dev/x", ["frontend/x.ts"]))
        self.assertFalse(w.workflow_runs_for(f, "main", ["backend/x.rs"]))
        self.assertFalse(
            w.workflow_runs_for({"paths-ignore": ["docs/**"]}, "main", ["docs/a.md"])
        )
        self.assertTrue(
            w.workflow_runs_for(
                {"paths-ignore": ["docs/**"]}, "main", ["docs/a.md", "x"]
            )
        )
        self.assertFalse(
            w.workflow_runs_for({"branches-ignore": ["dev/**"]}, "dev/x", ["a"])
        )

    def test_too_many_files_means_expected(self) -> None:
        files = [f"backend/{i}.rs" for i in range(301)]
        self.assertTrue(w.workflow_runs_for({"paths": ["frontend/**"]}, "main", files))

    def test_expected_workflows(self) -> None:
        git = FakeGit(["frontend/src/a.ts"])
        self.assertEqual(w.expected_workflows(git, "main", SHA), {FRONTEND})
        self.assertIn(["diff", "--name-only", f"origin/main...{SHA}"], git.calls)
        self.assertEqual(
            w.expected_workflows(FakeGit(["backend/a.rs"]), "main", SHA), set()
        )

    def test_unparseable_workflow_is_expected(self) -> None:
        git = FakeGit(
            ["x"], {".github/workflows/odd.yml": "on:\n  pull_request: *anchor\n"}
        )
        self.assertEqual(
            w.expected_workflows(git, "main", SHA), {".github/workflows/odd.yml"}
        )


class Evaluate(unittest.TestCase):
    def test_pass(self) -> None:
        verdict, _ = w.evaluate(
            {FRONTEND}, [run(FRONTEND)], [check("build")], VERCEL_OK
        )
        self.assertEqual(verdict, "pass")

    def test_ok_conclusions(self) -> None:
        for c in ("success", "neutral", "skipped"):
            self.assertEqual(
                w.evaluate(set(), [], [check("x", conclusion=c)], NO_STATUS)[0], "pass"
            )

    def test_bad_conclusions_fail(self) -> None:
        for c in (
            "failure",
            "cancelled",
            "timed_out",
            "action_required",
            "stale",
            "startup_failure",
            None,
        ):
            with self.subTest(conclusion=c):
                verdict, reasons = w.evaluate(
                    set(), [], [check("x", conclusion=c)], NO_STATUS
                )
                self.assertEqual(verdict, "fail")
                self.assertIn("check x", reasons[0])

    def test_failed_workflow_run(self) -> None:
        verdict, _ = w.evaluate(
            {FRONTEND}, [run(FRONTEND, conclusion="failure")], [], NO_STATUS
        )
        self.assertEqual(verdict, "fail")

    def test_running(self) -> None:
        self.assertEqual(
            w.evaluate(
                set(),
                [],
                [check("x", status="in_progress", conclusion=None)],
                NO_STATUS,
            )[0],
            "pending",
        )
        self.assertEqual(
            w.evaluate(
                {FRONTEND},
                [run(FRONTEND, status="queued", conclusion=None)],
                [],
                NO_STATUS,
            )[0],
            "pending",
        )

    def test_expected_workflow_not_started(self) -> None:
        verdict, reasons = w.evaluate({FRONTEND}, [], [], VERCEL_OK)
        self.assertEqual(verdict, "pending")
        self.assertIn("has not started", reasons[0])

    def test_zero_statuses_ignored(self) -> None:
        self.assertEqual(w.evaluate(set(), [], [], NO_STATUS)[0], "pass")

    def test_statuses(self) -> None:
        pending = {
            "total_count": 1,
            "state": "pending",
            "statuses": [{"context": "Vercel", "state": "pending"}],
        }
        failing = {
            "total_count": 1,
            "state": "failure",
            "statuses": [{"context": "Vercel", "state": "error"}],
        }
        self.assertEqual(w.evaluate(set(), [], [], pending)[0], "pending")
        self.assertEqual(w.evaluate(set(), [], [], failing)[0], "fail")

    def test_latest_rerun_wins(self) -> None:
        runs = [run(FRONTEND, conclusion="failure", id=1), run(FRONTEND, id=2)]
        self.assertEqual(w.evaluate({FRONTEND}, runs, [], NO_STATUS)[0], "pass")


class Wait(unittest.TestCase):
    def test_pass_first_poll_returns_sha(self) -> None:
        api = FakeApi(
            [{"runs": [run(FRONTEND)], "checks": [check("build")], "status": VERCEL_OK}]
        )
        sha, sleeps = do_wait(api, FakeGit(["frontend/a.ts"]))
        self.assertEqual((sha, sleeps), (SHA, []))

    def test_no_frontend_change_needs_no_workflow(self) -> None:
        api = FakeApi([{"status": VERCEL_OK}])
        sha, sleeps = do_wait(api, FakeGit(["backend/a.rs"]))
        self.assertEqual((sha, sleeps), (SHA, []))

    def test_expected_check_shows_up_late(self) -> None:
        late = [{"status": VERCEL_OK}] * 3 + [
            {"runs": [run(FRONTEND)], "checks": [check("build")], "status": VERCEL_OK}
        ]
        sha, sleeps = do_wait(FakeApi(late), FakeGit(["frontend/a.ts"]))
        self.assertEqual((sha, sleeps), (SHA, [60, 60, 60]))

    def test_expected_check_never_shows_up_times_out(self) -> None:
        with self.assertRaises(w.TimedOut) as ctx:
            do_wait(
                FakeApi([{"status": VERCEL_OK}]),
                FakeGit(["frontend/a.ts"]),
                timeout=300,
            )
        self.assertIn("has not started", ctx.exception.args[0][0])

    def test_failure_on_second_page(self) -> None:
        page1 = [check(f"c{i}", id=i) for i in range(100)]
        api = FakeApi(
            [{"check_pages": [page1, [check("late", conclusion="failure", id=200)]]}]
        )
        with self.assertRaises(w.ChecksFailed) as ctx:
            do_wait(api, FakeGit(["README.md"]))
        self.assertEqual(ctx.exception.args[0], ["check late concluded failure"])

    def test_head_changed_starts_over(self) -> None:
        git = FakeGit(["frontend/a.ts"])
        api = FakeApi(
            [
                {"runs": [], "status": VERCEL_OK},
                {
                    "sha": NEW_SHA,
                    "runs": [run(FRONTEND)],
                    "checks": [check("build")],
                    "status": VERCEL_OK,
                },
            ]
        )
        sha, _ = do_wait(api, git)
        self.assertEqual(sha, NEW_SHA)
        self.assertEqual(sum(c[0] == "fetch" for c in git.calls), 2)
        self.assertTrue(any(NEW_SHA in e for e in api.endpoints if "check-runs" in e))

    def test_merge_conflict_fails_fast(self) -> None:
        with self.assertRaises(w.ChecksFailed):
            do_wait(FakeApi([{"mergeable": False}]), FakeGit(["frontend/a.ts"]))

    def test_unknown_mergeable_keeps_going(self) -> None:
        sha, _ = do_wait(FakeApi([{"mergeable": None}]), FakeGit(["backend/a.rs"]))
        self.assertEqual(sha, SHA)

    def test_closed_pr_fails(self) -> None:
        with self.assertRaises(w.ChecksFailed):
            do_wait(FakeApi([{"state": "closed"}]), FakeGit([]))

    def test_api_error_propagates(self) -> None:
        with self.assertRaises(w.ToolError):
            do_wait(FakeApi([{"error": "HTTP 502"}]), FakeGit([]))

    def test_rest_calls_per_poll(self) -> None:
        api = FakeApi(
            [{"status": VERCEL_OK}] * 3
            + [{"runs": [run(FRONTEND)], "status": VERCEL_OK}]
        )
        do_wait(api, FakeGit(["frontend/a.ts"]))
        self.assertEqual(len(api.endpoints), 4 * 4)
        self.assertFalse(any("graphql" in e for e in api.endpoints))


class Main(unittest.TestCase):
    def test_exit_codes(self) -> None:
        cases = [
            (lambda *a, **k: SHA, 0),
            (lambda *a, **k: (_ for _ in ()).throw(w.ChecksFailed(["x"])), 1),
            (lambda *a, **k: (_ for _ in ()).throw(w.TimedOut(["x"])), 2),
            (lambda *a, **k: (_ for _ in ()).throw(w.ToolError("boom")), 3),
        ]
        for fake, code in cases:
            with (
                self.subTest(code=code),
                patch.object(w, "wait", fake),
                patch("sys.stdout"),
                patch("sys.stderr"),
            ):
                self.assertEqual(w.main(["7"]), code)


if __name__ == "__main__":
    unittest.main()
