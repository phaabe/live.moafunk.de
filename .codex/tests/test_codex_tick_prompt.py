"""Keep model instructions compatible with the worktree sandbox."""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "epic"))
import isolated_env  # noqa: E402, F401

import unittest


class TickPromptTests(unittest.TestCase):
    def test_fetch_skips_read_only_fetch_head(self) -> None:
        prompt = (Path(__file__).resolve().parents[1] / "epic-tick.md").read_text()

        self.assertIn("`git fetch --no-write-fetch-head origin`", prompt)
        self.assertNotRegex(prompt, r"(?i)\bfetch\s+origin\b")


if __name__ == "__main__":
    unittest.main()
