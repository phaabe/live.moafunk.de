#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parents[2] / ".claude/hooks/scripts/gh-watch-guard.py"


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
            'gh pr checks 12 "--watch"',
            "gh run watch 123",
            "gh run watch",
            "git push && gh pr checks 12 --watch",
            "cd x; gh run watch 5 --exit-status",
            "GH_REPO=a/b gh run watch 5",
            "env GH_REPO=owner/repo gh run watch 123",
            "env -u FOO gh run watch 1",
            "time gh run watch 1",
            'bash -c "gh run watch 1"',
            "sh -c 'cd x && gh pr checks 3 --watch'",
            "echo $(gh run watch 1)",
            "/usr/local/bin/gh run watch 1",
            'git commit -m "msg" && gh pr checks 3 --watch',
            "echo 'x'\ngh run watch 9",
            "true|gh run watch 1",
            'echo "`gh run watch 1`"',
            'echo "$(gh run watch 1)"',
            "x=$(cd a && gh pr checks 2 --watch)",
            "cat <<EOF > f\ntext\nEOF\ngh run watch 1",
            "cat <<-EOF\n\ttext\n\tEOF\ngh run watch 1",
            "cat <<EOF\n$(gh pr checks 12 --watch)\nEOF",
            "cat <<EOF\n$(echo hi) `gh run watch` text\nEOF",
            "bash -lc 'gh pr checks 12 --watch'",
            "sh -ec 'gh run watch 1'",
            "if true; then gh pr checks 12 --watch; fi",
            "for i in 1; do gh run watch $i; done",
            "while true; do gh run watch 1; done",
            "! gh run watch 1",
            "{ gh run watch 1; }",
            "cat <<EOF\n'$(gh pr checks 12 --watch)'\nEOF",
            "cat <<EOF\n$(\ngh pr checks 12 --watch\n)\nEOF",
            'cat <<EOF\n"`gh run watch 1`"\nEOF',
            "cat <<EOF\n$(gh run watch 1)",
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
            "grep -r 'gh run watch' .",
            "git commit -m 'uses `gh run watch` and $(gh pr checks 1 --watch)'",
            "cat > body.md <<'EOF'\n- blocks `gh pr checks --watch` and `gh run watch`.\nEOF\ngh api repos/o/r/pulls -F body=@body.md",
            "cat <<'EOF'\n$(gh pr checks 12 --watch)\n`gh run watch`\nEOF",
            'cat <<"EOF"\n$(gh run watch 1)\nEOF',
            "cat <<'EOF'\n$(\ngh pr checks 12 --watch\n)\nEOF",
            "cat <<EOF\n\\$(gh run watch 1)\nEOF",
            "bash -l script.sh gh run watch",
            "bash --login -x script.sh",
            "echo then gh run watch",
            "if true; then echo 'gh run watch'; fi",
            'cat <<< "gh run watch 1"',
            "git log --oneline",
            "echo 'unbalanced",
            "",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(hook(cmd), 0)

    def test_override(self) -> None:
        self.assertEqual(hook("gh run watch 1", CLAUDE_ALLOW_GH_WATCH="1"), 0)

    def test_bad_json_is_allowed(self) -> None:
        result = subprocess.run(
            [str(HOOK)], input="not json", capture_output=True, text=True, check=False
        )
        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
