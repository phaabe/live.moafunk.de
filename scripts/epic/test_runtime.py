"""Runtime contract tests (https://github.com/phaabe/live.moafunk.de/issues/584).

Run: python3 -m unittest discover -s scripts/epic

Disposable installs, locks and processes only: no model, no GitHub, no live
runner state (isolated_env gives every test its own HOME).
"""

from __future__ import annotations

import isolated_env  # noqa: F401  (first: hides live runner state)

import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import runtime
import smoke

HERE = Path(__file__).resolve().parent
RUNTIME = HERE / "runtime.py"
LOCKHOLD = HERE / "lockhold"
LAUNCHER = HERE / "epic-tick"
REV = "1" * 40
REV2 = "2" * 40
REV3 = "3" * 40
# Module code that leaves a trace when it runs.
TRAP = "import os\nopen(os.environ['EPIC_TEST_TRAP'], 'a').write('ran\\n')\n"


def make_writable(root: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(root):
        os.chmod(dirpath, 0o700)
        for name in filenames:
            path = Path(dirpath) / name
            if not path.is_symlink():
                os.chmod(path, 0o600)


def read_only(root: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames:
            if not (Path(dirpath) / name).is_symlink():
                os.chmod(Path(dirpath) / name, 0o444)
        os.chmod(dirpath, 0o555)


class TempCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(tmp.name)
        self.addCleanup(tmp.cleanup)
        self.addCleanup(make_writable, self.tmp)
        self.locks = self.tmp / "locks"
        self.home = self.tmp / "runtime-home"
        os.environ["EPIC_LOCK_DIR"] = str(self.locks)
        os.environ["EPIC_RUNTIME_HOME"] = str(self.home)

    def install(
        self, revision: str = REV, files: dict[str, str] | None = None,
        sealed: bool = True,
    ) -> Path:  # fmt: skip
        """A fake install: the given files, the entry scripts, a manifest."""
        install = self.home / "revisions" / revision
        install.mkdir(parents=True)
        content = {
            "scripts/epic/runtime.py": RUNTIME.read_text(),
            "scripts/epic/target_lock.py": (HERE / "target_lock.py").read_text(),
            "scripts/epic/claude-tick.sh": "#!/bin/bash\nexit 0\n",
            ".codex/codex-tick.sh": "#!/bin/bash\nexit 0\n",
            "scripts/epic/claude-runner-settings.json": "{}\n",
            **(files or {}),
        }
        for rel, text in content.items():
            path = install / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        runtime.build_manifest(
            install,
            revision,
            {"python3": {"path": sys.executable, "version": sys.version.split()[0]}},
            settings=("claude_runner_settings",),
        )
        if sealed:
            read_only(install)
        return install

    def validate(self, install: Path) -> tuple[int, list[str]]:
        code, failures = runtime.validate(install / runtime.MANIFEST)
        return code, [str(f["item"]) for f in failures]


# --- manifest ---------------------------------------------------------------


class ManifestTest(TempCase):
    def test_sealed_install_validates(self) -> None:
        install = self.install()
        self.assertEqual(self.validate(install), (runtime.OK, []))
        manifest = json.loads((install / runtime.MANIFEST).read_text())
        self.assertNotIn(runtime.MANIFEST, manifest["files"])
        self.assertEqual(
            (manifest["schema"], manifest["revision"], manifest["contract"]),
            (1, REV, runtime.CONTRACT),
        )

    def test_changed_extra_and_writable_files_fail(self) -> None:
        install = self.install(sealed=False)
        read_only(install)
        make_writable(install)
        (install / "scripts/epic/claude-tick.sh").write_text("#!/bin/bash\nexit 1\n")
        (install / "scripts/epic/extra.py").write_text("")
        read_only(install)
        code, items = self.validate(install)
        self.assertEqual(code, runtime.MISMATCH)
        self.assertIn("files[scripts/epic/claude-tick.sh]", items)
        self.assertIn("files[scripts/epic/extra.py]", items)
        os.chmod(install / "scripts/epic/runtime.py", 0o644)
        self.assertIn("mode[scripts/epic/runtime.py]", self.validate(install)[1])

    def test_unsealed_install_fails(self) -> None:
        code, items = self.validate(self.install(sealed=False))
        self.assertEqual(code, runtime.MISMATCH)
        self.assertIn("mode[.]", items)

    def test_symlink_fails(self) -> None:
        install = self.install(sealed=False)
        (install / "scripts/link").symlink_to("/etc/hosts")
        read_only(install)
        code, items = self.validate(install)
        self.assertEqual(code, runtime.MISMATCH)
        self.assertIn("files[scripts/link]", items)

    def test_executable_settings_and_contract_mismatch(self) -> None:
        tool = self.tmp / "tool"
        tool.write_text("v1")
        install = self.home / "revisions" / REV
        (install / "scripts/epic").mkdir(parents=True)
        (install / "scripts/epic/claude-runner-settings.json").write_text("{}")
        runtime.build_manifest(
            install, REV, {"gitnexus": {"path": str(tool), "version": "1"}},
            settings=("claude_runner_settings",),
        )  # fmt: skip
        read_only(install)
        tool.write_text("v2")
        code, items = self.validate(install)
        self.assertEqual((code, items), (runtime.MISMATCH, ["executables[gitnexus]"]))
        code, failures = runtime.validate(install / runtime.MANIFEST, contract=2)
        self.assertIn("contract", [f["item"] for f in failures])

    def test_git_executable_is_bound(self) -> None:
        git = self.tmp / "git"
        git.write_text("v1")
        install = self.home / "revisions" / REV
        install.mkdir(parents=True)
        runtime.build_manifest(
            install, REV, {"git": {"path": str(git), "version": "2.50.1"}}
        )  # fmt: skip
        read_only(install)
        self.assertEqual(self.validate(install), (runtime.OK, []))
        git.write_text("v2")
        self.assertEqual(
            self.validate(install), (runtime.MISMATCH, ["executables[git]"])
        )
        git.unlink()
        self.assertEqual(
            self.validate(install), (runtime.MISMATCH, ["executables[git]"])
        )

    def test_unknown_executable_is_refused(self) -> None:
        install = self.install(sealed=False)
        path = install / runtime.MANIFEST
        data = json.loads(path.read_text())
        spec = dict(data["executables"]["python3"])
        path.write_text(json.dumps({**data, "executables": {"git2": spec}}))
        code, failures = runtime.validate(path)
        self.assertEqual(code, runtime.UNREADABLE)
        self.assertIn("unknown executable git2", failures[0]["actual"])

    def test_helper_config_values_are_bound(self) -> None:
        helper, config = self.tmp / "helper.py", self.tmp / "helper.json"
        helper.write_text("print()")
        config.write_text(json.dumps({"runner_checkout": "/a", "other": 1}))
        entry = runtime.helper_entry(helper, config, ("runner_checkout",))
        install = self.home / "revisions" / REV
        install.mkdir(parents=True)
        runtime.build_manifest(install, REV, {}, helpers={str(helper): entry})
        read_only(install)
        self.assertEqual(self.validate(install)[0], runtime.OK)
        config.write_text(json.dumps({"runner_checkout": "/b", "other": 1}))
        items = self.validate(install)[1]
        self.assertIn(f"helpers[{helper}].config", items)
        self.assertIn(f"helpers[{helper}].config.runner_checkout", items)

    def test_unreadable_or_unknown_schema_exits_2(self) -> None:
        install = self.install(sealed=False)
        path = install / runtime.MANIFEST
        data = json.loads(path.read_text())
        for broken in ("{", json.dumps({**data, "schema": 2}), json.dumps([1])):
            path.write_text(broken)
            self.assertEqual(runtime.validate(path)[0], runtime.UNREADABLE)
        path.unlink()
        self.assertEqual(runtime.validate(path)[0], runtime.UNREADABLE)

    def test_cli_prints_json_and_exit_code(self) -> None:
        install = self.install()
        out = subprocess.run(
            [sys.executable, str(RUNTIME), "validate", "--manifest", str(install / runtime.MANIFEST)],
            capture_output=True, text=True, check=False,
        )  # fmt: skip
        self.assertEqual(
            (out.returncode, json.loads(out.stdout)), (0, {"ok": True, "failures": []})
        )


# --- pin, mode, roots, launcher --------------------------------------------


class PinTest(TempCase):
    def pin(self, install: Path) -> None:
        digest = runtime.sha256_file(install / runtime.MANIFEST)
        runtime.write_pin(self.home, install.name, digest, "p1", None)

    def test_resolve_needs_a_valid_pin_and_install(self) -> None:
        with self.assertRaisesRegex(runtime.RuntimeBlocked, "no pin"):
            runtime.resolve(self.home)
        install = self.install()
        self.pin(install)
        self.assertEqual(runtime.resolve(self.home)["install"], str(install))
        runtime.write_pin(self.home, REV, "0" * 64, "p1", None)
        with self.assertRaisesRegex(runtime.RuntimeBlocked, "pin's hash"):
            runtime.resolve(self.home)

    def test_resolve_refuses_a_changed_install(self) -> None:
        install = self.install()
        self.pin(install)
        os.chmod(install / "scripts/epic", 0o755)
        (install / "scripts/epic/new.py").write_text("")
        with self.assertRaisesRegex(runtime.RuntimeBlocked, "fails validation"):
            runtime.resolve(self.home)

    def test_mode(self) -> None:
        env = {"EPIC_RUNTIME_HOME": str(self.home)}
        with self.assertRaisesRegex(runtime.RuntimeBlocked, "no runtime"):
            runtime.mode(env)
        self.assertEqual(runtime.mode({**env, "EPIC_RUNTIME_LEGACY": "1"}), "legacy")
        self.assertEqual(runtime.mode({**env, "EPIC_RUNTIME_ROOT": "/x"}), "pinned")
        with self.assertRaisesRegex(runtime.RuntimeBlocked, "both set"):
            runtime.mode({**env, "EPIC_RUNTIME_ROOT": "/x", "EPIC_RUNTIME_LEGACY": "1"})
        runtime.write_configured(self.home)
        with self.assertRaisesRegex(runtime.RuntimeBlocked, "legacy mode is refused"):
            runtime.mode({**env, "EPIC_RUNTIME_LEGACY": "1"})

    def test_legacy_stays_refused_after_the_pin_is_deleted(self) -> None:
        runtime.write_configured(self.home)
        install = self.install()
        self.pin(install)
        (self.home / runtime.PIN).unlink()
        env = {"EPIC_RUNTIME_HOME": str(self.home), "EPIC_RUNTIME_LEGACY": "1"}
        with self.assertRaisesRegex(runtime.RuntimeBlocked, "legacy mode is refused"):
            runtime.mode(env)
        out = subprocess.run(
            [sys.executable, str(RUNTIME), "mode"], env={**os.environ, **env},
            capture_output=True, text=True, check=False,
        )  # fmt: skip
        self.assertEqual(out.returncode, runtime.BLOCKED)

    def test_code_root(self) -> None:
        fallback = Path("/checkout")
        env = {"EPIC_RUNTIME_HOME": str(self.home)}
        self.assertEqual(runtime.code_root(fallback, env), fallback)
        self.assertEqual(
            runtime.code_root(fallback, {**env, "EPIC_RUNTIME_ROOT": "/pinned"}),
            Path("/pinned"),
        )
        with self.assertRaises(runtime.RuntimeBlocked):
            runtime.code_root(fallback, {**env, "EPIC_RUNTIME_ROOT": "relative"})
        runtime.write_configured(self.home)
        with self.assertRaisesRegex(runtime.RuntimeBlocked, "EPIC_RUNTIME_ROOT"):
            runtime.code_root(fallback, env)


class LauncherTest(TempCase):
    ENV_DUMP = (
        "#!/bin/bash\n"
        'printf "%s\\n%s\\n%s\\n" "$EPIC_RUNTIME_ROOT" "$EPIC_RUNTIME_REVISION" '
        '"$EPIC_RUNTIME_MANIFEST" > "$EPIC_TEST_OUT"\n'
    )

    def launch(
        self, agent: str = "claude", **extra: str
    ) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "EPIC_TEST_OUT": str(self.tmp / "out"), **extra}
        return subprocess.run(
            [sys.executable, str(LAUNCHER), agent], env=env,
            capture_output=True, text=True, timeout=60, check=False,
        )  # fmt: skip

    def pinned(self) -> Path:
        install = self.install(files={"scripts/epic/claude-tick.sh": self.ENV_DUMP})
        digest = runtime.sha256_file(install / runtime.MANIFEST)
        runtime.write_pin(self.home, REV, digest, "p1", None)
        return install

    def test_missing_pin_blocks(self) -> None:
        runtime.write_configured(self.home)
        out = self.launch()
        self.assertEqual(out.returncode, runtime.BLOCKED, out.stderr)
        self.assertFalse((self.tmp / "out").exists())

    def test_runs_the_pinned_entry_with_its_root(self) -> None:
        install = self.pinned()
        out = self.launch()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(
            (self.tmp / "out").read_text().splitlines(),
            [str(install), REV, str(install / runtime.MANIFEST)],
        )

    def test_changed_install_or_manifest_blocks(self) -> None:
        install = self.pinned()
        os.chmod(install / "scripts/epic", 0o755)
        os.chmod(install / "scripts/epic/claude-tick.sh", 0o755)
        (install / "scripts/epic/claude-tick.sh").write_text(self.ENV_DUMP + "# x\n")
        self.assertEqual(self.launch().returncode, runtime.BLOCKED)
        self.assertFalse((self.tmp / "out").exists())
        os.chmod(install, 0o755)
        os.chmod(install / runtime.MANIFEST, 0o644)
        (install / runtime.MANIFEST).write_text("{}")
        self.assertEqual(self.launch().returncode, runtime.BLOCKED)

    def test_changed_import_or_planted_module_never_runs(self) -> None:
        # The launcher checks runtime.py, then runs it as the validator. No
        # other install file may run before the validator refuses it.
        install = self.pinned()
        trap = self.tmp / "trap"
        epic = install / "scripts/epic"
        os.chmod(epic, 0o755)
        os.chmod(epic / "target_lock.py", 0o644)
        (epic / "target_lock.py").write_text(TRAP)
        for name in ("json", "argparse", "hashlib", "fcntl"):
            (epic / f"{name}.py").write_text(TRAP)
        out = self.launch(EPIC_TEST_TRAP=str(trap))
        self.assertEqual(out.returncode, runtime.BLOCKED, out.stderr)
        self.assertFalse(trap.exists(), "install code ran before validation")
        self.assertFalse((self.tmp / "out").exists())

    def test_invalid_pin_blocks(self) -> None:
        self.pinned()
        good = json.loads((self.home / runtime.PIN).read_text())
        for bad in (
            {**good, "schema": 999},
            {**good, "manifest_sha256": "x" * 64},
            {k: v for k, v in good.items() if k != "promotion_id"},
        ):
            with self.subTest(pin=bad):
                runtime.write_json(self.home / runtime.PIN, bad)
                out = self.launch()
                self.assertEqual(out.returncode, runtime.BLOCKED, out.stderr)
                self.assertFalse((self.tmp / "out").exists())

    def test_manifest_naming_another_revision_blocks(self) -> None:
        install = self.install(
            REV2, files={"scripts/epic/claude-tick.sh": self.ENV_DUMP}, sealed=False
        )
        moved = install.rename(install.with_name(REV3))
        read_only(moved)
        digest = runtime.sha256_file(moved / runtime.MANIFEST)
        runtime.write_pin(self.home, REV3, digest, "p1", None)
        out = self.launch()
        self.assertEqual(out.returncode, runtime.BLOCKED, out.stderr)
        self.assertIn("another revision", out.stderr)
        self.assertFalse((self.tmp / "out").exists())

    def test_promotion_between_validation_and_admission_refuses_the_tick(self) -> None:
        # The launcher validates revision A and starts its entry. Before the
        # entry is admitted, a promotion switches the pin to B and ends. The
        # entry must not be admitted: it would run the old runtime.
        go, ready = self.tmp / "go", self.tmp / "ready"
        entry = (
            "#!/bin/bash\n"
            f'touch "{ready}"\n'
            f'while [[ ! -e "{go}" ]]; do sleep 0.02; done\n'
            'exec 17>>"$EPIC_LOCK_DIR/runtime.lock"\n'
            'python3 "$EPIC_RUNTIME_ROOT/scripts/epic/runtime.py" admit --fd 17 '
            "--tick-id late --agent claude --pid $$\n"
        )
        for switch in (False, True):
            with self.subTest(switch=switch):
                make_writable(self.tmp)
                for path in (self.home, self.locks, go, ready):
                    if path.is_dir():
                        shutil.rmtree(path)
                    else:
                        path.unlink(missing_ok=True)
                install = self.install(files={"scripts/epic/claude-tick.sh": entry})
                digest = runtime.sha256_file(install / runtime.MANIFEST)
                runtime.write_pin(self.home, REV, digest, "p1", None)
                self.locks.mkdir(parents=True)
                launcher = subprocess.Popen(
                    [sys.executable, str(LAUNCHER), "claude"], env=dict(os.environ),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )  # fmt: skip
                self.assertTrue(wait_until(ready.exists), launcher.stderr)
                if switch:
                    other = self.install(REV2)
                    digest = runtime.sha256_file(other / runtime.MANIFEST)
                    runtime.write_pin(self.home, REV2, digest, "p2", None)
                go.write_text("")
                _, err = launcher.communicate(timeout=60)
                record = self.locks / "admitted/late.json"
                if switch:
                    self.assertEqual(launcher.returncode, runtime.BUSY, err)
                    self.assertIn("the pin names", err)
                    self.assertFalse(record.exists())
                else:
                    self.assertEqual(launcher.returncode, 0, err)
                    self.assertTrue(record.exists())

    def test_legacy_flag_and_bad_usage_block(self) -> None:
        self.pinned()
        self.assertEqual(
            self.launch(EPIC_RUNTIME_LEGACY="1").returncode, runtime.BLOCKED
        )
        self.assertEqual(self.launch("other").returncode, runtime.BLOCKED)


# --- marker and admission ---------------------------------------------------


class Ticks:
    """Fake ticks: bash shells that admit like the runners do."""

    def __init__(self, case: TempCase) -> None:
        self.case = case
        self.procs: list[subprocess.Popen[str]] = []
        self.named: dict[str, subprocess.Popen[str]] = {}
        case.addCleanup(self.stop)

    def start(self, tick_id: str, body: str = "sleep 30") -> subprocess.Popen[str]:
        script = (
            'exec 17>>"$EPIC_LOCK_DIR/runtime.lock"\n'
            f'python3 "{RUNTIME}" admit --fd 17 --tick-id {tick_id} --agent claude '
            f"--pid $$ || exit $?\n"
            f'echo admitted > "{self.case.tmp}/{tick_id}.ok"\n'
            f"{body}\n"
        )
        self.case.locks.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(
            ["/bin/bash", "-c", script], text=True, start_new_session=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )  # fmt: skip
        self.procs.append(proc)
        self.named[tick_id] = proc
        return proc

    def admitted(self, tick_id: str, timeout: float = 10) -> bool:
        """The tick wrote its marker file; False as soon as it exited without."""
        ok = self.case.tmp / f"{tick_id}.ok"
        proc = self.named.get(tick_id)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if ok.exists():
                return True
            if proc is not None and proc.poll() is not None:
                return ok.exists()
            time.sleep(0.02)
        return False

    def stop(self) -> None:
        for proc in self.procs:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass  # gone; macOS refuses a group of zombies
            proc.wait(timeout=10)
            for stream in (proc.stdout, proc.stderr):
                if stream:
                    stream.close()


@contextmanager
def exclusive(lock: Path) -> Generator[bool]:
    """Try the promoter's LOCK_EX without waiting."""
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def wait_until(check, timeout: float = 10) -> bool:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.05)
    return False


class MarkerTest(TempCase):
    def test_marker_lifecycle(self) -> None:
        marker = runtime.begin_promotion(REV2, REV)
        self.assertEqual(marker["phase"], "prepared")
        with self.assertRaisesRegex(runtime.RuntimeBlocked, "marker exists"):
            runtime.begin_promotion(REV2, REV)
        with self.assertRaisesRegex(runtime.RuntimeBlocked, "belongs to"):
            runtime.set_phase("other", "switched")
        self.assertEqual(
            runtime.set_phase(marker["promotion_id"], "switched")["phase"], "switched"
        )
        with self.assertRaisesRegex(runtime.RuntimeBlocked, "belongs to"):
            runtime.clear_marker("other")
        runtime.clear_marker(marker["promotion_id"])
        self.assertIsNone(runtime.read_marker())

    def test_unreadable_marker_refuses_writes(self) -> None:
        self.locks.mkdir(parents=True)
        runtime.marker_path().write_text("{")
        self.assertIn("unreadable", runtime.write_barrier() or "")

    def test_no_marker_allows_writes(self) -> None:
        self.assertIsNone(runtime.write_barrier())

    def test_marker_appears_whole_and_never_replaces_one(self) -> None:
        # Before the marker is linked in, a write check sees no marker; right
        # after, the complete JSON with admitted null. Never an empty file.
        seen: list[object] = []
        real_link = os.link

        def link(src: object, dst: object) -> None:
            seen.append(runtime.marker_path().exists())
            seen.append(runtime.write_barrier())
            real_link(src, dst)  # type: ignore[arg-type]
            seen.append(runtime.read_marker()["admitted"])  # type: ignore[index]

        with patch.object(runtime.os, "link", link):
            marker = runtime.begin_promotion(REV2, REV)
        self.assertEqual(seen, [False, None, None])
        self.assertEqual(runtime.read_marker(), marker)
        with self.assertRaisesRegex(runtime.RuntimeBlocked, "marker exists"):
            runtime.begin_promotion(REV2, REV)
        self.assertEqual(runtime.read_marker(), marker)
        self.assertEqual(
            [p.name for p in self.locks.iterdir() if p.name.startswith(".")], []
        )

    def test_unpublished_snapshot_refuses_after_the_wait(self) -> None:
        # A promoter that died between the marker and its snapshot.
        self.locks.mkdir(parents=True)
        runtime.write_json(
            runtime.marker_path(),
            {
                "schema": 1,
                "promotion_id": "dead",
                "phase": "prepared",
                "admitted": None,
            },
        )
        with patch.object(runtime, "PUBLISH_WAIT", 0.2):
            self.assertIn("has not published", runtime.write_barrier() or "")


class AdmissionTest(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.ticks = Ticks(self)
        self.lock = self.locks / runtime.LOCK

    def test_admitted_tick_holds_the_lock_and_releases_its_record(self) -> None:
        tick = self.ticks.start("t1", "sleep 0.5")
        self.assertTrue(self.ticks.admitted("t1"))
        self.assertTrue((self.locks / "admitted/t1.json").exists())
        with exclusive(self.lock) as got:
            self.assertFalse(got)
        tick.wait(timeout=10)
        with exclusive(self.lock) as got:
            self.assertTrue(got)
        runtime.release("t1")
        self.assertFalse((self.locks / "admitted/t1.json").exists())

    def test_admission_is_refused_while_promotion_holds_the_lock(self) -> None:
        with exclusive(self.lock) as got:
            self.assertTrue(got)
            tick = self.ticks.start("t2")
            self.assertEqual(tick.wait(timeout=10), runtime.BUSY)
        self.assertFalse((self.locks / "admitted/t2.json").exists())

    def test_stale_marker_after_crashed_promoter_blocks_admission(self) -> None:
        promoter = subprocess.Popen(
            [sys.executable, "-c",
             "import fcntl, os, sys, time; sys.path.insert(0, sys.argv[1]);"
             "import runtime; runtime.begin_promotion('2'*40, None);"
             "f = open(os.path.join(os.environ['EPIC_LOCK_DIR'], 'runtime.lock'), 'a');"
             "fcntl.flock(f, fcntl.LOCK_EX); print('held', flush=True); time.sleep(60)",
             str(HERE)],
            stdout=subprocess.PIPE, text=True,
        )  # fmt: skip
        self.addCleanup(promoter.wait, 10)
        self.assertEqual(promoter.stdout.readline().strip(), "held")  # type: ignore[union-attr]
        promoter.kill()
        promoter.wait(timeout=10)
        promoter.stdout.close()  # type: ignore[union-attr]
        with exclusive(self.lock) as got:
            self.assertTrue(got)  # the OS released the promoter's lock
        tick = self.ticks.start("t3")
        self.assertEqual(tick.wait(timeout=10), runtime.BUSY)
        self.assertIn("in progress", tick.stderr.read())  # type: ignore[union-attr]
        self.assertFalse((self.locks / "admitted/t3.json").exists())

    def test_wrapper_death_with_live_child_keeps_promotion_waiting(self) -> None:
        # The child runs through the prefix without the tick's fd 17, like a
        # Claude tool: only its own LOCK_SH (fd 29) can keep the lock.
        # The command writes `ready` only after the prefix took its lock, so a
        # busy machine cannot kill the tick first. `exec` keeps the child's PID.
        ready = self.tmp / "ready"
        command = f'touch "{ready}" && exec sleep 30'
        body = f'"{LOCKHOLD}" \'{command}\' 17>&- &\necho $! > "{self.tmp}/child"\nwait'
        tick = self.ticks.start("t4", body)
        self.assertTrue(self.ticks.admitted("t4"))
        self.assertTrue(wait_until(lambda: (self.tmp / "child").exists()))
        child = int((self.tmp / "child").read_text())
        self.assertTrue(wait_until(ready.exists))
        os.kill(tick.pid, signal.SIGKILL)
        tick.wait(timeout=10)
        os.kill(child, 0)  # the child is alive
        with exclusive(self.lock) as got:
            self.assertFalse(got)
        os.kill(child, signal.SIGKILL)
        self.assertTrue(wait_until(lambda: exclusive_free(self.lock)))

    def test_promotion_during_post_model_cleanup(self) -> None:
        # The marker appears while the tick runs. The tick's own later writes
        # (review delivery, cleanup) pass; a new tick and an outside caller
        # are refused; the promoter waits until the tick exits.
        go = self.tmp / "go"
        body = (
            f'while [[ ! -e "{go}" ]]; do sleep 0.02; done\n'
            f'python3 "{RUNTIME}" barrier > "{self.tmp}/barrier" 2>&1; '
            f'echo $? >> "{self.tmp}/barrier"\n'
            f'"{LOCKHOLD}" "echo tool-ran" > "{self.tmp}/tool" 2>&1 17>&-; '
            f'echo $? >> "{self.tmp}/tool"\n'
            "sleep 1"
        )
        tick = self.ticks.start("t5", body)
        self.assertTrue(self.ticks.admitted("t5"))
        marker = runtime.begin_promotion(REV2, REV)
        self.assertEqual([r["tick_id"] for r in marker["admitted"]], ["t5"])
        self.assertIn("in progress", runtime.write_barrier(os.getpid()) or "")
        second = self.ticks.start("t6")
        self.assertEqual(second.wait(timeout=10), runtime.BUSY)
        go.write_text("")
        self.assertEqual(tick.wait(timeout=20), 0)
        self.assertEqual((self.tmp / "barrier").read_text().split(), ["0"])
        self.assertEqual((self.tmp / "tool").read_text().split(), ["tool-ran", "0"])
        with exclusive(self.lock) as got:
            self.assertTrue(got)

    def test_outside_process_with_admitted_env_is_refused(self) -> None:
        tick = self.ticks.start("t7")
        self.assertTrue(self.ticks.admitted("t7"))
        runtime.begin_promotion(REV2, REV)
        out = subprocess.run(
            [sys.executable, str(RUNTIME), "barrier"],
            env={**os.environ, "EPIC_ADMITTED": "1"}, capture_output=True, text=True,
            check=False,
        )  # fmt: skip
        self.assertEqual(out.returncode, runtime.MISMATCH, out.stderr)
        tick.kill()

    def test_wrong_start_time_does_not_match(self) -> None:
        table = {10: (1, "Wed Oct  1 10:00:00 2026"), 1: (0, "x")}
        marker = {
            "admitted": [
                {"processes": [{"pid": 10, "start": "Wed Oct 1 10:00:00 2026"}]}
            ]
        }
        self.assertTrue(runtime.admitted_caller(marker, 10, table))
        marker["admitted"][0]["processes"][0]["start"] = "Wed Oct 1 10:00:01 2026"
        self.assertFalse(runtime.admitted_caller(marker, 10, table))

    def test_concurrent_admission_is_in_the_snapshot_or_refused(self) -> None:
        # A tick writes its record before it checks the marker, and the
        # promoter snapshots after writing the marker: no admitted tick can
        # be missing from the snapshot.
        for round_ in range(3):
            ids = [f"c{round_}-{i}" for i in range(8)]
            procs = {i: self.ticks.start(i) for i in ids}
            time.sleep(0.05 * round_)
            marker = runtime.begin_promotion(REV2, REV)
            snapshot = {r["tick_id"] for r in marker["admitted"]}
            for tick_id, proc in procs.items():
                if self.ticks.admitted(tick_id, timeout=5):
                    self.assertIn(tick_id, snapshot)
                else:
                    self.assertEqual(proc.wait(timeout=10), runtime.BUSY)
            self.ticks.stop()
            self.ticks.procs.clear()
            runtime.clear_marker(marker["promotion_id"])

    def test_barrier_during_snapshot_publication_waits_for_it(self) -> None:
        # Between the marker and its snapshot, new ticks are refused at once,
        # and an admitted tick's write check waits for the snapshot instead
        # of being refused.
        tick = self.ticks.start("p1")
        self.assertTrue(self.ticks.admitted("p1"))
        publishing, publish = threading.Event(), threading.Event()
        real_records = runtime.admission_records
        results: dict[str, object] = {}

        def slow_records() -> list[dict[str, object]]:
            publishing.set()
            publish.wait(10)
            return real_records()

        with patch.object(runtime, "admission_records", slow_records):
            promoter = threading.Thread(
                target=lambda: results.update(marker=runtime.begin_promotion(REV2, REV))
            )
            promoter.start()
            self.assertTrue(publishing.wait(10))
            self.assertIsNone(runtime.read_marker()["admitted"])  # type: ignore[index]
            checker = threading.Thread(
                target=lambda: results.update(barrier=runtime.write_barrier(tick.pid))
            )
            checker.start()
            self.assertEqual(self.ticks.start("p2").wait(timeout=10), runtime.BUSY)
            self.assertTrue(checker.is_alive(), "the check did not wait")
            publish.set()
            promoter.join(10)
            checker.join(10)
        self.assertIsNone(results["barrier"])

    def test_prune_keeps_a_tick_admitted_after_the_process_scan(self) -> None:
        # Tick A scans the process table; tick B is admitted; A prunes. B is
        # not in A's table, but its record is newer than the scan and stays.
        real_table = runtime.process_table

        def scan_then_admit_b() -> dict[int, tuple[int, str]]:
            table = real_table()
            self.ticks.start("b")
            self.assertTrue(self.ticks.admitted("b"))
            return table

        self.locks.mkdir(parents=True, exist_ok=True)
        with (
            open(self.lock, "a") as stream,
            patch.object(runtime, "process_table", scan_then_admit_b),
        ):
            self.assertIsNone(
                runtime.admit(stream.fileno(), "a", "claude", os.getpid())
            )
        self.assertTrue((self.locks / "admitted/b.json").exists())
        marker = runtime.begin_promotion(REV2, REV)
        self.assertIn("b", {r["tick_id"] for r in marker["admitted"]})
        self.assertIsNone(runtime.write_barrier(self.ticks.named["b"].pid))

    def test_dead_tick_records_are_pruned(self) -> None:
        tick = self.ticks.start("t8")
        self.assertTrue(self.ticks.admitted("t8"))
        os.killpg(tick.pid, signal.SIGKILL)
        tick.wait(timeout=10)
        record = self.locks / "admitted/t8.json"
        self.assertTrue(record.exists())
        old = time.time() - 10  # older than the prune grace
        os.utime(record, (old, old))
        self.ticks.start("t9")
        self.assertTrue(self.ticks.admitted("t9"))
        self.assertFalse((self.locks / "admitted/t8.json").exists())


@unittest.skipUnless(sys.platform == "darwin", "libproc is macOS only")
class ProcessTableTest(TempCase):
    def test_macos_reads_libproc_without_starting_ps(self) -> None:
        with patch.object(runtime.subprocess, "run", side_effect=AssertionError):
            table = runtime.process_table()
        self.assertEqual(table[os.getpid()][0], os.getppid())
        self.assertIn(os.getppid(), table)

    def test_entries_equal_ps(self) -> None:
        pids = f"{os.getpid()},{os.getppid()}"
        try:
            out = subprocess.run(
                ["/bin/ps", "-o", "pid=,ppid=,lstart=", "-p", pids],
                capture_output=True, text=True, check=True,
            ).stdout  # fmt: skip
        except PermissionError:
            self.skipTest("this sandbox may not run /bin/ps")
        expected = {}
        for line in out.splitlines():
            pid, ppid, *start = line.split()
            expected[int(pid)] = (int(ppid), " ".join(start))
        table = runtime.process_table()
        self.assertEqual({pid: table[pid] for pid in expected}, expected)

    def test_failed_listing_refuses_writes_during_promotion(self) -> None:
        runtime.begin_promotion(REV2, REV)
        with patch("ctypes.CDLL") as cdll:
            cdll.return_value.proc_listallpids.return_value = 0
            with self.assertRaises(OSError):
                runtime.process_table()
            self.assertIn("unreadable", runtime.write_barrier() or "")


def exclusive_free(lock: Path) -> bool:
    with exclusive(lock) as got:
        return got


class LockholdTest(TempCase):
    def run_prefix(self, *args: str) -> subprocess.CompletedProcess[str]:
        self.locks.mkdir(parents=True, exist_ok=True)
        return subprocess.run(
            [str(LOCKHOLD), *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def test_runs_one_command_argument(self) -> None:
        out = self.run_prefix("echo one; echo two")
        self.assertEqual((out.returncode, out.stdout.split()), (0, ["one", "two"]))
        self.assertEqual(self.run_prefix("echo", "x").returncode, 2)
        self.assertEqual(self.run_prefix().returncode, 2)

    def test_refused_while_promotion_holds_the_lock(self) -> None:
        with exclusive(self.locks / runtime.LOCK) as got:
            self.assertTrue(got)
            out = self.run_prefix("echo ran")
        self.assertEqual(out.returncode, 2)
        self.assertNotIn("ran", out.stdout)

    def test_refused_outside_an_admitted_tick_while_a_marker_exists(self) -> None:
        runtime.begin_promotion(REV2, REV)
        out = self.run_prefix("echo ran")
        self.assertEqual(out.returncode, 2)
        self.assertNotIn("ran", out.stdout)

    def test_command_keeps_the_lock(self) -> None:
        self.locks.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen([str(LOCKHOLD), "sleep 2"])
        self.addCleanup(proc.wait, 10)
        self.assertTrue(
            wait_until(lambda: not exclusive_free(self.locks / runtime.LOCK))
        )
        proc.wait(timeout=10)
        self.assertTrue(exclusive_free(self.locks / runtime.LOCK))


# --- smoke ------------------------------------------------------------------


class SmokeTest(TempCase):
    PART = "def check(install, manifest):\n    return {result!r}\n"

    def smoke(self, install: Path, agent: str = "claude") -> tuple[int, list[str]]:
        return smoke.smoke(agent, install / runtime.MANIFEST)

    def test_missing_agent_part_fails(self) -> None:
        code, failures = self.smoke(self.install())
        self.assertEqual(code, smoke.FAIL)
        self.assertIn("scripts/epic/smoke_claude.py is missing", failures[0])

    def test_agent_part_decides(self) -> None:
        install = self.install(
            files={"scripts/epic/smoke_claude.py": self.PART.format(result=[])}
        )
        self.assertEqual(self.smoke(install), (smoke.PASS, []))
        install = self.install(
            REV2,
            files={
                "scripts/epic/smoke_claude.py": self.PART.format(result=["no hook"])
            },
        )
        self.assertEqual(self.smoke(install), (smoke.FAIL, ["no hook"]))

    def test_broken_part_and_bad_return_fail(self) -> None:
        install = self.install(
            files={"scripts/epic/smoke_claude.py": "raise SystemExit(0)\n"}
        )
        self.assertEqual(self.smoke(install)[0], smoke.FAIL)
        install = self.install(
            REV2, files={"scripts/epic/smoke_claude.py": self.PART.format(result="ok")}
        )
        self.assertEqual(self.smoke(install)[0], smoke.FAIL)

    def test_changed_part_is_never_loaded(self) -> None:
        install = self.install(
            files={"scripts/epic/smoke_claude.py": self.PART.format(result=[])}
        )
        part = install / "scripts/epic/smoke_claude.py"
        os.chmod(part.parent, 0o755)
        os.chmod(part, 0o644)
        part.write_text(TRAP + self.PART.format(result=[]))
        trap = self.tmp / "trap"
        with patch.dict(os.environ, {"EPIC_TEST_TRAP": str(trap)}):
            code, failures = self.smoke(install)
        self.assertEqual(code, smoke.FAIL)
        self.assertIn("scripts/epic/smoke_claude.py", " ".join(failures))
        self.assertFalse(trap.exists(), "the changed part ran")

    def test_invalid_manifest_and_usage(self) -> None:
        install = self.install(
            files={"scripts/epic/smoke_claude.py": self.PART.format(result=[])}
        )
        os.chmod(install / "scripts/epic", 0o755)
        (install / "scripts/epic/x.py").write_text("")
        self.assertEqual(self.smoke(install)[0], smoke.FAIL)
        self.assertEqual(smoke.smoke("claude", self.tmp / "none.json")[0], smoke.USAGE)
        with patch("sys.stderr"):
            self.assertEqual(
                smoke.main(["--agent", "other", "--manifest", "x"]), smoke.USAGE
            )


if __name__ == "__main__":
    unittest.main()
