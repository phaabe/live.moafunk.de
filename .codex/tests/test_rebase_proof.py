"""Exercise the real macOS sandbox used by the protected proof writer."""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/epic"))
import isolated_env  # noqa: E402, F401
import rebase_policy  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rebase_proof  # noqa: E402

import json
import os
import shutil
import socket
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch


class ProofCommandTests(unittest.TestCase):
    """Test command handoff only; the fake Codex does not enforce a sandbox."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="proof-command-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        (self.home / ".rustup").mkdir(parents=True)
        (self.home / ".rustup/settings.toml").write_text(
            'default_toolchain = "fixture"'
        )
        self.codex = self.root / "codex"
        self.codex.write_text(
            f"#!{sys.executable}\n"
            "import os, sys, tomllib\n"
            "args = sys.argv[1:]\n"
            "profile = tomllib.loads(args[args.index('-c') + 1])['permissions']\n"
            "permission, = profile.values()\n"
            "scratch = args[args.index('--allow-unix-socket') + 1]\n"
            "assert permission['filesystem'] == {':slash_tmp': 'read', "
            "':tmpdir': 'read', scratch: 'write'}\n"
            "assert permission['network'] == {'enabled': False}\n"
            "assert os.environ.get('CODEX_PROOF_SANDBOX') is None\n"
            "command = args[args.index('--') + 1:]\n"
            # Model the diagnosed filtering and PATH rewriting, not Seatbelt.
            "env = {'HOME': os.environ['HOME'], 'TMPDIR': scratch, "
            "'PATH': '/usr/bin:/bin', 'EPIC_STATE_DIR': '/must-not-reach-suite'}\n"
            "os.execve(command[0], command, env)\n"
        )
        self.codex.chmod(0o755)
        self.policy = SimpleNamespace(
            SUITE_TIMEOUT=10,
            suite_env=lambda: {
                **os.environ,
                "HOME": str(self.home),
                "EPIC_STATE_DIR": "/must-not-reach-suite",
                "XDG_STATE_HOME": "/must-not-reach-suite",
                "GIT_DIR": "/must-not-reach-suite",
                "PYTHONPATH": "/must-not-reach-suite",
            },
        )

    def run_suite(self, command: list[str]) -> dict[str, object]:
        suite = {"name": "fixture", "cwd": ".", "command": command}
        result = rebase_proof.sandbox_suite(
            self.root, suite, self.policy, str(self.codex), os.environ["PATH"]
        )
        self.assertEqual(result["command"], command)
        return result

    def test_marker_git_and_clean_environment_reach_suite_descendants(self) -> None:
        source = Path(rebase_proof.__file__).parent
        program = """
import json, os, pathlib, shutil, subprocess, sys
before = dict(os.environ)
assert (pathlib.Path.home() / '.rustup/settings.toml').read_text() == 'default_toolchain = "fixture"'
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, str(pathlib.Path(sys.argv[1]) / 'tests'))
import feature_git, test_feature_rebase, test_rebase_proof
assert pathlib.Path.home().is_relative_to(os.environ['TMPDIR'])
nested = [
    test_feature_rebase.FeatureRebaseTests.test_pinned_suite_proofs_reject_failed_or_skipped_suites,
    test_feature_rebase.FeatureRebaseTests.test_prove_blocks_suite_writes_to_protected_files,
    test_rebase_proof.ProofSandboxTests.test_descendants_can_write_worktree_and_temp_but_not_protected_paths,
]
for test in nested:
    assert test.__unittest_skip__, test.__name__
child = subprocess.run([sys.executable, '-c',
    'import os; print(os.environ["CODEX_PROOF_SANDBOX"])'],
    check=True, capture_output=True, text=True)
for binary in ('git', feature_git.GIT):
    git = subprocess.run([binary, '--version'], check=True, capture_output=True, text=True)
    assert not git.stderr, git.stderr
print(json.dumps({'env': before, 'git': shutil.which('git'),
    'git_resolved': str(pathlib.Path(shutil.which('git')).resolve()),
    'python': shutil.which('python3'),
    'pinned': feature_git.GIT, 'child_marker': child.stdout.strip()}))
"""
        result = self.run_suite([sys.executable, "-c", program, str(source)])
        self.assertEqual(result["result"], "passed", result["tail"])
        seen = json.loads(result["tail"])
        env = seen["env"]
        self.assertEqual(env["CODEX_PROOF_SANDBOX"], "1")
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertEqual(seen["child_marker"], "1")
        self.assertEqual(Path(env["HOME"]), self.home)
        self.assertFalse(any(k.startswith(("EPIC_", "XDG_", "GIT_")) for k in env))
        self.assertNotIn("PYTHONPATH", env)
        self.assertNotIn("CODEX_SANDBOX", env)
        self.assertEqual(seen["python"], shutil.which("python3"))
        if sys.platform == "darwin":
            self.assertEqual(seen["git_resolved"], str(Path(seen["pinned"]).resolve()))
            self.assertEqual(
                seen["pinned"], "/Library/Developer/CommandLineTools/usr/bin/git"
            )

    def test_success_and_failure_keep_complete_stdout_and_stderr(self) -> None:
        for code, expected in ((0, "passed"), (7, "failed")):
            with self.subTest(exit=code):
                result = self.run_suite(
                    [
                        sys.executable,
                        "-c",
                        "import sys; print('begin-' + 'x' * 3000); "
                        f"print('stderr-end', file=sys.stderr); sys.exit({code})",
                    ]
                )
                self.assertEqual(result["exit"], code)
                self.assertEqual(result["result"], expected)
                self.assertEqual(
                    result["tail"], "begin-" + "x" * 3000 + "\nstderr-end\n"
                )

    def test_missing_suite_is_skipped_with_its_error(self) -> None:
        result = self.run_suite([str(self.root / "missing-suite")])
        self.assertEqual(result["result"], "skipped")
        self.assertEqual(result["exit"], 127)
        self.assertIn("missing-suite", result["tail"])

    def test_timeout_keeps_partial_stdout_and_stderr(self) -> None:
        error = subprocess.TimeoutExpired(
            ["fixture"], 1, output=b"begin-" + b"x" * 3000, stderr=b"stderr-end"
        )
        with patch.object(rebase_proof.subprocess, "run", side_effect=error):
            result = self.run_suite(["fixture"])
        self.assertEqual(result["result"], "failed")
        self.assertIsNone(result["exit"])
        self.assertEqual(
            result["tail"], "begin-" + "x" * 3000 + "stderr-end\ntimed out"
        )


class ProofSandboxTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin", "macOS sandbox-exec integration")
    @unittest.skipIf(
        os.environ.get("CODEX_PROOF_SANDBOX") == "1"
        or os.environ.get("CODEX_SANDBOX") == "seatbelt",
        "macOS forbids nested sandboxes",
    )
    def test_descendants_can_write_worktree_and_temp_but_not_protected_paths(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="proof-sandbox-") as folder:
            root = Path(folder).resolve()
            worktree = root / "worktree"
            worktree.mkdir()
            protected = [
                root / name for name in ("state", "runner", "installed", "common-git")
            ]
            for path in protected:
                path.mkdir()
                (path / "keep").write_text("original")
            (worktree / "protected-link").symlink_to(
                protected[0], target_is_directory=True
            )
            codex = Path(shutil.which("codex")).resolve()
            user_temp = Path(os.confstr("CS_DARWIN_USER_TEMP_DIR")).resolve()
            denied_temp = user_temp / f"{root.name}-denied"
            socket_path = root / "blocked.sock"
            listener = socket.socket(socket.AF_UNIX)
            self.addCleanup(listener.close)
            listener.bind(str(socket_path))
            listener.listen()
            program = """
import os, pathlib, shutil, socket, subprocess, sys, tempfile
worktree = pathlib.Path(sys.argv[1])
source, denied_temp, socket_path = sys.argv[2:5]
assert os.environ['CODEX_PROOF_SANDBOX'] == '1'
assert not any(k.startswith(('EPIC_', 'XDG_', 'GIT_')) for k in os.environ)
sys.path.insert(0, str(pathlib.Path(source).parent / 'scripts/epic'))
import isolated_env
assert pathlib.Path.home().is_relative_to(os.environ['TMPDIR'])
sys.path.insert(0, source)
from feature_git import GIT
assert pathlib.Path(shutil.which('git')).resolve() == pathlib.Path(GIT).resolve()
for binary in ('git', GIT):
    result = subprocess.run([binary, '--version'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert not result.stderr, result.stderr
    print(binary, '=>', pathlib.Path(shutil.which(binary)).resolve(), result.stdout.strip())
try:
    pathlib.Path(denied_temp).write_text('forged')
except PermissionError:
    pass
else:
    raise SystemExit('per-user temp write was allowed')
with socket.socket(socket.AF_UNIX) as client:
    try:
        client.connect(socket_path)
    except PermissionError:
        pass
    else:
        raise SystemExit('socket outside proof TMPDIR was allowed')
(worktree / 'allowed').write_text('ok')
with tempfile.TemporaryDirectory() as folder:
    pathlib.Path(folder, 'allowed').write_text('ok')
for directory in sys.argv[5:]:
    for name in ('keep', 'new-file'):
        try:
            pathlib.Path(directory, name).write_text('forged')
        except PermissionError:
            pass
        else:
            raise SystemExit('protected write was allowed: ' + directory)
child = subprocess.run([sys.executable, '-c',
    'from pathlib import Path; import sys; Path(sys.argv[1]).write_text("forged")',
    str(pathlib.Path(sys.argv[5], 'keep'))], capture_output=True)
assert child.returncode != 0, 'descendant escaped the sandbox'
"""
            suite = {
                "name": "sandbox",
                "cwd": ".",
                "command": [
                    sys.executable,
                    "-c",
                    program,
                    str(worktree),
                    str(Path(rebase_proof.__file__).parent),
                    str(denied_temp),
                    str(socket_path),
                    *map(str, protected),
                    str(worktree / "protected-link"),
                ],
            }
            result = rebase_proof.sandbox_suite(
                worktree, suite, rebase_policy, str(codex), os.environ["PATH"]
            )
            self.assertEqual(result["result"], "passed", result["tail"])
            self.assertEqual(result["command"], suite["command"])
            print(result["tail"], end="")
            self.assertFalse(denied_temp.exists())
            self.assertEqual((worktree / "allowed").read_text(), "ok")
            for path in protected:
                self.assertEqual((path / "keep").read_text(), "original")
                self.assertFalse((path / "new-file").exists())


if __name__ == "__main__":
    unittest.main()
