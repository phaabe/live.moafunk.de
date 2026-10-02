"""The Claude runner in pinned mode (https://github.com/phaabe/live.moafunk.de/issues/585).

The real launcher (epic-tick) starts the real claude-tick.sh from a sealed fake
install built from the RunnerHarness stubs, with a real manifest and pin. Only
git, gh, the selector, the gate, the worktree step and the model are stubs.

Run: python3 -m unittest discover -s scripts/epic

Disposable installs, locks and processes only: no model, no GitHub, no live
runner state.
"""

from __future__ import annotations

import isolated_env  # noqa: F401  (first: hides live runner state)

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

import runtime
import smoke
from test_claude_tick import ROOT, RunnerHarness
from test_runtime import exclusive, wait_until

REV = "5" * 40
GTIMEOUT = shutil.which("gtimeout")
LAUNCHER = ROOT / "scripts/epic/epic-tick"
# A stub that must never run: it leaves a trace.
TRAP = '#!/bin/bash\necho "$0" >> "$TEST_CALLS.trap"\nexit 97\n'


def seal(root: Path) -> None:
    """Read-only like a real install; executables keep their x bit."""
    for dirpath, _, filenames in os.walk(root, topdown=False):
        for name in filenames:
            path = Path(dirpath) / name
            path.chmod(0o555 if os.access(path, os.X_OK) else 0o444)
        os.chmod(dirpath, 0o555)


def unseal(root: Path) -> None:
    for dirpath, _, filenames in os.walk(root):
        os.chmod(dirpath, 0o755)
        for name in filenames:
            (Path(dirpath) / name).chmod(0o644)


@unittest.skipUnless(GTIMEOUT, "needs gtimeout for the manifest")
class PinnedTick(RunnerHarness):
    def setUp(self) -> None:
        super().setUp()
        # Its own home: the harness home stays unconfigured for legacy ticks.
        self.home = self.root / "pinned-home"
        self.locks = self.root / "locks"
        # The runner checkout: git only. Code here must never run.
        self.checkout = self.root / "runner-checkout"
        (self.checkout / ".git").mkdir(parents=True)
        (self.checkout / "scripts/epic").mkdir(parents=True)
        for name in ("next_action.py", "tick_verify.py", "tick_gate.py"):
            (self.checkout / "scripts/epic" / name).write_text(
                "import os\nopen(os.environ['TEST_CALLS'] + '.checkout-code', 'a')"
                ".write(__file__ + '\\n')\n"
            )
        self.gitnexus = self.root / "gitnexus"
        self.gitnexus.write_text("#!/bin/bash\nexit 0\n")
        self.gitnexus.chmod(0o755)
        self.install = self.build_install()
        self.addCleanup(unseal, self.root)
        # PATH `claude` is not the pinned binary: a run of it is a failure.
        (self.root / "bin/claude").write_text(TRAP)

    def build_install(self, without: tuple[str, ...] = ()) -> Path:
        install = self.home / "revisions" / REV
        shutil.copytree(self.repo, install)
        shutil.copytree(
            ROOT / ".claude/hooks/scripts", install / ".claude/hooks/scripts"
        )
        shutil.copyfile(
            ROOT / ".claude/settings.json", install / ".claude/settings.json"
        )
        (install / "scripts/epic/smoke_claude.py").write_text(
            (ROOT / "scripts/epic/smoke_claude.py").read_text()
        )
        mcp = {
            "mcpServers": {"gitnexus": {"command": str(self.gitnexus), "args": ["mcp"]}}
        }
        (install / "scripts/epic/claude-mcp-config.json").write_text(json.dumps(mcp))
        claude = install / ".claude-runtime/bin/claude"
        claude.parent.mkdir(parents=True)
        shutil.copyfile(self.root / "bin/claude", claude)
        claude.chmod(0o755)
        executables = {
            "claude": {"path": str(claude), "version": "test"},
            "python3": {"path": sys.executable, "version": sys.version.split()[0]},
            "gtimeout": {"path": GTIMEOUT, "version": "test"},
            "gitnexus": {"path": str(self.gitnexus), "version": "test"},
        }
        for name in without:
            executables.pop(name, None)
        runtime.build_manifest(
            install,
            REV,
            executables,
            settings=("claude_runner_settings", "claude_mcp_config"),
        )
        seal(install)
        digest = runtime.sha256_file(install / runtime.MANIFEST)
        runtime.write_pin(self.home, REV, digest, "p1", None)
        return install

    def rebuild(self, without: tuple[str, ...]) -> None:
        unseal(self.home)
        shutil.rmtree(self.home / "revisions")
        self.install = self.build_install(without)

    def launch(self, **extra: str) -> subprocess.Popen[bytes]:
        env = {k: v for k, v in self.env.items() if k != "EPIC_RUNTIME_LEGACY"}
        env.update(
            EPIC_RUNTIME_HOME=str(self.home),
            EPIC_TRUSTED_ROOT=str(self.checkout),
            EPIC_WORKTREE_DIR=str(self.root / "worktrees"),
            **extra,
        )
        return subprocess.Popen([sys.executable, str(LAUNCHER), "claude"], env=env)

    def log(self) -> str:
        path = self.state / "claude.log"
        return path.read_text() if path.exists() else "(no runner log)"

    def made(self, kind: str) -> list[list[str]]:
        if not self.calls.exists():
            return []
        return [c for c in self.calls_made() if c[0] == kind]

    def session_env(self) -> list[str]:
        return Path(str(self.calls) + ".session-env").read_text().splitlines()

    def admission_records(self) -> list[Path]:
        return sorted((self.locks / "admitted").glob("*.json"))

    # --- the pinned session ------------------------------------------------

    def test_the_pinned_install_runs_with_the_pinned_session(self) -> None:
        self.assertEqual(self.launch().wait(timeout=60), 0, self.log())
        self.assertIn(f"runtime={REV}", self.log())
        # The checkout is fetched, never pulled, and its code never runs.
        git = [c[1] for c in self.made("git")]
        self.assertIn("fetch -q origin", git)
        self.assertFalse(any(c.startswith("pull") for c in git), git)
        self.assertFalse(Path(str(self.calls) + ".noise").exists())
        self.assertFalse(Path(str(self.calls) + ".checkout-code").exists())
        self.assertFalse(Path(str(self.calls) + ".trap").exists())
        (args,) = [c[1] for c in self.made("claude")]
        settings = self.install / "scripts/epic/claude-runner-settings.json"
        self.assertIn(f"--setting-sources  --settings {settings}", args)
        self.assertIn("--strict-mcp-config", args)
        config = json.loads(args.split("--mcp-config ", 1)[1].split(" --permission")[0])
        self.assertEqual(
            config["mcpServers"]["gitnexus"]["command"], str(self.gitnexus)
        )
        gate = config["mcpServers"]["epic-gate"]
        self.assertEqual(gate["command"], sys.executable)
        self.assertEqual(
            gate["args"], [str(self.install / "scripts/epic/permission_gate.py")]
        )
        self.assertEqual(gate["env"]["EPIC_TRUSTED_ROOT"], str(self.checkout))
        prefix, updater, python = self.session_env()
        self.assertEqual(prefix, str(self.install / "scripts/epic/lockhold"))
        self.assertEqual(updater, "1")
        self.assertEqual(os.path.realpath(python), os.path.realpath(sys.executable))
        # The record is gone once the tick ended.
        self.assertEqual(self.admission_records(), [])

    def test_a_later_tick_keeps_the_pinned_revision(self) -> None:
        # Integration advances: the checkout gets new runner code. Pinned
        # ticks keep running the install until a promotion.
        for name in ("claude-tick.sh", "permission_gate.py"):
            (self.checkout / "scripts/epic" / name).write_text("exit 99\n")
        for _ in range(2):
            self.assertEqual(self.launch().wait(timeout=60), 0, self.log())
        self.assertEqual(self.log().count(f"runtime={REV}"), 2)
        self.assertFalse(Path(str(self.calls) + ".checkout-code").exists())
        self.assertEqual(len(self.made("claude")), 2)

    def test_path_never_shadows_the_manifest_binaries(self) -> None:
        bin_dir = self.root / "bin"
        for name in ("python3", "gtimeout", "timeout"):
            (bin_dir / name).write_text(TRAP)
            (bin_dir / name).chmod(0o755)
        self.assertEqual(self.launch().wait(timeout=60), 0, self.log())
        self.assertFalse(Path(str(self.calls) + ".trap").exists(), self.log())
        python = self.session_env()[2]
        self.assertEqual(os.path.realpath(python), os.path.realpath(sys.executable))

    # --- fail closed -------------------------------------------------------

    def test_a_missing_manifest_executable_stops_before_selection(self) -> None:
        for name in ("gtimeout", "claude"):
            with self.subTest(missing=name):
                self.calls.unlink(missing_ok=True)
                self.rebuild(without=(name,))
                self.assertEqual(self.launch().wait(timeout=60), 78, self.log())
                self.assertEqual(self.made("select"), [])
                self.assertEqual(self.admission_records(), [])

    def test_a_component_changed_during_the_tick_blocks_the_model(self) -> None:
        # The selector (a pinned stub) changes another install file, as if
        # someone edited the install while the tick ran.
        target = self.install / "scripts/epic/tick_cooldown.py"
        selector = self.install / "scripts/epic/next_action.py"
        unseal(self.install)
        selector.write_text(
            selector.read_text()
            + f"\nimport os\nos.chmod({str(target.parent)!r}, 0o755)\n"
            f"os.chmod({str(target)!r}, 0o644)\n"
            f"open({str(target)!r}, 'a').write('# changed\\n')\n"
        )
        runtime.build_manifest(
            self.install,
            REV,
            json.loads((self.install / runtime.MANIFEST).read_text())["executables"],
            settings=("claude_runner_settings", "claude_mcp_config"),
        )
        seal(self.install)
        digest = runtime.sha256_file(self.install / runtime.MANIFEST)
        runtime.write_pin(self.home, REV, digest, "p1", None)
        self.assertEqual(self.launch().wait(timeout=60), 78, self.log())
        self.assertIn("fails validation", self.log())
        self.assertEqual(self.made("claude"), [])

    def test_legacy_without_the_flag_exits_78(self) -> None:
        env = {k: v for k, v in self.env.items() if k != "EPIC_RUNTIME_LEGACY"}
        tick = subprocess.Popen(
            ["/bin/bash", str(self.repo / "scripts/epic/claude-tick.sh")], env=env
        )
        self.assertEqual(tick.wait(timeout=60), 78)
        self.assertEqual(self.made("select"), [])

    # --- admission ---------------------------------------------------------

    def test_a_promotion_marker_refuses_pinned_and_legacy_ticks(self) -> None:
        os.environ["EPIC_LOCK_DIR"] = str(self.locks)
        self.addCleanup(os.environ.pop, "EPIC_LOCK_DIR", None)
        marker = runtime.begin_promotion("6" * 40, REV)
        self.assertEqual(self.launch().wait(timeout=60), runtime.BUSY, self.log())
        self.assertEqual(self.run_tick().wait(timeout=60), runtime.BUSY, self.log())
        self.assertEqual(self.made("select"), [])
        self.assertEqual(self.admission_records(), [])
        runtime.clear_marker(marker["promotion_id"])

    def test_an_admission_error_ends_the_tick_before_selection(self) -> None:
        self.locks.mkdir()
        (self.locks / "admitted").write_text("not a directory")
        self.assertEqual(self.launch().wait(timeout=60), runtime.UNREADABLE)
        self.assertEqual(self.made("select"), [])

    def test_admission_is_held_through_the_model_and_released_after(self) -> None:
        tick = self.launch(TEST_MODEL_SLEEP="3")
        self.assertTrue(wait_until(self.model_pid.exists, 30), self.log())
        self.assertEqual(len(self.admission_records()), 1)
        with exclusive(self.locks / runtime.LOCK) as got:
            self.assertFalse(got, "a promotion could start while the model runs")
        self.assertEqual(tick.wait(timeout=60), 0, self.log())
        self.assertEqual(self.admission_records(), [])
        with exclusive(self.locks / runtime.LOCK) as got:
            self.assertTrue(got)

    def test_a_prefix_child_keeps_admission_after_the_tick(self) -> None:
        for mode in ("pinned", "legacy"):
            with self.subTest(mode=mode):
                Path(str(self.calls) + ".child").unlink(missing_ok=True)
                start = self.launch if mode == "pinned" else self.run_tick
                if mode == "legacy":  # legacy runs `claude` from PATH: the stub
                    shutil.copyfile(
                        self.install / ".claude-runtime/bin/claude",
                        self.root / "bin/claude",
                    )
                self.assertEqual(start(TEST_PREFIX_CHILD="3").wait(timeout=60), 0)
                child = int(Path(str(self.calls) + ".child").read_text())
                with exclusive(self.locks / runtime.LOCK) as got:
                    self.assertFalse(got, "the child's lock went with the tick")

                def gone() -> bool:
                    try:
                        os.kill(child, 0)
                    except ProcessLookupError:
                        return True
                    return False

                self.assertTrue(wait_until(gone, 15))
                time.sleep(0.2)
                with exclusive(self.locks / runtime.LOCK) as got:
                    self.assertTrue(got)

    # --- smoke -------------------------------------------------------------

    def test_the_smoke_check_passes_on_the_install(self) -> None:
        code, failures = smoke.smoke("claude", self.install / runtime.MANIFEST)
        self.assertEqual((code, failures), (smoke.PASS, []))

    def resealed(self, change, without: tuple[str, ...] = ()) -> list[str]:  # type: ignore[no-untyped-def]
        """Smoke failures of an install changed by `change(install)`, with a
        fresh manifest (so the shared validation passes)."""
        unseal(self.home)
        shutil.rmtree(self.home / "revisions")
        install = self.build_install(without)
        unseal(install)
        change(install)
        executables = json.loads((install / runtime.MANIFEST).read_text())[
            "executables"
        ]
        runtime.build_manifest(
            install,
            REV,
            {k: v for k, v in executables.items() if k not in without},
            settings=("claude_runner_settings", "claude_mcp_config"),
        )
        seal(install)
        if "prefix" in without:
            os.chmod(install / "scripts/epic/lockhold", 0o444)
        code, failures = smoke.smoke("claude", install / runtime.MANIFEST)
        self.assertEqual(code, smoke.FAIL if failures else smoke.PASS)
        return failures

    def test_the_smoke_check_rejects_a_missing_kept_item(self) -> None:
        settings_rel = "scripts/epic/claude-runner-settings.json"

        def edit(install: Path, fn) -> None:  # type: ignore[no-untyped-def]
            path = install / settings_rel
            data = json.loads(path.read_text())
            fn(data)
            path.write_text(json.dumps(data))

        def drop_hook(data: dict) -> None:  # type: ignore[type-arg]
            bash = data["hooks"]["PreToolUse"][0]["hooks"]
            data["hooks"]["PreToolUse"][0]["hooks"] = [
                h for h in bash if "epic-guard" not in h["command"]
            ]

        def checkout_hook(data: dict) -> None:  # type: ignore[type-arg]
            hook = data["hooks"]["PreToolUse"][0]["hooks"][0]
            hook["command"] = hook["command"].replace(
                "$EPIC_RUNTIME_ROOT", "$CLAUDE_PROJECT_DIR"
            )

        cases = {
            "missing hook PreToolUse Bash epic-guard.sh": lambda i: edit(i, drop_hook),
            "is not a manifest file under the runtime root": lambda i: edit(
                i, checkout_hook
            ),
            "missing deny rule Read(~/.ssh/**)": lambda i: edit(
                i, lambda d: d["permissions"]["deny"].remove("Read(~/.ssh/**)")
            ),
            "missing ask rule Bash(git -*)": lambda i: edit(
                i, lambda d: d["permissions"]["ask"].remove("Bash(git -*)")
            ),
            "env DISABLE_TELEMETRY is not 1": lambda i: edit(
                i, lambda d: d["env"].pop("DISABLE_TELEMETRY")
            ),
            "does not run the manifest's gitnexus": lambda i: (
                i / "scripts/epic/claude-mcp-config.json"
            ).write_text(
                json.dumps({"mcpServers": {"gitnexus": {"command": "gitnexus"}}})
            ),
        }
        for expected, change in cases.items():
            with self.subTest(expected=expected):
                failures = self.resealed(change)
                self.assertTrue(any(expected in f for f in failures), failures)
        for name in ("gtimeout", "claude", "gitnexus"):
            with self.subTest(missing=name):
                failures = self.resealed(lambda i: None, without=(name,))
                self.assertIn(f"the manifest names no {name} executable", failures)
        with self.subTest(prefix="not executable"):
            failures = self.resealed(lambda i: None, without=("prefix",))
            self.assertIn(
                "the prefix scripts/epic/lockhold is not executable", failures
            )


class PinnedHookBarrier(unittest.TestCase):
    """The pinned hook during a promotion, outside admitted ticks (decision:
    https://github.com/phaabe/live.moafunk.de/issues/584#issuecomment-5948756741)."""

    def setUp(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="hook-")))
        self.locks = tmp / "locks"
        env = {"EPIC_LOCK_DIR": str(self.locks), "EPIC_RUNTIME_HOME": str(tmp / "rt")}
        self.enterContext(unittest.mock.patch.dict(os.environ, env))
        runtime.begin_promotion("7" * 40, REV)

    def hook(self, tool: str, tool_input: dict[str, str], **env: str) -> int:
        payload = {"tool_name": tool, "tool_input": tool_input, "cwd": str(ROOT)}
        base = {k: v for k, v in os.environ.items() if k != "EPIC_ACTION_FILE"}
        return subprocess.run(
            ["/bin/bash", str(ROOT / ".claude/hooks/scripts/epic-guard.sh")],
            input=json.dumps(payload), capture_output=True, text=True, timeout=60,
            env={**base, "EPIC_RUNTIME_ROOT": str(ROOT), **env}, check=False,
        ).returncode  # fmt: skip

    def test_every_bash_call_and_github_write_is_refused(self) -> None:
        for reader in ("0", "1"):
            for env in ({}, {"EPIC_ADMITTED": "1"}):
                with self.subTest(reader=reader, env=env):
                    shared = {"EPIC_SHARED_READER": reader, **env}
                    self.assertEqual(self.hook("Bash", {"command": "ls"}, **shared), 2)
                    create = "mcp__github__create_issue"
                    self.assertEqual(self.hook(create, {"title": "x"}, **shared), 2)
                    read = "mcp__github__get_issue"
                    self.assertEqual(
                        self.hook(read, {"issue_number": "1"}, **shared), 0
                    )


class PinnedCodeLoading(unittest.TestCase):
    """The gate and the hook load their checks from EPIC_RUNTIME_ROOT."""

    def test_permission_gate_loads_write_checks_from_the_runtime_root(self) -> None:
        tmp = Path(
            self.enterContext(tempfile.TemporaryDirectory(prefix="pinned-gate-"))
        )
        install, checkout = tmp / "install", tmp / "checkout"
        for root in (install, checkout):
            (root / "scripts/epic").mkdir(parents=True)
        shutil.copyfile(
            ROOT / "scripts/epic/permission_gate.py",
            install / "scripts/epic/permission_gate.py",
        )
        for name in ("git_gate.py", "runtime.py", "target_lock.py"):
            shutil.copyfile(
                ROOT / "scripts/epic" / name, install / "scripts/epic" / name
            )
        # The install's write checks: refuse one merge, allow the other.
        (install / "scripts/epic/write_checks.py").write_text(
            "def guard(tool, tool_input, cwd, reader=None, agent=None):\n"
            "    return 'pinned refusal' if 'b' * 40 in tool_input['command'] else None\n"
        )
        # The checkout's copy must never load.
        (checkout / "scripts/epic/write_checks.py").write_text(
            "raise SystemExit('checkout write_checks loaded')\n"
        )
        probe = (
            "import json, sys\n"
            f"sys.path.insert(0, {str(install / 'scripts/epic')!r})\n"
            "import permission_gate\n"
            # A head-pinned squash merge: rule() allows it, then the checks run.
            "merge = 'gh pr merge 5 --repo phaabe/live.moafunk.de --squash "
            "--match-head-commit '\n"
            "for sha in ('a' * 40, 'b' * 40):\n"
            "    print(json.dumps(permission_gate.decide('Bash', {'command': merge + sha})))\n"
            "import write_checks\n"
            "print(write_checks.__file__)\n"
        )
        env = {
            **os.environ,
            "EPIC_SHARED_READER": "1",
            "EPIC_ACTION_FILE": str(tmp / "action.json"),
            "EPIC_RUNTIME_ROOT": str(install),
            "EPIC_TRUSTED_ROOT": str(checkout),
        }
        out = subprocess.run(
            [sys.executable, "-c", probe], env=env, cwd=checkout,
            capture_output=True, text=True, timeout=60, check=False,
        )  # fmt: skip
        self.assertEqual(out.returncode, 0, out.stderr)
        allowed, refused, loaded = out.stdout.splitlines()
        self.assertTrue(json.loads(allowed)[0], allowed)
        self.assertEqual(json.loads(refused), [False, "fresh check: pinned refusal"])
        self.assertEqual(loaded, str(install / "scripts/epic/write_checks.py"))


if __name__ == "__main__":
    unittest.main()
