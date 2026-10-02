"""Fresh checks before each runner write (write_checks.py), the permission gate
and the epic-guard hook that call them.

Run: python3 -m unittest discover -s scripts/epic
"""

from __future__ import annotations

import isolated_env  # noqa: F401  (first: hides live runner state)

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import github_state as gs
import next_action as na
import permission_gate
import runtime
import write_checks as wc

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
A = "a" * 40
B = "b" * 40
ISSUES = f"https://github.com/{na.REPO}/issues"


def pull(n: int, body: str = "Executor: Claude", **kw: Any) -> dict[str, Any]:
    data = {
        "number": n,
        "body": body,
        "state": "open",
        "draft": False,
        "merged_at": None,
        "labels": [],
        "head": {"ref": f"feat/{n}-x", "sha": A},
    }
    data.update(kw)
    return data


class FakeReader:
    """Stands in for github_state.FreshReader; counts reads."""

    def __init__(self) -> None:
        self.pulls: dict[int, dict[str, Any]] = {}
        self.issues: dict[int, dict[str, Any]] = {}
        self.items: list[dict[str, Any]] = []
        self.branch_pulls: list[dict[str, Any]] = []
        self.guard_errors: list[str] = []
        self.fail: Exception | None = None
        self.reads = 0

    def _read(self) -> None:
        self.reads += 1
        if self.fail:
            raise self.fail

    def pull(self, n: int) -> dict[str, Any]:
        self._read()
        return self.pulls[n]

    def issue(self, n: int) -> dict[str, Any]:
        self._read()
        return self.issues.get(n, {"number": n, "labels": []})

    def board_items(self) -> list[dict[str, Any]]:
        self._read()
        return self.items

    def pulls_for_branch(self, branch: str) -> list[dict[str, Any]]:
        self._read()
        return self.branch_pulls

    def merge_errors(self, n: int, head: str) -> list[str]:
        self._read()
        return self.guard_errors


def item(n: int, status: str, executor: str = "Claude") -> dict[str, Any]:
    return {
        "id": f"PVTI_{n}",
        "rest_id": 50_000 + n,
        "status": status,
        "executor": executor,
        "content": {"type": "Issue", "number": n, "url": f"{ISSUES}/{n}"},
    }


class Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="write-checks-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.action_file = self.root / "action.json"
        env = patch.dict(
            os.environ,
            {"EPIC_SHARED_READER": "1", "EPIC_ACTION_FILE": str(self.action_file)},
        )
        env.start()
        self.addCleanup(env.stop)
        for name, value in (
            ("FOCUS_FILE", self.root / "focus"),
            ("PAUSE_FILE", self.root / "pause"),
        ):
            p = patch.object(na, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.reader = FakeReader()

    def action(self, **action: Any) -> None:
        self.action_file.write_text(json.dumps({"reason": "r", **action}))

    def bash(
        self, command: str, cwd: str | None = None, agent: str | None = None
    ) -> str | None:
        return wc.guard(
            "Bash",
            {"command": command},
            cwd or str(self.root),
            lambda: self.reader,
            agent,
        )


class Classify(unittest.TestCase):
    def writes(self, command: str, cwd: str = "/tmp") -> list[tuple[Any, ...]]:
        return [
            (w.kind, w.number, w.sha, w.branch, w.delete)
            for w in wc.bash_writes(command, cwd)
        ]

    def test_reads_are_not_writes(self) -> None:
        for command in (
            "gh pr view 5 --json headRefOid",
            "sha=$(gh api repos/phaabe/live.moafunk.de/pulls/5 --jq .head.sha)",
            "gh api graphql -f query='query{viewer{login}}'",
            "git fetch -q origin && git log --oneline -3",
            "gh project item-list 2 --owner anneoneone",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.writes(command), [])

    def test_pushes(self) -> None:
        self.assertEqual(
            self.writes("git push -u origin feat/21-x"),
            [("push", None, None, "feat/21-x", False)],
        )
        self.assertEqual(
            self.writes("git push origin --delete feat/5-x"),
            [("push", None, None, "feat/5-x", True)],
        )
        self.assertEqual(
            self.writes("git push --force-with-lease origin +HEAD:refs/heads/feat/5-x"),
            [("push", None, None, "feat/5-x", False)],
        )
        self.assertEqual(
            self.writes("cd wt && git -C sub push origin fix/5-y; echo done"),
            [("push", None, None, "fix/5-y", False)],
        )

    def test_push_of_head_uses_the_current_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q", "-b", "feat/9-z", tmp], check=True)
            self.assertEqual(self.writes("git push origin HEAD", tmp)[0][3], "feat/9-z")

    def test_gh_writes(self) -> None:
        verdict = f"Review: APPROVED by Claude at {A}"
        cases = {
            f"gh pr comment 5 --body '{verdict}'": ("verdict", 5, A),
            "gh pr comment 5 --body 'done, new head'": ("comment", 5, None),
            f"gh pr merge 5 --repo {na.REPO} --squash --match-head-commit {A}": (
                "merge",
                5,
                A,
            ),
            "gh pr ready 5": ("pr-write", 5, None),
            "gh pr edit 5 --add-label needs-anton": ("pr-write", 5, None),
            "gh issue comment 21 --body claim": ("comment", 21, None),
            "gh pr create --draft --base dev/312-interim --title t --body b": (
                "pr-create",
                None,
                None,
            ),
            "gh project item-edit --id X --field-id Y --single-select-option-id Z": (
                "board",
                None,
                None,
            ),
            "gh api --method PATCH users/anneoneone/projectsV2/2/items/1 --input -": (
                "board",
                None,
                None,
            ),
            f"gh api repos/{na.REPO}/issues/5/comments -f body='{verdict}'": (
                "verdict",
                5,
                A,
            ),
            f"gh api --method PATCH repos/{na.REPO}/pulls/5 -F body=@b.md": (
                "pr-write",
                5,
                None,
            ),
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertEqual(self.writes(command)[0][:3], expected)

    def test_verdict_in_a_body_file_or_heredoc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "v.md").write_text(f"Review: CHANGES REQUESTED by Claude at {B}")
            got = self.writes("gh pr comment 5 --body-file v.md", tmp)
            self.assertEqual(got[0][:3], ("verdict", 5, B))
        heredoc = (
            "gh pr comment 5 --body-file - <<'EOF'\n"
            f"Review: APPROVED by Claude at {A}\n"
            "EOF\n"
            "git push origin feat/5-x"
        )
        self.assertEqual([w[0] for w in self.writes(heredoc)], ["verdict", "push"])

    def test_unreadable_writes_are_refused(self) -> None:
        for command in (
            "out=$(gh pr merge 5 --squash)",
            "bash -c 'git push origin feat/5-x'",
            "git push --all origin",
            f"gh api --method PATCH repos/{na.REPO}/issues/comments/9 -f body=x",
        ):
            with self.subTest(command=command):
                with self.assertRaises(wc.Unclear):
                    wc.bash_writes(command, "/tmp")

    def test_mcp_tools(self) -> None:
        got = wc.tool_writes(
            "mcp__github__add_issue_comment",
            {"issue_number": 5, "body": f"Review: APPROVED by Claude at {A}"},
            "/tmp",
        )
        self.assertEqual((got[0].kind, got[0].number, got[0].sha), ("verdict", 5, A))


class Switch(Base):
    def test_off_or_interactive_checks_nothing(self) -> None:
        self.action(action="review", pr=5, sha=A)
        self.reader.fail = gs.ReadBlocked("must not read")
        for env in ({"EPIC_SHARED_READER": "0"}, {"EPIC_ACTION_FILE": ""}):
            with patch.dict(os.environ, env):
                self.assertIsNone(self.bash("git push origin feat/1-x"))
        self.assertEqual(self.reader.reads, 0)

    def test_reads_are_allowed_without_fresh_reads(self) -> None:
        self.action(action="review", pr=5, sha=A)
        self.assertIsNone(self.bash("gh pr view 5"))
        self.assertEqual(self.reader.reads, 0)


class Verdicts(Base):
    def setUp(self) -> None:
        super().setUp()
        self.action(action="review", pr=5, sha=A)
        self.reader.pulls[5] = pull(5, "Executor: Codex")
        self.cmd = f"gh pr comment 5 --body 'Review: APPROVED by Claude at {A}'"

    def test_valid_verdict(self) -> None:
        self.assertIsNone(self.bash(self.cmd))

    def test_head_moved(self) -> None:
        self.reader.pulls[5]["head"]["sha"] = B
        self.assertIn("head moved", self.bash(self.cmd) or "")

    def test_draft_or_closed(self) -> None:
        self.reader.pulls[5]["draft"] = True
        self.assertIsNotNone(self.bash(self.cmd))
        self.reader.pulls[5].update(draft=False, state="closed")
        self.assertIsNotNone(self.bash(self.cmd))

    def test_other_head_or_other_tick(self) -> None:
        other = f"gh pr comment 5 --body 'Review: APPROVED by Claude at {B}'"
        self.assertIn("another head", self.bash(other) or "")
        self.action(action="fix", pr=5, sha=A)
        self.reader.pulls[5]["body"] = "Executor: Claude"
        self.assertIn("not this tick", self.bash(self.cmd) or "")

    def test_failed_read_refuses(self) -> None:
        self.reader.fail = gs.ReadBlocked("HTTP 502")
        self.assertIn("fresh GitHub read failed", self.bash(self.cmd) or "")

    def test_pause_refuses(self) -> None:
        na.PAUSE_FILE.write_text("")
        self.assertIn("pause", self.bash(self.cmd) or "")

    def test_focus_refuses(self) -> None:
        na.FOCUS_FILE.write_text("project::Stream\n")
        self.assertIn("focus", self.bash(self.cmd) or "")
        self.reader.pulls[5]["labels"] = [{"name": "project::Stream"}]
        self.assertIsNone(self.bash(self.cmd))

    def test_wrong_assignment_refuses(self) -> None:
        self.reader.pulls[5]["body"] = "Executor: Claude"
        self.assertIn("Executor", self.bash(self.cmd) or "")


class Comments(Base):
    def test_targets(self) -> None:
        self.action(action="merge", pr=5, sha=A)
        self.reader.pulls[5] = pull(5, f"Executor: Claude\nIssue: {ISSUES}/21")
        self.assertIsNone(self.bash("gh issue comment 21 --body 'merged in ...'"))
        self.assertIsNone(self.bash("gh pr comment 5 --body note"))
        self.assertIn("target", self.bash("gh issue comment 22 --body x") or "")
        self.assertIn("number", self.bash("gh pr comment --body x") or "")
        self.reader.pulls[5]["state"] = "closed"
        self.assertIn("closed", self.bash("gh pr comment 5 --body note") or "")


class Pushes(Base):
    def test_fix_push_to_the_open_pr_branch(self) -> None:
        self.action(action="fix", pr=5, sha=A)
        self.reader.pulls[5] = pull(5, f"Executor: Claude\nIssue: {ISSUES}/21")
        self.reader.items = [item(21, "In progress")]
        self.assertIsNone(self.bash("git push origin feat/5-x"))
        self.assertIn("not PR 5", self.bash("git push origin feat/6-y") or "")
        self.reader.pulls[5]["merged_at"] = "2026-09-29T00:00:00Z"
        self.reader.pulls[5]["state"] = "closed"
        self.assertIn("merged", self.bash("git push origin feat/5-x") or "")

    def test_pr_push_needs_every_issue_of_the_pr_owned(self) -> None:
        self.action(action="fix", pr=5, sha=A)
        body = f"Executor: Claude\nIssue: {ISSUES}/21\nIssue: {ISSUES}/22"
        self.reader.pulls[5] = pull(5, body)
        self.reader.items = [item(21, "In progress"), item(22, "In progress")]
        self.assertIsNone(self.bash("git push origin feat/5-x"))
        for changed, reason in (
            (item(22, "Done"), "issue 22 is Done"),
            (item(22, "Ready"), "issue 22 is Ready"),
            (item(22, "In progress", "Codex"), "not In progress for Claude"),
        ):
            with self.subTest(reason=reason):
                self.reader.items[1] = changed
                self.assertIn(reason, self.bash("git push origin feat/5-x") or "")
        self.reader.items = [item(21, "In progress")]
        self.assertIn("not on the board", self.bash("git push origin feat/5-x") or "")
        self.reader.pulls[5] = pull(5)
        self.assertIn("names no issue", self.bash("git push origin feat/5-x") or "")

    def test_codex_push_needs_codex_ownership(self) -> None:
        self.action(action="fix", pr=5, sha=A)
        self.reader.pulls[5] = pull(5, f"Executor: Codex\nIssue: {ISSUES}/21")
        self.reader.items = [item(21, "In progress", "Codex")]
        self.assertIsNone(self.bash("git push origin feat/5-x", agent="Codex"))
        self.assertIn(
            "Executor is Codex, not Claude", self.bash("git push origin feat/5-x") or ""
        )
        self.reader.items = [item(21, "In progress")]
        self.assertIn(
            "not In progress for Codex",
            self.bash("git push origin feat/5-x", agent="Codex") or "",
        )

    def test_unknown_agent_is_refused(self) -> None:
        self.action(action="fix", pr=5, sha=A)
        self.assertIn(
            "unknown agent", self.bash("git push origin feat/5-x", agent="codex") or ""
        )

    def test_review_tick_does_not_push(self) -> None:
        self.action(action="review", pr=5, sha=A)
        self.reader.pulls[5] = pull(5, "Executor: Codex")
        self.assertIn("does not push", self.bash("git push origin feat/5-x") or "")

    def test_branch_delete_after_merge_only(self) -> None:
        self.action(action="merge", pr=5, sha=A)
        self.reader.pulls[5] = pull(5)
        self.assertIn(
            "still open", self.bash("git push origin --delete feat/5-x") or ""
        )
        self.reader.pulls[5].update(state="closed", merged_at="2026-09-29T00:00:00Z")
        self.assertIsNone(self.bash("git push origin --delete feat/5-x"))

    def test_first_push_after_claim(self) -> None:
        self.action(action="claim", issue=f"{ISSUES}/21")
        self.reader.items = [item(21, "In progress")]
        self.assertIsNone(self.bash("git push -u origin feat/21-new"))
        self.assertIsNone(
            self.bash("gh pr create --draft --base dev/312-interim -t t -b b")
        )
        self.assertIn("issue 21", self.bash("git push origin feat/22-new") or "")
        self.reader.branch_pulls = [{"number": 9, "state": "closed", "merged_at": None}]
        self.assertIn("closed PR", self.bash("git push origin feat/21-new") or "")

    def test_push_needs_the_owned_in_progress_issue(self) -> None:
        self.action(action="claim", issue=f"{ISSUES}/21")
        self.reader.items = [item(21, "Ready")]
        self.assertIn("not In progress", self.bash("git push origin feat/21-x") or "")
        self.reader.items = [item(21, "In progress", "Codex")]
        self.assertIsNotNone(self.bash("git push origin feat/21-x"))


class Merges(Base):
    def setUp(self) -> None:
        super().setUp()
        self.action(action="merge", pr=5, sha=A)
        self.reader.pulls[5] = pull(5)

    def test_merge_runs_the_full_guard(self) -> None:
        cmd = f"gh pr merge 5 --repo {na.REPO} --squash --match-head-commit {A}"
        self.assertIsNone(self.bash(cmd))
        self.reader.guard_errors = ["reviewer comment was edited; post a new verdict"]
        self.assertIn("merge guard", self.bash(cmd) or "")

    def test_merge_must_pin_the_selected_head(self) -> None:
        cmd = f"gh pr merge 5 --repo {na.REPO} --squash --match-head-commit {B}"
        self.assertIn("head SHA", self.bash(cmd) or "")


class Board(Base):
    EDIT = "gh project item-edit --id {} --field-id F --single-select-option-id O"

    def test_claim_board_write_rechecks_the_claim(self) -> None:
        self.action(action="claim", issue=f"{ISSUES}/21")
        self.reader.items = [item(21, "Ready"), item(22, "Ready")]
        with patch.object(gs, "recheck", return_value=None) as recheck:
            self.assertIsNone(self.bash(self.EDIT.format("PVTI_21")))
            recheck.assert_called_once()
        with patch.object(gs, "recheck", return_value="issue 21 is not Ready"):
            self.assertIn("not Ready", self.bash(self.EDIT.format("PVTI_21")) or "")
        with patch.object(gs, "recheck", return_value=None):
            self.assertIn("issue 22", self.bash(self.EDIT.format("PVTI_22")) or "")

    def test_continue_board_write_checks_item_and_assignment(self) -> None:
        # Codex review P1 on https://github.com/phaabe/live.moafunk.de/pull/511.
        self.action(action="continue", issue=f"{ISSUES}/21")
        self.reader.items = [item(21, "In progress"), item(22, "In progress")]
        self.assertIsNone(self.bash(self.EDIT.format("PVTI_21")))
        self.assertIn("issue 22", self.bash(self.EDIT.format("PVTI_22")) or "")
        self.assertIn("not on the board", self.bash(self.EDIT.format("PVTI_9")) or "")
        self.assertIn(
            "name the board item", self.bash("gh project item-add 2 --url x") or ""
        )
        self.reader.items = [item(21, "In progress", "Codex")]
        self.assertIn("Executor Codex", self.bash(self.EDIT.format("PVTI_21")) or "")
        self.reader.fail = gs.ReadBlocked("HTTP 502")
        self.assertIn("read failed", self.bash(self.EDIT.format("PVTI_21")) or "")

    def test_continue_on_a_pr_may_edit_its_issue_item(self) -> None:
        self.action(action="continue", pr=5, sha=A)
        self.reader.pulls[5] = pull(5, f"Executor: Claude\nIssue: {ISSUES}/21")
        self.reader.items = [item(21, "In progress"), item(22, "In progress")]
        rest = "gh api --method PATCH users/anneoneone/projectsV2/2/items/{} --input -"
        self.assertIsNone(self.bash(rest.format(50_021)))
        self.assertIsNotNone(self.bash(rest.format(50_022)))
        mutation = (
            "gh api graphql -f query='mutation {{ updateProjectV2ItemFieldValue("
            'input: {{projectId: "P", itemId: "{}", fieldId: "F"}}) {{ clientMutationId }} }}\''
        )
        self.assertIsNone(self.bash(mutation.format("PVTI_21")))
        self.assertIsNotNone(self.bash(mutation.format("PVTI_22")))

    def test_other_ticks_do_not_change_the_board(self) -> None:
        self.action(action="review", pr=5, sha=A)
        self.reader.pulls[5] = pull(5, "Executor: Codex")
        self.assertIn("board", self.bash(self.EDIT.format("PVTI_21")) or "")


class ClaimComments(Base):
    """Codex review P2 on https://github.com/phaabe/live.moafunk.de/pull/511."""

    def setUp(self) -> None:
        super().setUp()
        self.action(action="claim", issue=f"{ISSUES}/21")
        self.cmd = "gh issue comment 21 --body 'Claim by Claude'"

    def test_claim_comment_needs_the_claim_to_hold(self) -> None:
        self.reader.items = [item(21, "In progress", "Codex")]
        stale = "the selector now gives idle"
        with patch.object(gs, "recheck", return_value=stale) as recheck:
            self.assertEqual(self.bash(self.cmd), stale)
            recheck.assert_called_once()
        self.reader.items = [item(21, "Ready")]
        with patch.object(gs, "recheck", return_value=None):
            self.assertIsNone(self.bash(self.cmd))

    def test_owner_comments_after_the_claim(self) -> None:
        self.reader.items = [item(21, "In progress")]
        with patch.object(gs, "recheck", side_effect=AssertionError("not needed")):
            self.assertIsNone(self.bash(self.cmd))

    def test_continue_comment_needs_ownership(self) -> None:
        self.action(action="continue", issue=f"{ISSUES}/21")
        self.reader.items = [item(21, "In progress", "Codex")]
        self.assertIn("Executor Codex", self.bash(self.cmd) or "")


class Callers(Base):
    def test_permission_gate_denies_on_a_failed_fresh_check(self) -> None:
        self.action(action="merge", pr=5, sha=A)
        # A command the shape rule approves without a runner worktree.
        cmd = {
            "command": "gh pr merge 5 --repo phaabe/live.moafunk.de --squash "
            f"--match-head-commit {A}"
        }
        with patch.object(wc, "guard", return_value="PR 5 is merged"):
            self.assertEqual(
                permission_gate.decide("Bash", cmd),
                (False, "fresh check: PR 5 is merged"),
            )
        with patch.dict(os.environ, {"EPIC_SHARED_READER": "0"}):
            with patch.object(wc, "guard", side_effect=AssertionError("called")):
                self.assertTrue(permission_gate.decide("Bash", cmd)[0])

    def hook(self, root: Path, **env: str) -> subprocess.CompletedProcess[str]:
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "git push origin feat/5-x"},
            "cwd": str(self.root),
        }
        return subprocess.run(
            [sys.executable, str(ROOT / ".claude/hooks/scripts/epic_guard.py")],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env={**os.environ, "EPIC_TRUSTED_ROOT": str(root), **env},
            timeout=30,
        )

    def test_hook_blocks_what_the_check_refuses(self) -> None:
        self.action(action="fix", pr=5, sha=A)
        trusted = self.root / "trusted"
        (trusted / "scripts/epic").mkdir(parents=True)
        (trusted / "scripts/epic/write_checks.py").write_text(
            "def guard(tool, tool_input, cwd, agent=None):\n"
            "    assert agent == 'Claude', agent\n"
            "    return 'PR 5 is merged'\n"
        )
        blocked = self.hook(trusted)
        self.assertEqual(blocked.returncode, 2, blocked.stderr)
        self.assertIn("PR 5 is merged", blocked.stderr)
        self.assertEqual(self.hook(trusted, EPIC_SHARED_READER="0").returncode, 0)
        self.assertEqual(self.hook(trusted, EPIC_ACTION_FILE="").returncode, 0)

    def test_hook_blocks_when_the_check_raises(self) -> None:
        # Exit 1 would not block in Claude Code; any error must exit 2.
        self.action(action="fix", pr=5, sha=A)
        trusted = self.root / "trusted"
        (trusted / "scripts/epic").mkdir(parents=True)
        (trusted / "scripts/epic/write_checks.py").write_text(
            "def guard(tool, tool_input, cwd, agent=None):\n"
            "    raise KeyError('head')\n"
        )
        blocked = self.hook(trusted)
        self.assertEqual(blocked.returncode, 2, blocked.stderr)
        self.assertIn("Runner write check failed", blocked.stderr)

    def test_hook_fails_closed_when_the_check_cannot_load(self) -> None:
        self.action(action="fix", pr=5, sha=A)
        broken = self.hook(self.root / "missing")
        self.assertEqual(broken.returncode, 2)
        self.assertIn("unavailable", broken.stderr)


class PromotionBarrier(Base):
    """https://github.com/phaabe/live.moafunk.de/issues/584: the write barrier
    during a runtime promotion, in the checks and in the hook (every session)."""

    def setUp(self) -> None:
        super().setUp()
        self.locks = self.root / "locks"
        env = patch.dict(
            os.environ,
            {
                "EPIC_LOCK_DIR": str(self.locks),
                "EPIC_RUNTIME_HOME": str(self.root / "rt"),
            },
        )
        env.start()
        self.addCleanup(env.stop)

    def refusal(self, command: str) -> str | None:
        return wc.promotion_refusal("Bash", {"command": command}, str(self.root))

    def test_marker_name_matches_runtime(self) -> None:
        self.assertEqual(wc.PROMOTION_MARKER, runtime.MARKER)

    def test_no_marker_costs_nothing(self) -> None:
        with patch.object(wc, "promotion_writes", side_effect=AssertionError("parsed")):
            self.assertIsNone(self.refusal("gh issue comment 5 --body x"))

    # Bypasses Codex found in the reviews of
    # https://github.com/phaabe/live.moafunk.de/pull/592, plus plain reads.
    # Every one is a Bash call, so every one is refused.
    BASH_CALLS = (
        "git add -- example.txt",
        "git commit -m 'fix: x'",
        "git rebase origin/dev/312-interim",
        "gh issue create --title x --body y",
        "gh api repos/o/r/issues -ftitle=x",
        "bash <<'EOF'\ngit add -- example.txt\nEOF",
        "python3 - <<'EOF'\nimport subprocess\n"
        "subprocess.run(['git', 'add', '--', 'example.txt'])\nEOF",
        "if test -f example.txt; then git add -- example.txt; fi",
        "if true; then gtimeout 5 bash <<'EOF'\ngit add -- example.txt\nEOF\nfi",
        "cat <<EOF\n$(git add -- example.txt)\nEOF",
        "bash -s \"$(printf x)\" <<'EOF'\ngit add -- example.txt\nEOF",
        "printf 'git add -- example.txt\\n' |\nbash",
        "LESSOPEN='|git add -- %s' less example.txt",
        "# <<true\ngit config review.probe hit\ntrue",
        "QUERY='mutation { x }'\ngh api graphql -f query=\"$QUERY\"",
        "python3 scripts/x.py",
        "ls -la",
        "git status --short",
        "gh pr view 5",
    )

    def test_every_bash_call_is_refused(self) -> None:
        runtime.begin_promotion("2" * 40, None)
        for command in self.BASH_CALLS:
            with self.subTest(command=command):
                self.assertIn("runtime promotion", self.refusal(command) or "")

    def test_only_github_mcp_writes_are_refused_among_other_tools(self) -> None:
        runtime.begin_promotion("2" * 40, None)
        for tool in (
            "mcp__github__create_issue",
            "mcp__github__push_files",
            "mcp__github__create_or_update_file",
            "mcp__github__add_issue_comment",
        ):
            with self.subTest(tool=tool):
                refused = wc.promotion_refusal(tool, {}, str(self.root))
                self.assertIn("runtime promotion", refused or "")
        for tool in (
            "mcp__github__get_issue",
            "mcp__github__list_commits",
            "mcp__github__search_code",
            "Read",
            "Grep",
            "Edit",
        ):
            with self.subTest(tool=tool):
                self.assertIsNone(wc.promotion_refusal(tool, {}, str(self.root)))

    def test_hook_refuses_every_bash_call_during_promotion(self) -> None:
        runtime.begin_promotion("2" * 40, None)
        for command in self.BASH_CALLS:
            with self.subTest(command=command):
                out = self.run_hook(command)
                self.assertEqual(out.returncode, 2, out.stderr)
                self.assertIn("Runtime promotion", out.stderr)

    def test_hook_matcher_covers_every_github_mcp_tool(self) -> None:
        settings = json.loads((ROOT / ".claude/settings.json").read_text())
        matchers = [
            entry["matcher"]
            for entry in settings["hooks"]["PreToolUse"]
            if any("epic-guard.sh" in h["command"] for h in entry["hooks"])
        ]
        for tool in ("mcp__github__create_issue", "mcp__github__push_files"):
            with self.subTest(tool=tool):
                self.assertTrue(any(re.fullmatch(m, tool) for m in matchers))

    def test_split_flags_reads_attached_short_values(self) -> None:
        # The runner write check (tool_writes) must see -ftitle=x as a field.
        flags, positional = wc.split_flags(["repos/o/r/issues", "-ftitle=x", "-XPOST"])
        self.assertEqual(
            (flags["-f"], flags["-X"], positional),
            (["title=x"], ["POST"], ["repos/o/r/issues"]),
        )

    def test_guard_refuses_before_any_github_read(self) -> None:
        self.action(action="fix", pr=5, sha=A)
        runtime.begin_promotion("2" * 40, None)
        with patch.object(self.reader, "pull", side_effect=AssertionError("read")):
            self.assertIn(
                "runtime promotion", self.bash("gh pr comment 5 --body x") or ""
            )

    def test_permission_gate_refuses_merge_and_body_edit(self) -> None:
        runtime.begin_promotion("2" * 40, None)
        with patch.dict(os.environ, {"EPIC_SHARED_READER": "0"}):
            for command in (
                f"gh pr merge 5 --repo phaabe/live.moafunk.de --squash --match-head-commit {A}",
                "gh api --method PATCH repos/phaabe/live.moafunk.de/pulls/5 -F body=@/x",
            ):
                allowed, reason = permission_gate.decide("Bash", {"command": command})
                self.assertEqual(
                    (allowed, "runtime promotion" in reason), (False, True)
                )

    def run_hook(self, command: str, **env: str) -> subprocess.CompletedProcess[str]:
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "cwd": str(self.root),
        }
        return subprocess.run(
            [sys.executable, str(ROOT / ".claude/hooks/scripts/epic_guard.py")],
            input=json.dumps(payload), capture_output=True, text=True, timeout=30,
            env={**os.environ, "EPIC_SHARED_READER": "0", "EPIC_TRUSTED_ROOT": str(ROOT), **env},
        )  # fmt: skip

    def test_hook_blocks_writes_in_every_session_during_promotion(self) -> None:
        write = "gh issue comment 5 --body x"
        self.assertEqual(self.run_hook(write).returncode, 0)
        runtime.begin_promotion("2" * 40, None)
        blocked = self.run_hook(write)
        self.assertEqual(blocked.returncode, 2, blocked.stderr)
        self.assertIn("Runtime promotion", blocked.stderr)

    def test_hook_loads_runner_checks_from_the_runtime_root(self) -> None:
        self.action(action="fix", pr=5, sha=A)
        pinned = self.root / "pinned"
        (pinned / "scripts/epic").mkdir(parents=True)
        (pinned / "scripts/epic/write_checks.py").write_text(
            "def promotion_refusal(tool, tool_input, cwd):\n    return None\n"
            "def guard(tool, tool_input, cwd, agent=None):\n    return 'pinned check ran'\n"
        )
        out = self.run_hook("git push origin feat/5-x", EPIC_SHARED_READER="1",
                            EPIC_RUNTIME_ROOT=str(pinned))  # fmt: skip
        self.assertEqual(out.returncode, 2, out.stderr)
        self.assertIn("pinned check ran", out.stderr)

    def test_hook_refuses_pinned_mode_without_runtime_root(self) -> None:
        self.action(action="fix", pr=5, sha=A)
        runtime.write_configured(self.root / "rt")
        out = self.run_hook("git push origin feat/5-x", EPIC_SHARED_READER="1")
        self.assertEqual(out.returncode, 2, out.stderr)
        self.assertIn("EPIC_RUNTIME_ROOT is not set", out.stderr)

    def test_merge_guard_loads_from_the_runtime_root(self) -> None:
        with patch.dict(os.environ, {"EPIC_RUNTIME_ROOT": "/pinned"}):
            self.assertEqual(gs.trusted_root(), Path("/pinned"))
        runtime.write_configured(self.root / "rt")
        with self.assertRaises(gs.ConfigError):
            gs.trusted_root()


if __name__ == "__main__":
    unittest.main()
