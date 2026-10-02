"""Run a test directory in parallel parts and print one summary.

    python3 scripts/epic/run_tests.py scripts/epic [-j N]
    python3 scripts/epic/run_tests.py .codex/tests [-j N]

Every part runs as its own process under isolated_env.py, so import-time and
per-test isolation work as in a serial run:

    isolated_env.py --report FILE <dir> -p <module> [-k <pattern> ...]

The test ids come from `isolated_env.py --list` (the same isolation, no test
runs). Split rule (plan()): a module with more than SPLIT_ABOVE tests is split
into parts of at most PART_SIZE tests, one `-k '*<full test id>'` per test. A
module that defines `load_tests` (it may ignore -k), has class or module
fixtures (setUpClass, setUpModule; a split would run them once per part), or
failed to load runs whole.

Every listed test must start exactly once: the runner compares the ids each
part reports with the listed ids, and a missing, repeated or unknown id fails
the run. The exit code is 0 only when every part passed and every id ran once;
a part that fails to start, load or report also fails the run.

Standard library only. Temporary files stay under TMPDIR.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ISOLATED_ENV = Path(__file__).resolve().parent / "isolated_env.py"
SPLIT_ABOVE = 20
PART_SIZE = 15
PART_TIMEOUT = 1200
SLOWEST = 5
OUTPUT_TAIL = 20000


@dataclass
class Part:
    module: str
    ids: list[str]
    split: bool  # run with one -k per id
    index: int = 1
    count: int = 1

    @property
    def label(self) -> str:
        if self.count == 1:
            return self.module
        return f"{self.module}[{self.index}/{self.count}]"

    def args(self) -> list[str]:
        args = ["-p", self.module]
        if self.split:
            for test_id in self.ids:
                # A pattern with `*` is matched as is (fnmatch) against the
                # full test id; without one, unittest wraps it as *...*.
                args += ["-k", f"*{test_id}"]
        return args


@dataclass
class Outcome:
    part: Part
    seconds: float
    exit: int | None
    output: str
    report: dict[str, Any] | None
    problems: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return bool(self.problems)


def whole_reason(entry: dict[str, Any]) -> str | None:
    if entry["load_errors"]:
        return "failed to load"
    if entry["load_tests"]:
        return "defines load_tests"
    if entry["fixtures"]:
        return "has fixtures: " + ", ".join(entry["fixtures"])
    return None


def plan(listing: dict[str, Any]) -> list[Part]:
    """The parts of one run, largest first."""
    parts: list[Part] = []
    for module, entry in listing["modules"].items():
        ids = list(entry["ids"])
        if not ids and not entry["load_errors"]:
            continue
        if len(ids) <= SPLIT_ABOVE or whole_reason(entry):
            parts.append(Part(module, ids, split=False))
            continue
        count = math.ceil(len(ids) / PART_SIZE)
        size = math.ceil(len(ids) / count)
        for index in range(count):
            chunk = ids[index * size : (index + 1) * size]
            parts.append(Part(module, chunk, True, index + 1, count))
    return sorted(parts, key=lambda p: -len(p.ids))


class Pool:
    """Child processes that are still running, to stop them on a signal."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.live: set[subprocess.Popen[str]] = set()
        self.stopped = False

    def run(self, command: list[str], timeout: float) -> tuple[int | None, str]:
        with self.lock:
            if self.stopped:
                return None, "not started: the run was stopped\n"
            proc = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
            )
            self.live.add(proc)
        try:
            output, _ = proc.communicate(timeout=timeout)
            return proc.returncode, output
        except subprocess.TimeoutExpired:
            proc.kill()
            output, _ = proc.communicate()
            return None, output + f"\npart timed out after {timeout:.0f} s\n"
        finally:
            with self.lock:
                self.live.discard(proc)

    def stop(self) -> None:
        with self.lock:
            self.stopped = True
            for proc in self.live:
                proc.kill()


def run_part(
    pool: Pool, top: str, part: Part, folder: Path, number: int, timeout: float
) -> Outcome:
    report_file = folder / f"report-{number}.json"
    command = [
        sys.executable,
        str(ISOLATED_ENV),
        "--report",
        str(report_file),
        top,
        *part.args(),
    ]
    start = time.monotonic()
    try:
        code, output = pool.run(command, timeout)
    except OSError as error:
        code, output = None, f"part failed to start: {error}\n"
    seconds = time.monotonic() - start
    report = None
    try:
        report = json.loads(report_file.read_text())
    except (OSError, ValueError):
        pass
    return Outcome(part, seconds, code, output, report)


def check(outcomes: list[Outcome], listed: list[str]) -> list[str]:
    """Mark failed parts. Returns problems of the whole run (missing, repeated
    or unknown ids)."""
    for out in outcomes:
        if out.report is None:
            out.problems.append(f"no report (exit {out.exit})")
            continue
        if out.exit != 0:
            out.problems.append(f"exit {out.exit}")
        started = Counter(out.report["started"])
        missing = [i for i in out.part.ids if i not in started]
        extra = sorted(set(started) - set(out.part.ids))
        twice = sorted(i for i, n in started.items() if n > 1)
        for name, ids in (("missing", missing), ("not in part", extra)):
            if ids:
                out.problems.append(f"{name}: {', '.join(ids)}")
        if twice:
            out.problems.append(f"started twice: {', '.join(twice)}")
    expected = Counter(listed)
    ran = Counter(i for out in outcomes if out.report for i in out.report["started"])
    problems = []
    missing = sorted(i for i in expected if ran[i] < expected[i])
    repeated = sorted(i for i in ran if ran[i] > expected[i] and expected[i])
    unknown = sorted(i for i in ran if not expected[i])
    if missing:
        problems.append(f"{len(missing)} listed tests did not run: {missing[:20]}")
    if repeated:
        problems.append(f"{len(repeated)} tests ran more than once: {repeated[:20]}")
    if unknown:
        problems.append(f"{len(unknown)} tests ran but were not listed: {unknown[:20]}")
    return problems


def list_tests(top: str, folder: Path) -> dict[str, Any]:
    out = folder / "listing.json"
    proc = subprocess.run(
        [sys.executable, str(ISOLATED_ENV), "--list", str(out), top],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        timeout=PART_TIMEOUT,
    )
    if proc.returncode != 0 or not out.exists():
        raise RuntimeError(
            f"listing {top} failed (exit {proc.returncode}):\n{proc.stdout}"
        )
    return json.loads(out.read_text())


def summarize(
    outcomes: list[Outcome], problems: list[str], wall: float, jobs: int, total: int
) -> bool:
    failed = [o for o in outcomes if o.failed]
    for out in failed:
        print(f"\n{'=' * 70}\nFAILED part {out.part.label}: {'; '.join(out.problems)}")
        report = out.report or {}
        for kind in ("failures", "errors"):
            for test_id, _ in report.get(kind, []):
                print(f"  {kind[:-1]}: {test_id}")
        for test_id in report.get("unexpected_successes", []):
            print(f"  unexpected success: {test_id}")
        print("-" * 70)
        tail = out.output[-OUTPUT_TAIL:]
        if len(out.output) > OUTPUT_TAIL:
            print(f"[last {OUTPUT_TAIL} characters of the output]")
        print(tail.rstrip())
    for problem in problems:
        print(f"\nRUN PROBLEM: {problem}")
    print(f"\nSlowest {SLOWEST} parts:")
    for out in sorted(outcomes, key=lambda o: -o.seconds)[:SLOWEST]:
        print(f"  {out.seconds:7.1f} s  {out.part.label} ({len(out.part.ids)} tests)")
    ran = sum(len(o.report["started"]) for o in outcomes if o.report)
    ok = not failed and not problems
    status = "OK" if ok else f"FAILED ({len(failed)} parts failed)"
    print(
        f"\nRan {ran} of {total} tests in {len(outcomes)} parts, -j {jobs}, "
        f"wall {wall:.1f} s: {status}"
    )
    return ok


def run(
    top: str,
    jobs: int,
    timeout: float = PART_TIMEOUT,
    planner: Callable[[dict[str, Any]], list[Part]] = plan,
) -> int:
    start = time.monotonic()
    pool = Pool()

    def stop(signum: int, _frame: Any) -> None:
        pool.stop()
        raise SystemExit(128 + signum)

    main_thread = threading.current_thread() is threading.main_thread()
    previous = signal.signal(signal.SIGTERM, stop) if main_thread else None
    try:
        return _run(top, jobs, timeout, planner, pool, start)
    finally:
        if main_thread:
            signal.signal(signal.SIGTERM, previous)


def _run(
    top: str,
    jobs: int,
    timeout: float,
    planner: Callable[[dict[str, Any]], list[Part]],
    pool: Pool,
    start: float,
) -> int:
    with tempfile.TemporaryDirectory(prefix="epic-run-tests-") as name:
        folder = Path(name)
        try:
            listing = list_tests(top, folder)
        except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as e:
            print(f"run_tests: {e}")
            return 1
        listed = [i for entry in listing["modules"].values() for i in entry["ids"]]
        parts = planner(listing)
        outcomes: list[Outcome] = []
        try:
            with ThreadPoolExecutor(max_workers=jobs) as executor:
                futures = [
                    executor.submit(run_part, pool, top, part, folder, n, timeout)
                    for n, part in enumerate(parts)
                ]
                for future in as_completed(futures):
                    out = future.result()
                    outcomes.append(out)
                    state = "ok" if out.exit == 0 else f"exit {out.exit}"
                    print(
                        f"[{len(outcomes)}/{len(parts)}] {state:>8} "
                        f"{out.seconds:6.1f} s  {out.part.label}",
                        flush=True,
                    )
        finally:
            pool.stop()
        problems = check(outcomes, listed)
        ok = summarize(outcomes, problems, time.monotonic() - start, jobs, len(listed))
    return 0 if ok else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("top", help="test directory, for example scripts/epic")
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=os.cpu_count() or 1,
        help="parts that run at the same time (default: CPU count)",
    )
    parser.add_argument(
        "--part-timeout",
        type=float,
        default=PART_TIMEOUT,
        help=f"seconds before one part is stopped (default: {PART_TIMEOUT})",
    )
    args = parser.parse_args(argv)
    if args.jobs < 1:
        parser.error("-j must be at least 1")
    if not Path(args.top).is_dir():
        parser.error(f"no test directory: {args.top}")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return run(args.top, args.jobs, args.part_timeout)


if __name__ == "__main__":
    sys.exit(main())
