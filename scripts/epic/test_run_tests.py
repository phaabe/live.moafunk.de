"""The parallel test runner. Run: python3 scripts/epic/run_tests.py scripts/epic"""

from __future__ import annotations

import isolated_env  # noqa: F401  (first: hides live runner state)

import contextlib
import errno
import fcntl
import io
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import rebase_policy
import run_tests

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
RUNNER = HERE / "run_tests.py"
ISOLATED = HERE / "isolated_env.py"


def entry(
    count: int,
    load_tests: bool = False,
    fixtures: tuple[str, ...] = (),
    errors: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "ids": [f"m.C.test_{n:03}" for n in range(count)],
        "load_tests": load_tests,
        "fixtures": list(fixtures),
        "load_errors": list(errors),
    }


def plain_module(count: int, body: str = "pass") -> str:
    tests = "".join(
        f"    def test_{n:03}(self):\n        {body}\n" for n in range(count)
    )
    return f"import unittest\n\n\nclass Plain(unittest.TestCase):\n{tests}"


# Like .codex/tests/test_review_delivery_runner.py: load_tests returns every
# test and ignores -k.
LOAD_TESTS_IGNORES_K = plain_module(25) + textwrap.dedent(
    """

    def load_tests(loader, tests, pattern):
        return unittest.TestSuite(
            Plain(name) for name in Plain.__dict__ if name.startswith("test_")
        )
    """
)

CLASS_FIXTURE = plain_module(25).replace(
    "class Plain(unittest.TestCase):\n",
    "class Plain(unittest.TestCase):\n"
    "    @classmethod\n"
    "    def setUpClass(cls):\n"
    "        cls.ready = True\n\n",
)

MODULE_FIXTURE = plain_module(22) + "\n\ndef setUpModule():\n    pass\n"


def slot_folder(path: Path, count: int = 1) -> Path:
    """A shared slot folder as the operator makes it. `path` must be canonical."""
    path.mkdir(mode=0o700)
    path.chmod(0o700)  # whatever the umask
    capacity = path / run_tests.CAPACITY_FILE
    capacity.write_text(json.dumps({"schema": 1, "slots": count}) + "\n")
    capacity.chmod(0o600)
    return path


class SplitRuleTest(unittest.TestCase):
    def parts(self, **modules: dict[str, Any]) -> list[run_tests.Part]:
        return run_tests.plan({"modules": modules})

    def test_twenty_tests_run_whole(self) -> None:
        (part,) = self.parts(m=entry(20))
        self.assertFalse(part.split)
        self.assertEqual(part.args(), ["-p", "m"])

    def test_more_than_twenty_split_into_parts_of_at_most_fifteen(self) -> None:
        for count, sizes in ((21, [11, 10]), (30, [15, 15]), (46, [12, 12, 12, 10])):
            with self.subTest(count=count):
                parts = self.parts(m=entry(count))
                self.assertEqual([len(p.ids) for p in parts], sizes)
                self.assertEqual(sum((p.ids for p in parts), []), entry(count)["ids"])
                self.assertTrue(all(p.split for p in parts))

    def test_split_part_selects_each_test_by_full_id(self) -> None:
        part = self.parts(m=entry(21))[0]
        args = part.args()
        self.assertEqual(args[:2], ["-p", "m"])
        self.assertEqual(args[2::2], ["-k"] * len(part.ids))
        self.assertEqual(args[3::2], [f"*{i}" for i in part.ids])
        self.assertEqual(part.label, "m[1/2]")

    def test_load_tests_fixtures_and_load_errors_run_whole(self) -> None:
        for name, module in (
            ("load_tests", entry(40, load_tests=True)),
            ("setUpClass", entry(40, fixtures=("C.setUpClass",))),
            ("setUpModule", entry(40, fixtures=("m.setUpModule",))),
            ("load error", entry(40, errors=("unittest.loader._FailedTest.m",))),
        ):
            with self.subTest(name):
                (part,) = self.parts(m=module)
                self.assertFalse(part.split)
                self.assertEqual(len(part.ids), 40)

    def test_largest_parts_start_first(self) -> None:
        parts = self.parts(a=entry(3), b=entry(18), c=entry(9))
        self.assertEqual([p.module for p in parts], ["b", "c", "a"])

    def test_default_jobs_is_the_cpu_count(self) -> None:
        args = run_tests.parse_args([str(HERE)])
        self.assertEqual(args.jobs, os.cpu_count() or 1)
        self.assertEqual(run_tests.parse_args([str(HERE), "-j", "3"]).jobs, 3)


def signal_group_kill(proc: subprocess.Popen[str]) -> None:
    """Clean up a driver and its children, also when a test failed."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()


class FixtureSuite(unittest.TestCase):
    """A small test directory, run through the real runner."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="run-tests-fixture-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()  # a shared slot folder is canonical
        self.top = self.root / "suite"
        self.top.mkdir()
        self.tmpdir = self.root / "tmpdir"
        self.tmpdir.mkdir()
        self.slots = slot_folder(self.root / "slots")

    def write(self, name: str, text: str) -> None:
        (self.top / name).write_text(text)

    def env(self, **extra: str) -> dict[str, str]:
        # Own slot folder: a fixture run never takes a real machine slot.
        return {
            **os.environ,
            "TMPDIR": str(self.tmpdir),
            run_tests.SLOTS_DIR_ENV: str(self.slots),
            **extra,
        }

    def run_tests(
        self, *args: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(RUNNER), str(self.top), "-j", "4", *args],
            env=env or self.env(),
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=300,
        )

    def listing(self, env: dict[str, str] | None = None) -> dict[str, Any]:
        out = self.root / "listing.json"
        proc = subprocess.run(
            [sys.executable, str(ISOLATED), "--list", str(out), str(self.top)],
            env=env or self.env(),
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(out.read_text())["modules"]


class ListModeTest(FixtureSuite):
    def test_lists_ids_and_whole_module_reasons_without_running(self) -> None:
        self.write("test_plain.py", plain_module(3, "raise SystemExit('ran')"))
        self.write("test_load.py", LOAD_TESTS_IGNORES_K)
        self.write("test_class.py", CLASS_FIXTURE)
        self.write("test_module.py", MODULE_FIXTURE)
        self.write("test_broken.py", "import no_such_module_here\n")
        modules = self.listing()
        self.assertEqual(
            modules["test_plain.py"]["ids"],
            [f"test_plain.Plain.test_{n:03}" for n in range(3)],
        )
        self.assertTrue(modules["test_load.py"]["load_tests"])
        self.assertEqual(len(modules["test_load.py"]["ids"]), 25)
        self.assertEqual(modules["test_class.py"]["fixtures"], ["Plain.setUpClass"])
        self.assertEqual(
            modules["test_module.py"]["fixtures"], ["test_module.setUpModule"]
        )
        self.assertEqual(len(modules["test_broken.py"]["load_errors"]), 1)
        for name in ("test_plain.py", "test_class.py", "test_module.py"):
            self.assertFalse(modules[name]["load_tests"])
            self.assertEqual(modules[name]["load_errors"], [])


class RunTest(FixtureSuite):
    def test_every_test_runs_once_and_passes(self) -> None:
        self.write("test_big.py", plain_module(40))
        self.write("test_load.py", LOAD_TESTS_IGNORES_K)
        self.write("test_class.py", CLASS_FIXTURE)
        self.write("test_small.py", plain_module(2))
        out = self.run_tests()
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("Ran 92 of 92 tests in 6 parts, -j 4", out.stdout)
        self.assertIn("Slowest 5 parts:", out.stdout)
        self.assertEqual(list(self.tmpdir.iterdir()), [])  # no temp file left

    def test_split_of_a_module_that_ignores_k_is_caught(self) -> None:
        # Regression: splitting a load_tests module would run its tests twice.
        self.write("test_load.py", LOAD_TESTS_IGNORES_K)

        def split_everything(listing: dict[str, Any]) -> list[run_tests.Part]:
            ids = listing["modules"]["test_load.py"]["ids"]
            return [
                run_tests.Part("test_load.py", ids[:13], True, 1, 2),
                run_tests.Part("test_load.py", ids[13:], True, 2, 2),
            ]

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = run_tests.run(str(self.top), 2, planner=split_everything)
        text = stdout.getvalue()
        self.assertEqual(code, 1, text)
        self.assertIn("25 tests ran more than once", text)
        self.assertIn("not in part", text)
        self.assertIn("Ran 50 of 25 tests", text)

    def test_failures_show_their_output_and_fail_the_run(self) -> None:
        self.write(
            "test_red.py",
            plain_module(1)
            + "\n    def test_red(self):\n"
            + "        print('visible output of the red test')\n"
            + "        self.fail('red on purpose')\n",
        )
        self.write("test_green.py", plain_module(2))
        out = self.run_tests()
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("FAILED part test_red.py", out.stdout)
        self.assertIn("failure: test_red.Plain.test_red", out.stdout)
        self.assertIn("red on purpose", out.stdout)
        self.assertIn("visible output of the red test", out.stdout)
        self.assertIn("FAILED (1 parts failed)", out.stdout)

    def test_a_module_that_fails_to_load_fails_the_run(self) -> None:
        self.write("test_broken.py", "import no_such_module_here\n")
        self.write("test_green.py", plain_module(2))
        out = self.run_tests()
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("FAILED part test_broken.py", out.stdout)
        self.assertIn("no_such_module_here", out.stdout)

    def test_a_part_that_dies_without_report_fails_the_run(self) -> None:
        self.write("test_dies.py", plain_module(1, "import os; os._exit(3)"))
        out = self.run_tests()
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("no report (exit 3)", out.stdout)
        self.assertIn("1 listed tests did not run", out.stdout)

    def test_a_part_that_hangs_is_stopped(self) -> None:
        self.write("test_hangs.py", plain_module(1, "import time; time.sleep(60)"))
        out = self.run_tests("--part-timeout", "3")
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("part timed out after 3 s", out.stdout)

    def diagnostics_of(self, stdout: str, label: str) -> dict[str, Any]:
        prefix = f"diagnostics {label}: "
        (line,) = [x for x in stdout.splitlines() if x.startswith(prefix)]
        return json.loads(line[len(prefix) :])

    def test_a_timeout_prints_its_diagnostics_after_the_failure(self) -> None:
        self.write("test_hangs.py", plain_module(1, "import time; time.sleep(60)"))
        out = self.run_tests(
            "--part-timeout", "2", env=self.env(RUN_TESTS_CANARY="canary-secret-value")
        )
        self.assertEqual(out.returncode, 1, out.stdout)
        found = self.diagnostics_of(out.stdout, "test_hangs.py")
        self.assertTrue(found["timed_out"])
        self.assertEqual(found["deadline"], 2.0)
        self.assertEqual(found["jobs"], 4)
        self.assertEqual(found["tests"], 1)
        self.assertGreaterEqual(found["ran"], 2.0)
        # This fixture run is nested in a test: no slots, and the line says why.
        self.assertIsNone(found["slots"])
        self.assertEqual(found["slots_off"], "nested")
        for key in ("load", "cpus", "nice", "background", "slot_wait", "slot_dir"):
            self.assertIn(key, found)
        # The failure itself stays first and whole.
        failed = out.stdout.index("FAILED part test_hangs.py")
        self.assertLess(failed, out.stdout.index("part timed out after 2 s"))
        self.assertLess(
            out.stdout.index("part timed out after 2 s"),
            out.stdout.index("diagnostics test_hangs.py"),
        )
        line = out.stdout[out.stdout.index("diagnostics test_hangs.py") :].splitlines()[
            0
        ]
        self.assertNotIn("canary-secret-value", line)
        self.assertNotIn("isolated_env", line)
        self.assertNotIn("-k", line)

    def test_a_red_test_gets_diagnostics_without_a_timeout(self) -> None:
        self.write("test_red.py", plain_module(1, "self.fail('red')"))
        out = self.run_tests()
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertFalse(self.diagnostics_of(out.stdout, "test_red.py")["timed_out"])

    def test_a_green_run_prints_no_diagnostics(self) -> None:
        self.write("test_green.py", plain_module(2))
        out = self.run_tests()
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertNotIn("diagnostics ", out.stdout)
        self.assertIn("Slot wait: 0.0 s in total", out.stdout)

    def start_leaky_child(self) -> Path:
        """A test that starts a child which inherits stdout, then hangs."""
        pid_file = self.root / "child.pid"
        self.write(
            "test_leaky.py",
            plain_module(
                1,
                "import subprocess, sys, time, os; "
                "c = subprocess.Popen([sys.executable, '-c', "
                "'import time; time.sleep(60)']); "
                "open(os.environ['RUN_TESTS_PID'], 'w').write(str(c.pid)); "
                "time.sleep(60)",
            ),
        )
        return pid_file

    def assert_gone(self, pid_file: Path) -> None:
        pid = int(pid_file.read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        os.kill(pid, signal.SIGKILL)
        self.fail(f"child {pid} still runs")

    def test_a_timeout_ends_children_that_hold_the_output(self) -> None:
        pid_file = self.start_leaky_child()
        start = time.monotonic()
        out = self.run_tests(
            "--part-timeout", "1", env=self.env(RUN_TESTS_PID=str(pid_file))
        )
        self.assertLess(time.monotonic() - start, 20)
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("part timed out after 1 s", out.stdout)
        self.assert_gone(pid_file)

    def test_sigterm_ends_children_that_hold_the_output(self) -> None:
        pid_file = self.start_leaky_child()
        proc = subprocess.Popen(
            [sys.executable, str(RUNNER), str(self.top), "-j", "2"],
            env=self.env(RUN_TESTS_PID=str(pid_file)),
            cwd=self.root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.addCleanup(proc.kill)
        deadline = time.monotonic() + 20
        while not pid_file.exists() or not pid_file.read_text():
            self.assertLess(time.monotonic(), deadline, "the child did not start")
            time.sleep(0.1)
        start = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        output, _ = proc.communicate(timeout=20)
        self.assertLess(time.monotonic() - start, 10)
        self.assertEqual(proc.returncode, 128 + signal.SIGTERM, output)
        self.assert_gone(pid_file)

    def test_sigterm_right_after_a_spawn_does_not_deadlock(self) -> None:
        # The signal arrives after Popen returns and before the pool records
        # the process, while the pool lock is held (the listing, main thread).
        self.write("test_slow_import.py", "import time\ntime.sleep(60)\n")
        pid_file = self.root / "listing.pid"
        driver = self.root / "driver.py"
        driver.write_text(
            textwrap.dedent(
                f"""
                import os, signal, subprocess, sys
                sys.path.insert(0, {str(HERE)!r})
                import run_tests

                class Hooked(subprocess.Popen):
                    def __init__(self, *args, **kwargs):
                        super().__init__(*args, **kwargs)
                        with open({str(pid_file)!r}, "w") as f:
                            f.write(str(self.pid))
                        os.kill(os.getpid(), signal.SIGTERM)

                subprocess.Popen = Hooked
                sys.exit(run_tests.run({str(self.top)!r}, 1, 30))
                """
            )
        )
        proc = subprocess.Popen(
            [sys.executable, str(driver)],
            env=self.env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        self.addCleanup(signal_group_kill, proc)
        try:
            output, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            self.fail("the runner hung after SIGTERM")
        self.assertEqual(proc.returncode, 128 + signal.SIGTERM, output)
        self.assert_gone(pid_file)

    def test_sigterm_while_the_handler_is_restored_still_gives_143(self) -> None:
        # The signal arrives after a passing run, during the handler restore.
        self.write("test_passes.py", plain_module(1, "pass"))
        driver = self.root / "driver.py"
        driver.write_text(
            textwrap.dedent(
                f"""
                import os, signal, sys
                sys.path.insert(0, {str(HERE)!r})
                import run_tests

                real = signal.signal
                calls = []

                def hooked(signum, handler):
                    calls.append(signum)
                    if len(calls) == 2:
                        os.kill(os.getpid(), signal.SIGTERM)
                    return real(signum, handler)

                signal.signal = hooked
                sys.exit(run_tests.run({str(self.top)!r}, 1, 30))
                """
            )
        )
        proc = subprocess.Popen(
            [sys.executable, str(driver)],
            env=self.env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        self.addCleanup(signal_group_kill, proc)
        output, _ = proc.communicate(timeout=30)
        self.assertIn("Ran 1 of 1 tests", output)
        self.assertEqual(proc.returncode, 128 + signal.SIGTERM, output)

    def test_a_listing_that_fails_fails_the_run(self) -> None:
        sub = self.top / "pkg"
        sub.mkdir()
        (sub / "__init__.py").write_text("")
        (sub / "test_nested.py").write_text(plain_module(1))
        out = self.run_tests()
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("listing", out.stdout)
        self.assertIn("not supported", out.stdout)


# Records what a module sees at import and what each test sees, and tries to
# write live state the way a careless test would.
PROBE = textwrap.dedent(
    """
    import json, os, sys, unittest
    from pathlib import Path

    PROBE = Path(os.environ["RUN_TESTS_PROBE"])

    def record(kind):
        seen = {
            "argv": sys.argv,
            "home": str(Path.home()),
            "epic": sorted(k for k in os.environ if k.startswith("EPIC_")),
        }
        state = Path(os.environ.get("EPIC_STATE_DIR", "~/.local/state/epic-loop"))
        state = state.expanduser()
        state.mkdir(parents=True, exist_ok=True)
        (state / f"touched-{kind}").write_text("x")
        (PROBE / f"{kind}-{os.getpid()}-{len(list(PROBE.iterdir()))}.json").write_text(
            json.dumps(seen)
        )

    record("import")

    class Probe(unittest.TestCase):
        pass

    for n in range(21):
        setattr(Probe, f"test_{n:02}", lambda self: record("test"))
    """
)


class SlotsTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="run-tests-slots-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.folder = self.base / "slots"
        self.outside = self.base / "outside"
        self.outside.mkdir()

    def slots(self, count: int = 1, **extra: str) -> run_tests.Slots:
        if not self.folder.exists():
            slot_folder(self.folder, count)
        return run_tests.Slots.from_env(
            {run_tests.SLOTS_DIR_ENV: str(self.folder), **extra}
        )

    def refused(self, env: dict[str, str], why: str) -> run_tests.SlotError:
        with self.assertRaises(run_tests.SlotError) as caught:
            run_tests.Slots.from_env(env)
        self.assertIn(why, str(caught.exception))
        return caught.exception

    def fresh(self, name: str, count: int = 2) -> Path:
        return slot_folder(self.base / name, count)

    def entries(self, folder: Path) -> list[str]:
        return sorted(p.name for p in folder.iterdir())

    def test_a_held_slot_makes_the_next_part_wait(self) -> None:
        first, second = self.slots(1), self.slots(1)  # two runs, one folder
        order: list[str] = []
        with first.slot(lambda: False) as got:
            self.assertTrue(got)
            waiter = threading.Thread(
                target=lambda: second.slot(lambda: False).__enter__()
                and order.append("second")
            )
            waiter.start()
            time.sleep(0.5)
            order.append("first ends")
        waiter.join(10)
        self.assertEqual(order, ["first ends", "second"])
        self.assertIsNone(second.error)  # waiting for a held slot is no error

    def test_count_slots_run_together(self) -> None:
        slots = self.slots(2)
        with slots.slot(lambda: False) as a, slots.slot(lambda: False) as b:
            self.assertTrue(a and b)

    def test_a_stopped_run_stops_waiting(self) -> None:
        slots = self.slots(1)
        stop = threading.Event()
        with slots.slot(lambda: False):
            threading.Timer(0.3, stop.set).start()
            with slots.slot(stop.is_set) as got:
                self.assertFalse(got)

    def test_a_run_inside_a_test_takes_no_slot(self) -> None:
        self.assertEqual(run_tests.NESTED_ENV, isolated_env.MARKER)
        for extra in ({}, {run_tests.SLOTS_ENV: "0"}, {run_tests.SLOTS_ENV: "x"}):
            with self.subTest(extra=extra):
                env = {
                    run_tests.NESTED_ENV: "1",
                    run_tests.SLOTS_DIR_ENV: str(self.folder),
                    **extra,
                }
                slots = run_tests.Slots.from_env(env)
                self.assertIsNone(slots.folder)
                self.assertEqual(slots.off, "nested")
                with slots.slot(lambda: False) as got:
                    self.assertTrue(got)
                self.assertFalse(self.folder.exists())

    def test_the_capacity_sets_the_count(self) -> None:
        folder = self.fresh("two", 2)
        env = {run_tests.SLOTS_DIR_ENV: str(folder)}
        with patch.object(run_tests.os, "cpu_count", return_value=7):
            slots = run_tests.Slots.from_env(env)
            same = run_tests.Slots.from_env({**env, run_tests.SLOTS_ENV: "2"})
        self.assertEqual((slots.folder, slots.count, slots.shared), (folder, 2, True))
        self.assertEqual(same.count, 2)
        self.assertEqual(
            self.entries(folder), ["capacity.json", "slot-0.lock", "slot-1.lock"]
        )

    def test_a_differing_slot_count_setting_is_refused(self) -> None:
        folder = self.fresh("two", 2)
        for given in ("3", "1", "", "02", " 2", "2.0", "+2"):
            with self.subTest(given=given):
                env = {run_tests.SLOTS_DIR_ENV: str(folder), run_tests.SLOTS_ENV: given}
                self.refused(env, "differs from the capacity 2")
        self.assertEqual(self.entries(folder), ["capacity.json"])

    def test_capacity_must_be_exactly_schema_one_with_a_positive_count(self) -> None:
        cases = {
            "missing": None,
            "empty": "",
            "not json": "slots: 2",
            "list": "[1, 2]",
            "no slots": '{"schema": 1}',
            "extra key": '{"schema": 1, "slots": 2, "owner": "x"}',
            "schema 2": '{"schema": 2, "slots": 2}',
            "schema text": '{"schema": "1", "slots": 2}',
            "schema bool": '{"schema": true, "slots": 2}',
            "slots true": '{"schema": 1, "slots": true}',
            "slots false": '{"schema": 1, "slots": false}',
            "slots zero": '{"schema": 1, "slots": 0}',
            "slots negative": '{"schema": 1, "slots": -1}',
            "slots float": '{"schema": 1, "slots": 2.0}',
            "slots text": '{"schema": 1, "slots": "2"}',
            "slots null": '{"schema": 1, "slots": null}',
            "too large": '{"schema": 1, "slots": 2}' + " " * 5000,
        }
        for n, (name, content) in enumerate(cases.items()):
            with self.subTest(name):
                folder = self.fresh(f"bad-{n}")
                capacity = folder / "capacity.json"
                if content is None:
                    capacity.unlink()
                else:
                    capacity.write_text(content)
                why = "cannot open" if content is None else "must be exactly"
                self.refused({run_tests.SLOTS_DIR_ENV: str(folder)}, why)
                self.assertEqual(
                    self.entries(folder), [] if content is None else ["capacity.json"]
                )

    def test_the_shared_folder_must_be_canonical_private_and_owned(self) -> None:
        good = self.fresh("good")
        link = self.base / "link"
        link.symlink_to(good)
        afile = self.base / "afile"
        afile.write_text("")
        open_mode = self.fresh("open-mode")
        open_mode.chmod(0o755)
        group = self.fresh("group")
        group.chmod(0o770)
        cases = [
            ("slots", "must be an absolute path"),
            ("", "must be an absolute path"),
            (str(self.base / "missing"), "cannot be used"),
            (str(good) + "/", "must be canonical"),
            (f"{self.base}/./good", "must be canonical"),
            (f"{self.base}/outside/../good", "must be canonical"),
            (str(link), "must be canonical"),
            (str(afile), "must be a directory with mode 0700"),
            (str(open_mode), "must be a directory with mode 0700"),
            (str(group), "must be a directory with mode 0700"),
        ]
        for value, why in cases:
            with self.subTest(value=value):
                self.refused({run_tests.SLOTS_DIR_ENV: value}, why)
        with patch.object(run_tests.os, "getuid", return_value=os.getuid() + 1):
            self.refused({run_tests.SLOTS_DIR_ENV: str(good)}, "not owned by this user")
        self.assertEqual(self.entries(good), ["capacity.json"])

    def test_a_writable_parent_is_refused_unless_sticky(self) -> None:
        parent = self.base / "parent"
        parent.mkdir()
        folder = slot_folder(parent / "slots")
        self.addCleanup(parent.chmod, 0o700)
        parent.chmod(0o777)
        self.refused({run_tests.SLOTS_DIR_ENV: str(folder)}, "writable parent")
        parent.chmod(0o1777)  # like /tmp: others cannot replace the folder
        self.assertEqual(
            run_tests.Slots.from_env({run_tests.SLOTS_DIR_ENV: str(folder)}).count, 1
        )

    def test_the_capacity_file_must_be_private(self) -> None:
        folder = self.fresh("open-capacity")
        (folder / "capacity.json").chmod(0o664)
        self.refused({run_tests.SLOTS_DIR_ENV: str(folder)}, "group or world writable")

    def test_aliases_are_refused_and_left_alone(self) -> None:
        target = self.outside / "target.lock"
        target.write_text("other\n")
        valid = self.outside / "capacity.json"
        valid.write_text('{"schema": 1, "slots": 2}')
        valid.chmod(0o600)
        cases = {
            "lock symlink": lambda f: (f / "slot-0.lock").symlink_to(target),
            "dangling lock symlink": lambda f: (f / "slot-0.lock").symlink_to(
                self.outside / "never-created.lock"
            ),
            "lock hardlink": lambda f: os.link(target, f / "slot-0.lock"),
            "capacity symlink": lambda f: (
                (f / "capacity.json").unlink(),
                (f / "capacity.json").symlink_to(valid),
            ),
            "capacity hardlink": lambda f: (
                (f / "capacity.json").unlink(),
                os.link(valid, f / "capacity.json"),
            ),
        }
        for n, (name, alias) in enumerate(cases.items()):
            with self.subTest(name):
                folder = self.fresh(f"alias-{n}")
                alias(folder)
                before = self.entries(folder)
                with self.assertRaises(run_tests.SlotError):
                    run_tests.Slots.from_env({run_tests.SLOTS_DIR_ENV: str(folder)})
                self.assertEqual(self.entries(folder), before)
        self.assertEqual(target.read_text(), "other\n")
        self.assertFalse((self.outside / "never-created.lock").exists())

    def test_only_slot_lock_files_may_sit_beside_the_capacity(self) -> None:
        cases = {
            "slot-2.lock": lambda p: p.write_text(""),  # capacity 2: slots 0 and 1
            "notes.txt": lambda p: p.write_text(""),
            "sub": lambda p: p.mkdir(),
            "slot-01.lock": lambda p: p.write_text(""),
        }
        for n, (name, make) in enumerate(cases.items()):
            with self.subTest(name):
                folder = self.fresh(f"extra-{n}")
                make(folder / name)
                self.refused(
                    {run_tests.SLOTS_DIR_ENV: str(folder)},
                    f"unexpected entry in {folder}: {name}",
                )
        folder = self.fresh("lock-dir")
        (folder / "slot-0.lock").mkdir()
        self.refused({run_tests.SLOTS_DIR_ENV: str(folder)}, "cannot open")

    def test_existing_lock_files_are_kept_and_new_ones_are_private(self) -> None:
        folder = self.fresh("keep")
        kept = folder / "slot-0.lock"
        kept.write_text("keep\n")
        kept.chmod(0o600)
        inode = kept.stat().st_ino
        old_umask = os.umask(0)
        try:
            slots = run_tests.Slots.from_env({run_tests.SLOTS_DIR_ENV: str(folder)})
        finally:
            os.umask(old_umask)
        with slots.slot(lambda: False) as a, slots.slot(lambda: False) as b:
            self.assertTrue(a and b)
        self.assertEqual(
            self.entries(folder), ["capacity.json", "slot-0.lock", "slot-1.lock"]
        )
        self.assertEqual((kept.stat().st_ino, kept.read_text()), (inode, "keep\n"))
        self.assertEqual(stat.S_IMODE((folder / "slot-1.lock").stat().st_mode), 0o600)

    def test_a_writable_lock_file_in_the_shared_folder_is_refused(self) -> None:
        folder = self.fresh("open-lock")
        (folder / "slot-0.lock").write_text("")
        (folder / "slot-0.lock").chmod(0o660)
        self.refused({run_tests.SLOTS_DIR_ENV: str(folder)}, "group or world writable")

    def test_denied_lock_creation_fails(self) -> None:
        real_open = os.open

        def no_create(path: Any, flags: int, *args: Any) -> int:
            if flags & os.O_CREAT:
                raise PermissionError(errno.EACCES, "denied", str(path))
            return real_open(path, flags, *args)

        folder = self.fresh("no-create")
        with patch.object(run_tests.os, "open", side_effect=no_create):
            self.refused({run_tests.SLOTS_DIR_ENV: str(folder)}, "cannot open")
        self.assertEqual(self.entries(folder), ["capacity.json"])

    @unittest.skipIf(os.geteuid() == 0, "root ignores file permissions")
    def test_a_lock_file_that_cannot_be_opened_fails(self) -> None:
        folder = self.fresh("no-open")
        lock = folder / "slot-0.lock"
        lock.write_text("")
        lock.chmod(0o000)
        self.addCleanup(lock.chmod, 0o600)
        self.refused({run_tests.SLOTS_DIR_ENV: str(folder)}, "Permission denied")

    def test_a_lock_error_fails_the_part_and_stops_the_waiting_ones(self) -> None:
        slots = self.slots(2)
        out = io.StringIO()
        ran: list[str] = []
        denied = OSError(errno.ENOLCK, "no locks available")
        with contextlib.redirect_stdout(out):
            with patch.object(run_tests.fcntl, "flock", side_effect=denied):
                with self.assertRaises(run_tests.SlotError):
                    with slots.slot(lambda: False):
                        ran.append("first")
            # Later parts never start, also without the error in place.
            with slots.slot(lambda: False) as got:
                self.assertFalse(got)
        self.assertEqual(ran, [])
        self.assertIn("no locks available", str(slots.error))
        self.assertEqual(out.getvalue().count("test slots failed"), 1)

    def test_a_lock_file_replaced_or_removed_after_startup_fails(self) -> None:
        for change in ("replace", "remove"):
            with self.subTest(change):
                folder = self.fresh(f"{change}d")
                slots = run_tests.Slots.from_env({run_tests.SLOTS_DIR_ENV: str(folder)})
                lock = folder / "slot-0.lock"
                if change == "replace":
                    (folder / "new").write_text("")
                    os.replace(folder / "new", lock)
                else:
                    lock.unlink()
                ran: list[str] = []
                with contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(run_tests.SlotError):
                        with slots.slot(lambda: False):
                            ran.append("part")
                self.assertEqual(ran, [])
                why = "was replaced" if change == "replace" else "cannot open"
                self.assertIn(why, str(slots.error))

    def test_an_interrupted_holder_releases_its_slot(self) -> None:
        slot_folder(self.folder, 1)
        env = {**os.environ, run_tests.SLOTS_DIR_ENV: str(self.folder)}
        env.pop(run_tests.NESTED_ENV, None)
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys, time; sys.path.insert(0, sys.argv[1]); import run_tests\n"
                "slots = run_tests.Slots.from_env()\n"
                "with slots.slot(lambda: False):\n"
                "    print('held', flush=True); time.sleep(300)\n",
                str(HERE),
            ],
            env=env,
            stdout=subprocess.PIPE,
            text=True,
        )
        assert holder.stdout is not None
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        killer = threading.Timer(30, holder.kill)  # readline must not hang
        killer.start()
        self.assertEqual(holder.stdout.readline(), "held\n")
        killer.cancel()
        slots = run_tests.Slots.from_env({run_tests.SLOTS_DIR_ENV: str(self.folder)})
        self.assertIsNone(slots.take())  # the holder has the only slot
        holder.kill()
        holder.wait(10)
        held = slots.take()
        self.assertIsNotNone(held)
        assert held is not None
        os.close(held)

    def test_the_local_default_keeps_its_folder_and_count(self) -> None:
        temp = self.base / "user-temp"
        temp.mkdir()
        with (
            patch.object(run_tests, "user_temp_dir", return_value=str(temp)),
            patch.object(run_tests.os, "cpu_count", return_value=3),
        ):
            slots = run_tests.Slots.from_env({})
            two = run_tests.Slots.from_env({run_tests.SLOTS_ENV: "2"})
        folder = temp / "epic-test-slots"
        self.assertEqual((slots.folder, slots.count, slots.shared), (folder, 3, False))
        self.assertEqual(two.count, 2)
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        self.assertEqual(self.entries(folder), [f"slot-{i}.lock" for i in range(3)])

    def test_a_malformed_local_slot_count_is_refused(self) -> None:
        temp = self.base / "user-temp"
        temp.mkdir()
        with patch.object(run_tests, "user_temp_dir", return_value=str(temp)):
            for given in ("0", "-1", "", "abc", "1.5", "true", "02"):
                with self.subTest(given=given):
                    self.refused(
                        {run_tests.SLOTS_ENV: given}, "must be a positive integer"
                    )
        self.assertFalse((temp / "epic-test-slots").exists())

    def test_a_local_folder_that_cannot_be_made_fails(self) -> None:
        blocker = self.base / "a-file"
        blocker.write_text("not a folder")
        with patch.object(run_tests, "user_temp_dir", return_value=str(blocker)):
            self.refused({}, "cannot create")

    def test_a_local_symlink_folder_is_refused(self) -> None:
        temp = self.base / "user-temp"
        temp.mkdir()
        (temp / "epic-test-slots").symlink_to(self.outside)
        with patch.object(run_tests, "user_temp_dir", return_value=str(temp)):
            self.refused({}, "must be a directory")
        self.assertEqual(self.entries(self.outside), [])

    @unittest.skipIf(os.geteuid() == 0, "root ignores folder permissions")
    def test_a_local_folder_that_denies_new_files_fails(self) -> None:
        # Regression: mkdir passes on an existing folder that denies new files.
        temp = self.base / "user-temp"
        folder = temp / "epic-test-slots"
        folder.mkdir(parents=True, mode=0o500)
        self.addCleanup(folder.chmod, 0o700)
        with patch.object(run_tests, "user_temp_dir", return_value=str(temp)):
            self.refused({run_tests.SLOTS_ENV: "1"}, "cannot open")

    def test_the_user_temp_dir_prefers_the_os_answer_over_tmpdir(self) -> None:
        with patch.dict(os.environ, {"TMPDIR": "/elsewhere"}):
            with patch.object(run_tests.os, "confstr", return_value="/per-user/T/"):
                self.assertEqual(run_tests.user_temp_dir(), "/per-user/T/")
            # The Codex sandbox refuses the lookup (EIO) but sets TMPDIR.
            with patch.object(run_tests.os, "confstr", side_effect=OSError(5, "EIO")):
                self.assertEqual(run_tests.user_temp_dir(), "/elsewhere")


class SlotRunTest(FixtureSuite):
    # Each test appends "start <t>" / "end <t>" to a shared file.
    RECORD = (
        "import os, time; f = os.environ['RUN_TESTS_LOG']; "
        "open(f, 'a').write(f'start {time.monotonic()}\\n'); time.sleep(0.3); "
        "open(f, 'a').write(f'end {time.monotonic()}\\n')"
    )

    def slot_env(self, **extra: str) -> dict[str, str]:
        env = self.env(**{run_tests.SLOTS_ENV: "1", **extra})
        env.pop(run_tests.NESTED_ENV, None)  # this test runs under isolated_env
        return env

    def marker_modules(self, count: int) -> list[Path]:
        """Modules whose import and test each leave a file: proof that they ran."""
        marks = []
        for n in range(count):
            mark = self.root / f"ran-{n}"
            marks.append(mark)
            self.write(
                f"test_mark{n}.py",
                f"open({str(mark)!r}, 'a').write('import\\n')\n"
                + plain_module(1, f"open({str(mark)!r}, 'a').write('test\\n')"),
            )
        return marks

    def test_two_runs_with_different_tmpdirs_share_the_slots(self) -> None:
        log = self.root / "log"
        for n in range(3):
            self.write(f"test_m{n}.py", plain_module(1, self.RECORD))
        command = [sys.executable, str(RUNNER), str(self.top), "-j", "4"]
        runs = []
        for n in range(2):
            tmpdir = self.root / f"tmpdir-{n}"  # like the host and the sandbox
            tmpdir.mkdir()
            env = self.slot_env(RUN_TESTS_LOG=str(log), TMPDIR=str(tmpdir))
            runs.append(
                subprocess.Popen(command, env=env, cwd=self.root, text=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            )  # fmt: skip
        outputs = [run.communicate(timeout=120)[0] for run in runs]
        for run, output in zip(runs, outputs):
            self.assertEqual(run.returncode, 0, output)
            self.assertIn(f"at most 1 parts machine-wide ({self.slots})", output)
        events = sorted(
            (float(t), kind) for kind, t in (line.split() for line in log.read_text().splitlines())
        )  # fmt: skip
        self.assertEqual(len(events), 12)
        running = peak = 0
        for _, kind in events:
            running += 1 if kind == "start" else -1
            peak = max(peak, running)
        self.assertEqual(peak, 1)

    def test_tests_in_a_part_see_the_nested_marker(self) -> None:
        seen = self.root / "seen"
        self.write(
            "test_flag.py",
            plain_module(
                1,
                f"import os; open({str(seen)!r}, 'w').write(os.environ.get("
                f"{run_tests.NESTED_ENV!r}, 'unset'))",
            ),
        )
        out = self.run_tests(env=self.slot_env())
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertEqual(seen.read_text(), "1")

    def test_the_timeout_starts_after_the_slot(self) -> None:
        self.write("test_fast.py", plain_module(1, "pass"))
        held = open(self.slots / "slot-0.lock", "a")
        self.addCleanup(held.close)
        fcntl.flock(held, fcntl.LOCK_EX)
        threading.Timer(3, held.close).start()  # frees the slot after 3 s
        out = self.run_tests("--part-timeout", "2", env=self.slot_env())
        self.assertEqual(out.returncode, 0, out.stdout)
        # The wait is measured apart from the run time.
        self.assertIn("(slot wait ", out.stdout)
        total = out.stdout.split("Slot wait: ", 1)[1].split(" s in total", 1)[0]
        self.assertGreaterEqual(float(total), 2.0)

    def test_unusable_slots_fail_the_run_before_any_test(self) -> None:
        marks = self.marker_modules(2)
        capacity = self.slots / "capacity.json"
        cases = [
            ("env mismatch", lambda: None, {run_tests.SLOTS_ENV: "2"}),
            (
                "malformed capacity",
                lambda: capacity.write_text('{"schema": 1, "slots": 0}'),
                {},
            ),
            ("folder mode", lambda: self.slots.chmod(0o500), {}),
        ]
        self.addCleanup(self.slots.chmod, 0o700)
        for name, damage, extra in cases:
            with self.subTest(name):
                damage()
                out = self.run_tests(env=self.slot_env(**extra))
                self.assertEqual(out.returncode, 1, out.stdout)
                self.assertIn("test slots unusable, no test ran", out.stdout)
                self.assertNotIn("Ran ", out.stdout)
                self.assertFalse(any(m.exists() for m in marks))

    def test_a_slot_failure_while_a_part_waits_never_starts_it(self) -> None:
        marks = self.marker_modules(2)
        lock = self.slots / "slot-0.lock"
        lock.write_text("")
        lock.chmod(0o600)
        held = open(lock, "a")
        self.addCleanup(held.close)
        fcntl.flock(held, fcntl.LOCK_EX)  # every part waits
        proc = subprocess.Popen(
            [sys.executable, str(RUNNER), str(self.top), "-j", "2"],
            env=self.slot_env(),
            cwd=self.root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        assert proc.stdout is not None
        killer = threading.Timer(30, proc.kill)  # readline must not hang
        killer.start()
        first = proc.stdout.readline()
        killer.cancel()
        self.assertIn("at most 1 parts machine-wide", first)
        # The listing imported each module once; wait until parts wait.
        deadline = time.monotonic() + 30
        while not all(m.exists() for m in marks) and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.5)
        (self.slots / "new").write_text("")
        os.replace(self.slots / "new", lock)  # the held inode is gone
        output, _ = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 1, output)
        self.assertIn("test slots failed", output)
        self.assertIn("was replaced during the run", output)
        self.assertIn("RUN PROBLEM: test slots failed", output)
        for mark in marks:
            self.assertEqual(mark.read_text(), "import\n")  # listed, never run

    def test_check_slots_reports_the_folder_and_count(self) -> None:
        def check(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [sys.executable, str(RUNNER), "--check-slots"],
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )

        out = check(self.slot_env())
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(
            out.stdout, f"run_tests: 1 slots (capacity.json) in {self.slots}\n"
        )
        out = check(self.slot_env(**{run_tests.SLOTS_ENV: "4"}))
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("test slots unusable", out.stdout)
        out = check({**self.slot_env(), run_tests.NESTED_ENV: "1"})
        self.assertEqual(out.stdout, "run_tests: no slots (a run inside a test)\n")

    def test_ctrl_c_stops_a_part_that_waits_for_a_slot(self) -> None:
        # Regression: the executor joined the waiting part before the run
        # stopped, so Ctrl-C hung while another run held every slot.
        self.write("test_fast.py", plain_module(1, "pass"))
        held = open(self.slots / "slot-0.lock", "a")
        self.addCleanup(held.close)
        fcntl.flock(held, fcntl.LOCK_EX)
        proc = subprocess.Popen(
            [sys.executable, str(RUNNER), str(self.top), "-j", "2"],
            env=self.slot_env(),
            cwd=self.root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            # A parent that ignores SIGINT would pass that on; Ctrl-C needs it.
            preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL),
        )
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        assert proc.stdout is not None
        killer = threading.Timer(30, proc.kill)  # readline must not hang
        killer.start()
        first = proc.stdout.readline()
        killer.cancel()
        self.assertIn("at most 1 parts machine-wide", first)
        time.sleep(0.5)  # the part now waits for slot 0
        start = time.monotonic()
        proc.send_signal(signal.SIGINT)
        try:
            output, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            self.fail("the runner hung after SIGINT while a part waited")
        self.assertLess(time.monotonic() - start, 10)
        self.assertNotEqual(proc.returncode, 0, output)
        self.assertIn("KeyboardInterrupt", output)


class DiagnosticsTest(unittest.TestCase):
    def outcome(self) -> run_tests.Outcome:
        part = run_tests.Part("test_m.py", ["test_m.C.test_a"], split=False)
        return run_tests.Outcome(part, 1.25, None, "", None, waited=0.5)

    def test_unreadable_metrics_are_null(self) -> None:
        with (
            patch.object(run_tests.os, "getloadavg", side_effect=OSError("no")),
            patch.object(run_tests.os, "getpriority", side_effect=OSError("no")),
        ):
            found = run_tests.scheduling()
        self.assertIsNone(found["load"])
        self.assertIsNone(found["nice"])
        self.assertIsNone(found["background"])

    def test_slots_in_use_name_their_count_and_folder(self) -> None:
        slots = run_tests.Slots(Path("/tmp/epic-test-slots"), 3)
        found = run_tests.diagnostics(self.outcome(), 9.0, 2, slots)
        self.assertEqual(found["slots"], 3)
        self.assertIsNone(found["slots_off"])
        self.assertEqual(found["slot_dir"], "/tmp/epic-test-slots")
        self.assertEqual((found["ran"], found["slot_wait"]), (1.2, 0.5))

    def test_a_failed_lookup_prints_one_unavailable_line(self) -> None:
        stdout = io.StringIO()
        with (
            patch.object(run_tests, "diagnostics", side_effect=RuntimeError("x")),
            contextlib.redirect_stdout(stdout),
        ):
            run_tests.print_diagnostics(self.outcome(), 9.0, 2, None)
        self.assertEqual(
            stdout.getvalue(), "diagnostics test_m.py: unavailable (RuntimeError)\n"
        )


class IsolationTest(FixtureSuite):
    """Live runner state stays untouched in the list mode and in each part,
    also when the parent inherited isolated_env.MARKER."""

    def contaminated(self, marker: bool) -> tuple[dict[str, str], Path, Path]:
        home = self.root / f"live-home-{marker}"
        live = home / ".local" / "state" / "epic-loop"
        live.mkdir(parents=True)
        (live / "sentinel.txt").write_text("live runner state\n")
        probe = self.root / f"probe-{marker}"
        probe.mkdir()
        env = self.env(
            HOME=str(home),
            EPIC_STATE_DIR=str(live),
            EPIC_QUOTA_DIR=str(live),
            RUN_TESTS_PROBE=str(probe),
            **{run_tests.SLOTS_ENV: "1"},  # the slot settings stop at the runner
        )
        env.pop(isolated_env.MARKER, None)
        if marker:
            env[isolated_env.MARKER] = "1"
        return env, home, probe

    def test_list_mode_and_parts_never_touch_live_state(self) -> None:
        self.write("test_probe.py", PROBE)
        for marker in (False, True):
            with self.subTest(inherited_marker=marker):
                env, home, probe = self.contaminated(marker)
                before = sorted(p.relative_to(home) for p in home.rglob("*"))
                out = self.run_tests(env=env)
                self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
                self.assertIn("Ran 21 of 21 tests in 2 parts", out.stdout)
                after = sorted(p.relative_to(home) for p in home.rglob("*"))
                self.assertEqual(after, before)
                seen = [json.loads(p.read_text()) for p in probe.iterdir()]
                listed = [s for s in seen if "--list" in s["argv"]]
                parts = [s for s in seen if "--report" in s["argv"]]
                self.assertEqual(len(listed), 1)  # the import in list mode
                self.assertEqual(len(parts), 2 + 21)  # 2 part imports, 21 tests
                homes = [s["home"] for s in seen]
                self.assertEqual(len(set(homes)), len(homes))  # new home each
                for s in seen:
                    self.assertEqual(s["epic"], [])
                    self.assertFalse(Path(s["home"]).is_relative_to(home))


class SuiteTableTest(unittest.TestCase):
    def test_epic_and_codex_suites_use_the_parallel_runner(self) -> None:
        commands = {s["name"]: s["command"] for s in rebase_policy.SUITES}
        self.assertEqual(
            commands["epic"], ["python3", "scripts/epic/run_tests.py", "scripts/epic"]
        )
        self.assertEqual(
            commands["codex"], ["python3", "scripts/epic/run_tests.py", ".codex/tests"]
        )
        rules = (REPO / "docs/implementation/epic-rules.md").read_text()
        for name in ("epic", "codex"):
            self.assertIn(f"`{' '.join(commands[name])}`", rules)

    def test_suite_runs_get_only_the_shared_slot_settings(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="run-tests-suite-")
        self.addCleanup(tmp.cleanup)
        slots = slot_folder(Path(tmp.name).resolve() / "slots", 3)
        settings = {
            "EPIC_STATE_DIR": "/live/state",
            run_tests.SLOTS_DIR_ENV: str(slots),
            run_tests.SLOTS_ENV: "3",
        }
        suite = {
            "name": "probe",
            "cwd": ".",
            "command": [sys.executable, str(RUNNER), "--check-slots"],
        }
        with patch.dict(os.environ, settings):
            env = rebase_policy.suite_env()
            os.environ.pop(run_tests.NESTED_ENV, None)  # a top-level proof run
            result = rebase_policy.run_suite(REPO, suite)
        self.assertEqual(env[run_tests.SLOTS_DIR_ENV], str(slots))
        self.assertEqual(env[run_tests.SLOTS_ENV], "3")
        self.assertNotIn("EPIC_STATE_DIR", env)
        self.assertEqual(result["result"], "passed", result["tail"])
        self.assertIn(f"3 slots (capacity.json) in {slots}", result["tail"])


if __name__ == "__main__":
    unittest.main()
