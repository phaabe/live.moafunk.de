"""Plain test entry points must not inherit a tick's live runner state."""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/epic"))
import isolated_env  # noqa: E402, F401 (before production modules or fixtures)

import os
import subprocess
import tempfile
import time
import unittest

import github_quota

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / ".codex/tests"


def snapshot(root: Path) -> dict[str, tuple[bytes | None, int]]:
    """Include directory mtimes to catch creation and deletion of temporary files."""
    return {
        str(path.relative_to(root)): (
            path.read_bytes() if path.is_file() else None,
            path.stat().st_mtime_ns,
        )
        for path in (root, *sorted(root.rglob("*")))
    }


class RunnerIsolationTests(unittest.TestCase):
    def check_entry_points(self, *, future_wait: bool) -> None:
        with tempfile.TemporaryDirectory(prefix="codex-live-stand-in-") as directory:
            root = Path(directory)
            home = root / "home"
            live = home / ".local/state/epic-loop"
            live.mkdir(parents=True)
            if future_wait:
                now = time.time()
                github_quota.record(live, now, github_quota.iso(now + 3600))
            env = {k: v for k, v in os.environ.items() if k != isolated_env.MARKER}
            env.update(
                {
                    name: str(live)
                    for name in (
                        "EPIC_STATE_DIR",
                        "EPIC_QUOTA_DIR",
                        "EPIC_CACHE_DIR",
                        "EPIC_LOCK_DIR",
                        "EPIC_WORKTREE_DIR",
                        "EPIC_WORKTREE",
                        "EPIC_TRUSTED_ROOT",
                        "XDG_STATE_HOME",
                        "XDG_CACHE_HOME",
                        "XDG_CONFIG_HOME",
                    )
                }
            )
            env.update(
                HOME=str(home),
                GH_CONFIG_DIR=str(home / ".config/gh"),
                CLAUDE_CONFIG_DIR=str(home / ".claude"),
                CODEX_HOME=str(home / ".codex"),
                EPIC_ACTION_FILE=str(live / "action.json"),
                EPIC_SHARED_READER="1",
            )
            bin_dir = root / "bin"
            bin_dir.mkdir()
            calls = root / "unexpected-calls"
            for command in ("gh", "codex", "claude"):
                stub = bin_dir / command
                stub.write_text(
                    f"#!{sys.executable}\n"
                    "from pathlib import Path\n"
                    f"Path({str(calls)!r}).touch()\n"
                    "raise SystemExit(99)\n"
                )
                stub.chmod(0o755)
            env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]
            before = snapshot(home)

            # Each file must isolate on its own, even if discovery imported
            # another test first. Run in fresh interpreters without the marker.
            probe = (
                "import os, runpy, sys\n"
                "from pathlib import Path\n"
                "home = Path.home()\n"
                "sys.path.insert(0, str(Path(sys.argv[1]).parent))\n"
                "runpy.run_path(sys.argv[1])\n"
                "assert not any(k.startswith(('EPIC_', 'XDG_')) for k in os.environ)\n"
                "assert Path.home() != home\n"
                "import github_quota\n"
                "assert not github_quota.STATE_DIR.is_relative_to(home)\n"
            )
            commands = [
                (["-c", probe, str(path)], None)
                for path in sorted(TESTS.glob("test_*.py"))
            ]
            # These cases failed in a tick before the import-first fix. Keep
            # subprocess discovery focused so this regression never recurses.
            selected = {
                "test_feature_worktree": (
                    "FeatureWorktreeTests.test_shared_metadata_failure_starts_no_model_and_sets_no_cooldown",
                    "FeatureWorktreeTests.test_shared_metadata_prepares_pr_worktree",
                ),
                "test_codex_tick": (
                    "TickTests.test_fresh_backoff_codes_preserve_records_and_start_no_model",
                ),
            }
            # Some load_tests hooks ignore name filters. Filter the discovered
            # cases by exact id as well, then check none were lost or repeated.
            discover = (
                "import sys, unittest\n"
                "wanted = sys.argv[1:]\n"
                "loader = unittest.TestLoader()\n"
                "loader.testNamePatterns = wanted\n"
                "suite = loader.discover('.codex/tests')\n"
                "assert not loader.errors, loader.errors\n"
                "def cases(suite):\n"
                "    for test in suite:\n"
                "        if isinstance(test, unittest.TestSuite):\n"
                "            yield from cases(test)\n"
                "        else:\n"
                "            yield test\n"
                "tests = [t for t in cases(suite) if t.id() in wanted]\n"
                "assert sorted(t.id() for t in tests) == sorted(wanted)\n"
                "result = unittest.TextTestRunner().run(unittest.TestSuite(tests))\n"
                "sys.exit(not result.wasSuccessful())\n"
            )
            ids = [
                f"{module}.{test_id}"
                for module, test_ids in selected.items()
                for test_id in test_ids
            ]
            commands.append((["-c", discover, *ids], 3))
            commands.extend(
                ([f".codex/tests/{module}.py", *ids], len(ids))
                for module, ids in selected.items()
            )
            for args, expected_count in commands:
                with self.subTest(entry=args[-1], future_wait=future_wait):
                    result = subprocess.run(
                        [sys.executable, *args],
                        cwd=ROOT,
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=120,
                    )
                    self.assertEqual(snapshot(home), before)
                    self.assertFalse(calls.exists(), "Unexpected GitHub/model command")
                    self.assertEqual(result.returncode, 0, result.stderr[-6000:])
                    if expected_count is not None:
                        self.assertRegex(
                            result.stderr,
                            rf"(?m)^Ran {expected_count} tests? in ",
                        )

    def test_empty_runner_state(self) -> None:
        self.check_entry_points(future_wait=False)

    def test_future_quota_wait(self) -> None:
        self.check_entry_points(future_wait=True)


if __name__ == "__main__":
    unittest.main()
