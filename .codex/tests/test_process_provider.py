"""Opt-in process evidence, using temporary state and owned child processes."""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/epic"))
import isolated_env  # noqa: E402, F401

import importlib.util
import json
import os
import subprocess
import tempfile
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


if __name__ == "__main__":
    unittest.main()
