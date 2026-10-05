"""Section 7 of the Claude tick prompt must agree with the epic rules."""

from __future__ import annotations

import isolated_env  # noqa: F401  (first: hides live runner state)

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROMPT = ROOT / ".claude/commands/epic/epic-tick.md"


def tests_section() -> str:
    text = PROMPT.read_text()
    match = re.search(r"^## 7\. Tests and time\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    assert match, "section 7 is missing"
    return " ".join(match.group(1).split())


class TestsSectionTest(unittest.TestCase):
    def test_base_failure_still_blocks(self) -> None:
        # Rules section 3: a red required suite blocks, also when the base
        # fails too; only a fix or a ticket Anton approved lets it go on.
        section = tests_section()
        self.assertNotIn(
            "name it as a base failure in your PR comment and go on", section
        )
        self.assertIn("also when the base fails too", section)
        self.assertIn("do not mark the PR ready, do not approve it", section)
        self.assertIn("a ticket that Anton approved", section)

    def test_full_suite_reruns_after_a_fix(self) -> None:
        # resolve-conflict says fix, commit, prove again: a fix commit needs a
        # new full run in the same session.
        section = tests_section()
        self.assertNotIn("at most once per session", section)
        self.assertIn("Do not run a full suite again on the same commit", section)
        self.assertIn("after a fix commit", section)

    def test_flaky_test_needs_a_green_full_run(self) -> None:
        # Rules section 3: a single test passing alone does not make the
        # failed suite green; one full rerun on the same commit must pass.
        section = tests_section()
        self.assertNotIn("name it as flaky in your PR comment and go on", section)
        self.assertIn("run the full suite once more on the same commit", section)
        self.assertIn("Only a green full run counts", section)
        self.assertIn("also when the base fails too or the test is flaky", section)


if __name__ == "__main__":
    unittest.main()
