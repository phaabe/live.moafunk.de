"""Permission gate tests. Run: python3 -m unittest discover -s scripts/epic."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import permission_gate as gate

SHA = "a" * 40
MERGE = (
    f"gh pr merge 410 --repo phaabe/live.moafunk.de --squash --match-head-commit {SHA}"
)


def allowed(command: str, tool: str = "Bash") -> bool:
    return gate.decide(tool, {"command": command})[0]


class DecideTest(unittest.TestCase):
    def test_feature_branch_pushes_are_approved(self) -> None:
        for command in (
            "git push origin feat/312-x",
            "git push -u origin fix/350-retry",
            "git push origin chore/a.b_c",
            "git push origin --delete feat/312-x",
        ):
            with self.subTest(command=command):
                self.assertTrue(allowed(command))

    def test_other_pushes_are_denied(self) -> None:
        for command in (
            "git push",
            "git push origin main",
            "git push origin dev/312-interim",
            "git push origin HEAD:main",
            "git push origin feat/x:main",
            "git push origin +feat/x",
            "git push --force origin feat/x",
            "git push -f origin feat/x",
            "git push origin feat/x --force",
            "git push upstream feat/x",
            "git push origin feat/x feat/y",
            "git push origin feat/../main",
            "git push origin --delete main",
            "git push --tags origin feat/x",
        ):
            with self.subTest(command=command):
                self.assertFalse(allowed(command))

    def test_head_pinned_squash_merge_is_approved(self) -> None:
        self.assertTrue(allowed(MERGE))
        self.assertTrue(allowed(MERGE.replace("--squash", "--squash --delete-branch")))
        self.assertTrue(
            allowed(
                f"gh pr merge 1 --squash --match-head-commit {SHA} "
                "--repo phaabe/live.moafunk.de"
            )
        )

    def test_other_merges_are_denied(self) -> None:
        for command in (
            MERGE.replace("--squash ", ""),
            MERGE.replace("--squash", "--merge"),
            MERGE.replace(SHA, SHA[:7]),
            MERGE.replace(" --match-head-commit " + SHA, ""),
            MERGE + " --admin",
            MERGE + " --auto",
            MERGE.replace("phaabe/live.moafunk.de", "other/repo"),
            MERGE.replace(" --repo phaabe/live.moafunk.de", ""),
            MERGE.replace("410", "abc"),
        ):
            with self.subTest(command=command):
                self.assertFalse(allowed(command))

    def test_chained_or_substituted_commands_are_denied(self) -> None:
        for command in (
            "git push origin feat/x && git push origin main",
            "git push origin feat/x; rm -rf ~",
            "git push origin feat/x | tee log",
            "git push origin $(echo main)",
            "git push origin `echo main`",
            "git push origin feat/x > /dev/null",
            "git push origin feat/x\ngit push origin main",
        ):
            with self.subTest(command=command):
                self.assertFalse(allowed(command))

    def test_other_tools_and_commands_are_denied(self) -> None:
        self.assertFalse(allowed("rm -rf /tmp/x"))
        self.assertFalse(allowed("gh api repos/x/pulls/1/merge -X PUT"))
        self.assertFalse(allowed(MERGE, tool="Write"))
        self.assertFalse(allowed("git push origin 'feat/x"))


class ServerTest(unittest.TestCase):
    """The stdio MCP protocol as `claude -p --permission-prompt-tool` uses it."""

    def call(self, *messages: dict) -> list[dict]:
        with tempfile.TemporaryDirectory() as state:
            process = subprocess.run(
                [sys.executable, str(Path(__file__).with_name("permission_gate.py"))],
                input="".join(json.dumps(m) + "\n" for m in messages),
                capture_output=True,
                text=True,
                timeout=10,
                env={**os.environ, "EPIC_STATE_DIR": state},
            )
            self.log = (Path(state) / "claude-permissions.log").read_text()
        return [json.loads(line) for line in process.stdout.splitlines()]

    def test_initialize_list_and_decide(self) -> None:
        def ask(command: str, number: int) -> dict:
            return {
                "jsonrpc": "2.0",
                "id": number,
                "method": "tools/call",
                "params": {
                    "name": "approve",
                    "arguments": {"tool_name": "Bash", "input": {"command": command}},
                },
            }

        replies = self.call(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            ask("git push origin feat/x", 3),
            ask("git push origin main", 4),
        )
        self.assertEqual([reply["id"] for reply in replies], [1, 2, 3, 4])
        self.assertEqual(replies[1]["result"]["tools"][0]["name"], "approve")
        allow = json.loads(replies[2]["result"]["content"][0]["text"])
        deny = json.loads(replies[3]["result"]["content"][0]["text"])
        self.assertEqual(
            allow,
            {
                "behavior": "allow",
                "updatedInput": {"command": "git push origin feat/x"},
            },
        )
        self.assertEqual(deny["behavior"], "deny")
        self.assertIn("not a feature branch", deny["message"])
        self.assertIn("allow 'git push origin feat/x'", self.log)
        self.assertIn("deny 'git push origin main'", self.log)


if __name__ == "__main__":
    unittest.main()
