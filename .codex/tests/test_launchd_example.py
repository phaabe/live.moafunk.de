"""Validate the scheduler example without installing or starting a job."""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/epic"))
import isolated_env  # noqa: E402, F401 (before production modules or fixtures)

import plistlib
import unittest


TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / "launchd/de.moafunk.codex-epic-loop.plist.example"
)


class LaunchdExampleTests(unittest.TestCase):
    def test_template_uses_interval_without_restart_policy(self) -> None:
        data = plistlib.loads(TEMPLATE.read_bytes())
        self.assertEqual(data["Label"], "de.moafunk.codex-epic-loop")
        self.assertIs(type(data["StartInterval"]), int)
        self.assertEqual(data["StartInterval"], 180)
        self.assertNotIn("KeepAlive", data)
        self.assertNotIn("RunAtLoad", data)
        self.assertNotIn("StartCalendarInterval", data)

    def test_rendered_paths_keep_spaces_in_single_arguments(self) -> None:
        substitutions = {
            "__HOME__": "/Users/test operator",
            "__RUNNER_CHECKOUT__": "/Users/test operator/git/codex runner",
            "__CODEX_BIN_DIR__": "/Users/test operator/tools/bin",
            "__PROTECTED_RUNTIME_HOME__": "/Users/test operator/runtime",
            "__PROTECTED_CODEX_HOME__": "/Users/test operator/runtime/codex-homes/legacy",
            "__PROTECTED_CODEX_BINDING__": "/Users/test operator/runtime/codex-binding.json",
        }
        rendered = TEMPLATE.read_text()
        for placeholder, value in substitutions.items():
            rendered = rendered.replace(placeholder, value)
        self.assertNotIn("__", rendered)
        data = plistlib.loads(rendered.encode())
        self.assertEqual(
            data["ProgramArguments"],
            [
                "/bin/bash",
                substitutions["__RUNNER_CHECKOUT__"] + "/.codex/codex-tick.sh",
            ],
        )
        self.assertEqual(data["WorkingDirectory"], substitutions["__RUNNER_CHECKOUT__"])
        self.assertEqual(
            data["EnvironmentVariables"]["HOME"], substitutions["__HOME__"]
        )
        search_paths = data["EnvironmentVariables"]["PATH"].split(":")
        self.assertEqual(data["EnvironmentVariables"]["EPIC_RUNTIME_LEGACY"], "1")
        for name, placeholder in (
            ("EPIC_RUNTIME_HOME", "__PROTECTED_RUNTIME_HOME__"),
            ("CODEX_HOME", "__PROTECTED_CODEX_HOME__"),
            ("EPIC_CODEX_PROTECTED_CONFIG", "__PROTECTED_CODEX_BINDING__"),
        ):
            self.assertEqual(
                data["EnvironmentVariables"][name], substitutions[placeholder]
            )
        for directory in (
            substitutions["__CODEX_BIN_DIR__"],
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
        ):
            self.assertIn(directory, search_paths)
        for key in ("WorkingDirectory", "StandardOutPath", "StandardErrorPath"):
            self.assertTrue(Path(data[key]).is_absolute())
            self.assertNotIn("~", data[key])
            self.assertNotIn("$", data[key])
        for key in ("StandardOutPath", "StandardErrorPath"):
            self.assertEqual(
                Path(data[key]).parent,
                Path(substitutions["__HOME__"]) / ".local/state/epic-loop",
            )


if __name__ == "__main__":
    unittest.main()
