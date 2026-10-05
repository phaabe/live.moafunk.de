"""Tests for the shared GitHub quota wait. Run: python3 -m unittest discover -s scripts/epic"""

from __future__ import annotations

import isolated_env  # noqa: F401  (first: hides live runner state)

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import github_quota
from github_quota import (
    DEFERRED,
    FALLBACK_SECONDS,
    MARGIN,
    PROCEED,
    QUOTA,
    QuotaExhausted,
    check,
    describe,
    is_quota_error,
    iso,
    parse_iso,
    record,
    run_gh,
    stop_on_quota,
)

HERE = Path(__file__).resolve().parent
T0 = parse_iso("2026-09-28T12:00:00Z")
STUB_GH = "/tmp/stub/bin/gh"
RESET = "2026-09-28T13:00:00Z"
GRAPHQL_STDERR = "GraphQL: API rate limit already exceeded for user ID 1234567."
RATE_LIMITED_200 = json.dumps(
    {
        "data": None,
        "errors": [
            {"type": "RATE_LIMITED", "message": "API rate limit exceeded for user"}
        ],
    }
)


class DetectTest(unittest.TestCase):
    def test_graphql_command_error(self) -> None:
        self.assertTrue(is_quota_error(["pr", "list"], "", GRAPHQL_STDERR))
        self.assertTrue(is_quota_error(["project", "item-list"], "", GRAPHQL_STDERR))

    def test_rate_limited_inside_http_200(self) -> None:
        self.assertTrue(is_quota_error(["api", "graphql"], RATE_LIMITED_200, ""))

    def test_rest_error_is_not_the_graphql_quota(self) -> None:
        # REST reads keep failing visibly; only GraphQL stores the wait.
        stderr = "gh: API rate limit exceeded for user ID 1. (HTTP 403)"
        self.assertFalse(is_quota_error(["api", "repos/x/issues/1"], "", stderr))

    def test_secondary_limit_is_out_of_scope(self) -> None:
        stderr = "GraphQL: You have exceeded a secondary rate limit."
        self.assertFalse(is_quota_error(["pr", "list"], "", stderr))

    def test_other_errors_stay_visible(self) -> None:
        self.assertFalse(
            is_quota_error(["pr", "list"], "", "HTTP 401: Bad credentials")
        )
        other = json.dumps({"errors": [{"type": "NOT_FOUND"}]})
        self.assertFalse(is_quota_error(["api", "graphql"], other, ""))
        self.assertFalse(is_quota_error(["pr", "list"], "[]", ""))


def fake_run(stdout: str, stderr: str, code: int):
    return mock.patch.object(
        github_quota.subprocess,
        "run",
        return_value=subprocess.CompletedProcess([], code, stdout, stderr),
    )


def fake_gh(path: str | None):
    return mock.patch.object(github_quota.shutil, "which", return_value=path)


class RunGhTest(unittest.TestCase):
    def test_quota_error_raises(self) -> None:
        with fake_run("", GRAPHQL_STDERR, 1), self.assertRaises(QuotaExhausted):
            run_gh(["pr", "list"])

    def test_quota_error_carries_the_gh_path_that_ran(self) -> None:
        with fake_gh(STUB_GH), fake_run("", GRAPHQL_STDERR, 1) as run:
            with self.assertRaises(QuotaExhausted) as raised:
                run_gh(["pr", "list"])
        self.assertEqual(raised.exception.gh_path, STUB_GH)
        self.assertEqual(run.call_args.args[0][0], STUB_GH)  # resolved once, run

    def test_quota_error_with_exit_0_raises(self) -> None:
        with fake_run(RATE_LIMITED_200, "", 0), self.assertRaises(QuotaExhausted):
            run_gh(["api", "graphql", "-f", "query=x"])

    def test_other_failure_is_a_called_process_error(self) -> None:
        with fake_run("", "HTTP 401: Bad credentials", 1):
            with self.assertRaises(subprocess.CalledProcessError):
                run_gh(["pr", "list"])

    def test_success_returns_stdout(self) -> None:
        with fake_run("[]", "", 0):
            self.assertEqual(run_gh(["pr", "list"]), "[]")


class WaitTest(unittest.TestCase):
    """Injected clock: wait, expiry and renewed exhaustion."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.lookups = 0

    def lookup(self, reset: str | None, gh: str | None = STUB_GH):
        def read() -> tuple[str | None, str | None]:
            self.lookups += 1
            return reset, gh

        return read

    def test_no_file_proceeds(self) -> None:
        self.assertEqual(check(self.dir, T0), (PROCEED, None))

    def test_wait_until_reset_plus_margin(self) -> None:
        wait = record(self.dir, T0, lookup=self.lookup(RESET))
        self.assertEqual(self.lookups, 1)
        retry = iso(parse_iso(RESET) + MARGIN)
        self.assertEqual(wait["retry_at"], retry)
        self.assertEqual(wait["source"], "rateLimit")
        self.assertEqual(check(self.dir, T0 + 60), (DEFERRED, retry))
        self.assertEqual(check(self.dir, parse_iso(RESET)), (DEFERRED, retry))

    def test_expiry_proceeds(self) -> None:
        record(self.dir, T0, lookup=self.lookup(RESET))
        self.assertEqual(check(self.dir, parse_iso(RESET) + MARGIN), (PROCEED, None))

    def test_renewed_exhaustion_stores_a_new_wait(self) -> None:
        record(self.dir, T0, lookup=self.lookup(RESET))
        later = parse_iso(RESET) + MARGIN + 5
        self.assertEqual(check(self.dir, later)[0], PROCEED)
        record(self.dir, later, lookup=self.lookup("2026-09-28T14:00:00Z"))
        self.assertEqual(
            check(self.dir, later),
            (DEFERRED, iso(parse_iso("2026-09-28T14:00:00Z") + MARGIN)),
        )

    def test_caller_reset_needs_no_query(self) -> None:
        wait = record(self.dir, T0, RESET, lookup=self.lookup(None))
        self.assertEqual(self.lookups, 0)
        self.assertEqual(wait["source"], "caller")

    def test_unknown_reset_uses_bounded_backoff(self) -> None:
        wait = record(self.dir, T0, lookup=self.lookup(None))
        self.assertEqual(wait["source"], "fallback")
        self.assertEqual(wait["retry_at"], iso(T0 + FALLBACK_SECONDS))
        self.assertEqual(check(self.dir, T0 + FALLBACK_SECONDS - 1)[0], DEFERRED)
        self.assertEqual(check(self.dir, T0 + FALLBACK_SECONDS)[0], PROCEED)

    def test_past_or_bad_reset_uses_backoff_not_an_immediate_retry(self) -> None:
        for reset in ("2026-09-28T11:00:00Z", "soon"):
            wait = record(self.dir, T0, reset, lookup=self.lookup(None))
            self.assertEqual(wait["retry_at"], iso(T0 + FALLBACK_SECONDS))

    def test_bad_file_is_an_error(self) -> None:
        (self.dir / github_quota.WAIT_FILE).write_text("{}")
        with self.assertRaises(ValueError):
            check(self.dir, T0)

    def test_wait_leaves_other_state_intact(self) -> None:
        gate = self.dir / "claude-gate.json"
        backoff = self.dir / "codex-backoff.json"
        gate.write_text('{"at": 1}')
        backoff.write_text('{"issue:x": {"at": 1, "reason": "r"}}')
        record(self.dir, T0, lookup=self.lookup(RESET))
        self.assertEqual(gate.read_text(), '{"at": 1}')
        self.assertEqual(backoff.read_text(), '{"issue:x": {"at": 1, "reason": "r"}}')
        self.assertEqual(
            sorted(p.name for p in self.dir.iterdir()),
            ["claude-gate.json", "codex-backoff.json", github_quota.WAIT_FILE],
        )


class ProvenanceTest(unittest.TestCase):
    """Who wrote a wait and which gh answered. Diagnostic only."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def stored(self) -> dict:
        return json.loads((self.dir / github_quota.WAIT_FILE).read_text())

    def test_writer_is_this_process(self) -> None:
        before = time.time()
        wait = record(self.dir, T0, RESET)
        writer = wait["provenance"]["writer"]
        self.assertEqual(writer["pid"], os.getpid())
        self.assertEqual(writer["executable"], sys.executable)
        self.assertEqual(writer["cwd"], os.getcwd())
        self.assertGreaterEqual(parse_iso(writer["written_at"]), int(before))
        self.assertEqual(wait["recorded_at"], iso(T0))  # the injected clock
        self.assertEqual(self.stored(), wait)

    def test_explicit_reset_has_no_lookup(self) -> None:
        wait = record(self.dir, T0, RESET, lookup=mock.Mock(side_effect=AssertionError))
        self.assertEqual(wait["source"], "caller")
        prov = wait["provenance"]
        self.assertEqual(prov["origin"], "code")
        self.assertEqual(prov["reset_lookup"], {"attempted": False})
        self.assertEqual(prov["quota_call"], {"gh_path": None})
        self.assertIn("no reset lookup", describe(wait))
        self.assertIn("quota call gh not reported", describe(wait))

    def test_fetched_reset_names_the_lookup_gh(self) -> None:
        wait = record(self.dir, T0, lookup=lambda: (RESET, STUB_GH))
        self.assertEqual(wait["source"], "rateLimit")
        self.assertEqual(
            wait["provenance"]["reset_lookup"],
            {"attempted": True, "gh_path": STUB_GH, "result": "reset"},
        )
        self.assertIn(f"reset lookup gh {STUB_GH} (reset)", describe(wait))

    def test_failed_lookup_falls_back_and_keeps_the_gh_path(self) -> None:
        wait = record(self.dir, T0, lookup=lambda: (None, STUB_GH))
        self.assertEqual(wait["source"], "fallback")
        self.assertEqual(
            wait["provenance"]["reset_lookup"],
            {"attempted": True, "gh_path": STUB_GH, "result": "failed"},
        )

    def test_unresolvable_gh_is_said_plainly(self) -> None:
        with fake_gh(None), fake_run("", "", 0) as run:
            wait = record(self.dir, T0)
        run.assert_not_called()  # nothing to run, no call made
        self.assertEqual(wait["source"], "fallback")
        self.assertIsNone(wait["provenance"]["reset_lookup"]["gh_path"])
        self.assertIn("reset lookup gh unavailable (failed)", describe(wait))

    def test_lookup_runs_the_resolved_path(self) -> None:
        body = json.dumps({"data": {"rateLimit": {"resetAt": RESET}}})
        with fake_gh(STUB_GH), fake_run(body, "", 0) as run:
            wait = record(self.dir, T0)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][0], STUB_GH)
        self.assertEqual(wait["provenance"]["reset_lookup"]["gh_path"], STUB_GH)

    def test_model_result_origin(self) -> None:
        wait = record(self.dir, T0, RESET, origin="model-result")
        self.assertEqual(wait["source"], "caller")  # source values unchanged
        self.assertEqual(wait["provenance"]["origin"], "model-result")
        self.assertIn("origin model-result", describe(wait))
        with self.assertRaises(ValueError):
            record(self.dir, T0, RESET, origin="model")

    def test_stop_on_quota_records_the_failing_call(self) -> None:
        error = QuotaExhausted("GraphQL", "/stub/failing/gh")
        with mock.patch.object(
            github_quota, "query_reset_at", return_value=(RESET, STUB_GH)
        ):
            self.assertEqual(stop_on_quota(error, self.dir), QUOTA)
        prov = self.stored()["provenance"]
        self.assertEqual(prov["quota_call"], {"gh_path": "/stub/failing/gh"})
        self.assertEqual(prov["reset_lookup"]["gh_path"], STUB_GH)

    def test_quota_exhausted_without_a_path_is_not_reported(self) -> None:
        self.assertIsNone(QuotaExhausted("x").gh_path)

    def test_no_arguments_or_environment_values(self) -> None:
        secret = "ghp_" + "x" * 20
        with (
            mock.patch.dict(os.environ, {"GH_TOKEN": secret}),
            mock.patch.object(sys, "argv", ["tick.py", "--token", secret]),
        ):
            wait = record(self.dir, T0, RESET)
        text = (self.dir / github_quota.WAIT_FILE).read_text()
        self.assertNotIn(secret, text)
        self.assertNotIn("--token", text)
        self.assertEqual(
            set(wait["provenance"]["writer"]),
            {"pid", "executable", "script", "cwd", "written_at"},
        )

    def test_file_is_private(self) -> None:
        record(self.dir, T0, RESET)
        mode = (self.dir / github_quota.WAIT_FILE).stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_legacy_file_stays_valid(self) -> None:
        fixture = HERE / "fixtures" / "github-quota-wait.json"
        legacy = json.loads(fixture.read_text())
        self.assertNotIn("provenance", legacy)
        (self.dir / github_quota.WAIT_FILE).write_text(fixture.read_text())
        retry = legacy["retry_at"]
        self.assertEqual(check(self.dir, parse_iso(retry) - 1), (DEFERRED, retry))
        self.assertEqual(describe(legacy), "provenance unavailable (older wait file)")

    def test_provenance_changes_no_wait_decision(self) -> None:
        with_prov = record(self.dir, T0, RESET)
        bare = {k: v for k, v in with_prov.items() if k != "provenance"}
        (self.dir / github_quota.WAIT_FILE).write_text(json.dumps(bare))
        for at in (T0, parse_iso(RESET) + MARGIN - 1, parse_iso(RESET) + MARGIN):
            (self.dir / github_quota.WAIT_FILE).write_text(json.dumps(bare))
            without = check(self.dir, at)
            (self.dir / github_quota.WAIT_FILE).write_text(json.dumps(with_prov))
            self.assertEqual(check(self.dir, at), without)

    def test_failed_write_keeps_the_old_wait(self) -> None:
        old = record(self.dir, T0, RESET)
        with mock.patch.object(
            github_quota.os, "replace", side_effect=OSError("disk full")
        ):
            with self.assertRaises(OSError):
                record(self.dir, T0, "2026-09-28T14:00:00Z")
        self.assertEqual(self.stored(), old)
        self.assertEqual([p.name for p in self.dir.iterdir()], [github_quota.WAIT_FILE])

    def test_independent_writers_each_leave_one_consistent_wait(self) -> None:
        # Each writer stores its own reset; the file always pairs one wait
        # with the writer that stored it.
        script = (
            "import sys, time, github_quota as q\n"
            "d, reset = sys.argv[1], sys.argv[2]\n"
            "for _ in range(40):\n"
            "    q.record(__import__('pathlib').Path(d), time.time(), reset)\n"
            "print(__import__('os').getpid())\n"
        )
        resets = [iso(time.time() + 3600 + 60 * i) for i in range(3)]
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(self.dir), reset],
                cwd=HERE,
                stdout=subprocess.PIPE,
                text=True,
            )
            for reset in resets
        ]
        seen = []
        while any(p.poll() is None for p in procs):
            try:
                seen.append(self.stored())
            except FileNotFoundError:
                pass
        pids = {int(p.communicate()[0]): r for p, r in zip(procs, resets)}
        seen.append(self.stored())
        for wait in seen:
            self.assertEqual(
                wait["reset_at"], pids[wait["provenance"]["writer"]["pid"]]
            )
        self.assertEqual([p.name for p in self.dir.iterdir()], [github_quota.WAIT_FILE])


class SharedWaitTest(unittest.TestCase):
    def test_quota_dir_wins_over_the_agent_state_dir(self) -> None:
        # Registered agents have their own EPIC_STATE_DIR; the quota is shared.
        script = (
            "import github_quota, json; print(json.dumps(str(github_quota.STATE_DIR)))"
        )
        here = Path(__file__).resolve().parent
        for env, expected in (
            ({"EPIC_STATE_DIR": "/a/agents/x", "EPIC_QUOTA_DIR": "/a"}, "/a"),
            ({"EPIC_STATE_DIR": "/a/agents/x"}, "/a/agents/x"),
        ):
            out = subprocess.run(
                [sys.executable, "-c", script],
                cwd=here,
                env={**os.environ, "EPIC_QUOTA_DIR": "", **env},
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            self.assertEqual(json.loads(out), expected)


class ScriptTest(unittest.TestCase):
    """The real scripts with a fake gh that counts GitHub calls."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.state = root / "state"
        self.calls = root / "gh-calls.jsonl"
        home = root / "home"
        home.mkdir()
        bin_dir = root / "bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "with open(os.environ['TEST_GH_CALLS'], 'a') as f:\n"
            "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "if sys.argv[1:3] == ['api', 'graphql'] and 'rateLimit' in ' '.join(sys.argv):\n"
            "    print(json.dumps({'data': {'rateLimit': {'resetAt': os.environ['TEST_RESET']}}}))\n"
            "    sys.exit(0)\n"
            f"sys.stderr.write({GRAPHQL_STDERR!r} + '\\n')\n"
            "sys.exit(1)\n"
        )
        gh.chmod(0o755)
        self.gh = gh
        self.reset = iso(time.time() + 3600)
        self.env = {
            **os.environ,
            "HOME": str(home),
            "EPIC_STATE_DIR": str(self.state),
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "TEST_GH_CALLS": str(self.calls),
            "TEST_RESET": self.reset,
        }

    def run_script(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, *args],
            env=self.env,
            capture_output=True,
            text=True,
            cwd=HERE,
            timeout=60,
        )

    def gh_calls(self) -> list[list[str]]:
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def test_selector_stops_on_quota_and_stores_the_wait(self) -> None:
        out = self.run_script("next_action.py", "--agent", "claude")
        self.assertEqual(out.returncode, QUOTA, out.stderr)
        self.assertEqual(out.stdout, "")  # no action from that read
        calls = self.gh_calls()
        self.assertEqual(len(calls), 2)  # the failed read, one rateLimit query
        self.assertEqual(calls[0][:2], ["pr", "list"])
        self.assertEqual(calls[1][:2], ["api", "graphql"])
        wait = json.loads((self.state / github_quota.WAIT_FILE).read_text())
        self.assertEqual(wait["reset_at"], self.reset)
        self.assertIn("retry at", out.stderr)
        # The temporary stub answered both calls, and the wait says so.
        prov = wait["provenance"]
        self.assertEqual(prov["quota_call"], {"gh_path": str(self.gh)})
        self.assertEqual(
            prov["reset_lookup"],
            {"attempted": True, "gh_path": str(self.gh), "result": "reset"},
        )
        self.assertEqual(prov["writer"]["script"], str(HERE / "next_action.py"))
        self.assertEqual(prov["writer"]["cwd"], str(HERE))
        self.assertIn(str(self.gh), out.stderr)

    def test_selector_makes_no_call_during_a_wait(self) -> None:
        record(self.state, time.time(), self.reset)
        out = self.run_script("next_action.py", "--agent", "claude")
        self.assertEqual(out.returncode, DEFERRED, out.stderr)
        self.assertEqual(out.stdout, "")
        self.assertEqual(self.gh_calls(), [])

    def test_gate_writes_no_record_on_quota(self) -> None:
        # The gate reads REST, but a RATE_LIMITED body still stops it.
        gh = Path(self.env["PATH"].split(":")[0]) / "gh"
        gh.write_text(
            gh.read_text().replace(
                f"sys.stderr.write({GRAPHQL_STDERR!r} + '\\n')\nsys.exit(1)\n",
                f"print({RATE_LIMITED_200!r})\n",
            )
        )
        action = self.state.parent / "action.json"
        action.write_text(json.dumps({"action": "fix", "pr": 5, "sha": "a" * 40}))
        out = self.run_script(
            "tick_gate.py", "check", "--agent", "claude", "--action-file", str(action)
        )
        self.assertEqual(out.returncode, QUOTA, out.stderr)
        self.assertEqual(
            sorted(p.name for p in self.state.iterdir()), [github_quota.WAIT_FILE]
        )


class CodexInterfaceTest(unittest.TestCase):
    """Fixture for the Codex runner: the CLI calls codex-tick.sh can make."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)

    def cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(HERE / "github_quota.py"),
                *args,
                "--state-dir",
                str(self.state),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "PATH": "/nonexistent"},  # no gh: no GitHub call
        )

    def test_fixture_file_is_a_valid_wait(self) -> None:
        fixture = HERE / "fixtures" / "github-quota-wait.json"
        (self.state / github_quota.WAIT_FILE).write_text(fixture.read_text())
        retry = json.loads(fixture.read_text())["retry_at"]
        self.assertEqual(check(self.state, parse_iso(retry) - 1), (DEFERRED, retry))
        self.assertEqual(check(self.state, parse_iso(retry))[0], PROCEED)

    def test_check_record_expire(self) -> None:
        self.assertEqual(self.cli("check").returncode, PROCEED)
        future = iso(time.time() + 3600)
        stored = self.cli("record", "--reset-at", future)
        self.assertEqual(stored.returncode, 0, stored.stderr)
        self.assertIn("no reset lookup", stored.stdout)
        deferred = self.cli("check")
        self.assertEqual(deferred.returncode, DEFERRED)
        self.assertIn("retry at", deferred.stdout)
        self.assertIn("origin code; writer pid", deferred.stdout)
        # A reset in the past falls back to a bounded wait, never a retry now.
        self.cli("record", "--reset-at", iso(time.time() - 10))
        self.assertEqual(self.cli("check").returncode, DEFERRED)

    def test_record_without_gh_says_so(self) -> None:
        stored = self.cli("record")  # PATH has no gh
        self.assertEqual(stored.returncode, 0, stored.stderr)
        self.assertIn("(fallback;", stored.stdout)
        self.assertIn("reset lookup gh unavailable (failed)", stored.stdout)

    def test_check_names_a_legacy_file(self) -> None:
        legacy = {
            "reset_at": None,
            "retry_at": iso(time.time() + 600),
            "source": "fallback",
            "recorded_at": iso(time.time()),
        }
        (self.state / github_quota.WAIT_FILE).write_text(json.dumps(legacy))
        deferred = self.cli("check")
        self.assertEqual(deferred.returncode, DEFERRED)
        self.assertIn("provenance unavailable", deferred.stdout)

    def test_provenance_fixture_is_a_valid_wait(self) -> None:
        fixture = HERE / "fixtures" / "github-quota-wait-provenance.json"
        wait = json.loads(fixture.read_text())
        (self.state / github_quota.WAIT_FILE).write_text(fixture.read_text())
        retry = wait["retry_at"]
        self.assertEqual(check(self.state, parse_iso(retry) - 1), (DEFERRED, retry))
        self.assertEqual(wait["provenance"]["origin"], "model-result")
        self.assertIn("origin model-result", describe(wait))

    def test_check_reads_one_snapshot(self) -> None:
        # Another runner replaces the wait right after the first read. The
        # output must pair the retry time with the writer of that same wait.
        first = record(self.state, time.time(), iso(time.time() + 3600))
        first["provenance"]["writer"]["pid"] = 111
        second = json.loads(json.dumps(first))
        second["retry_at"] = iso(time.time() + 7200)
        second["provenance"]["writer"]["pid"] = 222
        reads = iter([json.dumps(first), json.dumps(second)])
        out = io.StringIO()
        with (
            mock.patch.object(Path, "read_text", lambda _self: next(reads)),
            mock.patch.object(
                sys, "argv", ["github_quota.py", "check", "--state-dir", "x"]
            ),
            contextlib.redirect_stdout(out),
        ):
            self.assertEqual(github_quota.main(), DEFERRED)
        self.assertIn(first["retry_at"], out.getvalue())
        self.assertIn("writer pid 111 ", out.getvalue())
        # Match the writer field, not bare digits: the checkout path in the
        # output can contain any digits (for example a commit SHA).
        self.assertNotIn("writer pid 222", out.getvalue())
        self.assertNotIn(second["retry_at"], out.getvalue())

    def test_bad_file_exits_2(self) -> None:
        (self.state / github_quota.WAIT_FILE).write_text("not json")
        self.assertEqual(self.cli("check").returncode, 2)


if __name__ == "__main__":
    unittest.main()
