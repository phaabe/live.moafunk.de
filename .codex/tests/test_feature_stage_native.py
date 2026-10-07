"""Stage and publish unstaged files through the native permission boundary."""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/epic"))
import isolated_env  # noqa: E402, F401

import os
import shlex
import unittest

from native_controls_fixture import Responses, identity, native_env
import test_feature_git as fixtures


@unittest.skipIf(
    sys.platform != "darwin" or os.environ.get("CODEX_SANDBOX") == "seatbelt",
    "requires macOS without a nested sandbox",
)
class NativeStageTests(unittest.TestCase):
    def test_unstaged_source_reaches_remote_only_through_helper(self) -> None:
        fixture = fixtures.FeatureGitTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        work = fixture.root / "linked"
        fixture.git("worktree", "add", "-b", "fix/661-native-stage", str(work), "HEAD")
        (work / ".codex").mkdir()
        codex_home = fixture.home / ".codex"
        rules = codex_home / "rules"
        rules.mkdir(parents=True)
        template = (
            Path(__file__).resolve().parents[1]
            / "runtime/rules/codex-feature-git.rules"
        ).read_text()
        rule = template.replace(
            "/REPLACE/operator/.local/libexec/codex-feature-git.py", str(fixture.helper)
        )
        (rules / "feature.rules").write_text(rule)
        env = native_env(fixture.home, codex_home, fixture.root)
        identity(env)
        probe = work / "probe.py"
        probe.write_text(
            "from pathlib import Path\nimport json, subprocess\n"
            "Path('.codex/README.md').write_text('native source edit\\n')\n"
            f"result = subprocess.run([{fixtures.GIT!r}, 'add', '--', '.codex/README.md'], capture_output=True, text=True)\n"
            "assert result.returncode != 0 and 'index.lock' in result.stderr, result\n"
            f"for name in {list(map(str, [fixture.repo / '.git/config', fixture.repo / '.git/hooks/native-probe', fixture.config]))!r}:\n"
            "    try:\n        Path(name).open('a').close()\n"
            "    except PermissionError:\n        pass\n"
            "    else:\n        raise AssertionError(name)\n"
            "Path('boundary-proof.json').write_text(json.dumps({'raw_stage_denied': True, 'configuration_denied': True}))\n"
        )
        helper = shlex.join(
            ["python3", "-I", str(fixture.helper), "--worktree", str(work)]
        )
        with Responses(fixture.root, []) as api:
            (codex_home / "config.toml").write_text(
                'approval_policy = "never"\ndefault_permissions = "epic-source-edit"\n'
                + api.config
                + '\n[permissions.epic-source-edit]\nextends = ":workspace"\n'
                + '[permissions.epic-source-edit.filesystem]\n":slash_tmp" = "read"\n":tmpdir" = "read"\n'
                + '[permissions.epic-source-edit.filesystem.":workspace_roots"]\n'
                + '".codex" = "read"\n".codex/epic_lock.py" = "write"\n".codex/tests" = "write"\n".codex/README.md" = "write"\n'
                + "[permissions.epic-source-edit.network]\nenabled = false\n"
            )
            api.commands[:] = [
                shlex.join([sys.executable, str(probe)]),
                helper + " stage -- .codex/README.md",
                helper + " commit --message-file " + shlex.quote(str(fixture.message)),
                helper + " push",
            ]
            result = api.launch(work, env, source_profile=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((work / "boundary-proof.json").is_file(), result.stdout)
        self.assertEqual(
            fixture.git(
                "show",
                "refs/heads/fix/661-native-stage:.codex/README.md",
                cwd=fixture.remote,
            ),
            "native source edit",
        )


if __name__ == "__main__":
    unittest.main()
