"""The real Codex hook enforces the shared host-side promotion barrier."""

from __future__ import annotations

from pathlib import Path
import sys
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts/epic"))
import isolated_env  # noqa: E402, F401
import runtime  # noqa: E402

sys.path.insert(0, str(ROOT / ".codex/tests"))
from protected_home_fixture import prepare  # noqa: E402
from test_runtime import make_writable, read_only  # noqa: E402

HOOK = ROOT / ".codex/hooks/scripts/epic_guard.py"
spec = importlib.util.spec_from_file_location("codex_promotion_guard", HOOK)
assert spec is not None and spec.loader is not None
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class PromotionGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.locks = self.root / "locks"
        self.locks.mkdir()
        self.marker = self.locks / runtime.MARKER
        env = patch.dict(
            os.environ,
            {
                "EPIC_LOCK_DIR": str(self.locks),
                "EPIC_RUNTIME_HOME": str(self.root / "runtime-home"),
            },
        )
        env.start()
        self.addCleanup(env.stop)

    def run_hook(
        self, tool: str = "exec_command", command: str = "git status", **env: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-I", str(HOOK)],
            input=json.dumps(
                {
                    "tool_name": tool,
                    "tool_input": {"cmd": command, "command": command},
                    "cwd": str(self.root),
                }
            ),
            env={**os.environ, **env},
            capture_output=True,
            text=True,
            timeout=15,
        )

    def publish(self, processes: list[dict] | None = None) -> None:
        self.marker.write_text(
            json.dumps(
                {
                    "promotion_id": "fixture",
                    "admitted": [{"processes": processes or []}],
                }
            )
        )

    def test_all_shell_aliases_block_before_no_action_and_shared_reader_returns(
        self,
    ) -> None:
        self.publish()
        action = self.root / "action.json"
        action.write_text('{"action":"review","pr":5}')
        for selected in ("", str(action)):
            for shared in ("0", "1"):
                for tool in ("Bash", "exec_command", "shell_command"):
                    for command in (
                        "ls -la",
                        "python3 -c 'import os; os.remove(\"x\")'",
                        "if true; then git add x; fi",
                    ):
                        with self.subTest(selected=selected, shared=shared, tool=tool):
                            result = self.run_hook(
                                tool,
                                command,
                                EPIC_ACTION_FILE=selected,
                                EPIC_SHARED_READER=shared,
                            )
                            self.assertEqual(result.returncode, 2, result.stderr)
                            self.assertIn("Runtime promotion", result.stderr)

    def test_only_github_mcp_reads_and_non_shell_tools_pass(self) -> None:
        self.publish()
        for tool in (
            "mcp__github__create_issue",
            "mcp__github__push_files",
            "mcp__github__add_issue_comment",
            "mcp__github__unknown",
        ):
            with self.subTest(tool=tool):
                self.assertEqual(self.run_hook(tool).returncode, 2)
        for tool in (
            "mcp__github__get_issue",
            "mcp__github__list_commits",
            "mcp__github__search_code",
            "Read",
            "Grep",
            "Glob",
            "Edit",
        ):
            with self.subTest(tool=tool):
                result = self.run_hook(tool)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_configured_manual_session_without_runtime_env(self) -> None:
        runtime.write_configured(Path(os.environ["EPIC_RUNTIME_HOME"]))
        result = self.run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        # No-marker manual calls must not import a runtime or checker at all.
        with patch.object(
            guard.importlib, "import_module", side_effect=AssertionError("import")
        ):
            guard.runner_write_check("exec_command", {"cmd": "ls"}, self.root)
        self.publish()
        self.assertEqual(self.run_hook().returncode, 2)
        self.assertEqual(self.run_hook("mcp__github__get_issue").returncode, 0)

    def test_valid_host_ancestry_passes_without_an_environment_claim(self) -> None:
        entry = runtime.process_entry(os.getpid(), runtime.process_table())
        self.publish([entry])
        result = self.run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_forged_environment_and_wrong_start_time_refuse(self) -> None:
        entry = runtime.process_entry(os.getpid(), runtime.process_table())
        entry["start"] = "wrong start time"
        self.publish([entry])
        result = self.run_hook(EPIC_ADMITTED="1", EPIC_TICK_ID="fixture")
        self.assertEqual(result.returncode, 2, result.stderr)

    def test_dead_admitted_wrapper_refuses(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"]
        )
        try:
            entry = runtime.process_entry(process.pid, runtime.process_table())
        finally:
            process.terminate()
            process.wait(timeout=5)
        self.publish([entry])
        self.assertEqual(self.run_hook().returncode, 2)

    def test_unreadable_marker_stat_blocks_before_import(self) -> None:
        with (
            patch.object(Path, "lstat", side_effect=PermissionError("denied")),
            patch.object(
                guard.importlib, "import_module", side_effect=AssertionError("import")
            ),
        ):
            with self.assertRaisesRegex(ValueError, "marker is unreadable"):
                guard.runner_write_check("exec_command", {"cmd": "ls"}, self.root)

    def test_unreadable_and_malformed_marker_refuse(self) -> None:
        self.publish()
        self.marker.chmod(0)
        try:
            self.assertEqual(self.run_hook().returncode, 2)
        finally:
            self.marker.chmod(0o600)
        self.marker.write_text("bad json")
        result = self.run_hook()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("unreadable", result.stderr)

    def test_missing_runtime_never_falls_back_to_repo_checker(self) -> None:
        result = self.run_hook(
            EPIC_RUNTIME_ROOT=str(self.root / "missing"), EPIC_TRUSTED_ROOT=str(ROOT)
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("Protected hook runtime", result.stderr)

    def test_legacy_protected_hook_validates_without_an_action(self) -> None:
        result = self.run_hook(
            EPIC_CODEX_PROTECTED_CONFIG=str(self.root / "missing.json"),
            EPIC_RUNTIME_LEGACY="1",
            EPIC_TRUSTED_ROOT=str(ROOT),
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("Protected hook runtime", result.stderr)

    def test_pinned_shell_hook_ignores_changed_repo_checker_and_refuses_changed_runtime(
        self,
    ) -> None:
        repo = self.root / "repo"
        repo.mkdir()
        for args in (
            ("init", "-q"),
            ("remote", "add", "origin", "https://example.invalid/repo"),
        ):
            subprocess.run(
                ["git", "-C", str(repo), *args], check=True, capture_output=True
            )
        runtime_home = Path(os.environ["EPIC_RUNTIME_HOME"])
        install = runtime_home / "revisions" / ("1" * 40)
        (install / ".codex/hooks/scripts").mkdir(parents=True)
        shutil.copytree(
            ROOT / "scripts/epic",
            install / "scripts/epic",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        for relative in (
            ".codex/protected_home.py",
            ".codex/codex-tick.sh",
            ".codex/tick-result.schema.json",
            ".codex/hooks/scripts/epic_guard.py",
            ".codex/hooks/scripts/epic-guard.sh",
        ):
            shutil.copy2(ROOT / relative, install / relative)
        fixture = prepare(self.root / "fixture", repo, install)
        (install / ".codex/runtime").mkdir()
        for relative in ("config.toml", "hooks.json", "rules"):
            source = fixture.home / relative
            destination = install / ".codex/runtime" / relative
            if source.is_dir():
                shutil.copytree(source, destination)
            else:
                shutil.copy2(source, destination)
        executables = {
            "python3": {"path": sys.executable, "version": sys.version.split()[0]},
            "git": {"path": shutil.which("git"), "version": "fixture"},
            "gtimeout": {
                "path": shutil.which("gtimeout") or shutil.which("timeout"),
                "version": "fixture",
            },
            "gitnexus": {"path": "/usr/bin/true", "version": "fixture"},
        }
        for entry in executables.values():
            assert entry["path"] is not None
            entry["path"] = str(Path(entry["path"]).resolve())
        runtime.build_manifest(
            install,
            "1" * 40,
            executables,
            settings=tuple(
                name for name in runtime.SETTINGS if name.startswith("codex_")
            ),
        )
        runtime.write_pin(
            runtime_home,
            "1" * 40,
            runtime.sha256_file(install / runtime.MANIFEST),
            "fixture",
            None,
        )
        read_only(install)
        self.addCleanup(make_writable, install)
        (repo / "scripts/epic").mkdir(parents=True)
        (repo / "scripts/epic/write_checks.py").write_text(
            "raise RuntimeError('repo checker ran')\n"
        )
        action = self.root / "action.json"
        action.write_text('{"action":"review","pr":5}')
        env = {
            **os.environ,
            **fixture.env,
            "EPIC_TRUSTED_ROOT": str(repo),
            "EPIC_RUNTIME_ROOT": str(install),
            "EPIC_RUNTIME_REVISION": "1" * 40,
            "EPIC_RUNTIME_MANIFEST": str(install / runtime.MANIFEST),
            "EPIC_ACTION_FILE": str(action),
            "EPIC_SHARED_READER": "1",
        }
        env.pop("EPIC_RUNTIME_LEGACY", None)

        def invoke() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["/bin/bash", str(install / ".codex/hooks/scripts/epic-guard.sh")],
                input=json.dumps(
                    {"tool_name": "exec_command", "tool_input": {"cmd": "git status"}}
                ),
                env=env,
                text=True,
                capture_output=True,
                timeout=20,
            )

        allowed = invoke()
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.publish()
        blocked = invoke()
        self.assertEqual(blocked.returncode, 2, blocked.stderr)
        self.assertIn("Runtime promotion", blocked.stderr)
        self.assertNotIn("repo checker ran", blocked.stderr)
        checker = install / "scripts/epic/write_checks.py"
        checker.chmod(0o644)
        checker.write_text("raise RuntimeError('changed runtime checker ran')\n")
        checker.chmod(0o444)
        changed = invoke()
        self.assertEqual(changed.returncode, 2, changed.stderr)
        self.assertNotIn("changed runtime checker ran", changed.stderr)


if __name__ == "__main__":
    unittest.main()
