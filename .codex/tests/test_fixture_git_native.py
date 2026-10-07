"""Run the real-Git fixture regressions inside the native runner sandbox."""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/epic"))
import isolated_env  # noqa: E402, F401

import json
import os
import runpy
import subprocess
import tempfile
import unittest
from unittest import mock

from native_controls_fixture import NATIVE, native_env


class FixtureGitSelectionTests(unittest.TestCase):
    def test_tick_uses_feature_git_independent_of_path(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with mock.patch.object(sys, "path", [str(root), *sys.path]):
            import feature_git

            # Force distinct choices without consulting PATH or Apple's cache.
            selected_git = "/Library/Developer/CommandLineTools/usr/bin/git"
            for path_git in ("/usr/bin/git", "/custom/bin/git", None):
                with (
                    self.subTest(path_git=path_git),
                    mock.patch.object(feature_git, "GIT", selected_git),
                    mock.patch("shutil.which", return_value=path_git),
                ):
                    # Execute afresh even if another test already imported tick.
                    tick = runpy.run_path(str(root / "tests/test_codex_tick.py"))
                    self.assertEqual(tick["REAL_GIT"], feature_git.GIT)


@unittest.skipIf(
    sys.platform != "darwin" or os.environ.get("CODEX_SANDBOX") == "seatbelt",
    "requires macOS without a nested sandbox",
)
class FixtureGitNativeTests(unittest.TestCase):
    def test_real_git_fixtures_under_readonly_system_tmp(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fixture-git-native-") as raw:
            base = Path(raw).resolve()
            home = base / "operator"
            codex_home = home / ".codex"
            codex_home.mkdir(parents=True)
            temporary = base / "model-tmp"
            temporary.mkdir()
            config = (
                'approval_policy = "never"\ndefault_permissions = "epic-source-edit"\n'
                '[permissions.epic-source-edit]\nextends = ":workspace"\n'
                "[permissions.epic-source-edit.filesystem]\n"
                '":slash_tmp" = "read"\n":tmpdir" = "read"\n'
                f'{json.dumps(str(temporary))} = "write"\n'
                '[permissions.epic-source-edit.filesystem.":workspace_roots"]\n'
                '".codex" = "read"\n".codex/epic_lock.py" = "write"\n'
                '".codex/tests" = "write"\n".codex/README.md" = "write"\n'
                "[permissions.epic-source-edit.network]\nenabled = false\n"
            )
            (codex_home / "config.toml").write_text(config)
            root = Path(__file__).resolve().parents[2]
            probe = base / "probe.py"
            probe.write_text(
                "import sys, unittest\n"
                f"sys.path.insert(0, {str(root / '.codex/tests')!r})\n"
                "names = [\n"
                "'test_codex_tick.TickTests.test_real_noise_helper_preserves_work_and_cleans_only_generated_blocks',\n"
                "'test_codex_tick.TickTests.test_real_pull_uses_new_selector_in_same_tick_and_rejects_divergence',\n"
                "'test_review_delivery_runner',\n]\n"
                "suite = unittest.defaultTestLoader.loadTestsFromNames(names)\n"
                "result = unittest.TextTestRunner(verbosity=2).run(suite)\n"
                "sys.exit(0 if result.wasSuccessful() and result.testsRun >= 14 else 1)\n"
            )
            result = subprocess.run(
                [
                    str(NATIVE),
                    "sandbox",
                    "-P",
                    "epic-source-edit",
                    "-C",
                    str(root),
                    "--",
                    sys.executable,
                    str(probe),
                ],
                env=native_env(home, codex_home, temporary),
                text=True,
                capture_output=True,
                timeout=300,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("xcrun_db", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
