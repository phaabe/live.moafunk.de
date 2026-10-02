"""The non-ASCII guard hooks (.claude/hooks/scripts/nonascii-*.sh).

They run with the macOS grep, which has no -P, and must also block long
input: under pipefail, `printf | grep -q` let a long payload through.

Run: python3 -m unittest discover -s scripts/epic
"""

from __future__ import annotations

import isolated_env  # noqa: F401  (first: hides live runner state)

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / ".claude/hooks/scripts"
JQ = shutil.which("jq")
# The system tools only (plus jq), like a runner session's PATH can be.
PATH = ":".join(["/usr/bin", "/bin", os.path.dirname(JQ or "/usr/bin/jq")])
LONG = "a" * 100_000
# Larger than any pipe buffer, so the writer is still writing at the exit.
HUGE = "a" * 1_000_000


@unittest.skipUnless(JQ, "the guards need jq")
class NonAsciiGuards(unittest.TestCase):
    def run_guard(self, name: str, tool_input: dict[str, str]) -> int:
        return subprocess.run(
            ["/bin/bash", str(HOOKS / name)],
            input=json.dumps({"tool_input": tool_input}),
            env={**os.environ, "PATH": PATH},
            capture_output=True, text=True, timeout=30, check=False,
        ).returncode  # fmt: skip

    def test_write_guard(self) -> None:
        for content, expected in (
            ("plain text", 0),
            ("café", 2),
            ("café " + LONG, 2),
            (LONG + " café", 2),
            (LONG, 0),
        ):
            with self.subTest(size=len(content), expected=expected):
                got = self.run_guard(
                    "nonascii-guard.sh", {"file_path": "x.py", "content": content}
                )
                self.assertEqual(got, expected)

    def test_a_grep_that_stops_at_the_first_match_still_blocks(self) -> None:
        # GNU grep -q exits at its first match without reading the rest. With
        # `printf | grep -q` under pipefail, printf then got SIGPIPE and the
        # check read as "no match" for long input. This stand-in reads only
        # the first 4 KiB, like an early exit.
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        grep = tmp / "grep"
        grep.write_text(
            "#!/bin/bash\n"
            'if [[ " $* " == *" -q "* ]]; then\n'
            '    exec /usr/bin/grep "$@" < <(head -c 4096)\n'
            "fi\n"
            'exec /usr/bin/grep "$@"\n'
        )
        grep.chmod(0o755)
        early = f"{tmp}:{PATH}"
        cases = (
            ("nonascii-guard.sh", {"file_path": "x.py", "content": "café " + HUGE}),
            (
                "nonascii-bash-guard.sh",
                {"command": 'git commit -m "café ' + HUGE + '"'},
            ),
        )
        for name, tool_input in cases:
            with self.subTest(guard=name):
                got = subprocess.run(
                    ["/bin/bash", str(HOOKS / name)],
                    input=json.dumps({"tool_input": tool_input}),
                    env={**os.environ, "PATH": early},
                    capture_output=True, text=True, timeout=30, check=False,
                ).returncode  # fmt: skip
                self.assertEqual(got, 2)

    def test_authoring_command_guard(self) -> None:
        for command, expected in (
            ('git commit -m "plain"', 0),
            ('git commit -m "café"', 2),
            ('git commit -m "café ' + LONG + '"', 2),
            ("git commit -m 'x' " + LONG + ' -m "café"', 2),
            ('echo "café"', 0),  # not an authoring command
        ):
            with self.subTest(size=len(command), expected=expected):
                got = self.run_guard("nonascii-bash-guard.sh", {"command": command})
                self.assertEqual(got, expected)


if __name__ == "__main__":
    unittest.main()
