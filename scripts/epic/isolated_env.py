"""Keep the epic tests away from live runner state.

The runners export EPIC_STATE_DIR, EPIC_QUOTA_DIR and more, and model sessions
inherit them. A test that copies os.environ, or a module that reads a setting
at import, then uses the live quota wait, locks and caches. Defaults under
~/.local/state/epic-loop are live state too.

Every test module in scripts/epic imports this module before any epic module.
The first import:
  - removes every EPIC_* and XDG_* variable, GitHub and model tokens, and
    config dirs of gh, Claude and Codex,
  - points HOME (so every ~ default) and GH_CONFIG_DIR at a temporary home.
Settings read at import time, such as github_quota.STATE_DIR, resolve there.
Each test then runs with its own new temporary home, and os.environ is
restored after the test (install() wraps unittest.TestCase.run). A test sets
only the switches it exercises.

A child process started by a test inherits MARKER. When it imports test code
(for example `from test_github_state import FakeGitHub`), this module keeps
the environment the test gave it.

Run another suite the same way (the Codex runner tests):
    python3 scripts/epic/isolated_env.py .codex/tests [unittest options]

Two more modes serve run_tests.py, which runs a suite in parallel parts:
    isolated_env.py --list FILE <test dir>
        writes every test id of the directory as JSON, per module, without
        running a test (see listing()).
    isolated_env.py --report FILE <test dir> [unittest options]
        runs the tests and writes the ids that started and the failures as
        JSON (see Recording).
Run as a script, this module always isolates, also when the parent has
MARKER: a suite run is not test code that a test started on purpose.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path

DROPPED_PREFIXES = ("EPIC_", "XDG_")
DROPPED = frozenset(
    {
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
        "GH_CONFIG_DIR",
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CONFIG_DIR",
        "OPENAI_API_KEY",
        "CODEX_HOME",
    }
)

# Not EPIC_*: it must survive clean_env() into the tests' child processes.
MARKER = "ISOLATED_EPIC_TESTS"

# The environment this process started with, before install().
PARENT: dict[str, str] = dict(os.environ)
_session: tempfile.TemporaryDirectory[str] | None = None


def clean_env(home: Path, base: Mapping[str, str]) -> dict[str, str]:
    """`base` without runner settings, with HOME and gh config under `home`."""
    env = {
        key: value
        for key, value in base.items()
        if not key.startswith(DROPPED_PREFIXES) and key not in DROPPED
    }
    env["HOME"] = str(home)
    env["GH_CONFIG_DIR"] = str(home / ".config" / "gh")
    return env


def replace_environ(env: Mapping[str, str]) -> None:
    os.environ.clear()
    os.environ.update(env)


_original_run = unittest.TestCase.run


def _isolated_run(
    self: unittest.TestCase, result: unittest.TestResult | None = None
) -> unittest.TestResult | None:
    saved = dict(os.environ)
    with tempfile.TemporaryDirectory(
        prefix="epic-test-home-", ignore_cleanup_errors=True
    ) as home:
        replace_environ(clean_env(Path(home), saved))
        try:
            return _original_run(self, result)
        finally:
            replace_environ(saved)


def install(force: bool = False) -> None:
    """Isolate this process and every test it runs. Safe to call twice.
    Without `force`, a process that inherited MARKER keeps its environment."""
    global _session
    if _session is not None or (PARENT.get(MARKER) == "1" and not force):
        return
    _session = tempfile.TemporaryDirectory(prefix="epic-test-env-")
    home = Path(_session.name) / "home"
    home.mkdir()
    replace_environ({**clean_env(home, PARENT), MARKER: "1"})
    unittest.TestCase.run = _isolated_run  # type: ignore[method-assign]


def cases(suite: unittest.TestSuite | unittest.TestCase) -> list[unittest.TestCase]:
    """The test cases of a suite, in run order."""
    if isinstance(suite, unittest.TestCase):
        return [suite]
    found: list[unittest.TestCase] = []
    for item in suite:
        found.extend(cases(item))
    return found


def _overrides(cls: type, name: str) -> bool:
    own = getattr(cls, name, None)
    base = getattr(unittest.TestCase, name)
    return getattr(own, "__func__", own) is not base.__func__


def fixture_reasons(tests: list[unittest.TestCase]) -> list[str]:
    """Class or module fixtures. A split part would run them once per part."""
    reasons: set[str] = set()
    for test in tests:
        cls = type(test)
        for name in ("setUpClass", "tearDownClass"):
            if _overrides(cls, name):
                reasons.add(f"{cls.__qualname__}.{name}")
        module = sys.modules.get(cls.__module__)
        for name in ("setUpModule", "tearDownModule"):
            if module is not None and hasattr(module, name):
                reasons.add(f"{cls.__module__}.{name}")
    return sorted(reasons)


def listing(top: Path) -> dict[str, object]:
    """Every test id under `top`, per module file, as a part would load it.

    A module's entry has its ids, `load_tests` (the module defines it, so it
    may ignore -k), `fixtures` (class or module fixtures) and `load_errors`
    (ids of tests that stand for a module that failed to import). The ids of
    all modules must equal the ids of one plain discovery of `top`."""
    whole = [t.id() for t in cases(unittest.TestLoader().discover(str(top)))]
    modules: dict[str, dict[str, object]] = {}
    for path in sorted(top.glob("test*.py")):
        tests = cases(unittest.TestLoader().discover(str(top), pattern=path.name))
        module = sys.modules.get(path.stem)
        modules[path.name] = {
            "ids": [t.id() for t in tests],
            "load_tests": module is not None and hasattr(module, "load_tests"),
            "fixtures": fixture_reasons(tests),
            "load_errors": [
                t.id() for t in tests if type(t).__module__ == "unittest.loader"
            ],
        }
    listed = sorted(i for entry in modules.values() for i in entry["ids"])  # type: ignore[attr-defined]
    if listed != sorted(whole):
        raise SystemExit(
            f"isolated_env: per-module ids ({len(listed)}) differ from one "
            f"discovery of {top} ({len(whole)}); tests outside top-level "
            "test*.py files are not supported"
        )
    return {"top": str(top), "modules": modules}


class Recording(unittest.TextTestResult):
    """A text result that also keeps the id of every test that started."""

    started: list[str]

    def startTest(self, test: unittest.TestCase) -> None:  # noqa: N802
        self.started = getattr(self, "started", [])
        self.started.append(test.id())
        super().startTest(test)

    def report(self) -> dict[str, object]:
        return {
            "started": getattr(self, "started", []),
            "failures": [[t.id(), tb] for t, tb in self.failures],
            "errors": [[t.id(), tb] for t, tb in self.errors],
            "unexpected_successes": [t.id() for t in self.unexpectedSuccesses],
            "skipped": len(self.skipped),
            "ok": self.wasSuccessful(),
        }


class RecordingRunner(unittest.TextTestRunner):
    resultclass = Recording


def _main(argv: list[str]) -> int:
    usage = (
        "usage: isolated_env.py [--list FILE | --report FILE] "
        "<test dir> [unittest options]"
    )
    mode = argv[0] if argv and argv[0] in ("--list", "--report") else None
    if mode:
        if len(argv) < 3:
            sys.exit(usage)
        out, argv = Path(argv[1]).resolve(), argv[2:]
    if not argv:
        sys.exit(usage)
    # Install through the importable module, so the tests' own
    # `import isolated_env` finds it done and does not install twice.
    import isolated_env

    isolated_env.install(force=True)
    if mode == "--list":
        if len(argv) != 1:
            sys.exit(usage)
        out.write_text(json.dumps(isolated_env.listing(Path(argv[0]))))
        return 0
    if mode == "--report":
        prog = unittest.main(
            module=None,
            argv=[sys.argv[0], "discover", "-s", *argv],
            testRunner=isolated_env.RecordingRunner,
            exit=False,
        )
        result = prog.result
        assert isinstance(result, isolated_env.Recording)
        out.write_text(json.dumps(result.report()))
        return 0 if result.wasSuccessful() else 1
    unittest.main(module=None, argv=[sys.argv[0], "discover", "-s", *argv])
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
else:
    install()
