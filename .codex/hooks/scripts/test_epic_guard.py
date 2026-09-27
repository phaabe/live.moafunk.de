"""Exercise the registered command without executing any proposed gh command."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TRUNK = "dev/streaming-architecture"
SHA = "0123456789abcdef" * 2 + "01234567"
CLAUDE_VERDICT = "Review: APPROVED by " + "Claude at " + SHA
COMMAND = json.loads((ROOT / ".codex/hooks.json").read_text())["hooks"]["PreToolUse"][
    0
]["hooks"][0]["command"]


class EpicGuardTest(unittest.TestCase):
    def run_hook(
        self,
        command: str | list[str] | dict[str, str] | int | None,
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
            ("gh pr create --base main --head ci/312-epic-guard --title x --body y", 0),
            ("gh pr create --base main --head ci/312-epic-guard-other", 2),
            ("gh pr create --base other", 2),
            ("gh pr create --base dev/312-interim --head feat/x", 0),
            ("gh pr create --base main --head dev/312-interim", 2),
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

    def test_api(self) -> None:
        pulls = "repos/phaabe/live.moafunk.de/pulls"
        cases = [
            (f"gh api -X PUT {pulls}/5/merge", 2),
            (f"gh api {pulls}/5/merge -X PUT -f sha={SHA}", 2),
            (f"gh api {pulls}/5/merge --method=PUT --raw-field=sha={SHA}", 2),
            (f"gh api {pulls}/5/merge -f sha=abc", 2),
            (f"gh api {pulls}/5/merge -F sha=@head.txt", 2),
            (f"gh api {pulls}/5/merge -f sha={SHA} -f sha=abc", 2),
            (f"gh api {pulls}/5/merge -f title='sha={SHA}'", 2),
            (f"gh api {pulls}/5/merge --input body.json -f sha={SHA}", 2),
            (f"gh api {pulls}/5/merge?sha={SHA} -X PUT", 2),
            (f"gh api https://api.github.com/{pulls}/5/merge -X PUT", 2),
            (f"gh api {pulls} -f base=main -f head=feat/x -f title=t", 2),
            (f"gh api {pulls} -F base=main -F head=feat/x", 2),
            (f"gh api {pulls} -f base={TRUNK} -f head=feat/x", 2),
            (f"gh api {pulls} -f base=main -f head={TRUNK}", 2),
            (f"gh api {pulls} -f base=main -f head=ci/312-epic-guard", 2),
            (f"gh api {pulls} -f base=main -f head=ci/312-epic-guard-other", 2),
            (f"gh api {pulls} -f title='base={TRUNK}'", 2),
            (f"gh api {pulls} --input body.json", 2),
            (f"gh api {pulls} --input body.json -X GET", 2),
            (f"gh api {pulls} -X POST", 2),
            (f"gh api {pulls}/5 -X PATCH -f base=main", 2),
            (f"gh api {pulls}/5/reviews -X POST -f body=review", 2),
            (f"gh api {pulls}", 0),
            (f"gh api '{pulls}?state=open'", 0),
            (f"gh api {pulls}/5/merge", 0),
            (f"gh api {pulls} -X GET -f state=open", 0),
            ("gh api graphql -F owner=phaabe -f query=q", 0),
            ("gh api graphql --field=owner=phaabe -f query=q", 0),
            ("gh api graphql --field owner=phaabe --raw-field query=q", 0),
            ("gh api repos/o/r/issues/5 -F state=closed", 0),
            (f"env gh api {pulls}/5/merge -X PUT", 2),
            (f"/usr/local/bin/gh api {pulls} -f base=main", 2),
            (f"gh api {pulls}/5/merge -f sha={SHA}; gh api {pulls}/6/merge", 2),
        ]
        for command, expected in cases:
            with self.subTest(command=command):
                self.run_hook(command, expected)

    def test_mcp_pr_tools(self) -> None:
        for tool in (
            "mcp__github__merge_pull_request",
            "mcp__github_tools__merge_pull_request",
        ):
            self.run_hook("", 2, tool=tool)
        for base, head, expected in (
            ("main", "feat/x", 2),
            ("main", "ci/312-epic-guard", 0),
            ("main", TRUNK, 0),
            (TRUNK, "feat/x", 0),
            ("dev/312-interim", "feat/x", 0),
            ("main", "dev/312-interim", 2),
            (None, "feat/x", 2),
        ):
            with self.subTest(base=base, head=head):
                result = subprocess.run(
                    ["/bin/bash", "-c", COMMAND],
                    cwd=ROOT,
                    input=json.dumps(
                        {
                            "tool_name": "mcp__github__create_pull_request",
                            "tool_input": {"base": base, "head": head},
                        }
                    ),
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, expected, result.stderr)

    def test_api_file_fields(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            path = Path(directory) / "verdict body.txt"
            relative = path.relative_to(ROOT)
            for verdict in (
                CLAUDE_VERDICT,
                CLAUDE_VERDICT.replace("APPROVED", "CHANGES REQUESTED"),
            ):
                path.write_text(verdict)
                for endpoint in ("repos/o/r/issues/5/comments", "graphql"):
                    for field in (
                        f"-F 'body=@{relative}'",
                        f"--field 'body=@{path}'",
                        f"--field='body=@{relative}'",
                    ):
                        with self.subTest(
                            endpoint=endpoint, field=field, verdict=verdict
                        ):
                            self.run_hook(f"gh api {endpoint} {field}", 2)
            # Raw fields send the @path literally; typed fields read the file.
            self.run_hook(f"gh api graphql -f 'body=@{relative}'", 0)
            path.write_text("Review: APPROVED by Codex at " + SHA)
            self.run_hook(f"gh api graphql -F 'body=@{relative}'", 0)
            self.run_hook(
                f"gh api repos/o/r/issues/5/comments --field 'body=@{path}'", 0
            )
            self.run_hook(f"gh api graphql -F 'body=@{relative}.missing'", 2)
        for field in ("-F body=@-", "--field body=@-", "--field=body=@-"):
            with self.subTest(field=field):
                self.run_hook(f"gh api graphql {field}", 2)

    def test_dash_prefixed_option_values(self) -> None:
        self.run_hook(
            f"gh pr create --base {TRUNK} --body '- fix workflow' --title x", 0
        )
        self.run_hook(f"gh pr create --base {TRUNK} --body '-Bmain' --title x", 0)
        self.run_hook("gh pr create --body --base --base main --head feat/x", 2)
        self.run_hook(f"gh pr create --base {TRUNK} --body", 2)

    def test_non_string_commands(self) -> None:
        for command in (["gh", "pr", "merge", "5", "--squash"], [], {}, 7, None):
            for tool, field in (("Bash", "command"), ("exec_command", "cmd")):
                with self.subTest(command=command, tool=tool):
                    self.run_hook(command, 2, tool=tool, field=field)

    def test_body_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "body.md"
            path.write_text(CLAUDE_VERDICT)
            self.run_hook(f"gh pr comment 1 --body-file '{path}'", 2)
            for verb in (
                "pr create",
                "issue create",
                "pr comment",
                "issue comment",
                "pr review",
                "pr merge",
            ):
                self.run_hook(f"gh {verb} -F '{path}'", 2)
            self.run_hook(f"gh pr merge 1 --match-head-commit {SHA} -F '{path}'", 2)
            self.run_hook(
                f"gh pr -R phaabe/live.moafunk.de comment 1 --body-file '{path}'", 2
            )
            self.run_hook(
                f"gh issue -R phaabe/live.moafunk.de comment 1 -F '{path}'", 2
            )
            path.write_text("Review: APPROVED by Codex at " + SHA)
            self.run_hook(f"gh pr comment 1 --body-file='{path}'", 0)
            self.run_hook(f"gh pr comment 1 -F '{path}'", 0)
        self.run_hook(f"gh pr comment 1 --body-file '{path}'", 2)
        self.run_hook("gh pr comment 1 --body-file -", 2)

    def test_flag_placement_and_attached_values(self) -> None:
        cases = [
            ("gh -R phaabe/live.moafunk.de pr merge 5 --squash", 2),
            ("gh pr -R phaabe/live.moafunk.de merge 5 --squash", 2),
            (f"gh pr -R phaabe/live.moafunk.de create --base {TRUNK}", 2),
            (f"gh pr create --base {TRUNK} -Bmain --head feat/x --fill", 2),
            (f"gh pr create -B {TRUNK} --base main --head feat/x --fill", 2),
            (f"gh pr create --base main -B {TRUNK} --head feat/x --fill", 2),
            ("gh pr create -Bmain -Hci/312-epic-guard", 2),
            ("gh api -XPUT repos/o/r/pulls/5/merge", 2),
            ("gh api repos/o/r/pulls -fbase=main -fhead=feat/x -ftitle=x", 2),
            ("gh api repos/o/r/pulls -Fbase=main", 2),
            ("gh api repos/o/r/pulls -X GET --method PUT", 2),
            ("gh -R phaabe/live.moafunk.de api repos/o/r/pulls -X POST", 2),
            ('bash -c "gh pr -R phaabe/live.moafunk.de merge 5 --squash"', 2),
            ('env -S "gh -R phaabe/live.moafunk.de pr merge 5 --squash"', 2),
            ('bash -c "gh --hostname github.com api repos/o/r/pulls -X POST"', 2),
            (f"gh pr create --base {TRUNK} --title api", 0),
        ]
        for command, expected in cases:
            with self.subTest(command=command):
                self.run_hook(command, expected)

    def test_continuations_and_wrapped_heredocs(self) -> None:
        cases = [
            ("gh pr \\\n  merge 5 --squash", 2),
            ("gh pr \\\n  create --base main --head feat/x --fill", 2),
            (f"gh pr \\\n  merge 5 --match-head-commit {SHA}", 0),
            (f"gh pr merge 5 \\\n  --match-head-commit {SHA}", 0),
            (f"g\\\nh p\\\nr mer\\\nge 5 --match-head-commit {SHA}", 0),
            (f"gh pr create --base {TRUNK} \\\n  --body 'hello'", 0),
            ("gh a\\\npi repos/o/r/pulls -X PUT", 2),
            (f"gh pr merge 5 --match-head-commit '{SHA[:20]}\\\n{SHA[20:]}'", 2),
            (f'gh pr merge 5 --match-head-commit "{SHA[:20]}\\\n{SHA[20:]}"', 0),
            (f"gh pr merge 5 --match-head-commit {SHA} \\\\\ngh pr merge 6", 2),
            # Stdin/heredoc bodies remain outside the supported grammar.
            (f"gh pr create --base {TRUNK} \\\n --body-file - <<'END'\nbody\nEND", 2),
            ("gh api repos/o/r/pulls --input - <<'END'\n{}\nEND", 2),
            ("bash <<'END'\ngh pr merge 5 --squash\nEND", 2),
            ("command bash <<'END'\ngh pr merge 5 --squash\nEND", 2),
            ("env bash <<'END'\ngh pr merge 5 --squash\nEND", 2),
            ("bash -c 'gh pr \\\n merge 5 --squash'", 2),
            ("bash -c 'gh \\\n api repos/o/r/pulls/5/merge -X PUT'", 2),
            ("if true; then gh pr merge 5 --squash; fi", 2),
            ('env -S "gh pr merge 5 --squash"', 2),
        ]
        for command, expected in cases:
            with self.subTest(command=command):
                self.run_hook(command, expected)

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
