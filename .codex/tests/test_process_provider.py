"""Opt-in process evidence, using temporary state and owned child processes."""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/epic"))
import isolated_env  # noqa: E402, F401

import importlib.util
import json
import os
import select
import subprocess
import tempfile
import textwrap
import unittest
from unittest.mock import patch

import leases
import runtime

HELPER = Path(__file__).resolve().parents[1] / "epic_lock.py"
spec = importlib.util.spec_from_file_location("process_helper", HELPER)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class ProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.provider = helper.ProcessProvider(self.root / "processes")
        self.wrapper = leases.ProcessIdentity(21001, "wrapper start")

    def register(self) -> leases.ProcessEvidence:
        with patch.object(self.provider, "identity", return_value=self.wrapper):
            return self.provider.register("codex", self.wrapper.pid)

    def test_loading_provider_does_not_initialize_coordination(self) -> None:
        with patch.dict(os.environ, {"EPIC_LOCK_DIR": str(self.root / "targets")}):
            loaded = leases.load_provider({"EPIC_PROCESS_PROVIDER": str(HELPER)})
            self.assertTrue(loaded.live)
            self.assertEqual(loaded.name, helper.PROVIDER_NAME)
            self.assertFalse(loaded.compatibility()["activation_ready"])
            self.assertFalse(loaded.admission_blocked("codex"))
            self.assertEqual(list(self.root.iterdir()), [])

    def test_legacy_lock_still_works_without_shared_modules_or_leases(self) -> None:
        standalone = self.root / "epic_lock.py"
        standalone.write_bytes(HELPER.read_bytes())
        result = subprocess.run(
            [
                sys.executable,
                str(standalone),
                str(self.root / "tick.lock"),
                str(os.getpid()),
                "60",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            set(json.loads((self.root / "tick.lock/owner.json").read_text())),
            {"pid", "started_at", "max_age"},
        )
        self.assertFalse((self.root / "leases").exists())

    def test_registration_and_persistent_admission_block(self) -> None:
        evidence = self.register()
        self.assertEqual(leases.ProcessEvidence.from_json(evidence.to_json()), evidence)
        self.assertFalse(self.provider.admission_blocked("codex"))
        self.provider.block("codex")
        self.assertTrue(
            helper.ProcessProvider(self.provider.root).admission_blocked("codex")
        )
        with self.assertRaises(ValueError):
            self.provider.launch("codex", ["must-not-execute"])
        with self.assertRaises(ValueError):
            self.register()

    def test_invalid_owner_does_not_create_store(self) -> None:
        with self.assertRaises(ValueError):
            self.provider.register("../codex", 21001)
        self.assertFalse(self.provider.root.exists())

    def test_legacy_or_corrupt_journal_never_proves_stop(self) -> None:
        evidence = self.register()
        for data in ({"pid": 21001}, {}, {"schema": 1, "blocked": True}):
            with self.subTest(data=data):
                self.provider.path("codex").write_text(json.dumps(data))
                self.assertFalse(self.provider.admission_blocked("codex"))
                self.assertTrue(
                    leases.stop_blockers(evidence, self.provider.stop(evidence))
                )

    def test_stop_rechecks_block_in_same_critical_section(self) -> None:
        evidence = self.register()
        self.provider.block("codex")
        self.assertTrue(self.provider.admission_blocked("codex"))
        data = self.provider.read("codex")
        data["blocked"] = False
        self.provider.write("codex", data)
        with patch.object(self.provider, "probe") as probe:
            self.assertTrue(
                leases.stop_blockers(evidence, self.provider.stop(evidence))
            )
            probe.assert_not_called()

    def test_pending_registration_and_released_workload_refuse(self) -> None:
        evidence = self.register()
        self.provider.block("codex")
        for field in ("pending", "released"):
            with self.subTest(field=field):
                data = self.provider.read("codex")
                data[field] = True
                self.provider.write("codex", data)
                with patch.object(self.provider, "probe") as probe:
                    report = self.provider.stop(evidence)
                    self.assertTrue(leases.stop_blockers(evidence, report))
                    probe.assert_not_called()

    def test_stale_evidence_refuses_even_when_pids_are_gone(self) -> None:
        evidence = self.register()
        self.provider.block("codex")
        data = self.provider.read("codex")
        data["evidence"]["groups"].append({"pid": 21002, "start": "child"})
        self.provider.write("codex", data)
        with patch.object(self.provider, "probe", return_value="gone"):
            self.assertTrue(
                leases.stop_blockers(evidence, self.provider.stop(evidence))
            )

    def test_empty_snapshot_cannot_prove_absence_of_orphaned_descendants(self) -> None:
        evidence = self.register()
        self.provider.block("codex")
        with patch.object(self.provider, "probe", return_value="gone"):
            report = self.provider.stop(evidence)
        self.assertEqual(report.wrapper, "error")
        self.assertTrue(leases.stop_blockers(evidence, report))

    def test_probes_distinguish_esrch_eperm_and_reused_identity(self) -> None:
        for group in (False, True):
            function = "killpg" if group else "kill"
            for exception, expected in (
                (ProcessLookupError(), "gone"),
                (PermissionError(), "eperm"),
                (OSError(), "error"),
            ):
                with self.subTest(group=group, expected=expected):
                    with patch.object(os, function, side_effect=exception):
                        self.assertEqual(
                            self.provider.probe(self.wrapper, group=group), expected
                        )
            with patch.object(os, function) as signal:
                with patch.object(self.provider, "identity", return_value=self.wrapper):
                    self.assertEqual(
                        self.provider.probe(self.wrapper, group=group), "alive"
                    )
                with patch.object(
                    self.provider,
                    "identity",
                    return_value=leases.ProcessIdentity(21001, "new"),
                ):
                    self.assertEqual(
                        self.provider.probe(self.wrapper, group=group), "reused"
                    )
                self.assertTrue(
                    all(call.args[1] == 0 for call in signal.call_args_list)
                )

    def test_live_group_without_leader_identity_is_uncertain(self) -> None:
        with (
            patch.object(os, "killpg"),
            patch.object(self.provider, "identity", side_effect=ValueError),
        ):
            self.assertEqual(self.provider.probe(self.wrapper, group=True), "error")

    def test_missing_snapshot_entry_is_not_esrch(self) -> None:
        with (
            patch.object(os, "kill"),
            patch.object(runtime, "process_table", return_value={}),
        ):
            self.assertEqual(self.provider.probe(self.wrapper), "error")

    def test_start_identity_uses_shared_runtime(self) -> None:
        with patch.object(
            runtime, "process_table", return_value={21001: (1, "shared start")}
        ):
            self.assertEqual(
                self.provider.identity(21001),
                leases.ProcessIdentity(21001, "shared start"),
            )

    def test_exec_gate_eof_never_runs_payload(self) -> None:
        marker = self.root / "ran"
        reader, writer = os.pipe()
        child = subprocess.Popen(
            [
                sys.executable,
                str(HELPER),
                "--process-child",
                str(reader),
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).touch()",
                str(marker),
            ],
            pass_fds=(reader,),
        )
        os.close(reader)
        os.close(writer)
        self.assertEqual(child.wait(timeout=10), 75)
        self.assertFalse(marker.exists())

    def test_launch_records_identity_and_uncertainty_before_exec(self) -> None:
        # The payload reads its own durable journal, proving the gate ordering.
        evidence = self.provider.register("codex", os.getpid())
        output = self.root / "observed.json"
        script = (
            "import json,os,sys; from pathlib import Path; "
            "d=json.loads(Path(sys.argv[1]).read_text()); "
            "assert d['released'] and not d['pending']; "
            "assert any(p['pid']==os.getpid() and p['start'] for p in d['evidence']['groups']); "
            "Path(sys.argv[2]).write_text(json.dumps(d))"
        )
        child = self.provider.launch(
            "codex",
            [
                sys.executable,
                "-c",
                script,
                str(self.provider.path("codex")),
                str(output),
            ],
        )
        self.assertEqual(child.wait(timeout=10), 0)
        self.assertTrue(output.exists())
        self.provider.block("codex")
        self.assertTrue(leases.stop_blockers(evidence, self.provider.stop(evidence)))

    def test_launch_failure_keeps_uncertain_registration(self) -> None:
        self.provider.register("codex", os.getpid())
        with patch.object(subprocess, "Popen", side_effect=OSError("spawn failed")):
            with self.assertRaises(OSError):
                self.provider.launch("codex", ["unused"])
        self.assertTrue(self.provider.read("codex")["pending"])
        with self.assertRaises(ValueError):
            self.provider.launch("codex", ["unused"])


class LifecycleTests(unittest.TestCase):
    """Real isolated wrappers; their children exit when our input pipe closes."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.provider = helper.ProcessProvider(self.root / "processes")
        self.loader = textwrap.dedent("""
            import importlib.util, json, os, signal, sys
            from pathlib import Path
            spec = importlib.util.spec_from_file_location("helper", sys.argv[1])
            helper = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(helper)
            provider = helper.ProcessProvider(Path(sys.argv[2]))
        """)

    def start(self, script: str, *args: str) -> subprocess.Popen:
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                self.loader + script,
                str(HELPER),
                str(self.provider.root),
                *args,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )

        def cleanup() -> None:
            # Kill only the Popen-owned wrapper; no orphan is signalled by PID.
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)
            child.stdin.close()  # EOF also releases any surviving payload.
            child.stdout.close()
            child.stderr.close()

        self.addCleanup(cleanup)
        return child

    def line(self, child: subprocess.Popen) -> bytes:
        ready, _, _ = select.select([child.stdout, child.stderr], [], [], 10)
        self.assertTrue(ready, "fixture failed to report progress")
        if child.stderr in ready:
            errors = []
            for _ in range(20):
                readable, _, _ = select.select([child.stderr], [], [], 0.1)
                if not readable:
                    break
                chunk = os.read(child.stderr.fileno(), 4096)
                if not chunk:
                    break
                errors.append(chunk)
            if errors:
                self.fail(f"fixture stderr: {b''.join(errors)!r}")
        line = child.stdout.readline()
        self.assertTrue(line, "fixture exited before reporting progress")
        return line.strip()

    def test_killed_wrapper_cannot_hide_live_or_escaped_child(self) -> None:
        for escape in (False, True):
            with self.subTest(escape=escape):
                owner = "codex-escaped" if escape else "codex-grouped"
                # The escaped grandchild is reparented and starts its own
                # session. Its ancestor exits, making a later tree scan lossy.
                payload = textwrap.dedent("""
                    import json, os, sys
                    if sys.argv[1] == "escape":
                        if os.fork():
                            os._exit(0)
                        os.setsid()
                    print(json.dumps({"pid": os.getpid(), "group": os.getpgrp()}), flush=True)
                    sys.stdin.buffer.read(1)
                    print("drained", flush=True)
                """)
                wrapper = self.start(
                    textwrap.dedent("""
                    provider.register(sys.argv[3], os.getpid())
                    provider.launch(sys.argv[3], [sys.executable, "-c", sys.argv[4], sys.argv[5]])
                    signal.pause()
                """),
                    owner,
                    payload,
                    "escape" if escape else "group",
                )
                live_child = json.loads(self.line(wrapper))
                data = self.provider.read(owner)
                evidence = leases.ProcessEvidence.from_json(data["evidence"])
                if escape:
                    self.assertNotIn(
                        live_child["group"], [p.pid for p in evidence.groups]
                    )
                else:
                    self.assertEqual(live_child["group"], evidence.groups[0].pid)
                wrapper.kill()
                wrapper.wait(timeout=10)
                self.provider.block(owner)
                self.assertEqual(self.provider.probe(evidence.wrapper), "gone")
                os.kill(live_child["pid"], 0)  # Child still lives after wrapper exit.
                self.assertTrue(
                    leases.stop_blockers(evidence, self.provider.stop(evidence))
                )
                wrapper.stdin.close()
                self.assertEqual(self.line(wrapper), b"drained")

    def test_competing_registrations_retain_one_wrapper_identity(self) -> None:
        contenders = [
            self.start(
                textwrap.dedent("""
            print("ready", flush=True)
            sys.stdin.buffer.read(1)
            try:
                evidence = provider.register("codex", os.getpid())
            except ValueError:
                print("refused", flush=True)
            else:
                print(json.dumps(evidence.to_json()), flush=True)
        """)
            )
            for _ in range(2)
        ]
        for contender in contenders:
            self.assertEqual(self.line(contender), b"ready")
        for contender in contenders:
            contender.stdin.write(b"1")
        outcomes = [self.line(contender) for contender in contenders]
        self.assertEqual(outcomes.count(b"refused"), 1)
        winner = json.loads(next(value for value in outcomes if value != b"refused"))
        self.assertEqual(self.provider.read("codex")["evidence"], winner)
        for contender in contenders:
            self.assertEqual(contender.wait(timeout=10), 0)

    def test_wrapper_death_during_registration_never_executes_payload(self) -> None:
        marker = self.root / "unexpected-payload"
        wrapper = self.start(
            textwrap.dedent("""
            provider.register("codex", os.getpid())
            original_identity = provider.identity
            def before_child_identity(pid):
                if pid != os.getpid():
                    print("spawned", flush=True)
                    signal.pause()
                return original_identity(pid)
            provider.identity = before_child_identity
            provider.launch("codex", [sys.executable, "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).touch()", sys.argv[3]])
        """),
            str(marker),
        )
        self.assertEqual(self.line(wrapper), b"spawned")
        self.assertTrue(self.provider.read("codex")["pending"])
        wrapper.kill()
        wrapper.wait(timeout=10)
        # EOF on stdout proves that the gated child closed its inherited pipe.
        ready, _, _ = select.select([wrapper.stdout], [], [], 10)
        self.assertTrue(ready, "gated child failed to exit after wrapper death")
        self.assertEqual(wrapper.stdout.read(), b"")
        self.assertFalse(marker.exists())
        self.provider.block("codex")
        evidence = leases.ProcessEvidence.from_json(
            self.provider.read("codex")["evidence"]
        )
        self.assertTrue(leases.stop_blockers(evidence, self.provider.stop(evidence)))

    def test_admission_block_serializes_with_inflight_launch(self) -> None:
        wrapper = self.start(
            textwrap.dedent("""
            provider.register("codex", os.getpid())
            original_write = provider.write
            def held_write(owner, data):
                original_write(owner, data)
                if data["pending"]:
                    print("pending", flush=True)
                    sys.stdin.buffer.read(1)
            provider.write = held_write
            child = provider.launch("codex", [sys.executable, "-c", "pass"])
            child.wait(timeout=10)
            print("launched", flush=True)
        """)
        )
        self.assertEqual(self.line(wrapper), b"pending")
        stale = leases.ProcessEvidence.from_json(
            self.provider.read("codex")["evidence"]
        )
        blocker = self.start(
            textwrap.dedent("""
            original_flock = helper.fcntl.flock
            announced = False
            def observed_flock(fd, flags):
                global announced
                try:
                    return original_flock(fd, flags)
                except BlockingIOError:
                    if not announced:
                        print("contended", flush=True)
                        announced = True
                    raise
            helper.fcntl.flock = observed_flock
            provider.block("codex")
            print("blocked", flush=True)
        """)
        )
        self.assertEqual(self.line(blocker), b"contended")
        wrapper.stdin.write(b"1")
        self.assertEqual(self.line(wrapper), b"launched")
        self.assertEqual(self.line(blocker), b"blocked")
        for child in (wrapper, blocker):
            self.assertEqual(child.wait(timeout=10), 0)
        data = self.provider.read("codex")
        self.assertTrue(data["blocked"])
        self.assertFalse(data["pending"])
        self.assertEqual(len(data["evidence"]["groups"]), 1)
        with self.assertRaises(ValueError):
            self.provider.launch("codex", ["must-not-execute"])
        self.assertTrue(leases.stop_blockers(stale, self.provider.stop(stale)))


if __name__ == "__main__":
    unittest.main()
