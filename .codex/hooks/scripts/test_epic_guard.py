"""Exercise the registered command without executing any proposed gh command."""

from __future__ import annotations

import json
import importlib
import importlib.util
import io
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

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
        action_file: Path | None = None,
    ) -> None:
        payload = {"tool_name": tool, "cwd": str(cwd), "tool_input": {field: command}}
        env = dict(os.environ)
        env.pop("EPIC_ACTION_FILE", None)
        if action_file is not None:
            env["EPIC_ACTION_FILE"] = str(action_file)
        result = subprocess.run(
            ["/bin/bash", "-c", COMMAND],
            cwd=cwd,
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            check=False,
            env=env,
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

    def test_adopt_body_edit(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            path = Path(directory)
            body = path / "body with spaces.md"
            action_file = path / "action.json"
            action_file.write_text(
                json.dumps(
                    {"action": "adopt", "pr": 5, "sha": SHA, "body_sha": "a" * 64}
                )
            )
            command = (
                "gh api --method PATCH repos/phaabe/live.moafunk.de/pulls/5 "
                f"-F 'body=@{body}'"
            )
            body.write_text("Original body\nExecutor: Codex\n")
            self.run_hook(command, 0, action_file=action_file)
            self.run_hook(command, 0, cwd=ROOT / "backend", action_file=action_file)
            self.run_hook(
                command, 0, tool="exec_command", field="cmd", action_file=action_file
            )
            self.run_hook(command, 2)
            self.run_hook(command, 2, action_file=action_file.relative_to(ROOT))
            for verdict in (
                CLAUDE_VERDICT,
                CLAUDE_VERDICT.replace("APPROVED", "CHANGES REQUESTED"),
            ):
                body.write_text(verdict)
                self.run_hook(command, 2, action_file=action_file)
            body.unlink()
            self.run_hook(command, 2, action_file=action_file)

    def test_adopt_requires_matching_valid_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            body = Path(directory) / "body.md"
            body.write_text("Original body\nExecutor: Codex\n")
            action_file = Path(directory) / "action.json"
            command = (
                "gh api --method PATCH repos/phaabe/live.moafunk.de/pulls/5 "
                f"-F body=@{body}"
            )
            valid = {"action": "adopt", "pr": 5, "sha": SHA, "body_sha": "a" * 64}
            for action in (
                {},
                [],
                None,
                {**valid, "action": "review"},
                {**valid, "pr": 6},
                {**valid, "pr": "5"},
                {**valid, "pr": True},
                {**valid, "sha": "abc"},
                {**valid, "body_sha": "abc"},
                {key: value for key, value in valid.items() if key != "sha"},
                {key: value for key, value in valid.items() if key != "body_sha"},
            ):
                with self.subTest(action=action):
                    action_file.write_text(json.dumps(action))
                    self.run_hook(command, 2, action_file=action_file)
            action_file.write_text("{")
            self.run_hook(command, 2, action_file=action_file)
            action_file.unlink()
            self.run_hook(command, 2, action_file=action_file)

    def test_adopt_rejects_other_command_shapes(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            path = Path(directory)
            body = path / "body.md"
            body.write_text("Original body\nExecutor: Codex\n")
            action_file = path / "action.json"
            action_file.write_text(
                json.dumps(
                    {"action": "adopt", "pr": 5, "sha": SHA, "body_sha": "a" * 64}
                )
            )
            endpoint = "repos/phaabe/live.moafunk.de/pulls/5"
            command = f"gh api --method PATCH {endpoint} -F body=@{body}"
            for changed in (
                command.replace("/pulls/5", "/pulls/6"),
                command.replace("/pulls/5", "/pulls/0"),
                command.replace("/pulls/5", "/pulls/05"),
                command.replace("phaabe/", "other/"),
                command.replace(endpoint, "https://api.github.com/" + endpoint),
                command.replace(endpoint, endpoint + "?state=open"),
                command.replace(endpoint, endpoint + "/"),
                command.replace(endpoint, endpoint + "/reviews"),
                command.replace("--method PATCH", "-X PATCH"),
                command.replace("--method PATCH", "--method=PATCH"),
                command.replace("PATCH", "POST"),
                command.replace("-F body=", "--field body="),
                command.replace("-F body=", "-f body="),
                command.replace(str(body), str(body.relative_to(ROOT))),
                command.replace(str(body), str(path / ".." / path.name / "body.md")),
                command.replace(str(body), "-"),
                command.replace("-F body=@", "--input "),
                command + " -f base=main",
                command + f" -F body=@{body}",
                command + " --hostname other.example",
                command + " --header 'Authorization: x'",
                command + " --silent",
                command + " && gh pr merge 5",
                "env " + command,
                f"gh api {endpoint} --method PATCH -F body=@{body}",
            ):
                with self.subTest(command=changed):
                    self.run_hook(changed, 2, action_file=action_file)

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

    def test_api_input_files(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            path = Path(directory) / "request body.json"
            relative = path.relative_to(ROOT)
            for verdict in (
                CLAUDE_VERDICT,
                CLAUDE_VERDICT.replace("APPROVED", "CHANGES REQUESTED"),
            ):
                path.write_text(json.dumps({"body": verdict}))
                for endpoint in ("repos/o/r/issues/5/comments", "graphql", "repos/o/r"):
                    for option in (f"--input '{relative}'", f"--input='{path}'"):
                        with self.subTest(
                            endpoint=endpoint, option=option, verdict=verdict
                        ):
                            self.run_hook(f"gh api {endpoint} {option}", 2)
            path.write_text(json.dumps({"body": "Review: APPROVED by Codex at " + SHA}))
            self.run_hook(f"gh api repos/o/r/issues/5/comments --input '{relative}'", 0)
            self.run_hook(f"gh api graphql --input='{path}'", 0)
            self.run_hook(f"gh api repos/o/r/pulls --input '{path}'", 2)
            self.run_hook(f"gh api graphql --input '{relative}.missing'", 2)
        for endpoint in ("repos/o/r/issues/5/comments", "graphql"):
            for option in ("--input -", "--input=-"):
                with self.subTest(endpoint=endpoint, option=option):
                    self.run_hook(f"gh api {endpoint} {option}", 2)

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


class FreshWriteCheckTest(unittest.TestCase):
    """Exercise Codex's hook with the real checker and fake GitHub reads."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        sys.path.insert(0, str(ROOT / "scripts/epic"))
        self.addCleanup(sys.path.pop, 0)
        import github_state
        import next_action
        import write_checks

        sys.path.insert(0, str(ROOT / ".codex"))
        self.addCleanup(sys.path.remove, str(ROOT / ".codex"))
        assignment = importlib.import_module("assignment")

        self.gs, self.na, self.checks = github_state, next_action, write_checks
        spec = importlib.util.spec_from_file_location(
            "codex_epic_guard", ROOT / ".codex/hooks/scripts/epic_guard.py"
        )
        assert spec is not None and spec.loader is not None
        self.hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.hook)
        self.action_file = self.directory / "action.json"
        self.patches = [
            patch.dict(
                os.environ,
                {
                    "EPIC_SHARED_READER": "1",
                    "EPIC_ACTION_FILE": str(self.action_file),
                    "EPIC_TRUSTED_ROOT": str(ROOT),
                    "EPIC_ACTIONS": "",
                },
            ),
            patch.object(next_action, "PAUSE_FILE", self.directory / "pause"),
            patch.object(next_action, "FOCUS_FILE", self.directory / "focus"),
            patch.object(github_state, "FreshReader", return_value=self),
            patch.object(assignment, "make_reader", return_value=self),
        ]
        for context in self.patches:
            context.start()
            self.addCleanup(context.stop)
        self.pull_data: dict[str, Any] = {
            "number": 5,
            "state": "open",
            "draft": False,
            "merged_at": None,
            "body": "Executor: Codex\nIssue: https://github.com/phaabe/live.moafunk.de/issues/21",
            "head": {"sha": SHA, "ref": "feat/21-work"},
            "labels": [],
        }
        self.items = [self.item(21, "In progress")]
        self.branch_pulls: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.read_error: Exception | None = None
        self.reads: list[str] = []
        self.client = object()

    def item(self, number: int, status: str, executor: str = "Codex") -> dict[str, Any]:
        return {
            "id": f"PVTI_{number}",
            "status": status,
            "executor": executor,
            "content": {
                "type": "Issue",
                "number": number,
                "url": f"https://github.com/phaabe/live.moafunk.de/issues/{number}",
            },
        }

    def read(self, kind: str) -> None:
        self.reads.append(kind)
        if self.read_error:
            raise self.read_error

    def pull(self, number: int) -> dict[str, Any]:
        self.read(f"pull:{number}")
        return self.pull_data

    def issue(self, number: int) -> dict[str, Any]:
        self.read(f"issue:{number}")
        return {"number": number, "labels": []}

    def board_items(self) -> list[dict[str, Any]]:
        self.read("board")
        return self.items

    def pulls_for_branch(self, branch: str) -> list[dict[str, Any]]:
        self.read(f"branch:{branch}")
        return self.branch_pulls

    def merge_errors(self, number: int, sha: str) -> list[str]:
        self.read(f"merge:{number}:{sha}")
        return self.errors

    def action(self, action: str, pr: int | None = 5) -> None:
        self.action_file.write_text(
            json.dumps(
                {
                    "action": action,
                    "pr": pr,
                    "sha": SHA if pr else None,
                    "issue": "https://github.com/phaabe/live.moafunk.de/issues/21",
                }
            )
        )

    def run_hook(
        self, command: str, expected: int = 0, tool: str = "exec_command"
    ) -> str:
        payload = {
            "tool_name": tool,
            "cwd": str(self.directory),
            "tool_input": {"cmd" if tool == "exec_command" else "command": command},
        }
        with (
            patch.object(sys, "path", list(sys.path)),
            patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
            patch.object(sys, "stderr", io.StringIO()) as stderr,
        ):
            self.assertEqual(self.hook.main(), expected, stderr.getvalue())
            return stderr.getvalue()

    def test_unclear_write_blocks_without_traceback_in_both_reader_modes(self) -> None:
        self.action("claim", None)
        for shared in ("0", "1"):
            with (
                self.subTest(shared=shared),
                patch.dict(os.environ, {"EPIC_SHARED_READER": shared}),
            ):
                stderr = self.run_hook("git push --all origin", 2)
                self.assertIn("BLOCKED by Codex epic guard:", stderr)
                self.assertNotIn("Traceback", stderr)
        self.assertEqual(self.reads, [])

    def test_claim_rechecks_ready_executor_blockers_and_slot(self) -> None:
        self.action("claim", None)
        command = (
            "gh project item-edit --id PVTI_21 --field-id F --single-select-option-id O"
        )
        self.items = [self.item(21, "Ready")]
        state: dict[str, Any] = {"prs": [], "items": self.items, "merged_prs": []}
        with patch.object(self.gs, "build_state", return_value=state):
            self.run_hook(command)
            for changes in (
                {"status": "Backlog"},
                {"executor": "Claude"},
                {
                    "readiness": "Start after https://github.com/phaabe/live.moafunk.de/issues/22."
                },
            ):
                with self.subTest(changes=changes):
                    self.items[0] = {**self.item(21, "Ready"), **changes}
                    self.run_hook(command, 2)
            self.items[0] = self.item(21, "Ready")
            state["prs"] = [
                {
                    "number": n,
                    "body": "Executor: Codex",
                    "isDraft": True,
                    "baseRefName": "dev/312-interim",
                }
                for n in (6, 7)
            ]
            self.run_hook(command, 2)

    def test_first_push_requires_owned_issue_without_another_slot(self) -> None:
        self.action("claim", None)
        command = "git push -u origin feat/21-work"
        with patch.object(
            self.gs, "recheck", side_effect=AssertionError("no extra slot")
        ):
            self.run_hook(command)
        self.items[0]["status"] = "Ready"
        self.run_hook(command, 2)
        self.items[0] = self.item(21, "In progress", "Claude")
        self.run_hook(command, 2)
        self.items[0] = self.item(21, "In progress")
        self.branch_pulls = [{"number": 5, "state": "closed", "merged_at": "today"}]
        self.run_hook(command, 2)

    def test_pr_push_checks_owned_issue_branch_and_open_pr(self) -> None:
        self.action("fix")
        self.run_hook("git push origin feat/21-work")
        self.run_hook("git push origin feat/22-other", 2)
        self.items[0]["executor"] = "Claude"
        self.run_hook("git push origin feat/21-work", 2)
        self.items[0]["executor"] = "Codex"
        self.pull_data.update(state="closed", merged_at="today")
        self.run_hook("git push origin feat/21-work", 2)

    def test_pr_push_requires_every_issue_in_progress_for_codex(self) -> None:
        self.action("fix")
        self.pull_data["body"] += (
            "\nIssue: https://github.com/phaabe/live.moafunk.de/issues/22"
        )
        command = "git push origin feat/21-work"
        for tool in ("Bash", "exec_command", "shell_command"):
            with self.subTest(tool=tool):
                self.items = [self.item(n, "In progress") for n in (21, 22)]
                self.run_hook(command, tool=tool)
                for index in (0, 1):
                    number = self.items[index]["content"]["number"]
                    for changes in (
                        {"status": "Ready"},
                        {"status": "Done"},
                        {"executor": "Claude"},
                        {"executor": "Unassigned"},
                    ):
                        with self.subTest(number=number, changes=changes):
                            with patch.dict(self.items[index], changes):
                                self.assertIn(
                                    f"issue {number}", self.run_hook(command, 2, tool)
                                )
                    item = self.items.pop(index)
                    self.assertIn(f"issue {number}", self.run_hook(command, 2, tool))
                    self.items.insert(index, item)

    def test_pr_push_without_any_issue_is_refused(self) -> None:
        self.action_file.write_text(json.dumps({"action": "fix", "pr": 5, "sha": SHA}))
        self.pull_data["body"] = "Executor: Codex"
        self.assertIn(
            "names no issue", self.run_hook("git push origin feat/21-work", 2)
        )

    def test_verdict_requires_reviewed_head_and_open_ready_pr(self) -> None:
        self.action("review")
        self.pull_data["body"] = "Executor: Claude"
        command = f"gh pr comment 5 --body 'Review: APPROVED by Codex at {SHA}'"
        self.run_hook(command)
        for changes in (
            {"state": "closed"},
            {"draft": True},
            {"head": {"sha": "b" * 40, "ref": "feat/21-work"}},
        ):
            with self.subTest(changes=changes), patch.dict(self.pull_data, changes):
                self.run_hook(command, 2)

    def test_other_comment_requires_open_pr_and_correct_target(self) -> None:
        self.action("fix")
        self.run_hook("gh pr comment 5 --body note")
        self.run_hook("gh pr comment 6 --body note", 2)
        self.pull_data["state"] = "closed"
        self.run_hook("gh pr comment 5 --body note", 2)

    def test_merge_runs_full_guard_and_pins_head(self) -> None:
        self.action("merge")
        command = f"gh pr merge 5 --squash --match-head-commit {SHA}"
        self.run_hook(command)
        self.assertIn(f"merge:5:{SHA}", self.reads)
        for error in ("reviewer comment was edited", "checks pending", "head moved"):
            self.errors = [error]
            self.assertIn(error, self.run_hook(command, 2))
        self.errors = []
        self.run_hook(command.replace(SHA, "b" * 40), 2)

    def test_failed_fresh_read_blocks_every_write_row(self) -> None:
        cases = [
            ("claim", None, "gh project item-edit --id PVTI_21"),
            ("claim", None, "git push origin feat/21-work"),
            ("fix", 5, "git push origin feat/21-work"),
            (
                "review",
                5,
                f"gh pr comment 5 --body 'Review: APPROVED by Codex at {SHA}'",
            ),
            ("fix", 5, "gh pr comment 5 --body note"),
            ("merge", 5, f"gh pr merge 5 --match-head-commit {SHA}"),
        ]
        self.read_error = self.gs.ReadBlocked("HTTP 502")
        for action, pr, command in cases:
            with self.subTest(action=action, command=command):
                self.action(action, pr)
                self.assertIn("read failed", self.run_hook(command, 2))

    def test_shell_aliases_pause_focus_and_assignment(self) -> None:
        self.action("fix")
        for tool in ("Bash", "exec_command", "shell_command"):
            self.run_hook("gh pr comment 5 --body note", tool=tool)
        self.na.PAUSE_FILE.touch()
        self.run_hook("gh pr comment 5 --body note", 2)
        self.na.PAUSE_FILE.unlink()
        self.na.FOCUS_FILE.write_text("project::Stream\n")
        self.run_hook("gh pr comment 5 --body note", 2)
        self.na.FOCUS_FILE.unlink()
        self.pull_data["body"] = "Executor: Claude"
        self.run_hook("gh pr comment 5 --body note", 2)

    def test_read_only_and_disabled_calls_make_no_fresh_reads(self) -> None:
        self.run_hook("git status --short")
        self.action("fix")
        with patch.dict(os.environ, {"EPIC_SHARED_READER": "0"}):
            self.run_hook("git push origin feat/21-work")
        self.assertEqual(self.reads, [])

    def test_off_mode_pr_actions_skip_write_parser(self) -> None:
        command = 'gh pr comment 5 --body "$(cat /tmp/findings.md)"'
        with patch.dict(os.environ, {"EPIC_SHARED_READER": "0"}):
            for action in ("review", "merge", "fix", "continue"):
                with self.subTest(action=action):
                    self.action(action)
                    self.run_hook(command)
        self.assertEqual(self.reads, [])

    def test_issue_and_shared_actions_keep_write_parser(self) -> None:
        command = 'gh pr comment 5 --body "$(cat /tmp/findings.md)"'
        for shared, action, pr in (
            ("0", "claim", None),
            ("0", "continue", None),
            ("1", "review", 5),
        ):
            with (
                self.subTest(shared=shared, action=action),
                patch.dict(os.environ, {"EPIC_SHARED_READER": shared}),
            ):
                self.action(action, pr)
                self.assertIn("plain command", self.run_hook(command, 2))
        self.assertEqual(self.reads, [])

    def test_bad_action_only_blocks_writes(self) -> None:
        for shared in ("0", "1"):
            for body in (None, "{", "[]"):
                with (
                    self.subTest(shared=shared, body=body),
                    patch.dict(os.environ, {"EPIC_SHARED_READER": shared}),
                ):
                    if body is None:
                        self.action_file.unlink(missing_ok=True)
                    else:
                        self.action_file.write_text(body)
                    self.run_hook("git status --short")
                    self.run_hook("gh issue comment 21 --body claim", 2)
        self.assertEqual(self.reads, [])

    def test_unavailable_trusted_checker_fails_closed(self) -> None:
        with patch.dict(os.environ, {"EPIC_TRUSTED_ROOT": str(self.directory)}):
            self.assertIn(
                "trusted checkout", self.run_hook("git push origin feat/21-work", 2)
            )

    def test_feature_worktree_cannot_override_trusted_checker(self) -> None:
        self.action("fix")
        trusted = self.directory / "trusted"
        helpers = trusted / "scripts/epic"
        helpers.mkdir(parents=True)
        (helpers / "write_checks.py").write_text(
            "AGENT = 'Claude'\n"
            "def check_push(ctx, write): return None\n"
            "original_push = check_push\n"
            "def tool_writes(tool, tool_input, cwd): return [object()]\n"
            "def guard(tool, tool_input, cwd, *, agent):\n"
            "    assert agent == 'Codex'\n"
            "    assert AGENT == 'Claude'\n"
            "    assert check_push is original_push\n"
            "    assert tool == 'Bash'\n"
            "    return 'trusted refusal'\n"
        )
        feature = self.directory / "feature"
        feature.mkdir()
        (feature / "write_checks.py").write_text(
            "raise RuntimeError('feature checker must not load')\n"
        )
        result = subprocess.run(
            [sys.executable, str(ROOT / ".codex/hooks/scripts/epic_guard.py")],
            cwd=feature,
            env={
                **os.environ,
                "EPIC_TRUSTED_ROOT": str(trusted),
                "PYTHONPATH": str(feature),
            },
            input=json.dumps(
                {
                    "tool_name": "exec_command",
                    "tool_input": {
                        "cmd": "git push origin feat/21-work",
                        "workdir": str(feature),
                    },
                }
            ),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("trusted refusal", result.stderr)
        self.assertNotIn("feature checker", result.stderr)

    def test_hook_timeout_covers_default_fresh_read_budget(self) -> None:
        hook = json.loads((ROOT / ".codex/hooks.json").read_text())["hooks"][
            "PreToolUse"
        ][0]["hooks"][0]
        with patch.dict(os.environ, {"EPIC_RECHECK_TIMEOUT_SECONDS": "60"}):
            self.assertGreater(hook["timeout"], self.gs.settings().recheck)

    def test_installed_helper_push_reads_fresh_state_for_actual_worktree(self) -> None:
        worktree = self.directory / "feature worktree"
        subprocess.run(
            ["/usr/bin/git", "init", "-q", "-b", "feat/21-work", str(worktree)],
            check=True,
        )
        self.action("fix")
        command = shlex.join(
            [
                "python3",
                "-I",
                str(self.hook.FEATURE_HELPER),
                "--worktree",
                str(worktree),
                "push",
            ]
        )
        with patch.dict(os.environ, {"GIT_DIR": "/missing-git-dir"}):
            self.run_hook(command)
        self.assertIn("pull:5", self.reads)
        self.assertIn("board", self.reads)
        self.action("claim", None)
        self.run_hook(command)
        self.assertIn("branch:feat/21-work", self.reads)
        self.action("fix")
        self.read_error = self.gs.ReadBlocked("HTTP 502")
        self.assertIn("read failed", self.run_hook(command, 2))
        self.read_error = None
        self.pull_data["head"]["ref"] = "feat/22-other"
        self.assertIn("not PR 5", self.run_hook(command, 2))

    def test_helper_write_variants_fail_closed(self) -> None:
        helper = str(self.hook.FEATURE_HELPER)
        prefix = f"python3 -I {helper}"
        commands = [
            f"{prefix} --worktree {self.directory} push; echo done",
            f"{prefix} --worktree {self.directory} push && echo done",
            f"{prefix} --worktree={self.directory} push",
            f"{prefix} --worktree {self.directory} --worktree /other push",
            f"{prefix} --worktree '$WORKTREE' push",
            f"env {prefix} --worktree {self.directory} push",
            f"python3 {helper} --worktree {self.directory} push",
            f"python3 -I /other/codex-feature-git.py --worktree {self.directory} push",
            f"bash -c '{prefix} --worktree {self.directory} push'",
            f"{prefix} --worktree {self.directory} pu$OP",
        ]
        for command in commands:
            with self.subTest(command=command):
                self.run_hook(command, 2)
        self.assertEqual(self.reads, [])

    def test_helper_local_commit_and_help_do_not_need_fresh_reads(self) -> None:
        helper = str(self.hook.FEATURE_HELPER)
        prefix = f"python3 -I {helper}"
        for command in (
            f"{prefix} --help",
            f"{prefix} --worktree {self.directory} push --help",
            f"{prefix} --worktree {self.directory} commit --message-file /tmp/message.txt",
            f"cat {helper}",
        ):
            with self.subTest(command=command):
                self.run_hook(command)
        self.assertEqual(self.reads, [])


if __name__ == "__main__":
    unittest.main()
