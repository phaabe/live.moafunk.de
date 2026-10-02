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

# The Codex tests that use the runner settings from the environment. Run
# without isolation under the stand-in settings, each of them fails. Each
# "-k" is a full test id, so it matches only that test.
# Not selected: test_tick_backoff.py passes and writes nothing there even
# without isolation (its state is in its own temporary directories), and
# test_review_delivery_runner.py takes about 90 s.
CODEX_TESTS = {
    "test_feature_worktree.py": (
        "test_feature_worktree.FeatureWorktreeTests"
        ".test_shared_metadata_failure_starts_no_model_and_sets_no_cooldown",
        "test_feature_worktree.FeatureWorktreeTests"
        ".test_shared_metadata_prepares_pr_worktree",
    ),
    "test_codex_tick.py": (
        "test_codex_tick.TickTests"
        ".test_fresh_backoff_codes_preserve_records_and_start_no_model",
    ),
}


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

    def run_suites(self, *suites: tuple[tuple[str, ...], int | None]) -> None:
        """Run (arguments, expected test count) suites at the same time.
        Each must pass, and the stand-in must not change."""
        before = snapshot(self.home)
        runs = []
        for args, count in suites:
            process = subprocess.Popen(
                [sys.executable, *args],
                env=self.contaminated(),
                cwd=REPO,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.addCleanup(process.kill)
            runs.append((args, count, process))
        for args, count, process in runs:
            _, err = process.communicate(timeout=120)
            with self.subTest(suite=" ".join(args)):
                self.assertEqual(process.returncode, 0, err[-4000:])
                if count is not None:
                    self.assertRegex(err, rf"(?m)^Ran {count} tests? in ")
        self.assertEqual(snapshot(self.home), before)

    def run_selected(self) -> None:
        # The quota tests wrote the live wait before; they also read it.
        epic = ("-m", "unittest", "discover", "-s", "scripts/epic")
        epic += ("-p", "test_github_quota.py")
        # Unchanged Codex tests through the helper. "-p" picks the module,
        # so a module whose load_tests() ignores "-k" does not run.
        codex = [
            (
                (
                    "scripts/epic/isolated_env.py",
                    ".codex/tests",
                    *("-p", module),
                    *(arg for test in tests for arg in ("-k", test)),
                ),
                len(tests),
            )
            for module, tests in CODEX_TESTS.items()
        ]
        self.run_suites((epic, None), *codex)

    def test_empty_live_state(self) -> None:
        self.run_selected()

    def test_live_state_with_a_future_quota_wait(self) -> None:
        self.future_wait()
        wait = json.loads((self.live / github_quota.WAIT_FILE).read_text())
        self.assertGreater(github_quota.parse_iso(wait["retry_at"]), time.time())
        self.run_selected()


if __name__ == "__main__":
    unittest.main()
