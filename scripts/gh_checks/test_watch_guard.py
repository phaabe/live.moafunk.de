#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parents[2] / ".claude/hooks/scripts/gh-watch-guard.sh"


def hook(command: str, **env: str) -> int:
    result = subprocess.run(
        [str(HOOK)],
        input=json.dumps({"tool_input": {"command": command}}),
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        check=False,
    )
    return result.returncode


class WatchGuard(unittest.TestCase):
    def test_blocks_watchers(self) -> None:
        for cmd in (
            "gh pr checks 12 --watch",
            "gh pr checks --watch 12",
            "gh pr checks 12 --interval 30 --watch",
            "gh pr checks 12 --watch=true",
            "gh run watch 123",
            "gh run watch",
            "git push && gh pr checks 12 --watch",
            "cd x; gh run watch 5 --exit-status",
            "GH_REPO=a/b gh run watch 5",
            'git commit -m "msg" && gh pr checks 3 --watch',
            "echo 'x'\ngh run watch 9",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(hook(cmd), 2)

    def test_allows_harmless_commands(self) -> None:
        for cmd in (
            "gh pr checks 12",
            "gh run view 123",
            "gh run list",
            "python3 scripts/gh_checks/wait_checks.py 12",
            "gh pr checks 12; echo --watch",
            "echo gh run watching",
            "echo gh run watch",
            'git commit -m "fix: stop using gh pr checks --watch"',
            "git commit -m 'body\ngh run watch polls GraphQL\n'",
            'git commit -m "a \\"quoted\\" gh run watch"',
            "git log --oneline",
            "",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(hook(cmd), 0)

    def test_override(self) -> None:
        self.assertEqual(hook("gh run watch 1", CLAUDE_ALLOW_GH_WATCH="1"), 0)


if __name__ == "__main__":
    unittest.main()
