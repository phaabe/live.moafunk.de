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
"""

from __future__ import annotations

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


def install() -> None:
    """Isolate this process and every test it runs. Safe to call twice."""
    global _session
    if _session is not None or PARENT.get(MARKER) == "1":
        return
    _session = tempfile.TemporaryDirectory(prefix="epic-test-env-")
    home = Path(_session.name) / "home"
    home.mkdir()
    replace_environ({**clean_env(home, PARENT), MARKER: "1"})
    unittest.TestCase.run = _isolated_run  # type: ignore[method-assign]


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: isolated_env.py <test dir> [unittest options]")
    # Install through the importable module, so the tests' own
    # `import isolated_env` finds it done and does not install twice.
    import isolated_env  # noqa: F401

    unittest.main(module=None, argv=[sys.argv[0], "discover", "-s", *sys.argv[1:]])
else:
    install()
