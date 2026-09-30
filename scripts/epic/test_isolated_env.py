"""The tests never touch live runner state. Run: python3 -m unittest discover -s scripts/epic"""

from __future__ import annotations

import isolated_env  # noqa: F401  (first: hides live runner state)

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import github_quota
import github_state
import next_action

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]

# Every directory or file setting the runners export or read.
RUNNER_PATHS = (
    "EPIC_STATE_DIR",
    "EPIC_QUOTA_DIR",
    "EPIC_CACHE_DIR",
    "EPIC_LOCK_DIR",
    "EPIC_WORKTREE_DIR",
    "EPIC_WORKTREE",
    "EPIC_TRUSTED_ROOT",
)


def snapshot(root: Path) -> dict[str, tuple[bytes, int] | None]:
    """Every entry under root, with content and mtime for files."""
    found: dict[str, tuple[bytes, int] | None] = {}
    for path in sorted(root.rglob("*")):
        name = str(path.relative_to(root))
        found[name] = (
            (path.read_bytes(), path.stat().st_mtime_ns) if path.is_file() else None
        )
    return found


class ImportTimeTest(unittest.TestCase):
    def test_settings_read_at_import_point_at_a_temporary_home(self) -> None:
        home = Path(isolated_env.PARENT.get("HOME", "/nonexistent")).resolve()
        live = home / ".local" / "state" / "epic-loop"
        self.assertEqual([key for key in os.environ if key.startswith("EPIC_")], [])
        temp = Path(tempfile.gettempdir()).resolve()
        for path in (
            github_quota.STATE_DIR,
            github_state.shared_root(),
            next_action.PAUSE_FILE,
            next_action.FOCUS_FILE,
        ):
            with self.subTest(path=path):
                resolved = path.resolve()
                self.assertTrue(resolved.is_relative_to(temp), resolved)
                self.assertFalse(resolved.is_relative_to(live), resolved)
                self.assertNotEqual(resolved.parent, home)
        self.assertNotEqual(Path.home().resolve(), home)

    def test_github_config_and_tokens_are_removed(self) -> None:
        for name in ("GH_TOKEN", "GITHUB_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
            self.assertNotIn(name, os.environ)
        self.assertEqual(
            Path(os.environ["GH_CONFIG_DIR"]), Path.home() / ".config" / "gh"
        )


class PerTestTest(unittest.TestCase):
    def test_environment_is_restored_after_each_test(self) -> None:
        seen: list[Path] = []

        class Leaky(unittest.TestCase):
            def runTest(self) -> None:  # noqa: N802 (unittest name)
                seen.append(Path.home())
                os.environ["EPIC_QUOTA_DIR"] = "/leaked"
                os.environ["HOME"] = "/leaked"

        before = dict(os.environ)
        for _ in range(2):
            result = unittest.TestResult()
            Leaky().run(result)
            self.assertTrue(result.wasSuccessful())
            self.assertEqual(dict(os.environ), before)
        self.assertNotEqual(seen[0], seen[1])  # a new home for each test
        self.assertNotEqual(seen[0], Path.home())


class ContaminatedParentTest(unittest.TestCase):
    """A runner-like parent: every runner setting and HOME point at stand-in
    live state. Nothing there is created, changed or deleted, and the tests
    still pass. The stand-in is a temporary directory, never real state."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="epic-live-stand-in-")
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.live = self.home / ".local" / "state" / "epic-loop"
        self.live.mkdir(parents=True)
        (self.live / "sentinel.txt").write_text("live runner state\n")
        (self.home / ".epic-pause").write_text("")

    def contaminated(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k != isolated_env.MARKER}
        env |= {name: str(self.live) for name in RUNNER_PATHS}
        env |= {
            "HOME": str(self.home),
            "EPIC_ACTION_FILE": str(self.live / "action.json"),
            "EPIC_SHARED_READER": "1",
            "GH_CONFIG_DIR": str(self.home / ".config" / "gh"),
        }
        return env

    def future_wait(self) -> None:
        now = time.time()
        github_quota.record(self.live, now, github_quota.iso(now + 3600))

    def run_suite(self, *args: str) -> None:
        before = snapshot(self.home)
        out = subprocess.run(
            [sys.executable, *args],
            env=self.contaminated(),
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=600,
        )
        self.assertEqual(out.returncode, 0, out.stderr[-4000:])
        self.assertEqual(snapshot(self.home), before)

    def epic_tests(self) -> None:
        # The quota tests wrote the live wait before; they also read it.
        self.run_suite(
            "-m",
            "unittest",
            "discover",
            "-s",
            "scripts/epic",
            "-p",
            "test_github_quota.py",
        )

    def codex_tests(self) -> None:
        # Unchanged Codex tests through the helper. Run plainly with these
        # runner settings, the shared_metadata and fresh_backoff tests fail.
        self.run_suite(
            "scripts/epic/isolated_env.py",
            ".codex/tests",
            *("-k", "test_shared_metadata"),
            *("-k", "test_fresh_backoff_codes"),
            *("-k", "test_tick_backoff"),
        )

    def test_empty_live_state(self) -> None:
        self.epic_tests()
        self.codex_tests()

    def test_live_state_with_a_future_quota_wait(self) -> None:
        self.future_wait()
        wait = json.loads((self.live / github_quota.WAIT_FILE).read_text())
        self.assertGreater(github_quota.parse_iso(wait["retry_at"]), time.time())
        self.epic_tests()
        self.codex_tests()


if __name__ == "__main__":
    unittest.main()
