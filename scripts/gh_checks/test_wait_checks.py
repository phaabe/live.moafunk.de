#!/usr/bin/env python3
from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

import wait_checks as w

SHA = "a" * 40
NEW_SHA = "b" * 40
FRONTEND = ".github/workflows/frontend.yml"

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
    pr: int | None = 7,
):
    return {
        "id": id,
        "path": path,
        "status": status,
        "conclusion": conclusion,
        "pull_requests": [{"number": pr}] if pr is not None else [],
    }


def check(
    name: str,
    status: str = "completed",
    conclusion: str | None = "success",
    id: int = 1,
    app: str = "github-actions",
):
    return {
        "id": id,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "app": {"slug": app},
    }


def statuses(
    state: str, *contexts: tuple[str, str], total: int | None = None
) -> dict[str, Any]:
    items = [{"context": c, "state": s} for c, s in contexts]
    return {
        "total_count": len(items) if total is None else total,
        "state": state,
        "statuses": items,
    }


NO_STATUS = statuses("pending")
VERCEL_OK = statuses("success", ("Vercel", "success"))


class FakeGit:
    def __init__(
        self, files: list[str], workflows: dict[str, str] | None = None
    ) -> None:
        self.files = files
        self.workflows = (
            workflows
            if workflows is not None
            else {
                FRONTEND: FRONTEND_YML,
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
            assert "-z" in args
            return "".join(f + "\0" for f in self.files)
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
                "base": {"ref": p.get("base", "main")},
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
        api, git, 7, 60, timeout, sleep=sleep, clock=lambda: now[0], log=lambda _: None
    )
    return sha, sleeps


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

    def test_quoted_block_items(self) -> None:
        text = "on:\n  pull_request:\n    paths:\n      - 'frontend/a,b.ts'\n"
        self.assertEqual(w.pull_request_filters(text), {"paths": ["frontend/a,b.ts"]})

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
        for text in (
            "on:\n  pull_request:\n    paths: *shared\n",
            "name: no trigger\n",
            "on: {pull_request: {}}\n",
            'on:\n  pull_request:\n    paths: ["frontend/a,b.ts"]\n',
            "on:\n  pull_request:\n    branches: main\n",
        ):
            with self.subTest(text=text), self.assertRaises(w.Unknown):
                w.pull_request_filters(text)


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

    def test_github_quantifiers_and_classes(self) -> None:
        self.assertTrue(w.glob_regex("*.jsx?").match("page.js"))
        self.assertTrue(w.glob_regex("*.jsx?").match("page.jsx"))
        self.assertFalse(w.glob_regex("*.jsx?").match("page.jsxx"))
        self.assertTrue(w.glob_regex("v1+.txt").match("v111.txt"))
        self.assertFalse(w.glob_regex("v1+.txt").match("v.txt"))
        self.assertTrue(w.glob_regex("release-[0-9].x").match("release-7.x"))
        self.assertFalse(w.glob_regex("release-[0-9].x").match("release-a.x"))
        self.assertTrue(w.glob_regex("a\\*b").match("a*b"))
        self.assertFalse(w.glob_regex("a\\*b").match("axb"))

    def test_unsupported_patterns_are_unknown(self) -> None:
        for p in ("?abc", "**+", "a[b"):
            with self.subTest(pattern=p), self.assertRaises(w.Unknown):
                w.glob_regex(p)

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

    def test_many_files_keep_certain_exclusions(self) -> None:
        backend_only = [f"backend/{i}.rs" for i in range(301)]
        self.assertFalse(
            w.workflow_runs_for({"paths": ["frontend/**"]}, "main", backend_only)
        )
        self.assertTrue(
            w.workflow_runs_for(
                {"paths": ["frontend/**"]}, "main", [*backend_only, "frontend/a.ts"]
            )
        )

    def test_expected_workflows(self) -> None:
        git = FakeGit(["frontend/src/a.ts"])
        self.assertEqual(w.expected_workflows(git, "main", SHA), {FRONTEND})
        self.assertIn(["diff", "--name-only", "-z", f"origin/main...{SHA}"], git.calls)
        self.assertEqual(
            w.expected_workflows(FakeGit(["backend/a.rs"]), "main", SHA), set()
        )

    def test_unusual_file_names(self) -> None:
        for name in ("frontend/ä.ts", "frontend/new\nline.ts", 'frontend/"q".ts'):
            with self.subTest(name=name):
                self.assertEqual(
                    w.expected_workflows(FakeGit([name]), "main", SHA), {FRONTEND}
                )

    def test_unparseable_workflow_is_expected(self) -> None:
        git = FakeGit(
            ["x"], {".github/workflows/odd.yml": "on:\n  pull_request: *anchor\n"}
        )
        self.assertEqual(
            w.expected_workflows(git, "main", SHA), {".github/workflows/odd.yml"}
        )

    def test_unsupported_pattern_is_expected(self) -> None:
        odd = "on:\n  pull_request:\n    paths:\n      - '?weird'\n"
        git = FakeGit(["backend/a.rs"], {".github/workflows/odd.yml": odd})
        self.assertEqual(
            w.expected_workflows(git, "main", SHA), {".github/workflows/odd.yml"}
        )


class Evaluate(unittest.TestCase):
    def test_pass(self) -> None:
        self.assertEqual(
            w.evaluate({FRONTEND}, [run(FRONTEND)], [check("build")], VERCEL_OK)[0],
            "pass",
        )

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
                self.assertIn("check github-actions/x", reasons[0])

    def test_failed_workflow_run(self) -> None:
        self.assertEqual(
            w.evaluate(
                {FRONTEND}, [run(FRONTEND, conclusion="failure")], [], NO_STATUS
            )[0],
            "fail",
        )

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
        self.assertEqual(
            w.evaluate(set(), [], [], statuses("pending", ("Vercel", "pending")))[0],
            "pending",
        )
        self.assertEqual(
            w.evaluate(set(), [], [], statuses("failure", ("Vercel", "error")))[0],
            "fail",
        )

    def test_combined_state_covers_statuses_past_first_page(self) -> None:
        visible = [(f"ctx{i}", "success") for i in range(100)]
        failing = statuses("failure", *visible, total=101)
        pending = statuses("pending", *visible, total=101)
        self.assertEqual(w.evaluate(set(), [], [], failing)[0], "fail")
        self.assertEqual(w.evaluate(set(), [], [], pending)[0], "pending")

    def test_latest_rerun_wins(self) -> None:
        runs = [run(FRONTEND, conclusion="failure", id=1), run(FRONTEND, id=2)]
        self.assertEqual(w.evaluate({FRONTEND}, runs, [], NO_STATUS)[0], "pass")
        checks = [check("build", conclusion="failure", id=1), check("build", id=2)]
        self.assertEqual(w.evaluate(set(), [], checks, NO_STATUS)[0], "pass")

    def test_same_name_from_other_app_is_a_separate_check(self) -> None:
        checks = [
            check("build", conclusion="failure", id=1, app="ci-a"),
            check("build", id=2, app="ci-b"),
        ]
        verdict, reasons = w.evaluate(set(), [], checks, NO_STATUS)
        self.assertEqual(
            (verdict, reasons), ("fail", ["check ci-a/build concluded failure"])
        )


class Wait(unittest.TestCase):
    def test_pass_first_poll_returns_sha(self) -> None:
        api = FakeApi(
            [{"runs": [run(FRONTEND)], "checks": [check("build")], "status": VERCEL_OK}]
        )
        self.assertEqual(do_wait(api, FakeGit(["frontend/a.ts"])), (SHA, []))

    def test_no_frontend_change_needs_no_workflow(self) -> None:
        self.assertEqual(
            do_wait(FakeApi([{"status": VERCEL_OK}]), FakeGit(["backend/a.rs"])),
            (SHA, []),
        )

    def test_expected_check_shows_up_late(self) -> None:
        late = [{"status": VERCEL_OK}] * 3 + [
            {"runs": [run(FRONTEND)], "checks": [check("build")], "status": VERCEL_OK}
        ]
        self.assertEqual(
            do_wait(FakeApi(late), FakeGit(["frontend/a.ts"])), (SHA, [60, 60, 60])
        )

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
        self.assertEqual(
            ctx.exception.args[0], ["check github-actions/late concluded failure"]
        )

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

    def test_retarget_same_head_recomputes_expected(self) -> None:
        # dev base: frontend.yml doesn't apply. After retarget to main it must run.
        api = FakeApi(
            [
                {"base": "dev/example", "status": VERCEL_OK, "mergeable": None},
                {"base": "main", "status": VERCEL_OK},
                {"base": "main", "runs": [run(FRONTEND)], "status": VERCEL_OK},
            ]
        )
        sha, sleeps = do_wait(api, FakeGit(["frontend/a.ts"]))
        self.assertEqual((sha, sleeps), (SHA, [60, 60]))

    def test_run_of_other_pr_with_same_sha_does_not_count(self) -> None:
        other = {"runs": [run(FRONTEND, pr=8)], "status": VERCEL_OK}
        mine = {
            "runs": [run(FRONTEND, pr=8, id=1), run(FRONTEND, id=2)],
            "status": VERCEL_OK,
        }
        sha, sleeps = do_wait(FakeApi([other, other, mine]), FakeGit(["frontend/a.ts"]))
        self.assertEqual((sha, sleeps), (SHA, [60, 60]))
        with self.assertRaises(w.TimedOut):
            do_wait(FakeApi([other]), FakeGit(["frontend/a.ts"]), timeout=120)

    def test_fork_run_without_pr_link_counts(self) -> None:
        api = FakeApi([{"runs": [run(FRONTEND, pr=None)], "status": VERCEL_OK}])
        self.assertEqual(do_wait(api, FakeGit(["frontend/a.ts"])), (SHA, []))

    def test_merge_conflict_fails_fast(self) -> None:
        with self.assertRaises(w.ChecksFailed):
            do_wait(FakeApi([{"mergeable": False}]), FakeGit(["frontend/a.ts"]))

    def test_unknown_mergeable_waits(self) -> None:
        api = FakeApi([{"mergeable": None}, {"mergeable": None}, {"mergeable": True}])
        self.assertEqual(do_wait(api, FakeGit(["backend/a.rs"])), (SHA, [60, 60]))

    def test_unknown_then_conflict_fails(self) -> None:
        with self.assertRaises(w.ChecksFailed):
            do_wait(
                FakeApi([{"mergeable": None}, {"mergeable": False}]),
                FakeGit(["backend/a.rs"]),
            )

    def test_unknown_mergeable_times_out(self) -> None:
        with self.assertRaises(w.TimedOut) as ctx:
            do_wait(
                FakeApi([{"mergeable": None}]), FakeGit(["backend/a.rs"]), timeout=120
            )
        self.assertIn("mergeability", ctx.exception.args[0][0])

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
        def raises(exc: Exception):
            def fake(*_a: Any, **_k: Any) -> str:
                raise exc

            return fake

        cases = [
            (lambda *_a, **_k: SHA, 0),
            (raises(w.ChecksFailed(["x"])), 1),
            (raises(w.TimedOut(["x"])), 2),
            (raises(w.ToolError("boom")), 3),
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
