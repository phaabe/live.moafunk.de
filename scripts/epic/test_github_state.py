"""Shared REST snapshot reader (github_state.py) with recorded `gh api -i` output.

Run: python3 -m unittest discover -s scripts/epic
"""

from __future__ import annotations

import isolated_env  # noqa: F401  (first: hides live runner state)

import hashlib
import json
import os
import signal
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

import github_state as gs
import next_action as na
import write_checks as wc

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures" / "gh_i"
REPO = f"repos/{na.REPO}"
A = "a" * 40
B = "b" * 40
ISSUES = f"https://github.com/{na.REPO}/issues"


def fixture(name: str) -> str:
    return (FIXTURES / f"{name}.txt").read_text(newline="")


def response(
    name: str, body: str | None = None, etag: str | None = None, link: str | None = None
) -> gs.Response:
    """A recorded response with this request's ETag, Link and body."""
    head, _, rest = fixture(name).partition("\r\n\r\n")
    keep = [
        line
        for line in head.split("\r\n")
        if not line.lower().startswith(("etag:", "link:"))
    ]
    if etag:
        keep.append(f"Etag: {etag}")
    if link:
        keep.append(f"Link: {link}")
    parsed = gs.parse_gh_i(
        "\r\n".join(keep) + "\r\n\r\n" + (rest if body is None else body)
    )
    assert parsed is not None
    return parsed


class FakeGitHub:
    """GET-only GitHub: 200 with ETag, 304 on a matching If-None-Match."""

    def __init__(self) -> None:
        self.pages: dict[str, tuple[str, str | None, str]] = {}
        self.errors: dict[str, list[str]] = {}
        self.calls: list[tuple[str, str | None]] = []
        self.set("user", {"login": "anneoneone"})

    def set(self, path: str, data: Any, link: str | None = None) -> str:
        url = gs.full_url(path)
        body = json.dumps(data)
        tag = '"' + hashlib.sha256(f"{body}{link}".encode()).hexdigest()[:16] + '"'
        self.pages[url] = (body, link, tag)
        return url

    def set_pages(self, path: str, pages: list[Any]) -> list[str]:
        """Pages linked by rel="next"; the first URL is `path`."""
        first = gs.full_url(path)
        urls = [first] + [f"{first}&page={i}" for i in range(2, len(pages) + 1)]
        for i, data in enumerate(pages):
            nxt = urls[i + 1] if i + 1 < len(urls) else None
            link = f'<{nxt}>; rel="next", <{urls[-1]}>; rel="last"' if nxt else None
            self.set(urls[i], data, link)
        return urls

    def fail(self, path: str, *names: str) -> None:
        self.errors.setdefault(gs.full_url(path), []).extend(names)

    def __call__(self, url: str, etag: str | None, timeout: float) -> gs.Response:
        self.calls.append((url, etag))
        queued = self.errors.get(url)
        if queued:
            return response(queued.pop(0))
        if url not in self.pages:
            return response("404")
        body, link, tag = self.pages[url]
        if etag == tag:
            return response("304", body="")
        return response("200-page", body=body, etag=tag, link=link)

    def count(self, path: str) -> int:
        url = gs.full_url(path)
        return sum(1 for u, _ in self.calls if u == url)


class Env(unittest.TestCase):
    """Isolated shared root, focus and pause files, with the reader on."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="github-state-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.cache = self.root / "shared"
        (self.cache / "github-cache").mkdir(parents=True)
        (self.cache / "github-cache" / "auth-context").write_text("test-context-1\n")
        env = {
            "EPIC_SHARED_READER": "1",
            "EPIC_CACHE_DIR": str(self.cache),
            "HOME": str(self.root),
        }
        for name in (
            "EPIC_SNAPSHOT_MAX_AGE_SECONDS",
            "EPIC_SNAPSHOT_LOCK_SECONDS",
            "EPIC_SNAPSHOT_REFRESH_SECONDS",
            "EPIC_RECHECK_TIMEOUT_SECONDS",
            "EPIC_SELECT_TIMEOUT_SECONDS",
            "GH_HOST",
        ):
            env[name] = ""
        patcher = patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name, value in (
            ("FOCUS_FILE", self.root / "focus"),
            ("PAUSE_FILE", self.root / "pause"),
        ):
            p = patch.object(na, name, value)
            p.start()
            self.addCleanup(p.stop)
        gs._LOGINS.clear()
        self.addCleanup(gs._LOGINS.clear)
        self.gh = FakeGitHub()

    def ns(self) -> gs.Namespace:
        return gs.resolve_namespace(self.gh)

    def client(self, writable: bool = True, seconds: float = 30) -> gs.Client:
        return gs.Client(self.ns(), "test", seconds, writable, http=self.gh)


class RecordedOutput(unittest.TestCase):
    def test_parses_recorded_200_304_and_404(self) -> None:
        ok = gs.parse_gh_i(fixture("200-page"))
        assert ok is not None
        self.assertEqual(ok.status, 200)
        self.assertTrue(ok.headers["etag"].startswith('W/"'))
        self.assertIn('rel="next"', ok.headers["link"])
        self.assertEqual(json.loads(ok.body)[0]["id"], 1001)
        cached = gs.parse_gh_i(fixture("304"))
        assert cached is not None
        self.assertEqual((cached.status, cached.body), (304, ""))
        self.assertNotIn("link", cached.headers)
        missing = gs.parse_gh_i(fixture("404"))
        assert missing is not None
        self.assertEqual(missing.status, 404)
        self.assertIn("Not Found", missing.body)

    def test_parses_lf_output_too(self) -> None:
        # subprocess text mode turns CRLF into LF.
        ok = gs.parse_gh_i(fixture("200-page").replace("\r\n", "\n"))
        assert ok is not None
        self.assertEqual(json.loads(ok.body)[0]["id"], 1001)

    def test_no_status_line_is_none(self) -> None:
        self.assertIsNone(gs.parse_gh_i("gh: connection refused"))

    def test_gh_http_reads_the_status_not_the_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp)
            args_file = bin_dir / "args"
            (bin_dir / "gh").write_text(
                "#!/bin/bash\n"
                f'printf "%s\\n" "$@" > "{args_file}"\n'
                'cat "$TEST_FIXTURE"\n'
                'exit "$TEST_EXIT"\n'
            )
            (bin_dir / "gh").chmod(0o755)
            env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}
            with patch.dict(
                os.environ,
                {**env, "TEST_FIXTURE": str(FIXTURES / "304.txt"), "TEST_EXIT": "1"},
            ):
                got = gs.gh_http(gs.full_url("repos/x/y"), 'W/"abc"', 10)
            self.assertEqual((got.status, got.body), (304, ""))
            sent = args_file.read_text().splitlines()
            self.assertIn('If-None-Match: W/"abc"', sent)
            self.assertIn(f"X-GitHub-Api-Version: {gs.API_VERSION}", sent)
            self.assertIn("-i", sent)
            (bin_dir / "empty").write_text("")
            with patch.dict(
                os.environ,
                {**env, "TEST_FIXTURE": str(bin_dir / "empty"), "TEST_EXIT": "1"},
            ):
                with self.assertRaises(gs.ReadBlocked):
                    gs.gh_http(gs.full_url("repos/x/y"), None, 10)


class Pagination(Env):
    def test_follows_every_page_and_revalidates_them_with_etags(self) -> None:
        urls = self.gh.set_pages(
            f"{REPO}/issues/1/comments?per_page=100",
            [[{"id": 1}], [{"id": 2}], [{"id": 3}]],
        )
        rows = self.client().pages(f"{REPO}/issues/1/comments?per_page=100")
        self.assertEqual([r["id"] for r in rows], [1, 2, 3])
        self.gh.calls.clear()
        again = self.client().pages(f"{REPO}/issues/1/comments?per_page=100")
        self.assertEqual(again, rows)
        # Every page, including those reached through a stored Link, is asked
        # again with its ETag; all answer 304.
        self.assertEqual([u for u, _ in self.gh.calls], urls)
        self.assertTrue(all(tag for _, tag in self.gh.calls))
        log = (self.cache / "github-cache" / "calls.jsonl").read_text().splitlines()
        results = [json.loads(line)["result"] for line in log]
        self.assertEqual(results.count("304"), 3)

    def test_changed_links_replace_the_stored_pagination(self) -> None:
        path = f"{REPO}/issues/1/comments?per_page=100"
        self.gh.set_pages(path, [[{"id": 1}], [{"id": 2}]])
        self.client().pages(path)
        self.gh.set_pages(path, [[{"id": 1}], [{"id": 2}], [{"id": 3}]])
        rows = self.client().pages(path)
        self.assertEqual([r["id"] for r in rows], [1, 2, 3])

    def test_failed_later_page_is_no_partial_success(self) -> None:
        path = f"{REPO}/issues/1/comments?per_page=100"
        urls = self.gh.set_pages(path, [[{"id": 1}], [{"id": 2}]])
        self.gh.fail(urls[1], "502")
        with self.assertRaises(gs.ReadBlocked):
            self.client().pages(path)

    def test_304_without_stored_body_blocks(self) -> None:
        path = f"{REPO}/issues/1"
        self.gh.fail(path, "304")
        with self.assertRaisesRegex(gs.ReadBlocked, "304 without a stored body"):
            self.client().json(path)

    def test_duplicate_rows_and_bad_pages_block(self) -> None:
        path = f"{REPO}/issues/1/comments?per_page=100"
        self.gh.set_pages(path, [[{"id": 1}], [{"id": 1}]])
        with self.assertRaisesRegex(gs.ReadBlocked, "duplicate"):
            self.client().pages(path)
        self.gh.set(path, {"not": "a list"})
        with self.assertRaisesRegex(gs.ReadBlocked, "malformed"):
            self.client().pages(path)

    def test_counted_pages_must_stay_complete(self) -> None:
        path = f"{REPO}/commits/{A}/check-runs?filter=all&per_page=100"
        self.gh.set_pages(
            path,
            [
                {"total_count": 3, "check_runs": [{"id": 1}, {"id": 2}]},
                {"total_count": 3, "check_runs": []},
            ],
        )
        with self.assertRaisesRegex(gs.ReadBlocked, "incomplete"):
            self.client().pages(path, key="check_runs")

    def test_refuses_links_to_other_hosts(self) -> None:
        path = f"{REPO}/issues/1/comments?per_page=100"
        self.gh.set(path, [{"id": 1}], link='<https://evil.example/x>; rel="next"')
        with self.assertRaises(gs.ReadBlocked):
            self.client().pages(path)

    def test_deadline_blocks(self) -> None:
        self.gh.set(f"{REPO}/issues/1", {"id": 1})
        client = self.client(seconds=0.01)
        time.sleep(0.02)
        with self.assertRaisesRegex(gs.ReadBlocked, "too long"):
            client.json(f"{REPO}/issues/1")


class FreshReads(Env):
    def test_fresh_reader_never_writes_etag_entries(self) -> None:
        self.gh.set(f"{REPO}/issues/1", {"id": 1})
        self.client(writable=False).json(f"{REPO}/issues/1")
        self.assertFalse((self.ns().dir / "etags").exists())

    def test_304_uses_the_body_kept_with_the_etag_sent(self) -> None:
        path = f"{REPO}/issues/1"
        self.gh.set(path, {"version": 1})
        self.client().json(path)  # the refresher stores version 1
        ns = self.ns()
        url = gs.full_url(path)

        def http(u: str, etag: str | None, timeout: float) -> gs.Response:
            # The refresher replaces the entry while this request runs.
            ns.save_entry(u, '"other"', json.dumps({"version": 2}), None)
            return self.gh(u, etag, timeout)

        got = gs.Client(ns, "fresh", 30, writable=False, http=http).json(url)
        self.assertEqual(got, {"version": 1})

    def test_rate_limits_block_without_auth_marker(self) -> None:
        path = f"{REPO}/issues/1"
        self.gh.set(path, {"id": 1})
        self.client().json(path)
        for name in ("403-rate-limit", "403-secondary"):
            self.gh.fail(path, name)
            with self.assertRaises(gs.ReadBlocked) as caught:
                self.client().json(path)
            self.assertNotIsInstance(caught.exception, gs.AuthLost)
        self.assertFalse(self.ns().auth_blocked())

    def test_forbidden_on_a_known_url_blocks_the_cache(self) -> None:
        path = f"{REPO}/issues/1"
        self.gh.set(path, {"id": 1})
        self.client().json(path)
        self.gh.fail(path, "403-forbidden")
        with self.assertRaises(gs.AuthLost):
            self.client(writable=False).json(path)
        self.assertTrue(self.ns().auth_blocked())

    def test_unknown_url_errors_are_plain_blocks(self) -> None:
        for name in ("403-forbidden", "404", "502"):
            path = f"{REPO}/issues/{name[:3]}"
            self.gh.fail(path, name)
            with self.assertRaises(gs.ReadBlocked) as caught:
                self.client().json(path)
            self.assertNotIsInstance(caught.exception, gs.AuthLost)

    def test_401_is_auth_loss(self) -> None:
        self.gh.fail(f"{REPO}/issues/1", "401")
        with self.assertRaises(gs.AuthLost):
            self.client().json(f"{REPO}/issues/1")


class SlowGitHub:
    """FakeGitHub on a fake clock. A read slower than its timeout hangs until
    the timeout and fails, as gh_http does."""

    def __init__(self, gh: FakeGitHub, delays: dict[str, float], default: float):
        self.gh, self.delays, self.default = gh, delays, default
        self.now = 0.0
        self.timeouts: list[tuple[str, float]] = []

    def clock(self) -> float:
        return self.now

    def __call__(self, url: str, etag: str | None, timeout: float) -> gs.Response:
        self.timeouts.append((gs.path_of(url), timeout))
        delay = self.delays.get(gs.path_of(url), self.default)
        if delay > timeout:
            self.now += timeout
            raise gs.ReadBlocked(f"GitHub read timed out: {gs.path_of(url)}")
        self.now += delay
        return self.gh(url, etag, timeout)


class FreshDeadline(Env):
    def reader(self, slow: SlowGitHub, seconds: float) -> gs.FreshReader:
        return gs.FreshReader("write-check", seconds, http=slow, clock=slow.clock)

    def test_login_read_counts_in_the_deadline(self) -> None:
        slow = SlowGitHub(self.gh, {"user": 25}, 1)
        reader = self.reader(slow, 60)
        self.assertEqual(slow.timeouts, [("user", 30)])
        self.assertEqual(reader.client.deadline - slow.now, 35)

    def test_login_read_never_waits_longer_than_the_deadline(self) -> None:
        slow = SlowGitHub(self.gh, {"user": 100}, 1)
        with self.assertRaisesRegex(gs.ReadBlocked, "timed out"):
            self.reader(slow, 20)
        self.assertEqual((slow.timeouts, slow.now), ([("user", 20)], 20))

    def test_login_read_that_uses_the_whole_deadline_blocks(self) -> None:
        slow = SlowGitHub(self.gh, {"user": 10}, 1)
        with self.assertRaisesRegex(gs.ReadBlocked, "login read took too long"):
            self.reader(slow, 10)

    def test_slow_login_and_reads_end_before_the_hook_timeout(self) -> None:
        # Worst case: the login read almost times out, then every read hangs.
        action = self.root / "action.json"
        action.write_text(json.dumps({"action": "fix", "pr": 5, "sha": A}))
        seconds = gs.settings().recheck
        slow = SlowGitHub(self.gh, {"user": 29}, 1000)
        with patch.dict(os.environ, {"EPIC_ACTION_FILE": str(action)}):
            refused = wc.guard(
                "Bash",
                {"command": "git push origin feat/5-x"},
                str(self.root),
                lambda: self.reader(slow, seconds),
            )
        self.assertIn("fresh GitHub read failed", refused or "")
        self.assertLessEqual(slow.now, seconds)
        self.assertLessEqual(slow.now + gs.HOOK_MARGIN_SECONDS, gs.HOOK_TIMEOUT_SECONDS)


class Config(Env):
    def test_defaults(self) -> None:
        self.assertEqual(gs.settings(), gs.Settings(120, 50, 45, 60))

    def test_invalid_values_are_config_errors(self) -> None:
        for value in ("0", "-1", "abc", "1.5"):
            with patch.dict(os.environ, {"EPIC_SNAPSHOT_MAX_AGE_SECONDS": value}):
                with self.assertRaises(gs.ConfigError):
                    gs.settings()

    def test_recheck_must_end_before_the_hook_timeout(self) -> None:
        with patch.dict(os.environ, {"EPIC_RECHECK_TIMEOUT_SECONDS": "80"}):
            self.assertEqual(gs.settings().recheck, 80)
        with patch.dict(os.environ, {"EPIC_RECHECK_TIMEOUT_SECONDS": "81"}):
            with self.assertRaisesRegex(gs.ConfigError, "hook timeout"):
                gs.settings()

    def test_hook_timeouts_match_the_check_limit(self) -> None:
        root = HERE.parents[1]
        claude = json.loads((root / ".claude/settings.json").read_text())
        codex = json.loads((root / ".codex/hooks.json").read_text())
        timeouts = [
            hook["timeout"]
            for config in (claude, codex)
            for group in config["hooks"]["PreToolUse"]
            for hook in group["hooks"]
            if "epic-guard" in hook["command"]
        ]
        self.assertGreaterEqual(len(timeouts), 3)
        self.assertEqual(set(timeouts), {gs.HOOK_TIMEOUT_SECONDS})

    def test_lock_and_refresh_must_fit_the_select_timeout(self) -> None:
        with patch.dict(os.environ, {"EPIC_SELECT_TIMEOUT_SECONDS": "95"}):
            with self.assertRaisesRegex(gs.ConfigError, "below"):
                gs.settings()

    def test_missing_or_empty_auth_context_is_a_config_error(self) -> None:
        context = self.cache / "github-cache" / "auth-context"
        context.write_text("  \n")
        with self.assertRaisesRegex(gs.ConfigError, "auth-context"):
            self.ns()
        context.unlink()
        with self.assertRaises(gs.ConfigError):
            self.ns()

    def test_key_changes_with_auth_context_and_login(self) -> None:
        first = self.ns()
        (self.cache / "github-cache" / "auth-context").write_text("test-context-2")
        second = self.ns()
        self.assertNotEqual(first.dir, second.dir)
        gs._LOGINS.clear()
        self.gh.set("user", {"login": "someone-else"})
        self.assertNotEqual(second.dir, self.ns().dir)
        self.assertNotIn("token", json.dumps(second.key).lower())

    def test_one_shared_root_resolver(self) -> None:
        self.assertEqual(
            gs.shared_root({"EPIC_QUOTA_DIR": "/q", "EPIC_STATE_DIR": "/s"}), Path("/q")
        )
        self.assertEqual(gs.shared_root({"EPIC_STATE_DIR": "/s"}), Path("/s"))
        self.assertEqual(
            gs.shared_root({"EPIC_CACHE_DIR": "/c", "EPIC_QUOTA_DIR": "/q"}), Path("/c")
        )


def small_state(tag: str = "one") -> dict[str, Any]:
    return {
        "prs": [],
        "items": [],
        "linked_labels": {},
        "merged_prs": [{"number": 1, "body": tag}],
        "batch_order": [],
        "focus_issues": [],
        "waiting": {},
    }


class Snapshots(Env):
    def setUp(self) -> None:
        super().setUp()
        self.builds = 0

    def build(self, tag: str = "one", fail: bool = False) -> Any:
        def run(client: gs.Client, focus: set[str]) -> dict[str, Any]:
            self.builds += 1
            if fail:
                raise gs.ReadBlocked("page 2 failed")
            return small_state(tag)

        return run

    def read(self, **kw: Any) -> gs.Snapshot:
        kw.setdefault("build", self.build())
        return gs.read_snapshot(http=self.gh, **kw)

    def test_refresh_then_cache_within_max_age(self) -> None:
        first = self.read()
        second = self.read()
        self.assertEqual((first.source, second.source), ("refresh", "cache"))
        self.assertEqual(self.builds, 1)
        self.assertEqual(second.state["merged_prs"][0]["body"], "one")

    def test_expired_snapshot_is_refreshed(self) -> None:
        self.read()
        later = time.time() + 121
        got = self.read(now=lambda: later, build=self.build("two"))
        self.assertEqual(
            (got.source, got.state["merged_prs"][0]["body"]), ("refresh", "two")
        )

    def test_failed_refresh_keeps_the_old_snapshot_and_time(self) -> None:
        self.read()
        path = self.ns().dir / "snapshot.json"
        before = path.read_bytes()
        later = time.time() + 121
        with self.assertRaises(gs.ReadBlocked):
            self.read(now=lambda: later, build=self.build(fail=True))
        self.assertEqual(path.read_bytes(), before)
        # Still expired: no action from it.
        with self.assertRaises(gs.ReadBlocked):
            self.read(now=lambda: later, build=self.build(fail=True))

    def test_corrupt_or_incompatible_snapshot_needs_a_rebuild(self) -> None:
        self.read()
        path = self.ns().dir / "snapshot.json"
        good = json.loads(path.read_text())
        for bad in (
            "{not json",
            json.dumps({**good, "schema": 99}),
            json.dumps({**good, "digest": "0" * 64}),
            json.dumps({**good, "key": {**good["key"], "repo": "x/y"}}),
            json.dumps({**good, "state": {**good["state"], "prs": [{"number": 1}]}}),
        ):
            path.write_text(bad)
            with self.assertRaises(gs.ReadBlocked):
                self.read(build=self.build(fail=True))
            self.assertEqual(self.read(build=self.build("new")).source, "refresh")

    def test_publication_is_atomic(self) -> None:
        self.read()
        path = self.ns().dir / "snapshot.json"
        before = path.read_bytes()
        later = time.time() + 121
        with patch.object(gs.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.read(now=lambda: later, build=self.build("two"))
        self.assertEqual(path.read_bytes(), before)
        leftovers = [p.name for p in path.parent.iterdir() if p.name != "snapshot.json"]
        self.assertNotIn(True, [n.startswith(".snapshot") for n in leftovers])

    def test_freshness_is_checked_again_under_the_lock(self) -> None:
        ns = self.ns()
        ns.dir.mkdir(parents=True, exist_ok=True)
        holding = threading.Event()

        def other_refresher() -> None:
            with (ns.dir / "refresh.lock").open("a") as handle:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                holding.set()
                time.sleep(0.5)
                gs.publish(ns, small_state("other"), time.time(), set())
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

        thread = threading.Thread(target=other_refresher)
        thread.start()
        holding.wait(5)
        got = self.read()
        thread.join()
        self.assertEqual((got.source, self.builds), ("cache", 0))
        self.assertEqual(got.state["merged_prs"][0]["body"], "other")

    def test_lock_timeout_blocks(self) -> None:
        ns = self.ns()
        ns.dir.mkdir(parents=True, exist_ok=True)
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import fcntl, sys, time\n"
                "f = open(sys.argv[1], 'a'); fcntl.flock(f, fcntl.LOCK_EX)\n"
                "print('locked', flush=True); time.sleep(30)\n",
                str(ns.dir / "refresh.lock"),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(holder.kill)
        assert holder.stdout is not None
        self.addCleanup(holder.stdout.close)
        self.assertEqual(holder.stdout.readline().strip(), "locked")
        with patch.dict(os.environ, {"EPIC_SNAPSHOT_LOCK_SECONDS": "1"}):
            with self.assertRaisesRegex(gs.ReadBlocked, "lock busy"):
                self.read()
            # A killed refresher frees the lock; the next reader refreshes.
            holder.send_signal(signal.SIGKILL)
            holder.wait()
            self.assertEqual(self.read().source, "refresh")

    def test_auth_marker_blocks_the_cache_until_a_refresh(self) -> None:
        self.read()
        ns = self.ns()
        ns.block_auth("401", time.time() - 1)
        got = self.read(build=self.build("after"))
        self.assertEqual(got.source, "refresh")
        self.assertFalse(ns.auth_blocked())
        # A marker newer than the refresh start stays, and the read is refused.
        later = time.time() + 121
        ns.block_auth("401", later + 5)
        with self.assertRaises(gs.ReadBlocked):
            self.read(now=lambda: later, build=self.build("x"))
        self.assertTrue(ns.auth_blocked())

    def test_access_loss_during_the_refresh_refuses_its_result(self) -> None:
        # Codex review P2 on https://github.com/phaabe/live.moafunk.de/pull/511.
        self.read()
        ns = self.ns()
        path = ns.dir / "snapshot.json"
        before = path.read_bytes()
        later = time.time() + 121

        def build(client: gs.Client, focus: set[str]) -> dict[str, Any]:
            ns.block_auth("403 on a known URL", later + 1)
            return small_state("during loss")

        with self.assertRaisesRegex(gs.ReadBlocked, "access loss"):
            self.read(now=lambda: later, build=build)
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(ns.auth_blocked())
        # The next refresh that starts after the marker clears it.
        after = later + 10
        got = self.read(now=lambda: after, build=self.build("ok"))
        self.assertEqual(got.source, "refresh")
        self.assertFalse(ns.auth_blocked())

    def test_refresh_older_than_the_max_age_is_refused(self) -> None:
        # Codex review P2 on https://github.com/phaabe/live.moafunk.de/pull/511.
        clock = [time.time()]

        def slow(client: gs.Client, focus: set[str]) -> dict[str, Any]:
            clock[0] += 2
            return small_state("slow")

        with patch.dict(os.environ, {"EPIC_SNAPSHOT_MAX_AGE_SECONDS": "1"}):
            with self.assertRaisesRegex(gs.ReadBlocked, "longer than the max age"):
                self.read(now=lambda: clock[0], build=slow)
        self.assertFalse((self.ns().dir / "snapshot.json").exists())

    def test_focus_labels_are_part_of_the_snapshot(self) -> None:
        def build(client: gs.Client, focus: set[str]) -> dict[str, Any]:
            self.builds += 1
            state = small_state()
            state["focus_issues"] = (
                [{"number": 7, "labels": sorted(focus), "url": f"{ISSUES}/7"}]
                if focus
                else []
            )
            return state

        na.FOCUS_FILE.write_text("project::A\n")
        monitor = self.read(build=build)  # no focus of its own
        self.assertEqual(monitor.state["focus_issues"], [])
        runner = self.read(focus=frozenset({"project::A"}), build=build)
        self.assertEqual((runner.source, self.builds), ("cache", 1))
        self.assertEqual(runner.state["focus_issues"][0]["number"], 7)
        self.read(focus=frozenset({"project::B"}), build=build)
        self.assertEqual(self.builds, 2)

    def test_concurrent_readers_share_one_refresh(self) -> None:
        counter = self.root / "builds"
        script = textwrap.dedent(
            f"""
            import sys, time
            sys.path.insert(0, {str(HERE)!r})
            import github_state as gs
            from test_github_state import FakeGitHub, small_state
            def build(client, focus):
                with open({str(counter)!r}, "a") as f:
                    f.write("x")
                time.sleep(0.5)
                return small_state()
            print(gs.read_snapshot(http=FakeGitHub(), build=build).source)
            """
        )
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", script], stdout=subprocess.PIPE, text=True
            )
            for _ in range(6)
        ]
        sources = [p.communicate(timeout=60)[0].strip() for p in procs]
        self.assertEqual(counter.read_text(), "x")
        self.assertEqual(sorted(sources), ["cache"] * 5 + ["refresh"])


def pull(n: int, head: str, body: str, **kw: Any) -> dict[str, Any]:
    data = {
        "id": 10_000 + n,
        "number": n,
        "title": f"PR {n}",
        "body": body,
        "state": "open",
        "draft": False,
        "mergeable": True,
        "labels": [],
        "comments": 0,
        "updated_at": "2026-09-29T10:00:00Z",
        "merged_at": None,
        "base": {"ref": "dev/312-interim"},
        "head": {"ref": f"feat/{n}-x", "sha": head},
    }
    data.update(kw)
    return data


def comment(
    cid: int, body: str, at: str, edited_at: str | None = None
) -> dict[str, Any]:
    return {
        "id": cid,
        "node_id": f"IC_{cid}",
        "body": body,
        "created_at": at,
        "updated_at": edited_at or at,
        "html_url": f"{ISSUES}/1#issuecomment-{cid}",
        "user": {"login": "anneoneone"},
    }


class GitHubRepo:
    """Serves the REST reads build_state() makes, on a FakeGitHub."""

    def __init__(self, gh: FakeGitHub):
        self.gh = gh
        self.open: dict[str, list[dict[str, Any]]] = {b: [] for b in na.BASES}
        self.closed: dict[str, list[dict[str, Any]]] = {b: [] for b in na.BASES}
        self.items: list[dict[str, Any]] = []
        self.epic: list[dict[str, Any]] = []
        self.publish()

    def add_pr(
        self,
        data: dict[str, Any],
        comments: list[dict[str, Any]] = (),  # type: ignore[assignment]
        runs: list[list[dict[str, Any]]] | None = None,
        statuses: list[list[dict[str, Any]]] | None = None,
    ) -> None:
        n, sha = data["number"], data["head"]["sha"]
        data = {**data, "comments": len(comments)}
        self.open[data["base"]["ref"]].append(data)
        self.gh.set(f"{REPO}/pulls/{n}", data)
        self.gh.set(f"{REPO}/issues/{n}/comments?per_page=100", list(comments))
        runs = runs or [
            [
                {
                    "id": 1,
                    "name": "ci",
                    "status": "completed",
                    "conclusion": "success",
                    "app": {"id": 1},
                }
            ]
        ]
        total = sum(len(p) for p in runs)
        self.gh.set_pages(
            f"{REPO}/commits/{sha}/check-runs?filter=all&per_page=100",
            [{"total_count": total, "check_runs": p} for p in runs],
        )
        statuses = statuses or [
            [{"id": 1, "context": "epic-guard", "state": "success"}]
        ]
        self.gh.set_pages(f"{REPO}/commits/{sha}/statuses?per_page=100", statuses)
        self.publish()

    def publish(self) -> None:
        for base in na.BASES:
            b = base.replace("/", "%2F")
            self.gh.set(
                f"{REPO}/pulls?state=open&base={b}&per_page=100", self.open[base]
            )
            self.gh.set(
                f"{REPO}/pulls?state=closed&base={b}&per_page=100", self.closed[base]
            )
        fields = [{"id": i, "name": name} for i, name in enumerate(na.PROJECT_FIELDS)]
        self.gh.set(f"{na.PROJECT_API}/fields?per_page=100", fields)
        query = "&".join(f"fields[]={i}" for i in range(len(na.PROJECT_FIELDS)))
        self.gh.set(f"{na.PROJECT_API}/items?per_page=100&{query}", self.items)
        self.gh.set(f"{REPO}/issues/{na.EPIC}/comments?per_page=100", self.epic)

    def add_item(
        self,
        n: int,
        status: str,
        executor: str,
        labels: tuple[str, ...] = (),
        readiness: str | list[str] = "**Ready**",
    ) -> None:
        def single(value: str) -> dict[str, Any]:
            return {"name": {"raw": value}}

        self.items.append(
            {
                "id": 50_000 + n,
                "node_id": f"PVTI_{n}",
                "content_type": "Issue",
                "content": {
                    "number": n,
                    "title": f"issue {n}",
                    "body": "",
                    "updated_at": "2026-09-29T09:00:00Z",
                    "html_url": f"{ISSUES}/{n}",
                },
                "fields": [
                    {"name": "Status", "value": single(status)},
                    {"name": "Executor", "value": single(executor)},
                    {"name": "Wave", "value": single("1")},
                    {"name": "Labels", "value": [{"name": x} for x in labels]},
                ],
            }
        )
        self.gh.set(
            f"{REPO}/issues/{n}/comments?per_page=100",
            [
                comment(900 + 10 * n + k, body, "2026-09-29T08:00:00Z")
                for k, body in enumerate(
                    [readiness] if isinstance(readiness, str) else readiness
                )
            ],
        )
        self.gh.set(
            f"{REPO}/issues/{n}",
            {"id": n, "number": n, "labels": [{"name": x} for x in labels]},
        )
        self.publish()


class BuildState(Env):
    def setUp(self) -> None:
        super().setUp()
        self.repo = GitHubRepo(self.gh)

    def build(self) -> dict[str, Any]:
        state = gs.build_state(self.client(), set())
        gs.validate_state(state)
        return state

    def test_rollup_uses_current_runs_and_latest_status_over_pages(self) -> None:
        body = f"Executor: Claude\nIssue: {ISSUES}/9"
        self.repo.add_item(9, "In progress", "Claude")
        verdict = comment(
            1, f"Review: APPROVED by Codex at {A}", "2026-09-29T11:00:00Z"
        )
        self.repo.add_pr(
            pull(5, A, body),
            [verdict],
            runs=[
                [
                    {
                        "id": 1,
                        "name": "ci",
                        "status": "completed",
                        "conclusion": "failure",
                        "app": {"id": 1},
                    }
                ],
                [
                    {
                        "id": 2,
                        "name": "ci",
                        "status": "completed",
                        "conclusion": "success",
                        "app": {"id": 1},
                    }
                ],
            ],
            statuses=[
                [{"id": 7, "context": "epic-guard", "state": "success"}],
                [{"id": 3, "context": "epic-guard", "state": "failure"}],
            ],
        )
        state = self.build()
        pr = state["prs"][0]
        self.assertEqual(na.checks_state(pr), "green")
        self.assertEqual(pr["mergeable"], "MERGEABLE")
        actions = na.decide("Claude", state)
        self.assertEqual(
            (actions[0].action, actions[0].pr, actions[0].sha), ("merge", 5, A)
        )

    def test_selector_rules_are_kept(self) -> None:
        body = "Executor: Claude"
        cases = [
            ({"mergeable": False}, None, None, "resolve-conflict"),
            ({"mergeable": None}, None, None, "merge"),  # UNKNOWN does not block
            (
                {},
                None,
                [[{"id": 9, "context": "epic-guard", "state": "failure"}]],
                "fix-checks",
            ),
            (
                {},
                None,
                [[{"id": 9, "context": "other", "state": "success"}]],
                None,
            ),  # guard missing
            ({}, [[]], None, "merge"),
        ]
        for extra, runs, statuses, expected in cases:
            with self.subTest(expected=expected, extra=extra):
                self.repo.open = {b: [] for b in na.BASES}
                verdict = comment(
                    1, f"Review: APPROVED by Codex at {A}", "2026-09-29T11:00:00Z"
                )
                if runs == [[]]:
                    runs_arg: Any = [
                        [
                            {
                                "id": 5,
                                "name": "epic-guard-runner",
                                "status": "completed",
                                "conclusion": "failure",
                                "app": {"id": 2},
                            }
                        ]
                    ]
                else:
                    runs_arg = runs
                self.repo.add_pr(
                    pull(5, A, body, **extra),
                    [verdict],
                    runs=runs_arg,
                    statuses=statuses,
                )
                got = [a.action for a in na.decide("Claude", self.build())]
                if expected is None:
                    self.assertNotIn("merge", got)
                else:
                    self.assertEqual(got[0], expected)

    def test_malformed_mergeable_blocks_the_snapshot(self) -> None:
        self.repo.add_pr(pull(5, A, "Executor: Claude", mergeable="dirty"))
        with self.assertRaises(gs.ReadBlocked):
            self.build()

    def test_snapshot_pr_without_mergeable_is_refused(self) -> None:
        self.repo.add_pr(pull(5, A, "Executor: Claude"))
        state = self.build()
        del state["prs"][0]["mergeable"]
        with self.assertRaises(gs.ReadBlocked):
            gs.validate_state(state)

    def test_merged_set_and_edited_merged_bodies(self) -> None:
        base = "dev/312-interim"
        self.repo.closed[base] = [
            {
                "id": 1,
                "number": 1,
                "body": "Leaf IDs: A1.1.1",
                "merged_at": "2026-09-01T00:00:00Z",
            },
            {"id": 2, "number": 2, "body": "Leaf IDs: A1.1.2", "merged_at": None},
        ]
        self.repo.publish()
        self.assertEqual(na.done_leaves(self.build()), {"A1.1.1"})
        self.repo.closed[base][0]["body"] = "Leaf IDs: A1.1.1, A1.1.3"
        self.repo.publish()
        self.assertEqual(na.done_leaves(self.build()), {"A1.1.1", "A1.1.3"})

    def test_claim_and_readiness(self) -> None:
        self.repo.add_item(
            21, "Ready", "Claude", readiness="**Ready:** Start after A9.9.9."
        )
        self.repo.add_item(22, "Ready", "Claude")
        actions = na.decide("Claude", self.build(), include_waiting=True)
        self.assertEqual(
            [(a.action, a.issue) for a in actions],
            [("claim", f"{ISSUES}/22"), ("wait", f"{ISSUES}/21")],
        )

    def test_only_readiness_comments_name_dependencies(self) -> None:
        review = "Review: the code follows the rule. Start after A8.8.8 is wrong."
        self.repo.add_item(21, "Ready", "Claude", readiness=["**Ready**", review])
        self.repo.add_item(
            22,
            "Ready",
            "Claude",
            readiness=[
                "**Ready, executor Claude:** Start after A9.9.9.",
                review,
                f"**Ready:** Start after {ISSUES}/30.",
            ],
        )
        items = {i["content"]["number"]: i for i in self.build()["items"]}
        self.assertEqual(na.start_after(items[21]), set())
        self.assertEqual(
            na.dependency_sources(items[22]),
            {
                "A9.9.9": [f"{ISSUES}/1#issuecomment-1120"],
                f"{ISSUES}/30": [f"{ISSUES}/1#issuecomment-1122"],
            },
        )

    def refinement_board(self) -> None:
        self.repo.add_item(21, "Ready", "Claude")
        self.repo.add_item(23, "Backlog", "Claude", labels=("refinement",))
        self.repo.add_item(24, "Todo", "Claude", labels=("refinement::review",))
        self.repo.add_item(25, "Backlog", "Claude")
        self.repo.add_item(26, "In progress", "Claude", labels=("refinement",))
        at = "2026-09-29T08:00:00Z"
        self.gh.set(
            f"{REPO}/issues/23/comments?per_page=100",
            [comment(1230, "**Ready**", at, edited_at="2026-09-29T09:00:00Z")],
        )

    def test_refinement_reads_enrolled_issues_and_the_exempt_list(self) -> None:
        self.refinement_board()
        exempt = f"Refinement exempt: accepted by Anton\n{ISSUES}/21"
        self.gh.set(
            f"{REPO}/issues/{na.EXEMPT_ISSUE}/comments?per_page=100",
            [comment(5510, exempt, "2026-09-29T08:00:00Z")],
        )
        with patch.dict(os.environ, {na.ACTIONS_ENV: "refine"}):
            state = self.build()
        items = {i["content"]["number"]: i for i in state["items"]}
        self.assertEqual(state["refinement_exempt"], [f"{ISSUES}/21"])
        self.assertEqual(items[21]["refinement_comments"][0]["id"], 1110)
        self.assertEqual(items[24]["refinement_comments"][0]["id"], 1140)
        # Comment IDs and the edited flag reach refinement.py.
        [edited] = items[23]["refinement_comments"]
        self.assertEqual((edited["id"], edited["includesCreatedEdit"]), (1230, True))
        # Not enrolled by a label, or past Ready: not read.
        self.assertNotIn("refinement_comments", items[25])
        self.assertNotIn("refinement_comments", items[26])

    def test_refinement_off_reads_only_ready_issues(self) -> None:
        self.refinement_board()
        with patch.dict(os.environ, {na.ACTIONS_ENV: "adopt"}):
            state = self.build()  # the exempt issue has no fake comments
        items = {i["content"]["number"]: i for i in state["items"]}
        self.assertEqual(state["refinement_exempt"], [])
        self.assertEqual(items[21]["readiness"], "**Ready**")
        for n in (21, 23, 24, 25):
            self.assertNotIn("refinement_comments", items[n])
        self.assertNotIn("readiness", items[23])

    def test_recheck_reads_its_own_issue_before_ready_only_to_refine(self) -> None:
        self.refinement_board()
        self.gh.set(f"{REPO}/issues/{na.EXEMPT_ISSUE}/comments?per_page=100", [])
        for actions, read in (("refine", True), ("", False)):
            with patch.dict(os.environ, {na.ACTIONS_ENV: actions}):
                state = gs.build_state(
                    self.client(), set(), pr_details=False, readiness_for={25}
                )
            items = {i["content"]["number"]: i for i in state["items"]}
            self.assertEqual("refinement_comments" in items[25], read, actions)
            self.assertNotIn("readiness", items[23])

    def test_linked_labels_off_the_board(self) -> None:
        self.gh.set(
            f"{REPO}/issues/77", {"id": 77, "labels": [{"name": "priority::high"}]}
        )
        self.repo.add_pr(pull(5, A, f"Executor: Claude\nIssue: {ISSUES}/77"))
        self.assertEqual(self.build()["linked_labels"], {"77": ["priority::high"]})

    def test_incomplete_comments_block(self) -> None:
        self.repo.add_pr(
            pull(5, A, "Executor: Claude"), [comment(1, "x", "2026-09-29T11:00:00Z")]
        )
        self.gh.set(
            f"{REPO}/pulls/5", {**pull(5, A, "Executor: Claude"), "comments": 2}
        )
        with self.assertRaisesRegex(gs.ReadBlocked, "incomplete"):
            self.build()

    def test_refresh_is_rest_only(self) -> None:
        self.repo.add_pr(pull(5, A, "Executor: Claude"))
        with patch.object(gs, "run_gh", side_effect=AssertionError("GraphQL used")):
            gs.read_snapshot(http=self.gh)
        self.assertTrue(all(u.startswith(gs.API) for u, _ in self.gh.calls))


class FakeReader(gs.FreshReader):
    """A FreshReader on the fake GitHub, with canned GraphQL edit evidence."""

    def __init__(self, gh: FakeGitHub, edited: set[int] = frozenset()):  # type: ignore[assignment]
        super().__init__("recheck", 30, http=gh, root=HERE.parents[1])
        self.edited = edited
        self.graphql_calls = 0

    def graphql(self, endpoint: str) -> Any:
        self.graphql_calls += 1
        n = int(endpoint.split("pullRequest%28number%3A+")[1].split("%29")[0])
        rows = json.loads(self.gh_body(f"{REPO}/issues/{n}/comments?per_page=100"))
        nodes = [
            {
                "databaseId": r["id"],
                "lastEditedAt": r["created_at"] if r["id"] in self.edited else None,
                "body": r["body"],
                "createdAt": r["created_at"],
                "updatedAt": r["updated_at"],
            }
            for r in rows
        ]
        return {
            "data": {
                "repository": {
                    "pullRequest": {
                        "comments": {
                            "totalCount": len(nodes),
                            "nodes": nodes,
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        }

    def gh_body(self, path: str) -> str:
        http = self.client.http
        assert isinstance(http, FakeGitHub)
        return http.pages[gs.full_url(path)][0]


class Recheck(Env):
    def setUp(self) -> None:
        super().setUp()
        self.repo = GitHubRepo(self.gh)

    def recheck(
        self,
        action: dict[str, Any],
        edited: set[int] = frozenset(),
        paused: bool = False,
    ) -> str | None:  # type: ignore[assignment]
        reader = FakeReader(self.gh, edited)
        self.reader = reader
        return gs.recheck("Claude", action, frozenset(), frozenset(), paused, reader)

    def merge_pr(self) -> dict[str, Any]:
        verdict = comment(
            1, f"Review: APPROVED by Codex at {A}", "2026-09-29T11:00:00Z"
        )
        self.repo.add_pr(pull(5, A, "Executor: Claude"), [verdict])
        return {"action": "merge", "reason": "r", "pr": 5, "sha": A}

    def test_valid_merge_stays_valid(self) -> None:
        self.assertIsNone(self.recheck(self.merge_pr()))
        self.assertEqual(self.reader.graphql_calls, 1)

    def test_same_second_verdict_edit_makes_it_stale(self) -> None:
        # REST timestamps are equal; only lastEditedAt shows the edit.
        self.assertIn("GitHub changed", self.recheck(self.merge_pr(), edited={1}) or "")

    def test_head_moved_makes_it_stale(self) -> None:
        action = self.merge_pr()
        self.gh.set(
            f"{REPO}/pulls/5", {**pull(5, B, "Executor: Claude"), "comments": 1}
        )
        self.gh.set_pages(
            f"{REPO}/commits/{B}/check-runs?filter=all&per_page=100",
            [{"total_count": 0, "check_runs": []}],
        )
        self.gh.set(f"{REPO}/commits/{B}/statuses?per_page=100", [])
        self.assertIsNotNone(self.recheck(action))

    def test_closed_pr_is_stale(self) -> None:
        action = self.merge_pr()
        self.gh.set(
            f"{REPO}/pulls/5",
            {**pull(5, A, "Executor: Claude"), "state": "closed", "comments": 1},
        )
        self.assertEqual(self.recheck(action), "PR 5 is closed")

    def test_review_needs_no_graphql(self) -> None:
        self.repo.add_pr(pull(6, A, "Executor: Codex"))
        action = {"action": "review", "reason": "r", "pr": 6, "sha": A}
        self.assertIsNone(self.recheck(action))
        self.assertEqual(self.reader.graphql_calls, 0)

    def test_pause_makes_it_stale(self) -> None:
        self.assertIsNotNone(self.recheck(self.merge_pr(), paused=True))

    def test_claim_valid_then_readiness_changes(self) -> None:
        self.repo.add_item(21, "Ready", "Claude")
        action = {"action": "claim", "reason": "r", "issue": f"{ISSUES}/21"}
        self.assertIsNone(self.recheck(action))
        self.repo.items.clear()
        self.repo.add_item(21, "Backlog", "Claude")
        self.assertIsNotNone(self.recheck(action))
        self.repo.items.clear()
        self.repo.add_item(
            21, "Ready", "Claude", readiness="**Ready:** Start after A9.9.9."
        )
        self.assertIn("idle", self.recheck(action) or "")  # blocked by Start after

    def test_claim_ignores_a_dependency_outside_readiness(self) -> None:
        review = "Looks fine. Start after A9.9.9."
        self.repo.add_item(21, "Ready", "Claude", readiness=["**Ready**", review])
        action = {"action": "claim", "reason": "r", "issue": f"{ISSUES}/21"}
        self.assertIsNone(self.recheck(action))

    def test_claim_without_free_slot_is_stale(self) -> None:
        self.repo.add_item(21, "Ready", "Claude")
        self.repo.add_pr(pull(5, A, "Executor: Claude"))
        self.repo.add_pr(pull(6, A, "Executor: Claude"))
        action = {"action": "claim", "reason": "r", "issue": f"{ISSUES}/21"}
        self.assertIsNotNone(self.recheck(action))

    def test_same_action_compares_digest_and_attempt_key(self) -> None:
        selected = {"action": "claim", "reason": "r", "issue": f"{ISSUES}/21"}
        now = na.Action("claim", "r", issue=f"{ISSUES}/21", digest="a" * 64)
        self.assertTrue(gs.same_action(now, {**selected, "digest": "a" * 64}))
        # A new approved proposal is other work than the one selected.
        self.assertFalse(gs.same_action(now, {**selected, "digest": "b" * 64}))
        self.assertFalse(gs.same_action(now, selected))
        refine = na.Action(
            "refine", "r", issue=f"{ISSUES}/21", attempt_key="refine:21:7:none"
        )
        self.assertFalse(
            gs.same_action(
                refine,
                {
                    "action": "refine",
                    "issue": f"{ISSUES}/21",
                    "attempt_key": "refine:21:0:none",
                },
            )
        )

    def test_failed_read_blocks(self) -> None:
        action = self.merge_pr()
        self.gh.fail(f"{REPO}/pulls/5", "502")
        with self.assertRaises(gs.ReadBlocked):
            self.recheck(action)

    def review_pr(self, **kw: Any) -> dict[str, Any]:
        self.repo.add_pr(pull(6, A, "Executor: Codex", **kw))
        return {"action": "review", "reason": "r", "pr": 6, "sha": A}

    def test_conflict_after_selection_makes_a_review_stale(self) -> None:
        action = self.review_pr()
        self.gh.set(f"{REPO}/pulls/6", pull(6, A, "Executor: Codex", mergeable=False))
        # Selector: no review on a conflicted head (status shows a wait).
        self.assertIn("gives idle", self.recheck(action) or "")

    def test_mergeability_still_computing_keeps_a_review(self) -> None:
        self.assertIsNone(self.recheck(self.review_pr(mergeable=None)))

    def test_cleared_conflict_makes_resolve_conflict_stale(self) -> None:
        self.repo.add_pr(pull(5, A, "Executor: Claude", mergeable=False))
        action = {"action": "resolve-conflict", "reason": "r", "pr": 5, "sha": A}
        self.assertIsNone(self.recheck(action))
        self.gh.set(f"{REPO}/pulls/5", pull(5, A, "Executor: Claude", mergeable=True))
        self.assertIsNotNone(self.recheck(action))

    def test_missing_or_malformed_mergeable_blocks(self) -> None:
        action = self.review_pr()
        broken = pull(6, A, "Executor: Codex")
        del broken["mergeable"]
        for data in (
            broken,
            {**broken, "mergeable": "dirty"},
            {**broken, "mergeable": 0},
        ):
            with self.subTest(mergeable=data.get("mergeable", "missing")):
                self.gh.set(f"{REPO}/pulls/6", data)
                with self.assertRaises(gs.ReadBlocked):
                    self.recheck(action)


class SharedReaderSwitch(Env):
    """One resolver (enabled), explicit child values (child_enabled), and the
    runner's `github_state.py resolve`. Both defaults, no runner touched."""

    def default(self, value: bool) -> Any:
        return patch.object(gs, "DEFAULT_ENABLED", value)

    def test_unset_and_empty_mean_the_default(self) -> None:
        self.assertIs(gs.DEFAULT_ENABLED, False)
        for default in (False, True):
            with self.default(default):
                self.assertIs(gs.enabled({}), default)
                self.assertIs(gs.enabled({"EPIC_SHARED_READER": ""}), default)
                self.assertIs(gs.enabled({"EPIC_SHARED_READER": "0"}), False)
                self.assertIs(gs.enabled({"EPIC_SHARED_READER": "1"}), True)

    def test_other_values_are_config_errors(self) -> None:
        for value in ("2", "true", "yes", " 1", "1 ", "00", "on"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(gs.ConfigError, "EPIC_SHARED_READER"):
                    gs.enabled({"EPIC_SHARED_READER": value})
                with self.assertRaisesRegex(gs.ConfigError, "EPIC_SHARED_READER"):
                    gs.child_enabled({"EPIC_SHARED_READER": value})

    def test_children_never_apply_the_default(self) -> None:
        for default in (False, True):
            with self.default(default):
                for env in ({}, {"EPIC_SHARED_READER": ""}):
                    with self.assertRaisesRegex(gs.ConfigError, "runner child"):
                        gs.child_enabled(env)
                self.assertIs(gs.child_enabled({"EPIC_SHARED_READER": "0"}), False)
                self.assertIs(gs.child_enabled({"EPIC_SHARED_READER": "1"}), True)

    def test_resolve_gives_the_switch_and_the_recheck_budget(self) -> None:
        self.assertEqual(gs.resolve({}), (0, 0))
        # Off reads no timing setting, as before.
        self.assertEqual(gs.resolve({"EPIC_RECHECK_TIMEOUT_SECONDS": "x"}), (0, 0))
        self.assertEqual(gs.resolve({"EPIC_SHARED_READER": "1"}), (1, 60))
        on = {"EPIC_SHARED_READER": "1", "EPIC_RECHECK_TIMEOUT_SECONDS": "25"}
        self.assertEqual(gs.resolve(on), (1, 25))
        with self.default(True):
            self.assertEqual(gs.resolve({}), (1, 60))
            self.assertEqual(gs.resolve({"EPIC_SHARED_READER": "0"}), (0, 0))
            with self.assertRaises(gs.ConfigError):
                gs.resolve({"EPIC_RECHECK_TIMEOUT_SECONDS": "0"})

    def run_resolve(self, **env: str) -> subprocess.CompletedProcess[str]:
        clean = {k: v for k, v in os.environ.items() if not k.startswith("EPIC_")}
        return subprocess.run(
            [sys.executable, str(HERE / "github_state.py"), "resolve"],
            env={**clean, **env},
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_resolve_command(self) -> None:
        self.assertEqual(self.run_resolve().stdout, "0 0\n")
        self.assertEqual(self.run_resolve(EPIC_SHARED_READER="").stdout, "0 0\n")
        self.assertEqual(self.run_resolve(EPIC_SHARED_READER="1").stdout, "1 60\n")
        done = self.run_resolve(
            EPIC_SHARED_READER="1", EPIC_RECHECK_TIMEOUT_SECONDS="25"
        )
        self.assertEqual(done.stdout, "1 25\n")
        for env in (
            {"EPIC_SHARED_READER": "yes"},
            {"EPIC_SHARED_READER": "1", "EPIC_RECHECK_TIMEOUT_SECONDS": "0"},
        ):
            with self.subTest(env=env):
                done = self.run_resolve(**env)
                self.assertEqual((done.returncode, done.stdout), (2, ""))
                self.assertIn("config:", done.stderr)


class NextActionCli(Env):
    """next_action.py exit codes with the shared reader."""

    def setUp(self) -> None:
        super().setUp()
        # The quota wait dir is fixed at import; never read the real one.
        p = patch.object(na, "quota_check", return_value=(0, None))
        p.start()
        self.addCleanup(p.stop)

    def main(self, *args: str) -> tuple[int, str, str]:
        import contextlib
        import io

        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["next_action.py", *args]):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = na.main()
        return code, out.getvalue(), err.getvalue()

    def test_blocked_read_exits_5_with_nothing_on_stdout(self) -> None:
        with patch.object(gs, "read_snapshot", side_effect=gs.ReadBlocked("lock busy")):
            code, out, err = self.main("--agent", "claude")
        self.assertEqual((code, out), (5, ""))
        self.assertIn("lock busy", err)

    def test_bad_settings_exit_2(self) -> None:
        with patch.dict(os.environ, {"EPIC_SNAPSHOT_LOCK_SECONDS": "zero"}):
            code, out, _ = self.main("--agent", "claude")
        self.assertEqual((code, out), (2, ""))
        (self.cache / "github-cache" / "auth-context").unlink()
        with patch.object(na, "run_gh", side_effect=AssertionError("no gh")):
            code, out, err = self.main("--status")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("auth-context", err)

    def test_snapshot_feeds_the_selector(self) -> None:
        snap = gs.Snapshot(small_state(), "2026-09-29T10:00:00Z", 3, "cache")
        with patch.object(gs, "read_snapshot", return_value=snap):
            code, out, _ = self.main("--agent", "claude")
        self.assertEqual((code, json.loads(out)["action"]), (0, "idle"))

    def reads(self, env: dict[str, str], default: bool, call: Any) -> str:
        """Which read `call` used: "shared", "old" or "config"."""
        snap = gs.Snapshot(small_state(), "2026-09-29T10:00:00Z", 3, "cache")
        with (
            patch.dict(os.environ, env),
            patch.object(gs, "DEFAULT_ENABLED", default),
            patch.object(gs, "read_snapshot", return_value=snap),
            patch.object(na, "gh_json", side_effect=RuntimeError("old path")),
        ):
            try:
                call()
            except RuntimeError:
                return "old"
            except gs.ConfigError:
                return "config"
        return "shared"

    def test_selection_and_monitor_share_the_default(self) -> None:
        import contextlib
        import io

        import monitor

        def selector() -> None:
            code, _, _ = self.main("--agent", "claude")
            if code == 2:
                raise gs.ConfigError("exit 2")

        def fetch_state() -> None:
            with patch.object(sys, "argv", ["monitor.py", "--fetch-state"]):
                with (
                    contextlib.redirect_stdout(io.StringIO()),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    try:
                        monitor.main()
                    except SystemExit as error:
                        if error.code == 2:
                            raise gs.ConfigError("exit 2") from error
                        raise

        cases = (
            ({"EPIC_SHARED_READER": ""}, False, "old"),
            ({"EPIC_SHARED_READER": ""}, True, "shared"),
            ({"EPIC_SHARED_READER": "0"}, True, "old"),
            ({"EPIC_SHARED_READER": "1"}, False, "shared"),
            ({"EPIC_SHARED_READER": "maybe"}, False, "config"),
        )
        for name, call in (("selector", selector), ("monitor", fetch_state)):
            for env, default, expected in cases:
                with self.subTest(path=name, env=env, default=default):
                    self.assertEqual(self.reads(env, default, call), expected)
        with patch.object(gs, "DEFAULT_ENABLED", True):
            os.environ.pop("EPIC_SHARED_READER")
            self.assertEqual(self.reads({}, True, selector), "shared")

    def test_invalid_switch_exits_2_before_any_read(self) -> None:
        with (
            patch.dict(os.environ, {"EPIC_SHARED_READER": "yes"}),
            patch.object(gs, "read_snapshot", side_effect=AssertionError("read")),
            patch.object(na, "gh_json", side_effect=AssertionError("read")),
        ):
            for args in (("--agent", "claude"), ("--status",)):
                code, out, err = self.main(*args)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("EPIC_SHARED_READER", err)

    def test_switch_off_keeps_the_old_reads(self) -> None:
        with patch.dict(os.environ, {"EPIC_SHARED_READER": ""}):
            with patch.object(
                gs, "read_snapshot", side_effect=AssertionError("shared")
            ):
                with patch.object(na, "gh_json", side_effect=RuntimeError("old path")):
                    with self.assertRaisesRegex(RuntimeError, "old path"):
                        na.fetch_state()

    def recheck(self, **kw: Any) -> tuple[int, str, str]:
        action = self.root / "action.json"
        action.write_text(json.dumps({"action": "merge", "pr": 5, "sha": A}))
        with patch.object(gs, "FreshReader", return_value=object()):
            with patch.object(gs, "recheck", **kw):
                return self.main("--agent", "claude", "--recheck", str(action))

    def test_recheck_exit_codes(self) -> None:
        self.assertEqual(self.recheck(return_value=None)[0], 0)
        code, out, err = self.recheck(return_value="PR 5 is closed")
        self.assertEqual((code, out), (6, ""))
        self.assertIn("PR 5 is closed", err)
        self.assertEqual(self.recheck(side_effect=gs.ReadBlocked("HTTP 502"))[0], 5)
        with patch.object(na, "stop_on_quota", return_value=4):
            quota = na.QuotaExhausted("GraphQL")
            self.assertEqual(self.recheck(side_effect=quota)[0], 4)

    def test_recheck_needs_an_explicit_child_setting(self) -> None:
        # The recheck runs only in a runner child: no default, no read.
        for env in ({}, {"EPIC_SHARED_READER": ""}, {"EPIC_SHARED_READER": "yes"}):
            with self.subTest(env=env), patch.dict(os.environ, env):
                if not env:
                    os.environ.pop("EPIC_SHARED_READER")
                with patch.object(gs, "DEFAULT_ENABLED", True):
                    code, out, err = self.recheck(side_effect=AssertionError("read"))
                self.assertEqual((code, out), (2, ""))
                self.assertIn("config: EPIC_SHARED_READER", err)
        with patch.object(gs, "FreshReader", side_effect=AssertionError("read")):
            with patch.dict(os.environ, {"EPIC_SHARED_READER": "no"}):
                self.assertEqual(self.main("--agent", "claude", "--recheck", "x")[0], 2)
        for value in ("0", "1"):
            with patch.dict(os.environ, {"EPIC_SHARED_READER": value}):
                self.assertEqual(self.recheck(return_value=None)[0], 0)

    def run_monitor(self, **env: str) -> subprocess.CompletedProcess[str]:
        clean = {k: v for k, v in os.environ.items() if not k.startswith("EPIC_")}
        return subprocess.run(
            [sys.executable, str(HERE / "monitor.py"), "--fetch-state"],
            env={**clean, "EPIC_CACHE_DIR": str(self.cache), **env},
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_monitor_fetch_state_exits_2_on_bad_settings(self) -> None:
        for env in (
            {"EPIC_SHARED_READER": "yes"},
            {"EPIC_SHARED_READER": "1", "EPIC_SNAPSHOT_LOCK_SECONDS": "zero"},
        ):
            with self.subTest(env=env):
                done = self.run_monitor(**env)
                self.assertEqual((done.returncode, done.stdout), (2, ""))
                self.assertIn("config: EPIC_", done.stderr)
                self.assertNotIn("Traceback", done.stderr)

    def test_recheck_needs_an_agent(self) -> None:
        with self.assertRaises(SystemExit):
            self.main("--status", "--recheck", "x.json")


class StatusTarget(Env):
    """The IDs set-ready may write, read from the board (REST)."""

    def fields(self, *options: str) -> None:
        status = {
            "id": 1,
            "node_id": "PVTSSF_status",
            "name": "Status",
            "options": [{"id": f"opt-{o}", "name": {"raw": o}} for o in options],
        }
        executor = {"id": 2, "node_id": "PVTSSF_exec", "name": "Executor"}
        self.gh.set(f"{na.PROJECT_API}/fields?per_page=100", [executor, status])

    def test_reads_project_status_field_and_ready_option(self) -> None:
        self.gh.set(na.PROJECT_API, {"id": 2, "node_id": "PVT_board"})
        self.fields("Backlog", "Ready", "Done")
        self.assertEqual(
            gs.status_target(self.client()),
            {"project": "PVT_board", "field": "PVTSSF_status", "ready": "opt-Ready"},
        )

    def test_missing_or_ambiguous_ready_option_blocks(self) -> None:
        self.gh.set(na.PROJECT_API, {"id": 2, "node_id": "PVT_board"})
        for options in (("Backlog", "Done"), ("Ready", "Ready")):
            with self.subTest(options=options):
                self.fields(*options)
                with self.assertRaises(gs.ReadBlocked):
                    gs.status_target(self.client())


if __name__ == "__main__":
    unittest.main()
