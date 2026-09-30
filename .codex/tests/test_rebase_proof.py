"""Exercise the real macOS sandbox used by the protected proof writer."""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/epic"))
import isolated_env  # noqa: E402, F401
import rebase_policy  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rebase_proof  # noqa: E402

import os
import shutil
import tempfile
import unittest


class ProofSandboxTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin", "macOS sandbox-exec integration")
    @unittest.skipIf(
        os.environ.get("CODEX_PROOF_SANDBOX") == "1", "macOS forbids nested sandboxes"
    )
    def test_descendants_can_write_worktree_and_temp_but_not_protected_paths(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="proof-sandbox-", dir="/private/tmp"
        ) as folder:
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
            program = """
import pathlib, subprocess, sys, tempfile
worktree = pathlib.Path(sys.argv[1])
(worktree / 'allowed').write_text('ok')
with tempfile.TemporaryDirectory() as folder:
    pathlib.Path(folder, 'allowed').write_text('ok')
for directory in sys.argv[2:]:
    for name in ('keep', 'new-file'):
        try:
            pathlib.Path(directory, name).write_text('forged')
        except PermissionError:
            pass
        else:
            raise SystemExit('protected write was allowed: ' + directory)
child = subprocess.run([sys.executable, '-c',
    'from pathlib import Path; import sys; Path(sys.argv[1]).write_text("forged")',
    str(pathlib.Path(sys.argv[2], 'keep'))], capture_output=True)
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
                    *map(str, protected),
                    str(worktree / "protected-link"),
                ],
            }
            result = rebase_proof.sandbox_suite(
                worktree, suite, rebase_policy, str(codex), os.environ["PATH"]
            )
            self.assertEqual(result["result"], "passed", result["tail"])
            self.assertEqual(result["command"], suite["command"])
            self.assertEqual((worktree / "allowed").read_text(), "ok")
            for path in protected:
                self.assertEqual((path / "keep").read_text(), "original")
                self.assertFalse((path / "new-file").exists())


if __name__ == "__main__":
    unittest.main()
