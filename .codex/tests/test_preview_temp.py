"""Keep preview history under the caller's private TMPDIR."""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/epic"))
import isolated_env  # noqa: E402, F401
import test_monitoring_scripts as fixtures  # noqa: E402

import unittest


class PreviewTempTests(unittest.TestCase):
    def test_history_uses_tmpdir_with_spaces(self) -> None:
        fixture = fixtures.StartScriptTest()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        fixture.stub("docker", "python3")
        scratch = fixture.root / "private scratch"
        scratch.mkdir()
        fixture.run_script("preview.sh", "normal", TMPDIR=str(scratch))
        histories = [
            Path(argv[argv.index("--history") + 1]).parent
            for tool, _, argv in fixture.calls_made()
            if tool == "python3" and "--history" in argv
        ]
        self.assertEqual(len(histories), 1)
        self.assertEqual(histories[0].parent, scratch)
        self.assertTrue(histories[0].name.startswith("agent-preview."))
        self.assertFalse(histories[0].exists())


if __name__ == "__main__":
    unittest.main()
