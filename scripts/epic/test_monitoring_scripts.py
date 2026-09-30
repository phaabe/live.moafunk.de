"""Run the real monitoring start scripts with fake docker/python3/launchctl."""

from __future__ import annotations

import isolated_env  # noqa: F401  (first: hides live runner state)

import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import monitor

ROOT = Path(__file__).resolve().parents[2]
PYTHON = shutil.which("python3") or sys.executable
# Record the tool name, EPIC_STATE_DIR and argv; never run the real tool.
RECORD = (
    f"#!{PYTHON}\n"
    "import json, os, sys\n"
    "with open(os.environ['TEST_CALLS'], 'a') as calls:\n"
    "    calls.write(json.dumps([os.path.basename(sys.argv[0]),"
    " os.environ.get('EPIC_STATE_DIR'), sys.argv[1:]]) + '\\n')\n"
)


class StartScriptTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="agent-monitoring-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.repo = self.root / "checkout"
        self.calls = self.root / "calls.jsonl"
        for rel in (
            "tools/agent-monitoring/run.sh",
            "tools/agent-monitoring/service.sh",
            "tools/agent-monitoring/preview.sh",
        ):
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / rel, self.repo / rel)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.caller = self.root / "caller"
        self.caller.mkdir()
        self.env = {
            **os.environ,
            "HOME": str(self.home),
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "TEST_CALLS": str(self.calls),
        }
        self.env.pop("EPIC_STATE_DIR", None)

    def stub(self, *names: str) -> None:
        for name in names:
            path = self.bin / name
            path.write_text(RECORD)
            path.chmod(0o755)

    def run_script(self, script: str, *args: str, **env: str) -> None:
        subprocess.run(
            ["/bin/bash", str(self.repo / "tools/agent-monitoring" / script), *args],
            cwd=self.caller,
            env={**self.env, **env},
            check=True,
        )

    def calls_made(self) -> list[list[object]]:
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def assert_run_uses(self, state_dir: Path) -> None:
        docker, python = self.calls_made()
        self.assertEqual(docker[0], "docker")
        self.assertEqual(docker[1], str(state_dir))
        self.assertEqual(python[0], "python3")
        argv = python[2]
        assert isinstance(argv, list)
        self.assertEqual(argv[-2:], ["--state-dir", str(state_dir)])

    def test_run_passes_custom_state_dir_to_alloy_and_collector(self) -> None:
        self.stub("docker", "python3")
        custom = self.root / "custom"
        self.run_script("run.sh", "--state-dir", str(custom), "--once")
        self.assert_run_uses(custom)

    def test_run_resolves_relative_state_dir_against_caller(self) -> None:
        self.stub("docker", "python3")
        self.run_script("run.sh", "--state-dir=rel/state")
        self.assert_run_uses(self.caller / "rel/state")

    def test_run_uses_env_state_dir_for_both(self) -> None:
        self.stub("docker", "python3")
        custom = self.root / "from-env"
        self.run_script("run.sh", EPIC_STATE_DIR=str(custom))
        self.assert_run_uses(custom)

    def test_run_defaults_to_runner_state_dir(self) -> None:
        self.stub("docker", "python3")
        self.run_script("run.sh")
        self.assert_run_uses(self.home / ".local/state/epic-loop")

    def test_service_passes_env_state_dir_to_alloy_and_collector(self) -> None:
        self.stub("docker")
        launchctl = self.bin / "launchctl"
        launchctl.write_text('#!/bin/bash\n[[ "$1" != print ]]\n')
        launchctl.chmod(0o755)
        custom = self.root / "from-env"
        self.run_script("service.sh", "start", EPIC_STATE_DIR=str(custom))
        (docker,) = self.calls_made()
        self.assertEqual(docker[1], str(custom))
        plist = self.repo / "tools/agent-monitoring/runtime/collector.plist"
        with plist.open("rb") as source:
            argv = plistlib.load(source)["ProgramArguments"]
        self.assertEqual(argv[-2:], ["--state-dir", str(custom)])

    def test_preview_gives_fixtures_and_compose_one_absolute_state_dir(self) -> None:
        """Codex review round 3: the fixtures run from the repo root and
        Compose resolves paths next to compose.yaml; a relative dir split."""
        self.stub("docker", "python3")
        self.run_script("preview.sh", "normal", AGENT_PREVIEW_STATE_DIR="rel/state")
        expected = str((self.caller / "rel/state").resolve())
        calls = self.calls_made()
        fixture_dirs = {
            argv[argv.index("--state-dir") + 1]
            for tool, _, argv in calls
            if tool == "python3" and isinstance(argv, list)
        }
        compose_dirs = {
            env
            for tool, env, argv in calls
            if tool == "docker" and isinstance(argv, list) and "up" in argv
        }
        self.assertEqual(fixture_dirs, {expected})
        self.assertEqual(compose_dirs, {expected})


class DefaultStateDirTest(unittest.TestCase):
    def test_env_overrides_default(self) -> None:
        with patch.dict(os.environ, {"EPIC_STATE_DIR": "/tmp/epic-state"}):
            self.assertEqual(monitor.default_state_dir(), Path("/tmp/epic-state"))

    def test_empty_env_uses_default_like_compose(self) -> None:
        with patch.dict(os.environ, {"EPIC_STATE_DIR": ""}):
            self.assertEqual(
                monitor.default_state_dir(), Path.home() / ".local/state/epic-loop"
            )


if __name__ == "__main__":
    unittest.main()
