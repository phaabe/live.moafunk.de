"""Host refusal controls use real Git and never start a model."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/epic"))
import isolated_env  # noqa: E402, F401

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest

from protected_home_fixture import enable_source_profile, prepare

SOURCE = Path(__file__).resolve().parents[1] / "protected_home.py"
spec = importlib.util.spec_from_file_location("protected_home", SOURCE)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ProtectedHomeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="protected-home-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.repo = self.base / "runner"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Fixture")
        self.git("config", "commit.gpgsign", "false")
        self.git(
            "remote", "add", "origin", "https://github.com/phaabe/live.moafunk.de.git"
        )
        (self.repo / "README").write_text("fixture\n")
        self.git("add", "README")
        self.git("commit", "-qm", "Fixture")
        self.fixture = prepare(self.base, self.repo)
        self.env = {**os.environ, **self.fixture.env}

    def git(self, *args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(self.repo), *args], text=True, stderr=subprocess.PIPE
        ).strip()

    def check(self, **kwargs: object) -> dict[str, object]:
        return module.validate("legacy", self.repo, self.repo, self.env, **kwargs)

    def test_accepts_bound_home_and_exact_roots(self) -> None:
        result = self.check()
        self.assertEqual(
            result["writable_roots"],
            [str(self.repo / ".git"), str(self.fixture.gitnexus)],
        )

    def test_source_profile_accepts_only_the_exact_bound_policy(self) -> None:
        enable_source_profile(self.fixture)
        self.assertEqual(self.check()["permission_profile"], "epic-source-edit")
        config = self.fixture.home / "config.toml"
        original = config.read_text()
        cases = [
            original.replace('".codex" = "read"', '".codex" = "write"'),
            original.replace('":slash_tmp" = "read"', '":slash_tmp" = "write"'),
            original.replace('".codex/tests"', '".codex/hooks"'),
            original.replace("enabled = false", "enabled = true"),
            'sandbox_mode = "workspace-write"\n' + original,
            original.replace(
                'default_permissions = "epic-source-edit"',
                'default_permissions = ":workspace"',
            ),
            original + '\n[permissions.extra]\nextends = ":workspace"\n',
        ]
        for changed in cases:
            with self.subTest(config=changed):
                config.write_text(changed)
                self.fixture.reseal()
                with self.assertRaisesRegex(module.ProtectedHomeError, "permission"):
                    self.check()

    def test_source_profile_refuses_existing_symlink_and_hardlink_grants(self) -> None:
        enable_source_profile(self.fixture)
        temp = self.fixture.temporary_parent / "codex-tick-source"
        temp.mkdir()
        sources = temp / ".codex"
        sources.mkdir()
        target = self.fixture.home / "config.toml"
        source = sources / "epic_lock.py"
        for hardlink in (False, True):
            with self.subTest(hardlink=hardlink):
                if hardlink:
                    os.link(target, source)
                else:
                    source.symlink_to(target)
                try:
                    with self.assertRaisesRegex(module.ProtectedHomeError, "alias"):
                        self.check(temp_dir=temp, model_root=temp)
                finally:
                    source.unlink()
        tests = sources / "tests"
        tests.mkdir()
        (tests / "escape").symlink_to(self.fixture.home, target_is_directory=True)
        with self.assertRaisesRegex(module.ProtectedHomeError, "alias"):
            self.check(temp_dir=temp, model_root=temp)

    def test_missing_home_or_binding_refuses(self) -> None:
        for key in self.fixture.env:
            with self.subTest(key=key):
                env = self.env.copy()
                del env[key]
                with self.assertRaises(module.ProtectedHomeError):
                    module.validate("legacy", self.repo, self.repo, env)

    def test_symlink_runner_alias_refuses(self) -> None:
        alias = self.base / "alias"
        alias.symlink_to(self.repo, target_is_directory=True)
        with self.assertRaisesRegex(module.ProtectedHomeError, "canonical"):
            module.validate("legacy", alias, self.repo, self.env)

    def test_changed_effective_fetch_or_push_origin_refuses(self) -> None:
        self.git(
            "config", "url.https://invalid.example/.insteadOf", "https://github.com/"
        )
        with self.assertRaisesRegex(module.ProtectedHomeError, "effective Git origin"):
            self.check()
        self.git("config", "--unset", "url.https://invalid.example/.insteadOf")
        self.git(
            "remote", "set-url", "--push", "origin", "https://invalid.example/push"
        )
        with self.assertRaisesRegex(module.ProtectedHomeError, "effective Git origin"):
            self.check()

    def test_untracked_ignored_and_nested_config_refuse_before_start(self) -> None:
        for relative, ignored in (
            (".codex/config.toml", False),
            ("nested/.codex/config.toml", False),
            ("nested/.codex/config.toml", True),
        ):
            with self.subTest(relative=relative, ignored=ignored):
                target = self.repo / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('sandbox_mode = "danger-full-access"\n')
                if ignored:
                    (self.repo / ".gitignore").write_text(".codex/\n")
                result = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        str(SOURCE),
                        "check",
                        "--mode",
                        "legacy",
                        "--repo",
                        str(self.repo),
                        "--code-root",
                        str(self.repo),
                    ],
                    env=self.env,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 78, result.stderr)
                self.assertIn("untracked or ignored", result.stderr)
                target.unlink()

    def test_aliased_codex_directory_refuses(self) -> None:
        other = self.base / "other-config"
        other.mkdir()
        (other / "config.toml").write_text("# config\n")
        (self.repo / ".codex").rename(self.base / "original-codex")
        (self.repo / ".codex").symlink_to(other, target_is_directory=True)
        with self.assertRaisesRegex(module.ProtectedHomeError, "aliased"):
            self.check()

    def test_changed_config_hook_and_rule_bytes_refuse(self) -> None:
        for target in (
            self.fixture.home / "config.toml",
            self.fixture.home / "hooks.json",
            self.fixture.hook,
            self.fixture.home / "rules/codex-feature-git.rules",
        ):
            before = target.read_bytes()
            target.write_bytes(before + b"\n")
            with (
                self.subTest(path=target),
                self.assertRaisesRegex(
                    module.ProtectedHomeError, "protected file changed"
                ),
            ):
                self.check()
            target.write_bytes(before)

    def test_writable_parent_of_home_or_code_refuses(self) -> None:
        for root in (self.base, self.fixture.protected, self.repo):
            with self.subTest(root=root):
                self.fixture.data["writable_roots"]["gitnexus"] = str(root)
                self.fixture.render()
                with self.assertRaisesRegex(module.ProtectedHomeError, "protected"):
                    self.check()

    def test_direct_home_writable_root_refuses(self) -> None:
        self.fixture.data["writable_roots"]["gitnexus"] = str(self.fixture.home)
        self.fixture.render()
        with self.assertRaisesRegex(module.ProtectedHomeError, "protected"):
            self.check()

    def test_untrusted_hook_or_project_configuration_refuses(self) -> None:
        config = self.fixture.home / "config.toml"
        original = config.read_text()
        for before, after in (
            ('trust_level = "untrusted"', 'trust_level = "trusted"'),
            ("hooks = true", "hooks = false"),
            ("exclude_slash_tmp = true", "exclude_slash_tmp = false"),
        ):
            with self.subTest(setting=before):
                config.write_text(original.replace(before, after))
                self.fixture.reseal()
                with self.assertRaises(module.ProtectedHomeError):
                    self.check()
        config.write_text(original)

    def test_wrong_hook_trust_key_and_unbound_script_refuse(self) -> None:
        self.fixture.hook_trust = {"wrong-key": {"trusted_hash": "sha256:" + "a" * 64}}
        self.fixture.render()
        with self.assertRaisesRegex(module.ProtectedHomeError, "trust keys"):
            self.check()
        self.fixture.hooks_json["hooks"]["PreToolUse"][0]["hooks"][0]["command"] = (
            "/bin/true"
        )
        self.fixture.render()
        with self.assertRaisesRegex(module.ProtectedHomeError, "bound absolute script"):
            self.check()

    def test_extra_rules_refuse(self) -> None:
        (self.fixture.home / "rules/override.rules").write_text("# unexpected\n")
        with self.assertRaisesRegex(module.ProtectedHomeError, "unexpected.*rule"):
            self.check()

    def test_resealed_stale_native_trust_hash_refuses(self) -> None:
        key = next(iter(self.fixture.hook_trust))
        self.fixture.hook_trust[key]["trusted_hash"] = "sha256:" + "a" * 64
        self.fixture.render()
        with self.assertRaisesRegex(module.ProtectedHomeError, "native hook identity"):
            self.check()

    def test_writable_code_or_home_descendant_refuses(self) -> None:
        for root in (
            self.repo / "scripts/epic",
            self.repo / ".codex",
            self.fixture.home / "sessions",
        ):
            with self.subTest(root=root):
                root.mkdir(parents=True, exist_ok=True)
                self.fixture.data["writable_roots"]["gitnexus"] = str(root)
                self.fixture.render()
                with self.assertRaisesRegex(
                    module.ProtectedHomeError, "overlaps protected"
                ):
                    self.check()

    def test_bound_network_policy_is_preserved(self) -> None:
        self.fixture.data["network_access"] = True
        self.fixture.render()
        self.check()
        config = self.fixture.home / "config.toml"
        config.write_text(
            config.read_text().replace(
                "network_access = true", "network_access = false"
            )
        )
        self.fixture.reseal()
        with self.assertRaisesRegex(module.ProtectedHomeError, "network"):
            self.check()

    def test_alternate_permissions_and_profiles_refuse(self) -> None:
        for table in ("profiles.override", "permissions"):
            with self.subTest(table=table):
                self.fixture.config_extra = (
                    f'[{table}]\nsandbox_mode = "danger-full-access"\n'
                )
                self.fixture.render()
                with self.assertRaisesRegex(
                    module.ProtectedHomeError, "alternate permission"
                ):
                    self.check()

    def test_unreadable_runner_directory_refuses(self) -> None:
        hidden = self.repo / "hidden"
        hidden.mkdir()
        hidden.chmod(0)
        try:
            with self.assertRaises(PermissionError):
                self.check()
        finally:
            hidden.chmod(0o700)

    def test_writable_pin_parent_refuses_before_git_or_import(self) -> None:
        runtime_home = self.base / "runtime-home"
        runtime_home.mkdir()
        self.fixture.data["writable_roots"]["gitnexus"] = str(runtime_home)
        self.fixture.render()
        self.env.update(
            EPIC_RUNTIME_HOME=str(runtime_home), EPIC_RUNTIME_ROOT=str(self.repo)
        )
        self.env.pop("EPIC_RUNTIME_LEGACY")
        # No pin or manifest exists: overlap must fail before either is read,
        # before a pinned Git could run, and before a module could import.
        with self.assertRaisesRegex(module.ProtectedHomeError, "protected path"):
            module.validate("pinned", self.repo, self.repo, self.env)

    def test_malformed_nested_shapes_exit_78(self) -> None:
        config = self.fixture.home / "config.toml"
        original = config.read_text()
        malformed = original.replace(
            "[sandbox_workspace_write]", "[[sandbox_workspace_write]]"
        )
        config.write_text(malformed)
        self.fixture.reseal()
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                str(SOURCE),
                "check",
                "--mode",
                "legacy",
                "--repo",
                str(self.repo),
                "--code-root",
                str(self.repo),
            ],
            env=self.env,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 78, result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_configured_runtime_disables_explicit_legacy(self) -> None:
        runtime_home = self.base / "runtime-home"
        runtime_home.mkdir()
        self.env["EPIC_RUNTIME_HOME"] = str(runtime_home)
        for name in ("configured.json", "pin.json"):
            with self.subTest(name=name):
                target = runtime_home / name
                target.write_text("{}")
                with self.assertRaisesRegex(
                    module.ProtectedHomeError, "disabled after"
                ):
                    self.check()
                target.unlink()

    def test_exact_tick_temp_is_allowed_but_its_parent_is_not(self) -> None:
        temp = self.fixture.temporary_parent / "codex-tick-test"
        temp.mkdir()
        result = self.check(model_root=temp, temp_dir=temp)
        self.assertIn(str(temp), result["writable_roots"])
        self.assertNotIn(str(temp.parent), result["writable_roots"])
        with self.assertRaisesRegex(module.ProtectedHomeError, "exact allocated"):
            self.check(temp_dir=temp.parent)

    def test_static_root_cannot_grant_all_allocated_directories(self) -> None:
        for parent in (
            self.fixture.temporary_parent,
            self.fixture.review_parent,
            self.fixture.worktree_parent,
        ):
            with self.subTest(parent=parent):
                self.fixture.data["writable_roots"]["gitnexus"] = str(parent)
                self.fixture.render()
                with self.assertRaisesRegex(
                    module.ProtectedHomeError, "overlaps allocation parent"
                ):
                    self.check()

    def test_approved_linked_worktree_only(self) -> None:
        work = self.fixture.worktree_parent / "feature"
        self.git("worktree", "add", "--detach", str(work))
        self.assertIn(str(work), self.check(model_root=work)["writable_roots"])
        with self.assertRaises(module.ProtectedHomeError):
            self.check(model_root=self.repo)
        other = self.fixture.worktree_parent / "foreign"
        subprocess.run(["git", "init", "-q", str(other)], check=True)
        with self.assertRaisesRegex(module.ProtectedHomeError, "another repository"):
            self.check(model_root=other)

    def test_exact_review_evidence_directory_only(self) -> None:
        evidence = self.fixture.review_parent / "123" / ("a" * 40)
        evidence.mkdir(parents=True)
        self.assertIn(
            str(evidence), self.check(extra_write_dirs=(evidence,))["writable_roots"]
        )
        with self.assertRaises(module.ProtectedHomeError):
            self.check(extra_write_dirs=(evidence.parent,))

    def test_pinned_missing_foundation_entries_refuses_before_runtime_import(
        self,
    ) -> None:
        manifest = self.repo / "runtime-manifest.json"
        manifest.write_text(json.dumps({"files": {}, "settings": {}}))
        self.env["EPIC_RUNTIME_MANIFEST"] = str(manifest)
        self.env["EPIC_RUNTIME_ROOT"] = str(self.repo)
        self.env.pop("EPIC_RUNTIME_LEGACY")
        with self.assertRaises((module.ProtectedHomeError, FileNotFoundError)):
            module.validate("pinned", self.repo, self.repo, self.env)

    def test_pinned_executable_symlink_under_writable_git_metadata_refuses(
        self,
    ) -> None:
        runtime_home = self.base / "runtime-home"
        revision = "1" * 40
        install = runtime_home / "revisions" / revision
        install.mkdir(parents=True)
        executable = Path(sys.executable).resolve()
        alias = self.repo / ".git/python-link"
        alias.symlink_to(executable)
        executables = {
            name: {
                "path": str(executable),
                "version": "fixture",
                "sha256": module.digest(executable),
            }
            for name in ("python3", "git", "gtimeout", "gitnexus")
        }
        executables["python3"]["path"] = str(alias)
        manifest = install / "runtime-manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "contract": 1,
                    "revision": revision,
                    "files": {},
                    "settings": {},
                    "executables": executables,
                }
            )
        )
        (runtime_home / "pin.json").write_text(
            json.dumps(
                {
                    "revision": revision,
                    "manifest_sha256": module.digest(manifest),
                }
            )
        )
        manifest.chmod(0o444)
        install.chmod(0o555)
        env = {
            **self.env,
            "EPIC_RUNTIME_HOME": str(runtime_home),
            "EPIC_RUNTIME_MANIFEST": str(manifest),
            "EPIC_RUNTIME_REVISION": revision,
        }
        try:
            with self.assertRaisesRegex(
                module.ProtectedHomeError, "executable python3 must be canonical"
            ):
                module.checked_manifest(install, env)
        finally:
            install.chmod(0o755)
            manifest.chmod(0o644)


if __name__ == "__main__":
    unittest.main()
