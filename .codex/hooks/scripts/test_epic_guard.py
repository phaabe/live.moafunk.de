"""Exercise the registered command without executing any proposed gh command."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SHA = "0123456789abcdef" * 2 + "01234567"
CLAUDE_VERDICT = "Review: APPROVED by " + "Claude at " + SHA
COMMAND = json.loads((ROOT / ".codex/hooks.json").read_text())["hooks"]["PreToolUse"][
    0
]["hooks"][0]["command"]


class EpicGuardTest(unittest.TestCase):
    def run_hook(
        self,
        command: str,
        expected: int,
        cwd: Path = ROOT,
        tool: str = "Bash",
        field: str = "command",
    ) -> None:
        payload = {"tool_name": tool, "cwd": str(cwd), "tool_input": {field: command}}
        result = subprocess.run(
            ["/bin/bash", "-c", COMMAND],
            cwd=cwd,
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, expected, (command, result.stderr))
        if expected == 2:
            self.assertIn("BLOCKED", result.stderr)

    def test_create(self) -> None:
        cases = [
            ("gh pr create --fill", 2),
            ("gh pr create --base main --head feat/x", 2),
            ("gh pr create --base main", 2),
            ("gh pr create --base dev/streaming-architecture --fill", 0),
            ("gh pr create --base=dev/streaming-architecture", 0),
            ("gh pr create -B dev/streaming-architecture", 0),
            ("gh pr create --base main --head dev/streaming-architecture", 0),
            ("gh pr create --base other", 2),
            (
                "gh pr create --base main --head feat/x --body 'use --base dev/streaming-architecture'",
                2,
            ),
            ("gh pr create --body '--base dev/streaming-architecture'", 2),
            ("gh pr create --base main --base dev/streaming-architecture", 2),
            ("gh pr 'create' --fill", 2),
            ("g''h pr create --fill", 2),
            ("gh pr create --base main --head '$BRANCH'", 2),
            ("cd backend && gh pr create --base main", 2),
            ("bash -c 'gh pr create --fill'", 2),
            ("gh pr create --base main --head $(git branch --show-current)", 2),
        ]
        for command, expected in cases:
            with self.subTest(command=command):
                self.run_hook(command, expected)

    def test_merge(self) -> None:
        cases = [
            ("gh pr merge 1 --squash", 2),
            (f"gh pr merge 1 --squash --match-head-commit {SHA}", 0),
            (f"gh pr merge 1 --match-head-commit '{SHA}'", 0),
            (f"gh pr merge 1 --match-head-commit={SHA}", 0),
            (f"gh pr merge 1 --match-head-commit {SHA}a", 2),
            ("gh pr merge 1 --match-head-commit abc", 2),
            (f"gh pr merge 1 --body '--match-head-commit {SHA}'", 2),
            (f"gh pr merge 1 --match-head-commit {SHA} && gh pr merge 2", 2),
            (f"gh pr merge 1 --match-head-commit {SHA}\ngh pr merge 2", 2),
            (f"gh pr merge 1 --match-head-commit {SHA}; gh pr merge 2", 2),
            ("gh pr create --base dev/streaming-architecture ;\ngh pr merge 1", 2),
            (f"gh pr merge 1 --match-head-commit {SHA}\n\ngh pr merge 2", 2),
        ]
        for command, expected in cases:
            with self.subTest(command=command):
                self.run_hook(command, expected)

    def test_verdicts_and_tool_shapes(self) -> None:
        self.run_hook("gh pr comment 1 --body '" + CLAUDE_VERDICT + "'", 2)
        self.run_hook(
            "gh pr comment 1 --body '"
            + CLAUDE_VERDICT.replace("Claude", "Cla''ude")
            + "'",
            2,
        )
        self.run_hook(
            CLAUDE_VERDICT.replace("APPROVED", "CHANGES REQUESTED"),
            2,
            tool="apply_patch",
        )
        self.run_hook(
            CLAUDE_VERDICT, 2, tool="mcp__github__add_issue_comment", field="body"
        )
        self.run_hook("Review: APPROVED by Codex at " + SHA, 0, tool="apply_patch")
        self.run_hook(
            "Review: APPROVED by Claude at <40-char SHA>", 0, tool="apply_patch"
        )
        self.run_hook("gh pr merge 1", 2, tool="exec_command", field="cmd")
        self.run_hook("git status --short", 0)

    def test_body_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "body.md"
            path.write_text(CLAUDE_VERDICT)
            self.run_hook(f"gh pr comment 1 --body-file '{path}'", 2)
            path.write_text("Review: APPROVED by Codex at " + SHA)
            self.run_hook(f"gh pr comment 1 --body-file='{path}'", 0)
            self.run_hook(f"gh pr comment 1 -F '{path}'", 0)
        self.run_hook(f"gh pr comment 1 --body-file '{path}'", 2)
        self.run_hook("gh pr comment 1 --body-file -", 2)

    def test_malformed_payload(self) -> None:
        result = subprocess.run(
            ["/bin/bash", "-c", COMMAND],
            cwd=ROOT,
            input="{",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)

    def test_worktree_and_subdirectory(self) -> None:
        # Copy only tracked adapter files into an independent repo, then let Git
        # populate a linked worktree. No untracked main-checkout files can help.
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            worktree = Path(directory) / "worktree with spaces"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            for relative in (
                ".codex/hooks.json",
                ".codex/hooks/scripts/epic-guard.sh",
                ".codex/hooks/scripts/epic_guard.py",
            ):
                target = repo / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT / relative).read_bytes())
            subprocess.run(["git", "-C", str(repo), "add", ".codex"], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.invalid",
                    "-c",
                    "commit.gpgsign=false",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "commit",
                    "-qm",
                    "test fixture",
                ],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "worktree",
                    "add",
                    "-qb",
                    "dev/streaming-architecture",
                    str(worktree),
                ],
                check=True,
            )
            nested = worktree / "backend"
            nested.mkdir()
            self.run_hook("gh pr create --base main", 0, cwd=nested)
            self.run_hook("gh pr merge 1", 2, cwd=nested)
            self.run_hook("gh pr create --base main --head feat/x", 2, cwd=worktree)


if __name__ == "__main__":
    unittest.main()
