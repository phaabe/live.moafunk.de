"""Shared slot bindings use disposable homes and never change live settings."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

from protected_home_fixture import ProtectedFixture, enable_source_profile
import test_foundation_tick as foundation
import test_protected_home as protected

module = protected.module


def bind_slots(fixture: ProtectedFixture, *, source_profile: bool = False) -> Path:
    directory = fixture.base / "slot-state" / "slots"
    directory.mkdir(parents=True, mode=0o700)
    capacity = directory / "capacity.json"
    capacity.write_text(json.dumps({"schema": 1, "slots": 2}))
    capacity.chmod(0o600)
    fixture.data["test_slots"] = {"directory": str(directory), "count": 2}
    if source_profile:
        enable_source_profile(fixture)
    config = fixture.home / "config.toml"
    text = config.read_text()
    if source_profile:
        marker = '[permissions.epic-source-edit.filesystem.":workspace_roots"]'
        text = text.replace(marker, f'{json.dumps(str(directory))} = "write"\n{marker}')
    else:
        old = json.dumps(list(fixture.data["writable_roots"].values()))
        new = json.dumps([*fixture.data["writable_roots"].values(), str(directory)])
        text = text.replace(f"writable_roots = {old}", f"writable_roots = {new}")
    config.write_text(text)
    fixture.reseal()
    return directory


class SlotBindingTests(unittest.TestCase):
    setUp = protected.ProtectedHomeTests.setUp
    git = protected.ProtectedHomeTests.git
    check = protected.ProtectedHomeTests.check

    def test_exact_grant_works_in_both_permission_profiles(self) -> None:
        slots = bind_slots(self.fixture)
        for source_profile in (False, True):
            with self.subTest(source_profile=source_profile):
                if source_profile:
                    enable_source_profile(self.fixture)
                    config = self.fixture.home / "config.toml"
                    marker = (
                        '[permissions.epic-source-edit.filesystem.":workspace_roots"]'
                    )
                    config.write_text(
                        config.read_text().replace(
                            marker, f'{json.dumps(str(slots))} = "write"\n{marker}'
                        )
                    )
                    self.fixture.reseal()
                checked = self.check()
                self.assertEqual(
                    checked["test_slots"], {"directory": str(slots), "count": 2}
                )
                self.assertIn(str(slots), checked["writable_roots"])
                self.assertNotIn(str(slots.parent), checked["writable_roots"])

    def test_parent_grant_is_refused_in_both_profiles(self) -> None:
        slots = bind_slots(self.fixture)
        for source_profile in (False, True):
            if source_profile:
                enable_source_profile(self.fixture)
                config = self.fixture.home / "config.toml"
                marker = '[permissions.epic-source-edit.filesystem.":workspace_roots"]'
                config.write_text(
                    config.read_text().replace(
                        marker, f'{json.dumps(str(slots.parent))} = "write"\n{marker}'
                    )
                )
            else:
                config = self.fixture.home / "config.toml"
                config.write_text(
                    config.read_text().replace(str(slots), str(slots.parent))
                )
            self.fixture.reseal()
            with (
                self.subTest(source_profile=source_profile),
                self.assertRaises(module.ProtectedHomeError),
            ):
                self.check()

    def test_absent_binding_preserves_old_home_but_refuses_slot_environment(
        self,
    ) -> None:
        self.assertIsNone(self.check()["test_slots"])
        for name, value in (
            ("EPIC_TEST_SLOTS_DIR", str(self.base)),
            ("EPIC_TEST_SLOTS", "2"),
        ):
            self.env[name] = value
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(module.ProtectedHomeError, "binding"),
            ):
                self.check()
            del self.env[name]

    def test_inherited_environment_must_match_binding(self) -> None:
        slots = bind_slots(self.fixture)
        self.env.update(EPIC_TEST_SLOTS_DIR=str(slots), EPIC_TEST_SLOTS="2")
        self.check()
        for name, value in (
            ("EPIC_TEST_SLOTS_DIR", str(slots.parent)),
            ("EPIC_TEST_SLOTS", "3"),
            ("EPIC_TEST_SLOTS", ""),
        ):
            saved = self.env[name]
            self.env[name] = value
            with (
                self.subTest(name=name, value=value),
                self.assertRaisesRegex(module.ProtectedHomeError, "binding"),
            ):
                self.check()
            self.env[name] = saved

    def test_bad_binding_and_capacity_are_refused(self) -> None:
        slots = bind_slots(self.fixture)
        valid = dict(self.fixture.data["test_slots"])
        for value in (
            None,
            {},
            {**valid, "extra": 1},
            *({**valid, "count": count} for count in (True, 0, -1, "2", 2.0)),
        ):
            self.fixture.data["test_slots"] = value
            self.fixture.reseal()
            with (
                self.subTest(binding=value),
                self.assertRaises(module.ProtectedHomeError),
            ):
                self.check()
        self.fixture.data["test_slots"] = valid
        self.fixture.reseal()
        capacity = slots / "capacity.json"
        for value in (
            {},
            {"schema": True, "slots": 2},
            {"schema": 1, "slots": True},
            {"schema": 1, "slots": 3},
            {"schema": 1, "slots": 2, "extra": 1},
            [],
        ):
            capacity.write_text(json.dumps(value))
            with (
                self.subTest(capacity=value),
                self.assertRaises(module.ProtectedHomeError),
            ):
                self.check()
        capacity.unlink()
        with self.assertRaises(FileNotFoundError):
            self.check()

    def test_aliases_and_unprotected_directory_modes_are_refused(self) -> None:
        slots = bind_slots(self.fixture)
        alias = self.base / "alias"
        alias.symlink_to(slots, target_is_directory=True)
        self.fixture.data["test_slots"]["directory"] = str(alias)
        self.fixture.reseal()
        with self.assertRaisesRegex(module.ProtectedHomeError, "canonical"):
            self.check()
        self.fixture.data["test_slots"]["directory"] = str(slots)
        self.fixture.reseal()
        slots.chmod(0o755)
        with self.assertRaisesRegex(module.ProtectedHomeError, "0700"):
            self.check()
        slots.chmod(0o700)
        capacity = slots / "capacity.json"
        saved = self.base / "capacity-saved.json"
        capacity.rename(saved)
        for hardlink in (False, True):
            if hardlink:
                os.link(saved, capacity)
            else:
                capacity.symlink_to(saved)
            with (
                self.subTest(hardlink=hardlink),
                self.assertRaises(module.ProtectedHomeError),
            ):
                self.check()
            capacity.unlink()
        saved.rename(capacity)
        other = self.base / "other-lock"
        other.touch()
        for hardlink in (False, True):
            lock = slots / "slot-0.lock"
            if hardlink:
                os.link(other, lock)
            else:
                lock.symlink_to(other)
            with (
                self.subTest(lock_hardlink=hardlink),
                self.assertRaisesRegex(module.ProtectedHomeError, "test slot lock"),
            ):
                self.check()
            lock.unlink()

    def test_unexpected_entries_cannot_expose_protected_files(self) -> None:
        slots = bind_slots(self.fixture)
        protected_config = self.fixture.home / "config.toml"
        for name, nested in (
            ("config-copy", False),
            ("nested", True),
            ("slot-2.lock", False),
        ):
            entry = slots / name
            if nested:
                entry.mkdir()
                link = entry / "config-copy"
            else:
                link = entry
            os.link(protected_config, link)
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(module.ProtectedHomeError, "unexpected entry"),
            ):
                self.check()
            link.unlink()
            if nested:
                entry.rmdir()

    def test_overlapping_roots_are_refused(self) -> None:
        bind_slots(self.fixture)
        for path in (
            self.repo,
            self.fixture.home,
            self.fixture.gitnexus,
            self.fixture.temporary_parent,
            self.fixture.review_parent,
            self.fixture.worktree_parent,
        ):
            slots = path / "nested-slots"
            slots.mkdir(mode=0o700)
            self.fixture.data["test_slots"]["directory"] = str(slots)
            self.fixture.reseal()
            with (
                self.subTest(path=path),
                self.assertRaisesRegex(module.ProtectedHomeError, "overlaps"),
            ):
                self.check()

    @unittest.skipIf(
        os.environ.get("CODEX_SANDBOX") == "seatbelt", "macOS refuses nested sandboxes"
    )
    def test_native_grant_allows_slots_but_denies_parent(self) -> None:
        slots = bind_slots(self.fixture)
        codex = shutil.which("codex")
        self.assertIsNotNone(codex, "native Codex is required for this control")
        for source_profile in (False, True):
            if source_profile:
                enable_source_profile(self.fixture)
                config = self.fixture.home / "config.toml"
                marker = '[permissions.epic-source-edit.filesystem.":workspace_roots"]'
                config.write_text(
                    config.read_text().replace(
                        marker, f'{json.dumps(str(slots))} = "write"\n{marker}'
                    )
                )
                self.fixture.reseal()
            self.check()
            workspace = self.base / f"native-{source_profile}"
            workspace.mkdir()
            probe = (
                "import pathlib,sys\n"
                "root=pathlib.Path(sys.argv[1]); (root/'slot-0.lock').touch()\n"
                "try: (root.parent/'denied').touch()\n"
                "except PermissionError: pass\n"
                "else: raise AssertionError('parent is writable')\n"
            )
            env = {
                "PATH": os.environ["PATH"],
                "HOME": str(self.base),
                "CODEX_HOME": str(self.fixture.home),
            }
            options = (
                ["-P", "epic-source-edit"]
                if source_profile
                else ["-c", 'sandbox_mode="workspace-write"']
            )
            result = subprocess.run(
                [
                    str(codex),
                    "sandbox",
                    *options,
                    "--",
                    sys.executable,
                    "-I",
                    "-c",
                    probe,
                    str(slots),
                ],
                cwd=workspace,
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(
                result.returncode,
                0,
                f"{source_profile=}: " + result.stdout + result.stderr,
            )


class SlotTickTests(unittest.TestCase):
    setUp = foundation.FoundationTickTests.setUp
    git = foundation.FoundationTickTests.git
    make_writable = foundation.FoundationTickTests.make_writable
    run_tick = foundation.FoundationTickTests.run_tick
    diagnostics = foundation.FoundationTickTests.diagnostics
    assert_no_operational_effects = (
        foundation.FoundationTickTests.assert_no_operational_effects
    )

    def test_bound_slots_reach_model_tool_environment(self) -> None:
        slots = bind_slots(self.protected)
        self.h.adopt_action()
        result = self.run_tick()
        self.assertEqual(result.returncode, 0, self.diagnostics(result))
        call = json.loads(self.h.calls.read_text().splitlines()[-1])
        self.assertEqual(call["tool_env"]["EPIC_TEST_SLOTS_DIR"], str(slots))
        self.assertEqual(call["tool_env"]["EPIC_TEST_SLOTS"], "2")

    def test_mismatched_slot_capacity_refuses_before_model(self) -> None:
        slots = bind_slots(self.protected)
        (slots / "capacity.json").write_text('{"schema":1,"slots":3}')
        result = self.run_tick()
        self.assertEqual(result.returncode, 78, self.diagnostics(result))
        self.assert_no_operational_effects()
