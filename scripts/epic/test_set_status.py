"""set_status.py tests. Run: python3 -m unittest discover -s scripts/epic"""

from __future__ import annotations

import isolated_env  # noqa: F401  (first: hides live runner state)

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import set_status as ss

REPO_URL = "https://github.com/phaabe/live.moafunk.de/issues/"
OPTIONS = {s: f"opt-{i}" for i, s in enumerate(ss.STATUSES)}


class FakeGh:
    """Board items and labels in memory, answering the `gh api` calls."""

    def __init__(self, status: str | None, labels: list[str], url: str = "") -> None:
        self.status = status
        self.labels = list(labels)
        self.url = url or f"{REPO_URL}7"
        self.calls: list[list[str]] = []
        self.added: list[str] = []
        self.fail: dict[str, str] = {}  # write kind -> "error" | "timeout"
        self.fail_reads = False
        self.after_board_write: str | None = None  # someone else moves it
        self.pulls: dict[int, str] = {}  # PR number -> body
        self.fail_pulls = False
        # Only the status::sync POST: "error", "timeout" (after writing) or
        # "lost" (answers ok, but the label never lands).
        self.fail_marker: str | None = None

    def item_row(self) -> dict[str, Any]:
        value = None if self.status is None else {"name": {"raw": self.status}}
        return {
            "id": 99,
            "content_type": "Issue",
            "content": {"number": 7, "html_url": self.url},
            "fields": [{"name": "Status", "value": value}],
        }

    def __call__(self, args: list[str], stdin: str | None) -> str:
        self.calls.append(args)
        if "--method" in args:
            return self.write(args, stdin)
        if self.fail_reads:
            raise RuntimeError("network down")
        endpoint = args[-1]
        if "/pulls/" in endpoint:
            if self.fail_pulls:
                raise RuntimeError("PR read failed")
            number = int(endpoint.rsplit("/", 1)[1])
            return json.dumps({"number": number, "body": self.pulls[number]})
        if "/fields" in endpoint:
            options = [{"id": v, "name": {"raw": k}} for k, v in OPTIONS.items()]
            return json.dumps([[{"id": 11, "name": "Status", "options": options}]])
        if "/items" in endpoint:
            return json.dumps([[self.item_row()]])
        if endpoint.startswith("repos/") and "/labels" in endpoint:
            return json.dumps([[{"name": n} for n in self.labels]])
        raise AssertionError(f"unexpected read {args}")

    def write(self, args: list[str], stdin: str | None) -> str:
        method = args[args.index("--method") + 1]
        kind = {"PATCH": "board", "POST": "add", "DELETE": "remove"}[method]
        mode = self.fail.get(kind)
        if kind == "add" and ss.MARKER in json.loads(stdin or "{}")["labels"]:
            mode = self.fail_marker or mode
        if mode == "error":
            raise RuntimeError(f"{kind} failed")
        if kind == "board":
            option = json.loads(stdin or "{}")["fields"][0]["value"]
            self.status = {v: k for k, v in OPTIONS.items()}[option]
            if self.after_board_write:
                self.status = self.after_board_write
        elif kind == "add":
            for name in json.loads(stdin or "{}")["labels"]:
                self.added.append(name)
                if mode == "lost":
                    continue
                if name not in self.labels:
                    self.labels.append(name)
        else:
            name = args[-1].rsplit("/", 1)[1].replace("%3A", ":")
            if name in self.labels:
                self.labels.remove(name)
        if mode == "timeout":
            raise TimeoutError(f"{kind} timed out after writing")
        return "{}"

    def label_writes(self) -> list[list[str]]:
        return [c for c in self.calls if "--method" in c and "PATCH" not in c]


class SetStatusTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        patcher = mock.patch.dict(
            os.environ, {"EPIC_LOCK_DIR": str(self.tmp / "locks")}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_main(self, argv: list[str], gh: FakeGh, env: dict[str, str] | None = None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ss.main(argv, ss.Board(gh), env or {})
        lines = out.getvalue().splitlines()
        return code, (json.loads(lines[-1]) if lines else None), err.getvalue()

    def tick_env(self, issue: int = 7) -> dict[str, str]:
        action = self.tmp / "action.json"
        action.write_text(
            json.dumps({"action": "continue", "issue": f"{REPO_URL}{issue}"})
        )
        return {"EPIC_ACTION_FILE": str(action)}

    def test_label_names(self) -> None:
        self.assertEqual(ss.label_for("In progress"), "status::in-progress")
        self.assertEqual(ss.label_for("Done"), "status::done")

    def test_set_writes_board_then_swaps_label(self) -> None:
        gh = FakeGh("Ready", ["type::ci", "status::ready"])
        code, out, _ = self.run_main(["set", "7", "In progress"], gh)
        self.assertEqual(code, ss.OK)
        self.assertEqual(gh.status, "In progress")
        self.assertEqual(sorted(gh.labels), ["status::in-progress", "type::ci"])
        self.assertEqual(
            out,
            {
                "issue": 7,
                "mode": "set",
                "from": "Ready",
                "to": "In progress",
                "board": "ok",
                "label": "ok",
            },
        )
        methods = [c[c.index("--method") + 1] for c in gh.calls if "--method" in c]
        self.assertEqual(methods, ["PATCH", "POST", "DELETE"])

    def test_set_repeat_writes_nothing(self) -> None:
        gh = FakeGh("In progress", ["status::in-progress"])
        code, out, _ = self.run_main(["set", "7", "In progress"], gh)
        self.assertEqual(code, ss.OK)
        self.assertEqual([c for c in gh.calls if "--method" in c], [])
        self.assertEqual(out["board"], "ok")

    def test_set_refused_after_crashed_sync(self) -> None:
        gh = FakeGh("Ready", ["status::ready", "status::sync"])
        code, _, err = self.run_main(["set", "7", "Done"], gh)
        self.assertEqual(code, ss.NOT_CLEAN)
        self.assertIn("run repair first", err)
        self.assertEqual(gh.status, "Ready")
        self.assertEqual([c for c in gh.calls if "--method" in c], [])

    def test_set_refused_after_board_ok_label_fail(self) -> None:
        gh = FakeGh("In progress", ["status::ready"])
        code, _, _ = self.run_main(["set", "7", "In review"], gh)
        self.assertEqual(code, ss.NOT_CLEAN)
        self.assertEqual(gh.status, "In progress")

    def test_set_refused_without_or_with_two_labels(self) -> None:
        for labels in ([], ["status::ready", "status::done"]):
            gh = FakeGh("Ready", labels)
            code, _, _ = self.run_main(["set", "7", "Done"], gh)
            self.assertEqual(code, ss.NOT_CLEAN, labels)

    def test_label_fail_then_repair(self) -> None:
        gh = FakeGh("Ready", ["status::ready"])
        gh.fail["add"] = "error"
        code, out, _ = self.run_main(["set", "7", "Done"], gh)
        self.assertEqual(code, ss.FAILED)
        self.assertEqual((out["board"], out["label"]), ("ok", "failed"))
        gh.fail.clear()
        code, out, _ = self.run_main(["repair", "7"], gh, self.tick_env())
        self.assertEqual(code, ss.OK)
        self.assertEqual(gh.labels, ["status::done"])
        self.assertEqual(out["mode"], "repair")

    def test_board_moved_during_call_is_a_conflict(self) -> None:
        gh = FakeGh("Ready", ["status::ready"])
        gh.after_board_write = "Backlog"
        code, _, err = self.run_main(["set", "7", "In progress"], gh)
        self.assertEqual(code, ss.CONFLICT)
        self.assertIn("conflict", err)
        self.assertEqual(gh.label_writes(), [])

    def test_timeout_after_successful_write_is_decided_by_read(self) -> None:
        gh = FakeGh("Ready", ["status::ready"])
        gh.fail["board"] = "timeout"
        code, out, _ = self.run_main(["set", "7", "In review"], gh)
        self.assertEqual(code, ss.OK)
        self.assertEqual((out["board"], out["label"]), ("ok", "ok"))

    def test_failed_board_write_writes_no_label(self) -> None:
        gh = FakeGh("Ready", ["status::ready"])
        gh.fail["board"] = "error"
        code, out, _ = self.run_main(["set", "7", "In review"], gh)
        self.assertEqual(code, ss.FAILED)
        self.assertEqual(out["board"], "failed")
        self.assertEqual(gh.label_writes(), [])

    def test_unreadable_result_is_unknown(self) -> None:
        gh = FakeGh("Ready", ["status::ready"])
        real = gh.write

        def write_then_go_dark(args: list[str], stdin: str | None) -> str:
            gh.fail_reads = True
            real(args, stdin)
            raise TimeoutError("timed out")

        gh.write = write_then_go_dark  # type: ignore[method-assign]
        code, out, _ = self.run_main(["set", "7", "Done"], gh)
        self.assertEqual(code, ss.UNKNOWN)
        self.assertEqual(out["board"], "unknown")
        self.assertNotIn("ok", (out["board"], out["label"]))

    def test_sync_uses_marker_window(self) -> None:
        gh = FakeGh("Done", ["status::ready", "type::ci"])
        code, out, _ = self.run_main(["sync", "7"], gh)
        self.assertEqual(code, ss.OK)
        self.assertEqual(sorted(gh.labels), ["status::done", "type::ci"])
        writes = gh.label_writes()
        self.assertEqual(gh.added[0], "status::sync")
        self.assertTrue(writes[-1][-1].endswith("status%3A%3Async"))
        self.assertFalse([c for c in gh.calls if "PATCH" in c])
        self.assertEqual(out["from"], "status::ready")

    def test_sync_after_crash_removes_marker(self) -> None:
        gh = FakeGh("Ready", ["status::ready", "status::sync"])
        code, _, _ = self.run_main(["sync", "7"], gh)
        self.assertEqual(code, ss.OK)
        self.assertEqual(gh.labels, ["status::ready"])

    def test_sync_of_clean_issue_writes_nothing(self) -> None:
        gh = FakeGh("Ready", ["status::ready"])
        code, _, _ = self.run_main(["sync", "7"], gh)
        self.assertEqual(code, ss.OK)
        self.assertEqual(gh.label_writes(), [])

    def test_sync_refused_inside_a_tick(self) -> None:
        gh = FakeGh("Ready", [])
        code, _, err = self.run_main(["sync", "7"], gh, self.tick_env())
        self.assertEqual(code, ss.USAGE)
        self.assertIn("repair", err)
        self.assertEqual(gh.calls, [])

    def test_sync_stops_when_the_marker_is_not_added(self) -> None:
        # Only the marker write fails, or it answers ok and never lands: no
        # status label may change, else the swap reads as a real change.
        for mode in ("error", "lost"):
            with self.subTest(mode=mode):
                gh = FakeGh("Done", ["status::ready"])
                gh.fail_marker = mode
                code, out, err = self.run_main(["repair", "7"], gh, self.tick_env())
                self.assertEqual(code, ss.FAILED)
                self.assertEqual((out["board"], out["label"]), ("ok", "failed"))
                self.assertIn("status::sync", err)
                self.assertEqual(gh.labels, ["status::ready"])
                self.assertNotIn("status::done", gh.added)
                posts = [c for c in gh.calls if "POST" in c]
                self.assertEqual(len(posts), 1)  # the marker only
                self.assertFalse([c for c in gh.calls if "DELETE" in c])

    def test_sync_goes_on_after_a_timeout_that_wrote_the_marker(self) -> None:
        gh = FakeGh("Done", ["status::ready"])
        gh.fail_marker = "timeout"
        code, out, _ = self.run_main(["repair", "7"], gh, self.tick_env())
        self.assertEqual(code, ss.OK)
        self.assertEqual(gh.labels, ["status::done"])
        self.assertEqual(gh.added, ["status::sync", "status::done"])

    def test_sync_is_unknown_when_the_marker_cannot_be_read(self) -> None:
        gh = FakeGh("Done", ["status::ready"])
        real = gh.write

        def mark_then_go_dark(args: list[str], stdin: str | None) -> str:
            gh.fail_reads = True
            real(args, stdin)
            raise TimeoutError("timed out")

        gh.write = mark_then_go_dark  # type: ignore[method-assign]
        code, out, _ = self.run_main(["repair", "7"], gh, self.tick_env())
        self.assertEqual(code, ss.UNKNOWN)
        self.assertEqual((out["board"], out["label"]), ("ok", "unknown"))
        self.assertEqual(gh.added, ["status::sync"])
        self.assertIn("status::ready", gh.labels)

    def pr_tick(self, kind: str, pr: int = 5) -> dict[str, str]:
        """The selector's PR action: a PR and a SHA, no issue."""
        action = self.tmp / "action.json"
        action.write_text(json.dumps({"action": kind, "pr": pr, "sha": "a" * 40}))
        return {"EPIC_ACTION_FILE": str(action)}

    def test_pr_tick_may_change_the_prs_issue_end_to_end(self) -> None:
        import write_checks as wc

        body = f"Executor: Claude\nIssue: {REPO_URL}7\nRefs: {REPO_URL}8"
        for kind, argv, status in (
            ("continue", ["set", "7", "Done"], "In progress"),
            ("fix", ["repair", "7"], "Done"),
        ):
            with self.subTest(kind=kind):
                env = self.pr_tick(kind)
                action = json.loads(Path(env["EPIC_ACTION_FILE"]).read_text())
                reader = mock.Mock()
                reader.pull.return_value = {"number": 5, "body": body}
                reader.board_items.return_value = [
                    {
                        "status": status,
                        "executor": "Claude",
                        "content": {"number": n, "url": f"{REPO_URL}{n}"},
                    }
                    for n in (7, 8)
                ]
                ctx = wc.Context(action, lambda: reader)
                command = f"python3 {wc.STATUS_HELPER} {' '.join(argv)}"
                (write,) = wc.bash_writes(command, str(self.tmp))
                self.assertIsNone(wc.status_rule(write, kind))
                self.assertIsNone(wc.check_status(ctx, write))

                gh = FakeGh(
                    status,
                    ["status::ready" if kind == "fix" else "status::in-progress"],
                )
                gh.pulls[5] = body
                code, out, err = self.run_main(argv, gh, env)
                self.assertEqual(code, ss.OK, err)
                self.assertEqual((out["board"], out["label"]), ("ok", "ok"))

                # An issue the PR only links (no `Issue:` line) stays refused.
                other = [argv[0], "8", *argv[2:]]
                (write,) = wc.bash_writes(
                    f"python3 {wc.STATUS_HELPER} {' '.join(other)}", str(self.tmp)
                )
                self.assertIsNotNone(wc.check_status(ctx, write))
                gh = FakeGh(status, ["status::in-progress"])
                gh.pulls[5] = body
                code, _, err = self.run_main(other, gh, env)
                self.assertEqual(code, ss.USAGE)
                self.assertIn("not this tick's target", err)
                self.assertFalse([c for c in gh.calls if "--method" in c])

    def test_pr_tick_takes_the_issue_lock_the_runner_does_not_hold(self) -> None:
        env = self.pr_tick("continue")
        pr_lock = ss.lock(self.tmp / "locks" / "5.lock")  # the runner's lock
        self.addCleanup(os.close, pr_lock)
        gh = FakeGh("In progress", ["status::in-progress"])
        gh.pulls[5] = f"Issue: {REPO_URL}7"
        held = ss.lock(self.tmp / "locks" / "7.lock")  # another runner on 7
        code, _, _ = self.run_main(["set", "7", "Done"], gh, env)
        self.assertEqual(code, ss.BUSY)
        self.assertFalse([c for c in gh.calls if "--method" in c])
        os.close(held)
        code, _, _ = self.run_main(["set", "7", "Done"], gh, env)
        self.assertEqual(code, ss.OK)
        self.assertEqual(gh.status, "Done")

    def test_pr_tick_with_an_unreadable_pr_is_refused(self) -> None:
        gh = FakeGh("In progress", ["status::in-progress"])
        gh.fail_pulls = True
        code, _, err = self.run_main(["set", "7", "Done"], gh, self.pr_tick("continue"))
        self.assertEqual(code, ss.USAGE)
        self.assertIn("cannot read PR 5", err)
        self.assertFalse([c for c in gh.calls if "--method" in c])

    def test_wrong_tick_target_refused(self) -> None:
        gh = FakeGh("Ready", ["status::ready"])
        code, _, _ = self.run_main(["set", "7", "Done"], gh, self.tick_env(issue=8))
        self.assertEqual(code, ss.USAGE)
        self.assertEqual(gh.calls, [])

    def test_inside_a_tick_the_held_tick_lock_does_not_block(self) -> None:
        held = ss.lock(self.tmp / "locks" / "7.lock")
        self.addCleanup(os.close, held)
        gh = FakeGh("Ready", ["status::ready"])
        code, _, _ = self.run_main(["set", "7", "Done"], gh, self.tick_env())
        self.assertEqual(code, ss.OK)

    def test_busy_tick_lock_outside_a_tick(self) -> None:
        held = ss.lock(self.tmp / "locks" / "7.lock")
        self.addCleanup(os.close, held)
        gh = FakeGh("Ready", ["status::ready"])
        code, _, _ = self.run_main(["set", "7", "Done"], gh)
        self.assertEqual(code, ss.BUSY)
        self.assertEqual(gh.calls, [])

    def test_two_helpers_at_once_exit_busy(self) -> None:
        held = ss.lock(self.tmp / "locks" / "status-7.lock")
        self.addCleanup(os.close, held)
        gh = FakeGh("Ready", ["status::ready"])
        for env in ({}, self.tick_env()):
            code, _, _ = self.run_main(["set", "7", "Done"], gh, env)
            self.assertEqual(code, ss.BUSY)
        self.assertEqual(gh.calls, [])

    def test_wrong_repo_or_not_on_board(self) -> None:
        gh = FakeGh(
            "Ready", ["status::ready"], url="https://github.com/other/repo/issues/7"
        )
        code, out, err = self.run_main(["set", "7", "Done"], gh)
        self.assertEqual(code, ss.USAGE)
        self.assertIsNone(out)
        self.assertIn("not on board", err)

    def test_malformed_arguments(self) -> None:
        for argv in (
            [],
            ["move", "7"],
            ["set", "7"],
            ["set", "7", "Doing"],
            ["set", "x", "Done"],
            ["repair"],
            ["repair", "7", "8"],
            ["sync"],
            ["set", "-7", "Done"],
        ):
            code, _, _ = self.run_main(argv, FakeGh("Ready", []))
            self.assertEqual(code, ss.USAGE, argv)


if __name__ == "__main__":
    unittest.main()
