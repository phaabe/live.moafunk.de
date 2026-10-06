"""Real foundation entry, protected bootstrap and admission; fake model/API."""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/epic"))
import isolated_env  # noqa: E402, F401

import fcntl
import json
import os
import shutil
import subprocess
import unittest

import runtime
from protected_home_fixture import prepare
import test_codex_tick as tick


class FoundationTickTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = tick.TickTests("test_idle_and_stop_do_not_start_codex")
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.repo = self.h.repo
        self.git("init", "-q")
        self.git(
            "remote", "add", "origin", "https://github.com/phaabe/live.moafunk.de.git"
        )
        shutil.copyfile(
            tick.ROOT / "protected_home.py", self.repo / ".codex/protected_home.py"
        )
        shutil.copytree(tick.ROOT / "hooks", self.repo / ".codex/hooks")
        self.protected = prepare(self.h.root / "foundation", self.repo)
        self.h.env.update(self.protected.env)
        self.h.env["TEST_DECISION"] = json.dumps({"action": "idle"})
        self.entry = self.h.runner
        self.addCleanup(self.make_writable)

    def git(self, *args: str) -> str:
        return subprocess.check_output(
            [str(tick.REAL_GIT), "-C", str(self.repo), *args],
            env=self.h.env,
            text=True,
            stderr=subprocess.STDOUT,
        ).strip()

    def make_writable(self) -> None:
        for root, dirs, files in os.walk(self.h.root):
            Path(root).chmod(0o700)
            for name in files:
                path = Path(root) / name
                if not path.is_symlink():
                    path.chmod(0o600)

    def run_tick(self) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["/bin/bash", str(self.entry)],
            env=self.h.env,
            cwd=self.h.home,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=40,
        )
        return result

    def diagnostics(self, result: subprocess.CompletedProcess[str]) -> str:
        log = self.h.state / "codex.log"
        return result.stderr + (log.read_text() if log.exists() else "")

    def assert_released(self) -> None:
        self.assertEqual(list((self.h.target_locks / "admitted").glob("*.json")), [])
        lock = self.h.target_locks / "runtime.lock"
        if lock.exists():
            with lock.open("a") as descriptor:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def assert_no_operational_effects(self) -> None:
        self.assertFalse(self.h.selections.exists())
        self.assertFalse(self.h.calls.exists())
        self.assertFalse(self.h.gh_calls.exists())
        self.assertFalse(self.h.pulls.exists())
        self.assertFalse((self.h.state / "codex.log").exists())
        self.assertFalse((self.h.state / "codex-ticks.jsonl").exists())
        self.assertFalse((self.h.state / "agents").exists())

    def test_explicit_legacy_reaches_selector_with_protected_home(self) -> None:
        result = self.run_tick()
        self.assertEqual(result.returncode, 0, self.diagnostics(result))
        self.assertEqual(self.h.selections.read_text(), "call\n")
        self.assertIn("runtime=legacy", self.diagnostics(result))
        self.assert_released()

    def test_explicit_legacy_reaches_existing_model_path(self) -> None:
        self.h.adopt_action()
        result = self.run_tick()
        self.assertEqual(result.returncode, 0, self.diagnostics(result))
        call = json.loads(self.h.calls.read_text())
        self.assertEqual(call["args"][0], "exec")
        cwd = Path(call["args"][call["args"].index("--cd") + 1])
        self.assertEqual(cwd.parent, self.protected.temporary_parent)
        self.assertIn("--skip-git-repo-check", call["args"])
        self.assertNotEqual(cwd, self.repo)
        self.assertEqual(call["tool_env"]["CODEX_HOME"], str(self.protected.home))
        self.assert_released()

    def test_missing_explicit_mode_and_configured_legacy_refuse(self) -> None:
        del self.h.env["EPIC_RUNTIME_LEGACY"]
        result = self.run_tick()
        self.assertEqual(result.returncode, 78, self.diagnostics(result))
        self.assert_no_operational_effects()
        self.h.env["EPIC_RUNTIME_LEGACY"] = "1"
        home = Path(self.h.env["EPIC_RUNTIME_HOME"])
        home.mkdir()
        (home / "configured.json").write_text("{}")
        self.assertEqual(self.run_tick().returncode, 78)
        self.assert_no_operational_effects()

    def test_pause_has_no_admission_or_state_side_effects(self) -> None:
        (self.h.home / ".epic-pause").touch()
        result = self.run_tick()
        self.assertEqual(result.returncode, 0, self.diagnostics(result))
        self.assert_no_operational_effects()
        self.assertFalse(self.h.target_locks.exists())

    def test_exclusive_promotion_refuses_before_registration_and_context_delete(
        self,
    ) -> None:
        self.h.state.mkdir(parents=True)
        context = self.h.state / "feature-git-context.json"
        context.write_text("other tick")
        self.h.env["EPIC_AGENT_ID"] = "codex-2"
        self.h.target_locks.mkdir()
        with (self.h.target_locks / "runtime.lock").open("a") as descriptor:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_tick()
        self.assertEqual(result.returncode, 75, self.diagnostics(result))
        self.assertEqual(context.read_text(), "other tick")
        self.assert_no_operational_effects()
        self.assert_released()

    def test_unreadable_admission_input_exits_two_without_state(self) -> None:
        self.h.target_locks.mkdir()
        (self.h.target_locks / "admitted").write_text("not a directory")
        result = self.run_tick()
        self.assertEqual(result.returncode, 2, self.diagnostics(result))
        self.assert_no_operational_effects()
        (self.h.target_locks / "admitted").unlink()
        self.assert_released()

    def test_marker_and_malformed_marker_refuse_before_operational_effects(
        self,
    ) -> None:
        self.h.target_locks.mkdir()
        marker = self.h.target_locks / "runtime-promotion.json"
        for value in ("{}", "not JSON"):
            with self.subTest(value=value):
                marker.write_text(value)
                result = self.run_tick()
                self.assertEqual(result.returncode, 75, self.diagnostics(result))
                self.assert_no_operational_effects()
                self.assert_released()

    def test_failure_after_admission_releases_only_owned_state(self) -> None:
        self.h.env["EPIC_TICK_TIMEOUT_SECONDS"] = "invalid"
        self.h.state.mkdir(parents=True)
        context = self.h.state / "feature-git-context.json"
        context.write_text("other tick")
        result = self.run_tick()
        self.assertEqual(result.returncode, 2, self.diagnostics(result))
        self.assertEqual(context.read_text(), "other tick")
        self.assert_released()
        self.assertFalse(self.h.lock.exists())

    def test_untracked_and_ignored_configs_refuse_before_api(self) -> None:
        config = self.repo / ".codex/config.toml"
        for ignored in (False, True):
            with self.subTest(ignored=ignored):
                if ignored:
                    (self.repo / ".gitignore").write_text(".codex/config.toml\n")
                config.write_text('approval_policy = "never"\n')
                result = self.run_tick()
                self.assertEqual(result.returncode, 78, self.diagnostics(result))
                self.assert_no_operational_effects()
                self.assertFalse(self.h.target_locks.exists())

    def test_config_created_by_pull_refuses_before_selection(self) -> None:
        git_stub = self.h.bin / "git"
        git_stub.write_text(
            git_stub.read_text().replace(
                "sys.exit(int(os.environ.get('TEST_PULL_EXIT', '0')))",
                "(pathlib.Path(os.environ['TEST_REPO']) / '.codex/config.toml').write_text('# ignored')\n"
                "sys.exit(0)",
            )
        )
        result = self.run_tick()
        self.assertEqual(result.returncode, 78, self.diagnostics(result))
        self.assertTrue(self.h.pulls.exists())
        self.assertFalse(self.h.selections.exists())
        self.assertFalse(self.h.calls.exists())
        self.assert_released()

    def test_admit_blocked_after_partial_record_removes_only_its_record(self) -> None:
        # Inject at the process-table boundary after the real mode/root checks.
        path = self.repo / "scripts/epic/runtime.py"
        path.write_text(
            path.read_text().replace(
                "table = process_table()\n    prune(table, scanned_ns)",
                "path = admitted_dir() / f'{tick_id}.json'\n"
                "    write_json(path, {'tick_id': tick_id})\n"
                "    raise RuntimeBlocked('injected process identity failure')\n"
                "    table = process_table()\n    prune(table, scanned_ns)",
            )
        )
        result = self.run_tick()
        self.assertEqual(result.returncode, 78, self.diagnostics(result))
        self.assertIn("injected process identity failure", self.diagnostics(result))
        self.assert_no_operational_effects()
        self.assert_released()

    def test_admission_spans_target_release_verification_and_cleanup(self) -> None:
        adopt = self.h.adopt_action()
        self.h.candidates(self.h.review_action(407), adopt)
        self.h.env["TEST_REVIEW_PREPARE_EXIT"] = "3"
        observed = self.h.root / "admission-checkpoints.jsonl"
        witness = self.h.root / "witness.py"
        witness.write_text(
            "import fcntl, json, os, pathlib\n"
            f"OUTPUT = pathlib.Path({str(observed)!r})\n"
            "def check(stage, target_free=False):\n"
            "    locks = pathlib.Path(os.environ['EPIC_LOCK_DIR'])\n"
            "    with (locks / 'runtime.lock').open('a') as fd:\n"
            "        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "        except BlockingIOError: pass\n"
            "        else: raise AssertionError('admission released at ' + stage)\n"
            "    if target_free:\n"
            "        with (locks / '407.lock').open('a') as fd:\n"
            "            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "    with OUTPUT.open('a') as out: out.write(json.dumps(stage) + '\\n')\n"
        )
        load = f"exec(compile(open({str(witness)!r}).read(), 'witness', 'exec'))\n"
        assignment = self.repo / ".codex/assignment.py"
        assignment.write_text(assignment.read_text() + "\n")
        # The first candidate cleans up its review, then releases its target.
        review = self.repo / ".codex/review_worktree.py"
        review.write_text(
            review.read_text().replace(
                "context_file = pathlib.Path(args.context_file)",
                load + "if args.command == 'cleanup': check('review-cleanup')\n"
                "context_file = pathlib.Path(args.context_file)",
            )
        )
        assignment.write_text(
            assignment.read_text().replace(
                "args = parser.parse_args()",
                "args = parser.parse_args()\n"
                + load
                + "if json.loads(pathlib.Path(args.action_file).read_text()).get('pr') == 406:\n"
                "    check('after-target-release', True)",
            )
        )
        verify = self.repo / "scripts/epic/tick_verify.py"
        verify.write_text(
            verify.read_text().replace(
                'if __name__ == "__main__":',
                load + 'if __name__ == "__main__":\n    check("post-model-verify")',
            )
        )
        result = self.run_tick()
        self.assertEqual(result.returncode, 0, self.diagnostics(result))
        checkpoints = [json.loads(line) for line in observed.read_text().splitlines()]
        self.assertIn("review-cleanup", checkpoints)
        self.assertIn("after-target-release", checkpoints)
        self.assertIn("post-model-verify", checkpoints)
        self.assert_released()

    def test_log_reopen_failure_still_removes_admission_record(self) -> None:
        self.h.adopt_action()
        model = self.h.bin / "codex"
        model.write_text(
            model.read_text().replace(
                "print('fake Codex stdout', flush=True)",
                "log = pathlib.Path(os.environ['EPIC_STATE_DIR']) / 'codex.log'\n"
                "log.rename(log.with_suffix('.saved'))\n"
                "log.mkdir()\n"
                "print('fake Codex stdout', flush=True)",
            )
        )
        result = self.run_tick()
        self.assertNotEqual(result.returncode, 0)
        saved = (self.h.state / "codex.saved").read_text()
        self.assertIn("cannot reopen log during cleanup", saved)
        self.assert_released()

    def pinned(self) -> Path:
        install = Path(self.h.env["EPIC_RUNTIME_HOME"]) / "revisions" / ("1" * 40)
        shutil.copytree(self.repo, install, ignore=shutil.ignore_patterns(".git"))
        self.protected.code_root = install
        self.protected.data["code_root"] = str(install)
        self.protected.reseal()
        for name, relative in runtime.SETTINGS.items():
            if not name.startswith("codex_") or name == "codex_result_schema":
                continue
            target = install / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(
                self.protected.home / relative.removeprefix(".codex/runtime/"), target
            )
        git = self.h.bin / "pinned-git"
        git.write_text(
            f"#!{sys.executable}\nimport os, pathlib, sys\n"
            "if sys.argv[1:] != ['-C', os.environ['TEST_REPO'], 'fetch', 'origin']:\n"
            f"    os.execv({str(tick.REAL_GIT)!r}, [{str(tick.REAL_GIT)!r}, *sys.argv[1:]])\n"
            "pathlib.Path(os.environ['TEST_PULLS']).write_text('fetch\\n')\n"
        )
        git.chmod(0o700)
        timeout = shutil.which("gtimeout") or shutil.which("timeout")
        assert timeout is not None
        manifest = runtime.build_manifest(
            install,
            "1" * 40,
            {
                name: {"path": path, "version": "fixture"}
                for name, path in (
                    ("python3", str(Path(sys.executable).resolve())),
                    ("git", str(git.resolve())),
                    ("gtimeout", str(Path(timeout).resolve())),
                    ("gitnexus", "/usr/bin/false"),
                )
            },
            settings=tuple(
                name for name in runtime.SETTINGS if name.startswith("codex_")
            ),
        )
        home = Path(self.h.env["EPIC_RUNTIME_HOME"])
        runtime.write_configured(home)
        runtime.write_pin(
            home,
            "1" * 40,
            runtime.sha256_file(install / runtime.MANIFEST),
            "fixture",
            None,
        )
        for root, dirs, files in os.walk(install, topdown=False):
            for name in files:
                (Path(root) / name).chmod(0o444)
            Path(root).chmod(0o555)
        self.h.env.pop("EPIC_RUNTIME_LEGACY")
        self.h.env.update(
            EPIC_RUNTIME_ROOT=str(install),
            EPIC_RUNTIME_REVISION=manifest["revision"],
            EPIC_RUNTIME_MANIFEST=str(install / runtime.MANIFEST),
            EPIC_TRUSTED_ROOT=str(self.repo),
        )
        self.entry = install / ".codex/codex-tick.sh"
        return install

    def test_pinned_fetches_and_keeps_selector_after_checkout_changes(self) -> None:
        self.pinned()
        (self.repo / "scripts/epic/next_action.py").write_text(
            "raise SystemExit('untrusted selector ran')\n"
        )
        (self.repo / "scripts/epic/gitnexus_noise.py").write_text(
            "raise SystemExit('noise must not run')\n"
        )
        for _ in range(2):
            result = self.run_tick()
            self.assertEqual(result.returncode, 0, self.diagnostics(result))
            self.assertEqual(self.h.pulls.read_text(), "fetch\n")
            self.assertFalse(self.h.noise_checks.exists())
            self.assertFalse(self.h.calls.exists())
            self.assert_released()
        self.assertEqual(self.h.selections.read_text(), "call\ncall\n")
        events = [
            json.loads(line)
            for line in (self.h.state / "codex-ticks.jsonl").read_text().splitlines()
        ]
        self.assertTrue(all(event["runtime"] == "1" * 40 for event in events))

    def test_pinned_environment_refuses_a_checkout_entry(self) -> None:
        self.pinned()
        self.entry = self.h.runner
        result = self.run_tick()
        self.assertEqual(result.returncode, 78, self.diagnostics(result))
        self.assertIn("entry source differs", result.stderr)
        self.assert_no_operational_effects()
        self.assertFalse(self.h.target_locks.exists())

    def test_pinned_model_start_refuses_until_child_mechanism(self) -> None:
        self.h.adopt_action()
        self.pinned()
        result = self.run_tick()
        self.assertEqual(result.returncode, 78, self.diagnostics(result))
        self.assertIn("requires the child-lock mechanism", self.diagnostics(result))
        self.assertFalse(self.h.calls.exists())
        self.assert_released()

    def test_changed_pinned_component_blocks_before_admission(self) -> None:
        install = self.pinned()
        target = install / ".codex/tick-result.schema.json"
        target.chmod(0o644)
        target.write_text("{}")
        result = self.run_tick()
        self.assertEqual(result.returncode, 78, self.diagnostics(result))
        self.assert_no_operational_effects()
        self.assertFalse(self.h.target_locks.exists())


if __name__ == "__main__":
    unittest.main()
