"""Upstream test recovery (https://github.com/phaabe/live.moafunk.de/issues/725).

The selector rule for both agents, both readers (legacy gh and the shared
snapshot with its fresh recheck), the gate's typed exception, the fresh
admission in attempt-check, the pinned proof table and the worktree check.
The runner path through the real claude-tick.sh is in
test_upstream_recovery_runner.py.

Run: python3 -m unittest discover -s scripts/epic
"""

from __future__ import annotations

import isolated_env  # noqa: F401  (first: hides live runner state)

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import git_gate
import io
import permission_gate
import tarfile
import github_state as gs
import next_action as na
import rebase_policy as rp
import runner_worktree
import test_github_state as fx
import test_write_checks as twc
import tick_cooldown
import tick_gate
import tick_verify
import write_checks as wc

HEAD = "a" * 40
NEW_HEAD = "e" * 40
TIP = "c" * 40
NEW_TIP = "d" * 40
BASE = "dev/312-interim"
PULL = f"https://github.com/{na.REPO}/pull"
ISSUES = f"https://github.com/{na.REPO}/issues"
REPO = f"repos/{na.REPO}"
ON = frozenset({na.RECOVERY})
GREEN = [{"conclusion": "SUCCESS"}, {"context": "epic-guard", "state": "SUCCESS"}]
RED = [{"conclusion": "FAILURE"}, {"context": "epic-guard", "state": "SUCCESS"}]
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def request(
    owner: str = "Claude",
    head: str = HEAD,
    base: str = BASE,
    tip: str = TIP,
    suites: str = "codex",
) -> str:
    return (
        f"Upstream test recovery by {owner}\nHead: {head}\nBase: {base}\n"
        f"Base tip: {tip}\nSuites: {suites}"
    )


def note(n: int, cid: int, body: str, **kw: Any) -> dict[str, Any]:
    """A PR comment in the decide() shape, unedited with edit evidence."""
    data = {
        "body": body,
        "createdAt": f"2026-10-11T00:00:{cid:02d}Z",
        "url": f"{PULL}/{n}#issuecomment-{cid}",
        "includesCreatedEdit": False,
        "id": cid,
        "author": "anneoneone",
        "editEvidence": True,
    }
    data.update(kw)
    return data


def pr(n: int, owner: str, body: str | None = None, **kw: Any) -> dict[str, Any]:
    """An owned PR with failing checks and a valid request for its head."""
    data = {
        "number": n,
        "title": f"PR {n}",
        "body": f"Executor: {owner}\nIssue: {ISSUES}/{900 + n}",
        "baseRefName": BASE,
        "headRefName": f"fix/{n}-x",
        "headRefOid": HEAD,
        "isDraft": False,
        "labels": [],
        "mergeable": "MERGEABLE",
        "statusCheckRollup": RED,
        "comments": [note(n, 1, request(owner) if body is None else body)],
        "recoveryRead": {"tip": TIP, "head": HEAD, "baseTip": TIP, "compare": "behind"},
        "updatedAt": "2026-10-11T00:01:00Z",
    }
    data.update(kw)
    return data


def evidence(n: int, owner: str = "Claude", suites: list[str] = ["codex"]) -> dict:  # noqa: B006
    return {
        "kind": "upstream-test",
        "comment_id": 1,
        "url": f"{PULL}/{n}#issuecomment-1",
        "owner": owner,
        "head": HEAD,
        "base": BASE,
        "tip": TIP,
        "suites": suites,
    }


def decide(agent: str, *prs: dict[str, Any], **kw: Any) -> list[na.Action]:
    kw.setdefault("enabled", ON)
    return na.decide(agent, {"prs": list(prs), "items": []}, **kw)


def git(cwd: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, **GIT_ENV},
    )
    if out.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {out.stderr}")
    return out.stdout.strip()


class SelectorTest(unittest.TestCase):
    def test_valid_request_gives_recovery_to_both_owners(self) -> None:
        for owner in ("Claude", "Codex"):
            with self.subTest(owner=owner):
                act = decide(owner, pr(7, owner))[0]
                self.assertEqual(act.action, "resolve-conflict")
                self.assertEqual(act.recovery, evidence(7, owner))
                self.assertIn("upstream test recovery", act.reason)
                self.assertEqual(json.loads(act.to_json())["recovery"], act.recovery)

    def test_without_the_opt_in_nothing_changes(self) -> None:
        act = decide("Claude", pr(7, "Claude"), enabled=frozenset())[0]
        self.assertEqual((act.action, act.recovery), ("fix-checks", None))
        draft = decide("Claude", pr(7, "Claude", isDraft=True), enabled=frozenset())
        self.assertEqual(draft[0].action, "continue")
        self.assertEqual(draft[0].warnings, [])

    def test_draft_recovers_and_holds_back_claims(self) -> None:
        item = {
            "content": {"type": "Issue", "number": 30, "url": f"{ISSUES}/30"},
            "status": "Ready",
            "executor": "Claude",
            "labels": [],
        }
        state = {"prs": [pr(7, "Claude", isDraft=True)], "items": [item]}
        acts = na.decide("Claude", state, enabled=ON)
        self.assertEqual([a.action for a in acts], ["resolve-conflict"])
        self.assertEqual(acts[0].recovery, evidence(7))
        # Without the request the draft continues; claims stay held as today.
        state["prs"][0]["comments"] = []
        self.assertEqual(
            [a.action for a in na.decide("Claude", state, enabled=ON)], ["continue"]
        )

    def test_waiting_draft_keeps_waiting(self) -> None:
        waiting = pr(7, "Claude", isDraft=True, labels=[{"name": "waiting"}])
        acts = decide("Claude", waiting, include_waiting=True)
        self.assertEqual([a.action for a in acts], ["wait"])

    def test_escalation_pause_focus_and_needs_anton_come_first(self) -> None:
        verdicts = [
            note(7, i, f"Review: CHANGES REQUESTED by Codex at {s}")
            for i, s in enumerate(("1" * 40, "2" * 40, HEAD), start=2)
        ]
        escalated = pr(7, "Claude")
        escalated["comments"] += verdicts
        self.assertEqual(decide("Claude", escalated)[0].action, "escalate")
        self.assertEqual(
            decide("Claude", pr(7, "Claude"), paused=True)[0].action, "stop"
        )
        out = decide("Claude", pr(7, "Claude"), focus=frozenset({"project::Stream"}))
        self.assertEqual(out[0].action, "idle")
        parked = pr(7, "Claude", labels=[{"name": "needs-anton"}])
        self.assertEqual(decide("Claude", parked)[0].action, "idle")
        # The other agent never gets the owner's recovery.
        self.assertNotIn(
            "resolve-conflict", [a.action for a in decide("Codex", pr(7, "Claude"))]
        )

    def test_conflict_keeps_the_plain_resolve_conflict(self) -> None:
        act = decide("Claude", pr(7, "Claude", mergeable="CONFLICTING"))[0]
        self.assertEqual((act.action, act.recovery), ("resolve-conflict", None))
        self.assertNotIn("recovery", json.loads(act.to_json()))

    def test_only_fresh_mergeable_recovers(self) -> None:
        acts = decide(
            "Claude", pr(7, "Claude", mergeable="UNKNOWN"), include_waiting=True
        )
        # Settling mergeability: no fix-checks, no merge, a status-only wait.
        self.assertEqual([a.action for a in acts], ["wait"])
        self.assertIn("not MERGEABLE yet", acts[0].reason)

    def test_a_pr_only_behind_is_not_rebased(self) -> None:
        behind = pr(7, "Claude", comments=[], statusCheckRollup=GREEN)
        behind.pop("recoveryRead")
        self.assertNotIn(
            "resolve-conflict", [a.action for a in decide("Claude", behind)]
        )
        prose = pr(7, "Claude", body="Please rebase: tests pass on the base now.")
        self.assertEqual(decide("Claude", prose)[0].action, "fix-checks")

    def test_requests_that_are_not_the_owners_current_request_are_ignored(self) -> None:
        cases = {
            "edited": pr(7, "Claude", comments=[note(7, 1, request(), includesCreatedEdit=True)]),
            "no edit evidence": pr(7, "Claude", comments=[note(7, 1, request(), editEvidence=False)]),
            "other login": pr(7, "Claude", comments=[note(7, 1, request(), author="someone")]),
            "other owner": pr(7, "Claude", body=request(owner="Codex")),
            "trailing newline": pr(7, "Claude", body=request() + "\n"),
            "extra line": pr(7, "Claude", body=request() + "\nplease"),
            "command": pr(7, "Claude", body=request(suites="codex; rm -rf /")),
            "old head": pr(7, "Claude", headRefOid=NEW_HEAD),
            "other base": pr(7, "Claude", body=request(base="main")),
        }  # fmt: skip
        for name, case in cases.items():
            with self.subTest(name):
                act = decide("Claude", case)[0]
                self.assertEqual(
                    (act.action, act.recovery, act.refresh), ("fix-checks", None, None)
                )
                self.assertIn("not used", " ".join(act.warnings))
        # A quoted request is prose, not a request at all.
        act = decide("Claude", pr(7, "Claude", body="> " + request()))[0]
        self.assertEqual(
            (act.action, act.recovery, act.warnings), ("fix-checks", None, [])
        )

    def test_current_request_without_valid_evidence_asks_the_owner(self) -> None:
        def read(base_tip: str = TIP, compare: str | None = "behind") -> dict:
            return {"tip": TIP, "head": HEAD, "baseTip": base_tip, "compare": compare}

        cases = {
            "base_moved": pr(7, "Claude", recoveryRead=read(NEW_TIP)),
            "tip_in_head": pr(7, "Claude", recoveryRead=read(compare="ahead")),
            "unknown_commit": pr(7, "Claude", recoveryRead=read(compare=None)),
            "bad_suites": pr(7, "Claude", body=request(suites="codex, nope")),
        }
        for why, case in cases.items():
            for checks in (RED, GREEN):
                with self.subTest(why, checks=checks is GREEN):
                    case["statusCheckRollup"] = checks
                    act = decide("Claude", case)[0]
                    self.assertEqual((act.action, act.recovery), ("fix-checks", None))
                    self.assertEqual(act.refresh["why"], why)
                    self.assertEqual(act.refresh["kind"], na.REFRESH_KIND)
                    self.assertEqual(act.refresh["comment_id"], 1)
                    self.assertIn("needs new evidence", act.reason)
        for name in ("unsorted", "duplicate"):
            suites = {"unsorted": "epic, codex", "duplicate": "codex, codex"}[name]
            act = decide("Claude", pr(7, "Claude", body=request(suites=suites)))[0]
            self.assertEqual(act.refresh["why"], "bad_suites", name)

    def test_stale_request_blocks_merge_and_review_and_asks_the_owner(self) -> None:
        moved = {"tip": TIP, "head": HEAD, "baseTip": NEW_TIP, "compare": "behind"}
        approved = note(7, 2, f"Review: APPROVED by Codex at {HEAD}")
        mine = pr(7, "Claude", statusCheckRollup=GREEN, recoveryRead=moved)
        # Green CI and no verdict: not idle, the owner refreshes the evidence.
        act = decide("Claude", mine)[0]
        self.assertEqual(act.action, "fix-checks")
        self.assertEqual(
            act.refresh,
            {
                "kind": "upstream-test-refresh",
                "comment_id": 1,
                "url": f"{PULL}/7#issuecomment-1",
                "owner": "Claude",
                "head": HEAD,
                "base": BASE,
                "tip": TIP,
                "base_tip": NEW_TIP,
                "why": "base_moved",
            },
        )
        self.assertEqual(json.loads(act.to_json())["refresh"], act.refresh)
        # Approved with green checks: still no merge.
        mine["comments"].append(approved)
        acts = decide("Claude", mine)
        self.assertNotIn("merge", [a.action for a in acts])
        self.assertEqual(acts[0].action, "fix-checks")
        # Without the request the approved PR merges as before.
        plain = pr(7, "Claude", statusCheckRollup=GREEN, comments=[approved])
        self.assertEqual(decide("Claude", plain)[0].action, "merge")
        # Changes requested: the fix carries the refresh too.
        asked = pr(7, "Claude", recoveryRead=moved)
        asked["comments"].append(
            note(7, 3, f"Review: CHANGES REQUESTED by Codex at {HEAD}")
        )
        act = decide("Claude", asked)[0]
        self.assertEqual((act.action, act.refresh["why"]), ("fix", "base_moved"))
        # A draft continues with it.
        draft = pr(7, "Claude", isDraft=True, recoveryRead=moved)
        act = decide("Claude", draft)[0]
        self.assertEqual((act.action, act.refresh["why"]), ("continue", "base_moved"))
        # The other agent waits instead of reviewing the old evidence.
        theirs = pr(7, "Codex", statusCheckRollup=GREEN, recoveryRead=moved)
        acts = decide("Claude", theirs, include_waiting=True)
        self.assertEqual([a.action for a in acts], ["wait"])
        self.assertIn("refreshes its test evidence", acts[0].reason)

    def test_a_base_advance_gives_a_new_refresh_identity(self) -> None:
        def refresh(base_tip: str) -> dict:
            read = {"tip": TIP, "head": HEAD, "baseTip": base_tip, "compare": "behind"}
            act = decide("Claude", pr(7, "Claude", recoveryRead=read))[0]
            return json.loads(act.to_json())

        first, second = refresh(NEW_TIP), refresh("f" * 40)
        self.assertNotEqual(tick_gate.fingerprint(first), tick_gate.fingerprint(second))
        self.assertNotEqual(tick_cooldown.key(first), tick_cooldown.key(second))
        self.assertTrue(tick_cooldown.key(second).endswith(":base:" + "f" * 40))
        # The plain fix-checks key of the same head stays apart.
        plain = {"action": "fix-checks", "pr": 7, "sha": HEAD}
        self.assertEqual(tick_cooldown.key(plain), f"claude:fix-checks:pr:7:{HEAD}")
        self.assertEqual(tick_cooldown.REFRESH_KIND, na.REFRESH_KIND)

    def test_a_newer_invalid_request_hides_an_older_valid_one(self) -> None:
        older = note(7, 1, request())
        newer = note(7, 2, request() + "\n")
        act = decide("Claude", pr(7, "Claude", comments=[older, newer]))[0]
        self.assertEqual((act.action, act.refresh), ("fix-checks", None))
        # A later unrelated comment does not hide the request.
        chat = note(7, 3, "unrelated comment")
        act = decide("Claude", pr(7, "Claude", comments=[older, chat]))[0]
        self.assertEqual(act.action, "resolve-conflict")

    def test_peer_waits_instead_of_reviewing_the_old_head(self) -> None:
        theirs = pr(7, "Codex", statusCheckRollup=GREEN)
        acts = decide("Claude", theirs, include_waiting=True)
        self.assertEqual([a.action for a in acts], ["wait"])
        self.assertEqual(acts[0].reason_code, "conflict_wait")
        self.assertIn("recovers the PR", acts[0].reason)
        # Pushed: the request names the old head, the new head gets a review.
        pushed = pr(7, "Codex", statusCheckRollup=GREEN, headRefOid=NEW_HEAD)
        self.assertEqual(decide("Claude", pushed)[0].action, "review")
        # An ignored request (edited) does not block the review.
        edited = pr(
            7, "Codex", statusCheckRollup=GREEN,
            comments=[note(7, 1, request("Codex"), includesCreatedEdit=True)],
        )  # fmt: skip
        self.assertEqual(decide("Claude", edited)[0].action, "review")
        # Without the opt-in the peer reviews as before.
        self.assertEqual(
            decide("Claude", theirs, enabled=frozenset())[0].action, "review"
        )

    def test_a_bad_suite_table_admits_no_request(self) -> None:
        with patch.object(rp, "suites", side_effect=rp.Problem("bad table")):
            act = decide("Claude", pr(7, "Claude"))[0]
        self.assertEqual((act.action, act.refresh["why"]), ("fix-checks", "bad_suites"))


class LegacyReaderTest(unittest.TestCase):
    """fetch_state() with the shared reader off: evidence and recovery reads
    only for the PR whose request names its head."""

    def setUp(self) -> None:
        self.calls: list[list[str]] = []
        self.edited: set[int] = set()
        self.compare: dict[str, Any] = {"status": "behind"}
        self.rows = {7: [self.rest(7, 1, request())], 8: [self.rest(8, 2, "hello")]}
        self.prs = []
        for n in (7, 8):
            data = pr(n, "Claude")
            for key in ("comments", "recoveryRead"):
                data.pop(key)
            self.prs.append(data)

    @staticmethod
    def rest(n: int, cid: int, body: str) -> dict[str, Any]:
        at = f"2026-10-11T00:00:{cid:02d}Z"
        return {
            "id": cid,
            "body": body,
            "created_at": at,
            "updated_at": at,
            "html_url": f"{PULL}/{n}#issuecomment-{cid}",
            "user": {"login": "anneoneone"},
        }

    def run_gh(self, args: list[str], timeout: int = 120) -> str:
        self.calls.append(args)
        if args[:2] == ["api", "graphql"]:
            n = int(args[-1].split("pullRequest(number: ")[1].split(")")[0])
            nodes = [
                {
                    "databaseId": r["id"],
                    "lastEditedAt": r["created_at"] if r["id"] in self.edited else None,
                    "body": r["body"],
                    "createdAt": r["created_at"],
                    "updatedAt": r["updated_at"],
                }
                for r in self.rows[n]
            ]
            connection = {
                "totalCount": len(nodes),
                "nodes": nodes,
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }
            return json.dumps(
                {"data": {"repository": {"pullRequest": {"comments": connection}}}}
            )
        if args[:2] == ["pr", "list"]:
            open_prs = args[args.index("--state") + 1] == "open"
            first = args[args.index("--base") + 1] == BASE
            return json.dumps(self.prs if open_prs and first else [])
        endpoint = args[1] if args[0] == "api" and args[1] != "--paginate" else args[-1]
        if args[-2:] == ["--jq", "{comments}"]:
            n = int(endpoint.rsplit("/", 1)[1])
            return json.dumps({"comments": len(self.rows[n])})
        for n, rows in self.rows.items():
            if endpoint.endswith(f"/issues/{n}/comments?per_page=100"):
                return json.dumps([json.loads(json.dumps(rows))])
        if endpoint.endswith("/comments?per_page=100"):
            return json.dumps([[]])
        if endpoint == f"{REPO}/git/ref/heads/{BASE}":
            return json.dumps({"object": {"sha": TIP}})
        if endpoint == f"{REPO}/compare/{TIP}...{HEAD}?per_page=1":
            if self.compare is None:
                raise subprocess.CalledProcessError(
                    1, args, "", "gh: Not Found (HTTP 404)"
                )
            return json.dumps(self.compare)
        if endpoint.startswith(f"{REPO}/issues/9"):
            return json.dumps({"labels": []})
        raise AssertionError(f"unexpected gh call {args}")

    def state(self) -> dict[str, Any]:
        with (
            patch.object(na, "run_gh", self.run_gh),
            patch.object(na, "project_items", lambda: []),
            patch.dict("os.environ", {"EPIC_SHARED_READER": "0"}),
        ):
            return na.fetch_state()

    def endpoints(self, part: str) -> list[list[str]]:
        return [c for c in self.calls if any(part in a for a in c)]

    def mine(self, state: dict[str, Any]) -> na.Action:
        return [a for a in na.decide("Claude", state, enabled=ON) if a.pr == 7][0]

    def test_request_reads_evidence_tip_and_ancestry_for_its_pr_only(self) -> None:
        state = self.state()
        act = self.mine(state)
        self.assertEqual(
            (act.action, act.pr, act.recovery), ("resolve-conflict", 7, evidence(7))
        )
        self.assertEqual(len(self.endpoints("graphql")), 1)
        self.assertEqual(len(self.endpoints("/compare/")), 1)
        self.assertEqual(len(self.endpoints("/git/ref/heads/")), 1)
        self.assertNotIn("recoveryRead", state["prs"][1])

    def test_same_second_edit_is_not_used(self) -> None:
        self.edited = {1}
        act = self.mine(self.state())
        self.assertEqual((act.action, act.refresh), ("fix-checks", None))

    def test_unknown_commit_is_no_read_failure(self) -> None:
        self.compare = None
        state = self.state()
        self.assertIsNone(state["prs"][0]["recoveryRead"]["compare"])
        act = self.mine(state)
        self.assertEqual(
            (act.action, act.refresh["why"]), ("fix-checks", "unknown_commit")
        )

    def test_request_for_an_old_head_reads_nothing_more(self) -> None:
        self.rows[7] = [self.rest(7, 1, request(head=NEW_HEAD))]
        self.state()
        self.assertEqual(self.endpoints("/compare/"), [])
        self.assertEqual(self.endpoints("/git/ref/heads/"), [])


class SnapshotReaderTest(fx.Env):
    """The shared snapshot and the fresh recheck read the same evidence."""

    def setUp(self) -> None:
        super().setUp()
        self.repo = fx.GitHubRepo(self.gh)
        self.rows = [self.row(1, request()), self.row(2, "unrelated")]
        self.add()
        self.gh.set(f"{REPO}/pulls/8", fx.pull(8, HEAD, "Executor: Claude"))

    def row(self, cid: int, body: str) -> dict[str, Any]:
        at = f"2026-10-11T00:00:{cid:02d}Z"
        return {**fx.comment(cid, body, at), "html_url": f"{PULL}/7#issuecomment-{cid}"}

    def add(self, **kw: Any) -> None:
        self.repo.open = {b: [] for b in na.BASES}
        self.repo.add_pr(fx.pull(7, HEAD, "Executor: Claude", **kw), self.rows)
        self.repo.add_pr(fx.pull(8, HEAD, "Executor: Claude"), [])
        self.gh.set(f"{REPO}/git/ref/heads/{BASE}", {"object": {"sha": TIP}})
        self.gh.set(f"{REPO}/compare/{TIP}...{HEAD}?per_page=1", {"status": "diverged"})

    def selected(self, edited: set[int] = frozenset()) -> list[na.Action]:  # type: ignore[assignment]
        self.snapshot = fx.FakeReader(self.gh, edited)
        with patch.object(gs, "graphql", lambda client, e: self.snapshot.graphql(e)):
            state = gs.build_state(self.client(), set())
        gs.validate_state(state)
        return na.decide("Claude", state, enabled=ON)

    def recheck(
        self, action: dict[str, Any], enabled: frozenset[str] = ON
    ) -> str | None:
        self.reader = fx.FakeReader(self.gh)
        return gs.recheck("Claude", action, frozenset(), enabled, False, self.reader)

    def test_snapshot_selects_the_recovery_with_bounded_reads(self) -> None:
        acts = self.selected()
        mine = [a for a in acts if a.pr == 7]
        self.assertEqual(mine[0].action, "resolve-conflict")
        self.assertEqual(mine[0].recovery, evidence(7))
        self.assertEqual(self.snapshot.graphql_calls, 1)
        self.assertEqual(self.gh.count(f"{REPO}/compare/{TIP}...{HEAD}?per_page=1"), 1)
        self.assertEqual(self.gh.count(f"{REPO}/git/ref/heads/{BASE}"), 1)

    def test_snapshot_sees_a_same_second_edit(self) -> None:
        # Ignored: PR 7 is green and unreviewed, so the owner has nothing to do.
        self.assertEqual([a for a in self.selected(edited={1}) if a.pr == 7], [])

    def test_unknown_commit_does_not_block_the_snapshot(self) -> None:
        self.gh.pages.pop(gs.full_url(f"{REPO}/compare/{TIP}...{HEAD}?per_page=1"))
        mine = [a for a in self.selected() if a.pr == 7]
        self.assertEqual(
            (mine[0].action, mine[0].refresh["why"]), ("fix-checks", "unknown_commit")
        )

    def test_stale_request_on_a_green_pr_asks_the_owner_and_rechecks(self) -> None:
        self.gh.set(f"{REPO}/git/ref/heads/{BASE}", {"object": {"sha": NEW_TIP}})
        act = [a for a in self.selected() if a.pr == 7][0]
        self.assertEqual((act.action, act.refresh["base_tip"]), ("fix-checks", NEW_TIP))
        action = json.loads(act.to_json())
        self.assertIsNone(self.recheck(action))
        # The base moves again without a PR change: the old refresh is stale.
        self.gh.set(f"{REPO}/git/ref/heads/{BASE}", {"object": {"sha": "f" * 40}})
        self.assertIn("GitHub changed", self.recheck(action) or "")
        # The owner posted a fresh request for the new tip: recovery again.
        self.gh.set(
            f"{REPO}/compare/{'f' * 40}...{HEAD}?per_page=1", {"status": "behind"}
        )
        self.rows.append(self.row(3, request(tip="f" * 40)))
        self.add()
        self.gh.set(f"{REPO}/git/ref/heads/{BASE}", {"object": {"sha": "f" * 40}})
        act = [a for a in self.selected() if a.pr == 7][0]
        self.assertEqual(
            (act.action, act.recovery["tip"]), ("resolve-conflict", "f" * 40)
        )

    def test_failed_base_read_blocks(self) -> None:
        self.gh.pages.pop(gs.full_url(f"{REPO}/git/ref/heads/{BASE}"))
        with self.assertRaises(gs.ReadBlocked):
            self.selected()

    def test_recheck_keeps_the_recovery_and_refuses_changes(self) -> None:
        act = [a for a in self.selected() if a.pr == 7][0]
        action = json.loads(act.to_json())
        self.assertIsNone(self.recheck(action))
        # The opt-in left the runner's environment: stale.
        self.assertIn("GitHub changed", self.recheck(action, frozenset()) or "")
        # A plain resolve-conflict of the same PR is not this action.
        plain = {k: v for k, v in action.items() if k != "recovery"}
        self.assertIn("GitHub changed", self.recheck(plain) or "")
        # The owner posted a new request (other suites): the old action is stale.
        self.rows.append(self.row(3, request(suites="codex, epic")))
        self.add()
        self.assertIn("GitHub changed", self.recheck(action) or "")
        # The base moved on: stale too, and the owner gets fix-checks back.
        self.rows.pop()
        self.add()
        self.gh.set(f"{REPO}/git/ref/heads/{BASE}", {"object": {"sha": NEW_TIP}})
        reason = self.recheck(action) or ""
        self.assertIn("GitHub changed", reason)


class GateTest(unittest.TestCase):
    def action(self, **kw: Any) -> dict[str, Any]:
        data = {
            "action": "resolve-conflict",
            "pr": 7,
            "sha": HEAD,
            "recovery": evidence(7),
        }
        data.update(kw)
        return data

    def seen(self, merge: str) -> dict[str, Any]:
        return {"updated_at": None, "state": "open", "head": HEAD, "mergeable": merge}

    def test_recovery_runs_only_on_fresh_mergeable(self) -> None:
        self.assertIsNone(tick_gate.stale(self.action(), self.seen("MERGEABLE")))
        for merge in ("UNKNOWN", "CONFLICTING"):
            with self.subTest(merge):
                self.assertIn(
                    "needs a MERGEABLE PR",
                    tick_gate.stale(self.action(), self.seen(merge)) or "",
                )

    def test_plain_or_untyped_resolve_conflict_is_unchanged(self) -> None:
        plain = self.action(recovery=None)
        self.assertIn(
            "no longer conflicts", tick_gate.stale(plain, self.seen("MERGEABLE")) or ""
        )
        self.assertIsNone(tick_gate.stale(plain, self.seen("CONFLICTING")))
        self.assertIsNone(tick_gate.stale(plain, self.seen("UNKNOWN")))
        for bad in (
            {**evidence(7), "kind": "other"},
            {**evidence(7), "head": NEW_HEAD},
            {**evidence(7), "tip": "short"},
            "upstream-test",
        ):
            with self.subTest(bad=bad):
                untyped = self.action(recovery=bad)
                self.assertIn(
                    "no longer conflicts",
                    tick_gate.stale(untyped, self.seen("MERGEABLE")) or "",
                )

    def test_kind_matches_the_selector(self) -> None:
        self.assertEqual(tick_gate.RECOVERY_KIND, na.RECOVERY_KIND)


SUITES = [
    {"name": "files", "paths": ["f.txt", "g.txt"], "cwd": ".", "command": ["python3", "-c", "pass"]},
    {
        "name": "upstream",
        "paths": ["never/"],
        "cwd": ".",
        "command": [
            "python3", "-c",
            "import pathlib, sys; sys.exit(0 if pathlib.Path('fix.txt').exists() else 1)",
        ],
    },
]  # fmt: skip


class TableTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="recovery-table-")
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "suites.json"
        path.write_text(json.dumps(SUITES))
        patcher = patch.dict(os.environ, {rp.SUITES_ENV: str(path)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_mandatory_is_an_optional_bool(self) -> None:
        self.assertTrue(rp.valid_suite({**SUITES[0], "mandatory": True}))
        self.assertTrue(rp.valid_suite(SUITES[0]))
        self.assertFalse(rp.valid_suite({**SUITES[0], "mandatory": "yes"}))

    def test_required_adds_mandatory_suites_whatever_the_paths(self) -> None:
        table = rp.pinned_table(["upstream"])
        self.assertEqual(
            [s["name"] for s in rp.required(["scripts/epic/x.py"], table)], ["upstream"]
        )
        self.assertEqual(
            [s["name"] for s in rp.required(["f.txt"], table)], ["files", "upstream"]
        )
        self.assertEqual(
            [s["name"] for s in rp.required(["f.txt"], rp.suites())], ["files"]
        )

    def test_pinned_table_names_only_known_suites(self) -> None:
        with self.assertRaisesRegex(rp.Problem, "unknown suites: nope"):
            rp.pinned_table(["nope"])
        self.assertEqual(rp.pinned_table([]), rp.suites())

    def test_attempt_suites_can_add_but_never_drop(self) -> None:
        pinned = rp.pinned_table(["upstream"])
        self.assertEqual(rp.attempt_suites({"proof_suites": pinned}), pinned)
        # A legacy attempt file without a pin: the runner's table.
        self.assertEqual(rp.attempt_suites({"key": "k"}), rp.suites())
        for name, table in (
            ("dropped", pinned[:1]),
            ("changed", [{**pinned[0], "command": ["true"]}, pinned[1]]),
            ("malformed", [{**pinned[0], "mandatory": 1}, pinned[1]]),
            ("not a list", {"files": 1}),
        ):
            with self.subTest(name):
                with self.assertRaises(rp.Problem):
                    rp.attempt_suites({"proof_suites": table})


class Repo:
    """A bare remote and a clone on the PR branch, behind a base fix."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.remote = root / "remote/phaabe/live.moafunk.de.git"
        self.remote.parent.mkdir(parents=True)
        git(root, "init", "-q", "--bare", "-b", BASE, str(self.remote))
        seed = root / "seed"
        git(root, "clone", "-q", str(self.remote), str(seed))
        git(seed, "switch", "-q", "-c", BASE)
        (seed / "f.txt").write_text("one\n")
        git(seed, "add", ".")
        git(seed, "commit", "-q", "-m", "B0")
        git(seed, "push", "-q", "origin", BASE)
        git(seed, "switch", "-q", "-c", "fix/7-x")
        (seed / "f.txt").write_text("one\nfeature\n")
        git(seed, "commit", "-q", "-am", "feature")
        git(seed, "push", "-q", "origin", "fix/7-x")
        self.head = git(seed, "rev-parse", "HEAD")
        git(seed, "switch", "-q", BASE)
        (seed / "fix.txt").write_text("fixed upstream\n")
        git(seed, "add", ".")
        git(seed, "commit", "-q", "-m", "upstream fix")
        git(seed, "push", "-q", "origin", BASE)
        self.tip = git(seed, "rev-parse", "HEAD")
        self.top = Path(os.path.realpath(root / "wt"))
        git(root, "clone", "-q", "-b", "fix/7-x", str(self.remote), str(self.top))

    def rebase(self) -> str:
        git(self.top, "rebase", "-q", f"origin/{BASE}")
        return git(self.top, "rev-parse", "HEAD")


class ProofTest(TableTest):
    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory(prefix="recovery-proof-")
        self.addCleanup(tmp.cleanup)
        self.repo = Repo(Path(os.path.realpath(tmp.name)))
        self.state = self.repo.root / "state"
        self.pinned = rp.pinned_table(["upstream"])

    def test_mandatory_suite_runs_and_must_pass(self) -> None:
        top = self.repo.top
        # On the old head the upstream suite fails: no valid proof.
        failed = rp.prove(top, 7, BASE, self.state, self.pinned)
        self.assertIn(
            ("upstream", "failed"), [(s["name"], s["result"]) for s in failed["suites"]]
        )
        problem = rp.proof_problem(
            failed, top, 7, failed["commit"], failed["onto"], self.pinned
        )
        self.assertEqual(problem, "suite upstream failed")
        # Rebased onto the tip with the fix: both suites pass.
        new = self.repo.rebase()
        proof = rp.prove(top, 7, BASE, self.state, self.pinned)
        self.assertEqual([(s["name"], s["result"]) for s in proof["suites"]],
                         [("files", "passed"), ("upstream", "passed")])  # fmt: skip
        self.assertIsNone(
            rp.proof_problem(proof, top, 7, new, self.repo.tip, self.pinned)
        )

    def test_proof_without_the_mandatory_suite_is_refused(self) -> None:
        top = self.repo.top
        new = self.repo.rebase()
        legacy = rp.prove(top, 7, BASE, self.state)  # normal table only
        self.assertEqual([s["name"] for s in legacy["suites"]], ["files"])
        self.assertIsNone(rp.proof_problem(legacy, top, 7, new, self.repo.tip))
        self.assertEqual(
            rp.proof_problem(legacy, top, 7, new, self.repo.tip, self.pinned),
            "required suite upstream did not run",
        )
        attempt = {
            "pr": 7,
            "head": self.repo.head,
            "tip": self.repo.tip,
            "proof_suites": self.pinned,
        }
        problem = rp.verify_resolution("claude", attempt, new, top, self.state, [], "")
        self.assertEqual(problem, "required suite upstream did not run")

    def test_skipped_mandatory_suite_is_no_proof(self) -> None:
        missing = [
            self.pinned[0],
            {**self.pinned[1], "command": ["no-such-command-725"]},
        ]
        with patch.object(rp, "suites", lambda: [rp.without_flag(s) for s in missing]):
            new = self.repo.rebase()
            proof = rp.prove(self.repo.top, 7, BASE, self.state, missing)
            problem = rp.proof_problem(
                proof, self.repo.top, 7, new, self.repo.tip, missing
            )
        self.assertEqual(problem, "suite upstream skipped")

    def test_prove_cli_uses_the_attempt_pin(self) -> None:
        new = self.repo.rebase()
        pin = self.repo.root / "attempt.json"
        script = str(Path(rp.__file__))

        def cli(*extra: str) -> int:
            return subprocess.run(
                [sys.executable, script, "prove", "--worktree", str(self.repo.top),
                 "--pr", "7", "--base", BASE, "--state-dir", str(self.state), *extra],
                capture_output=True, text=True, timeout=120,
            ).returncode  # fmt: skip

        pin.write_text(
            json.dumps({"key": "k", "pr": 7, "base": BASE, "proof_suites": self.pinned})
        )
        self.assertEqual(cli("--attempt-file", str(pin)), rp.RUN)
        proof = rp.load_proof(self.state, 7, new)
        assert proof is not None
        self.assertEqual([s["name"] for s in proof["suites"]], ["files", "upstream"])
        pin.write_text(
            json.dumps({"key": "k", "pr": 8, "base": BASE, "proof_suites": self.pinned})
        )
        self.assertEqual(cli("--attempt-file", str(pin)), rp.BAD)

    def test_git_gate_lease_proof_needs_the_pinned_suites(self) -> None:
        top = self.repo.top
        new = self.repo.rebase()
        rp.prove(top, 7, BASE, self.state)  # without the mandatory suite
        files = self.repo.root / "gate"
        files.mkdir()
        (files / "action.json").write_text(
            json.dumps({"action": "resolve-conflict", "pr": 7, "sha": self.repo.head})
        )
        (files / "attempt.json").write_text(
            json.dumps(
                {
                    "pr": 7,
                    "head": self.repo.head,
                    "base": BASE,
                    "tip": self.repo.tip,
                    "proof_suites": self.pinned,
                }
            )  # fmt: skip
        )
        env = {
            "EPIC_ACTION_FILE": str(files / "action.json"),
            "EPIC_ATTEMPT_FILE": str(files / "attempt.json"),
            "EPIC_STATE_DIR": str(self.state),
        }
        with patch.dict(os.environ, env):
            with self.assertRaisesRegex(
                git_gate.Refused, "required suite upstream did not run"
            ):
                git_gate.proven(top, 7, self.repo.tip)
            # A fix tick has no pin: the normal table, unchanged.
            (files / "action.json").write_text(
                json.dumps({"action": "fix", "pr": 7, "sha": new})
            )
            git_gate.proven(top, 7, self.repo.tip)
            # With the mandatory suite in the proof the recovery push passes.
            (files / "action.json").write_text(
                json.dumps(
                    {"action": "resolve-conflict", "pr": 7, "sha": self.repo.head}
                )
            )
            rp.prove(top, 7, BASE, self.state, self.pinned)
            git_gate.proven(top, 7, self.repo.tip)


class AttemptCheckTest(unittest.TestCase):
    """attempt-check admits a recovery from fresh reads, before the store."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="recovery-attempt-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / "state"
        self.out = self.root / "attempt.json"
        self.pull = {
            "state": "open",
            "draft": True,
            "mergeable": True,
            "comments": 1,
            "body": "Executor: Claude\nReviewer: Codex",
            "base": {"ref": BASE},
            "head": {"sha": HEAD, "ref": "fix/7-x"},
        }
        self.rows = [LegacyReaderTest.rest(7, 1, request())]
        self.edited: set[int] = set()
        self.tip = TIP
        self.compare: Any = {"status": "behind"}
        self.labels: list[int] = []
        for target, value in (
            ("read_pr", lambda n: dict(self.pull)),
            ("comments", lambda n: json.loads(json.dumps(self.rows))),
            ("read_json", self.read_json),
            ("base_ref", self.no_plain_read),
            ("base_tip", self.no_plain_read),
        ):
            p = patch.object(rp, target, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(na, "legacy_edit_evidence", self.evidence)
        p.start()
        self.addCleanup(p.stop)

    def no_plain_read(self, *args: Any) -> str:
        raise AssertionError("a recovery pins the request's tip, never a new read")

    def evidence(self, n: int, rows: list[dict[str, Any]]) -> None:
        for r in rows:
            r["last_edited_at"] = r["created_at"] if r["id"] in self.edited else None

    def read_json(self, endpoint: str, missing: frozenset[int] = frozenset()) -> Any:
        if endpoint == f"{REPO}/git/ref/heads/{BASE}":
            return {"object": {"sha": self.tip}}
        if endpoint == f"{REPO}/compare/{TIP}...{HEAD}?per_page=1":
            if isinstance(self.compare, Exception):
                raise self.compare
            return self.compare
        raise AssertionError(endpoint)

    def action(self, **kw: Any) -> dict[str, Any]:
        data = {
            "action": "resolve-conflict",
            "pr": 7,
            "sha": HEAD,
            "recovery": evidence(7),
        }
        data.update(kw)
        return data

    def check(self, action: dict[str, Any] | None = None) -> int:
        return rp.attempt_check(
            self.state,
            "claude",
            action or self.action(),
            self.out,
            post=self.labels.append,
        )

    def test_valid_request_pins_its_tip_and_mandatory_suites(self) -> None:
        self.assertEqual(self.check(), rp.RUN)
        pin = json.loads(self.out.read_text())
        self.assertEqual(pin["key"], rp.attempt_key(7, HEAD, TIP))
        self.assertEqual(
            (pin["base"], pin["tip"], pin["recovery"]), (BASE, TIP, evidence(7))
        )
        mandatory = [s["name"] for s in pin["proof_suites"] if s.get("mandatory")]
        self.assertEqual(mandatory, ["codex"])
        self.assertEqual([rp.without_flag(s) for s in pin["proof_suites"]], rp.suites())

    def test_plain_conflict_pins_the_normal_table(self) -> None:
        with (
            patch.object(rp, "base_ref", lambda n: BASE),
            patch.object(rp, "base_tip", lambda ref: TIP),
        ):
            self.assertEqual(self.check(self.action(recovery=None)), rp.RUN)
        pin = json.loads(self.out.read_text())
        self.assertNotIn("recovery", pin)
        self.assertEqual(pin["proof_suites"], rp.suites())

    def test_changed_evidence_is_stale_and_counts_no_attempt(self) -> None:
        def newer_request() -> None:
            self.rows.append(LegacyReaderTest.rest(7, 2, request(suites="codex, epic")))
            self.pull["comments"] = 2

        cases = {
            "head moved": lambda: self.pull["head"].update(sha=NEW_HEAD),
            "base moved": lambda: setattr(self, "tip", NEW_TIP),
            "edited": lambda: self.edited.add(1),
            "new request": newer_request,
            "closed": lambda: self.pull.update(state="closed"),
            "unknown": lambda: self.pull.update(mergeable=None),
            "conflicting": lambda: self.pull.update(mergeable=False),
            "owner": lambda: self.pull.update(body="Executor: Codex"),
            "tip in head": lambda: setattr(self, "compare", {"status": "ahead"}),
            "unknown commit": lambda: setattr(self, "compare", None),
        }
        for name, change in cases.items():
            with self.subTest(name):
                self.setUp()
                change()
                with self.assertRaises(rp.Stale):
                    self.check()
                self.assertFalse(self.out.exists())
                self.assertFalse((self.state / rp.ATTEMPTS).exists())
                self.assertEqual(self.labels, [])

    def test_base_advance_after_admission_turns_into_a_refresh(self) -> None:
        self.assertEqual(self.check(), rp.RUN)
        self.tip = NEW_TIP
        with self.assertRaisesRegex(rp.Stale, "base moved"):
            self.check()
        # The next selection asks the owner for new evidence instead.
        read = {"tip": TIP, "head": HEAD, "baseTip": NEW_TIP, "compare": "behind"}
        act = decide("Claude", pr(7, "Claude", isDraft=False, recoveryRead=read))[0]
        self.assertEqual((act.action, act.refresh["base_tip"]), ("fix-checks", NEW_TIP))

    def test_failed_reads_defer(self) -> None:
        self.compare = rp.ReadFailed("compare read failed")
        with self.assertRaises(rp.ReadFailed):
            self.check()
        self.compare = {"status": "behind"}
        with patch.object(na, "legacy_edit_evidence", side_effect=ValueError("retry")):
            with self.assertRaises(rp.ReadFailed):
                self.check()
        self.pull["comments"] = 5  # REST lost a comment: no partial history
        with self.assertRaises(rp.ReadFailed):
            self.check()
        self.assertFalse((self.state / rp.ATTEMPTS).exists())

    def test_attempt_limit_is_shared_with_plain_conflicts(self) -> None:
        for tick in ("t1", "t2"):
            self.assertEqual(self.check(), rp.RUN)
            attempt = rp.load_attempt(self.out)
            self.assertEqual(rp.attempt_start(self.state, attempt, tick), rp.RUN)
            rp.attempt_finish(
                self.state, attempt, tick, "failed", post=self.labels.append
            )
        self.assertEqual(self.labels, [7])
        self.assertEqual(self.check(), rp.SKIP)
        with (
            patch.object(rp, "base_ref", lambda n: BASE),
            patch.object(rp, "base_tip", lambda ref: TIP),
        ):
            self.assertEqual(self.check(self.action(recovery=None)), rp.SKIP)

    def test_cli_maps_stale_to_exit_6(self) -> None:
        self.tip = NEW_TIP
        action = self.root / "action.json"
        action.write_text(json.dumps(self.action()))
        argv = ["rebase_policy.py", "attempt-check", "--state-dir", str(self.state),
                "--agent", "claude", "--action-file", str(action), "--out", str(self.out)]  # fmt: skip
        with patch.object(sys, "argv", argv):
            self.assertEqual(rp.main(), rp.STALE)


class VerifyTest(unittest.TestCase):
    """A fix or fix-checks tick that only posted the owner's recovery request
    landed (a completed handoff); resolve-conflict stays strict."""

    SINCE = "2026-10-11T01:00:00Z"

    def setUp(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.calls: list[list[str]] = []
        self.owner = "Claude"
        self.head = HEAD
        self.graphql_edited: set[int] = set()
        self.compare: Any = {"status": "behind"}
        p = patch.dict(os.environ, {"EPIC_FOCUS_ACTIONS": "adopt, upstream-recovery"})
        p.start()
        self.addCleanup(p.stop)

    def post(self, body: str, at: str = "2026-10-11T01:00:05Z", **kw: Any) -> None:
        cid = len(self.rows) + 1
        row = {
            "id": cid,
            "node_id": f"IC_{cid}",
            "body": body,
            "created_at": at,
            "updated_at": at,
            "html_url": f"{PULL}/7#issuecomment-{cid}",
            "user": {"login": "anneoneone"},
        }
        row.update(kw)
        self.rows.append(row)

    def fetch(self, args: list[str]) -> Any:
        self.calls.append(args)
        if args[:2] == ["pr", "view"]:
            return {"state": "OPEN", "headRefOid": self.head}
        if args[:2] == ["api", "graphql"]:
            cid = int(args[-1].split("IC_")[1])
            edited = "2026-10-11T01:00:09Z" if cid in self.graphql_edited else None
            return {"data": {"node": {"lastEditedAt": edited}}}
        endpoint = args[1]
        if endpoint.startswith(f"{REPO}/issues/7/comments?since="):
            return [r for r in self.rows if r["updated_at"] >= self.SINCE]
        if endpoint == f"{REPO}/pulls/7":
            return {"body": f"Executor: {self.owner}", "base": {"ref": BASE}}
        if endpoint == f"{REPO}/compare/{TIP}...{HEAD}?per_page=1":
            if self.compare is None:
                raise subprocess.CalledProcessError(
                    1, args, "", "gh: Not Found (HTTP 404)"
                )
            return self.compare
        raise AssertionError(f"unexpected read {args}")

    def landed(
        self, kind: str = "fix-checks", agent: str = "claude"
    ) -> tuple[bool, str]:
        action = {"action": kind, "pr": 7, "sha": HEAD}
        return tick_verify.landed(agent, action, self.SINCE, fetch=self.fetch)

    def test_request_only_fix_and_fix_checks_are_completed_handoffs(self) -> None:
        self.post(request())
        self.post("Evidence: test_x fails on the head, passes on the base tip.")
        for kind in ("fix-checks", "fix"):
            with self.subTest(kind):
                ok, why = self.landed(kind)
                self.assertTrue(ok, why)
                self.assertIn("recovery request", why)
        # Publication is not admission: no base tip read, so a base that
        # moved on right after the post does not fail the tick.
        self.assertFalse([c for c in self.calls if "/git/ref/heads/" in " ".join(c)])

    def test_both_owners(self) -> None:
        self.owner = "Codex"
        self.post(request(owner="Codex"))
        self.assertTrue(self.landed(agent="codex")[0])
        self.assertFalse(self.landed(agent="claude")[0])

    def test_invalid_or_old_requests_do_not_land(self) -> None:
        cases = {
            "old": dict(body=request(), at="2026-10-11T00:59:00Z"),
            "edited": dict(body=request(), updated_at="2026-10-11T01:00:09Z"),
            "other login": dict(body=request(), user={"login": "someone"}),
            "other owner": dict(body=request(owner="Codex")),
            "other head": dict(body=request(head=NEW_HEAD)),
            "other base": dict(body=request(base="dev/streaming-architecture")),
            "malformed": dict(body=request() + "\n"),
            "unknown suite": dict(body=request(suites="nope")),
            "unsorted": dict(body=request(suites="epic, codex")),
        }
        for name, row in cases.items():
            with self.subTest(name):
                self.setUp()
                body = row.pop("body")
                self.post(body, **row)
                self.assertFalse(self.landed()[0])

    def test_graphql_edit_tip_in_head_and_unknown_tip_do_not_land(self) -> None:
        self.post(request())
        self.graphql_edited = {1}
        self.assertFalse(self.landed()[0])
        self.graphql_edited = set()
        self.compare = {"status": "ahead"}
        self.assertIn("already contains", self.landed()[1])
        self.compare = None
        self.assertIn("does not know", self.landed()[1])

    def test_the_newest_request_counts(self) -> None:
        self.post(request(), at="2026-10-11T01:00:05Z")
        self.post(request(suites="nope"), at="2026-10-11T01:00:07Z")
        self.assertFalse(self.landed()[0])

    def test_without_the_opt_in_nothing_changes(self) -> None:
        self.post(request())
        with patch.dict(os.environ, {"EPIC_FOCUS_ACTIONS": "adopt"}):
            ok, why = self.landed()
            self.assertFalse(ok)
            self.assertIn("did not move", why)
            # fix-checks reads no comments then, as before.
            self.assertFalse([c for c in self.calls if "/comments" in " ".join(c)])
            self.assertFalse(self.landed("fix")[0])

    def test_resolve_conflict_needs_a_moved_head(self) -> None:
        self.post(request())
        self.assertFalse(self.landed("resolve-conflict")[0])


class CooldownTransferTest(unittest.TestCase):
    """An old issue cooldown never moves to a recovery refresh `continue`."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="recovery-cooldown-")
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        now = time.time()
        entry = {"at": now, "until": now + 3600, "reason": "blocked on the issue"}
        key = f"claude:continue:issue:{ISSUES}/907"
        (self.state / tick_cooldown.FILE).write_text(json.dumps({key: entry}))
        self.now = now
        p = patch.object(tick_cooldown, "pr_issue", lambda n: f"{ISSUES}/907")
        p.start()
        self.addCleanup(p.stop)

    def check(self, action: dict[str, Any]) -> int:
        return tick_cooldown.check(
            action, self.state, self.state / "seen.json", self.now
        )

    def test_refresh_continue_runs_and_plain_continue_still_takes_over(self) -> None:
        read = {"tip": TIP, "head": HEAD, "baseTip": NEW_TIP, "compare": "behind"}
        act = decide("Claude", pr(7, "Claude", isDraft=True, recoveryRead=read))[0]
        refresh = json.loads(act.to_json())
        self.assertEqual(refresh["action"], "continue")
        self.assertEqual(self.check(refresh), tick_cooldown.RUN)
        self.assertEqual(
            len(json.loads((self.state / tick_cooldown.FILE).read_text())), 1
        )
        plain = {"action": "continue", "pr": 7, "sha": HEAD}
        self.assertEqual(self.check(plain), tick_cooldown.SKIP)
        # A refresh whose head is not the action's is no typed refresh.
        self.assertIsNone(tick_cooldown.refresh_tip({**refresh, "sha": NEW_HEAD}))


class FreshGitHub:
    """The fresh gh reads of rebase_policy.fresh_pr(), as GitHub is now."""

    def __init__(self, owner: str = "Codex") -> None:
        self.pull = {
            "state": "open", "draft": False, "mergeable": True, "comments": 0,
            "body": f"Executor: {owner}", "base": {"ref": BASE},
            "head": {"sha": HEAD, "ref": "fix/5-x"},
        }  # fmt: skip
        self.rows: list[dict[str, Any]] = []
        self.edited: set[int] = set()
        self.tip = TIP
        self.fail = False
        self.reads = 0

    def post(self, body: str, **kw: Any) -> None:
        row = LegacyReaderTest.rest(5, len(self.rows) + 1, body)
        row.update(kw)
        self.rows.append(row)
        self.pull["comments"] = len(self.rows)

    def read_pr(self, n: int) -> dict[str, Any]:
        self.reads += 1
        if self.fail:
            raise rp.ReadFailed("gh api pulls failed")
        return json.loads(json.dumps(self.pull))

    def comments(self, n: int) -> list[dict[str, Any]]:
        return json.loads(json.dumps(self.rows))

    def evidence(self, n: int, rows: list[dict[str, Any]]) -> None:
        for r in rows:
            r["last_edited_at"] = r["created_at"] if r["id"] in self.edited else None

    def read_json(self, endpoint: str, missing: frozenset[int] = frozenset()) -> Any:
        if endpoint == f"{REPO}/git/ref/heads/{BASE}":
            return {"object": {"sha": self.tip}}
        if endpoint.startswith(f"{REPO}/compare/"):
            return {"status": "behind"}
        raise AssertionError(endpoint)

    def patches(self) -> list[Any]:
        return [
            patch.object(rp, "read_pr", self.read_pr),
            patch.object(rp, "comments", self.comments),
            patch.object(rp, "read_json", self.read_json),
            patch.object(na, "legacy_edit_evidence", self.evidence),
        ]


class RecoveryHoldTest(unittest.TestCase):
    """rebase_policy.recovery_hold(): only the owner's request for the head."""

    def setUp(self) -> None:
        self.github = FreshGitHub()
        for p in self.github.patches():
            p.start()
            self.addCleanup(p.stop)

    def test_identified_requests_hold(self) -> None:
        self.assertIsNone(rp.recovery_hold(5))
        self.github.post(request("Codex"))
        self.assertIn("(valid) holds merge and review", rp.recovery_hold(5) or "")
        self.github.tip = NEW_TIP
        self.assertIn("(refresh)", rp.recovery_hold(5) or "")
        self.github.tip = TIP
        self.github.pull["mergeable"] = None
        self.assertIn("(pending)", rp.recovery_hold(5) or "")

    def test_ignored_requests_hold_nothing(self) -> None:
        for name, body, kw in (
            ("edited", request("Codex"), {"updated_at": "2026-10-11T00:00:09Z"}),
            ("foreign login", request("Codex"), {"user": {"login": "someone"}}),
            ("not the owner", request("Claude"), {}),
            ("old head", request("Codex", head=NEW_HEAD), {}),
            ("malformed", request("Codex") + "\nplease", {}),
            ("prose", "Upstream test recovery by Codex is needed, I think", {}),
        ):
            with self.subTest(name):
                self.setUp()
                self.github.post(body, **kw)
                self.assertIsNone(rp.recovery_hold(5))
        self.setUp()
        self.github.post(request("Codex"))
        self.github.edited = {1}  # a same-second edit, seen only by GraphQL
        self.assertIsNone(rp.recovery_hold(5))


class WriteHoldTest(twc.Base):
    """A request posted after selection and model start holds the merge and
    the verdict right before the write, in both reader modes."""

    MERGE = f"gh pr merge 5 --repo {na.REPO} --squash --match-head-commit {HEAD}"
    VERDICT = f"gh pr comment 5 --body 'Review: APPROVED by Claude at {HEAD}'"

    def setUp(self) -> None:
        super().setUp()
        self.github = FreshGitHub()
        for p in self.github.patches():
            p.start()
            self.addCleanup(p.stop)
        on = patch.dict(os.environ, {"EPIC_FOCUS_ACTIONS": "upstream-recovery"})
        on.start()
        self.addCleanup(on.stop)
        self.reader.pulls[5] = twc.pull(5, head={"ref": "fix/5-x", "sha": HEAD})

    def mode0(self, command: str) -> str | None:
        with patch.dict(os.environ, {"EPIC_SHARED_READER": "0"}):
            return wc.status_refusal("Bash", {"command": command}, str(self.root))

    def test_merge_waits_for_the_owners_request_in_both_modes(self) -> None:
        self.github = FreshGitHub(owner="Claude")
        for p in self.github.patches():
            p.start()
            self.addCleanup(p.stop)
        self.action(action="merge", pr=5, sha=HEAD)
        self.assertIsNone(self.bash(self.MERGE))
        self.assertIsNone(self.mode0(self.MERGE))
        # The owner's session posted a request after this merge was selected.
        self.github.post(request("Claude"))
        self.assertIn("holds merge and review", self.bash(self.MERGE) or "")
        self.assertIn("holds merge and review", self.mode0(self.MERGE) or "")
        # Its base moved on: a refresh still holds the merge.
        self.github.tip = NEW_TIP
        self.assertIn("(refresh)", self.bash(self.MERGE) or "")
        self.assertIn("(refresh)", self.mode0(self.MERGE) or "")

    def test_verdict_waits_for_the_owners_request_in_both_modes(self) -> None:
        self.action(action="review", pr=5, sha=HEAD)
        self.reader.pulls[5]["body"] = "Executor: Codex"
        self.assertIsNone(self.bash(self.VERDICT))
        self.github.post(request("Codex"))
        self.assertIn("holds merge and review", self.bash(self.VERDICT) or "")
        self.assertIn("holds merge and review", self.mode0(self.VERDICT) or "")

    def test_foreign_or_old_requests_are_no_blockers(self) -> None:
        self.action(action="review", pr=5, sha=HEAD)
        self.reader.pulls[5]["body"] = "Executor: Codex"
        self.github.post(request("Codex"), user={"login": "someone"})
        self.github.post(request("Codex", head=NEW_HEAD))
        self.assertIsNone(self.bash(self.VERDICT))
        self.assertIsNone(self.mode0(self.VERDICT))

    def test_unreadable_merge_or_comment_fails_closed_with_reader_off(self) -> None:
        self.action(action="review", pr=5, sha=HEAD)
        hidden = f'gh pr comment 5 --repo {na.REPO} --body "$(cat /tmp/verdict.txt)"'
        self.assertIn("upstream recovery hold", self.mode0(hidden) or "")
        merge = "gh pr " + "merge 5 --squash --match-head-commit $(cat /tmp/sha)"
        self.assertIn("upstream recovery hold", self.mode0(merge) or "")
        # Unrelated unreadable commands and the opt-out stay as they were.
        self.assertIsNone(self.mode0('echo "$(gh pr view 5 --json title)"'))
        self.assertIsNone(self.mode0("git push $(echo origin) fix/5-x"))
        with patch.dict(os.environ, {"EPIC_FOCUS_ACTIONS": "adopt"}):
            self.assertIsNone(self.mode0(hidden))
        self.assertEqual(self.github.reads, 0)

    def test_failed_reads_refuse_and_the_opt_in_gates_all_reads(self) -> None:
        self.action(action="review", pr=5, sha=HEAD)
        self.reader.pulls[5]["body"] = "Executor: Codex"
        self.github.fail = True
        self.assertIn("fresh recovery read failed", self.bash(self.VERDICT) or "")
        self.assertIn("fresh recovery read failed", self.mode0(self.VERDICT) or "")
        reads = self.github.reads
        with patch.dict(os.environ, {"EPIC_FOCUS_ACTIONS": "adopt"}):
            self.assertIsNone(self.bash(self.VERDICT))
            self.assertIsNone(self.mode0(self.VERDICT))
        self.assertEqual(self.github.reads, reads)
        # Reader on: status_refusal leaves the hold to guard(); no double read.
        with patch.dict(os.environ, {"EPIC_SHARED_READER": "1"}):
            refused = wc.status_refusal(
                "Bash", {"command": self.VERDICT}, str(self.root)
            )
            self.assertIsNone(refused)
        self.assertEqual(self.github.reads, reads)


class ExportTest(unittest.TestCase):
    """rebase_policy.export(): a private read-only copy of the PR head or the
    base tip; the runner worktree and its unpublished work stay untouched."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="recovery-export-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))
        self.repo = Repo(self.root)
        self.top = self.repo.top
        (self.root / "tmp").mkdir()
        p = patch.dict(os.environ, {"TMPDIR": str(self.root / "tmp")})
        p.start()
        self.addCleanup(p.stop)
        self.action = {"action": "fix-checks", "pr": 7, "sha": self.repo.head}

    def snapshot(self) -> tuple[str, ...]:
        return (
            git(self.top, "status", "--porcelain", "--untracked-files=all"),
            git(self.top, "rev-parse", "HEAD"),
            git(self.top, "for-each-ref"),
            git(self.top, "worktree", "list", "--porcelain"),
            (self.top / "f.txt").read_text(),
        )

    def test_head_export_is_a_private_copy_and_keeps_the_worktree(self) -> None:
        (self.top / "f.txt").write_text("unpublished edit\n")
        (self.top / "notes.txt").write_text("untracked\n")
        before = self.snapshot()
        out = rp.export(self.repo.head, self.action, self.top)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual((self.top / "notes.txt").read_text(), "untracked\n")
        self.assertTrue(out.is_relative_to(self.root / "tmp"))
        self.assertEqual(out.stat().st_mode & 0o777, 0o700)
        self.assertEqual((out / "f.txt").read_text(), "one\nfeature\n")
        self.assertFalse((out / "notes.txt").exists())
        self.assertFalse((out / "fix.txt").exists())
        self.assertFalse((out / ".git").exists())

    def test_only_the_head_or_the_current_base_tip(self) -> None:
        with (
            patch.object(rp, "base_ref", lambda n: BASE),
            patch.object(rp, "base_tip", lambda ref: self.repo.tip),
        ):
            out = rp.export(self.repo.tip, self.action, self.top)
            self.assertTrue((out / "fix.txt").exists())
            older = git(self.top, "rev-parse", f"{self.repo.tip}~1")
            with self.assertRaisesRegex(ValueError, "neither the PR head"):
                rp.export(older, self.action, self.top)
        with (
            patch.object(rp, "base_ref", lambda n: BASE),
            patch.object(rp, "base_tip", lambda ref: "f" * 40),
        ):
            with self.assertRaisesRegex(rp.Problem, "fetch origin first"):
                rp.export("f" * 40, self.action, self.top)
        with self.assertRaisesRegex(ValueError, "40-char"):
            rp.export("abc", self.action, self.top)
        with self.assertRaises((ValueError, rp.Problem)):
            rp.export(self.repo.head, self.action, self.root / "tmp")
        with self.assertRaisesRegex(ValueError, "PR head"):
            rp.export(self.repo.head, {"action": "fix-checks"}, self.top)

    def test_unsafe_archive_entries_are_refused(self) -> None:
        def archive(*entries: tarfile.TarInfo) -> bytes:
            data = io.BytesIO()
            with tarfile.open(fileobj=data, mode="w") as tar:
                for entry in entries:
                    tar.addfile(entry, io.BytesIO(b"x") if entry.isfile() else None)
            return data.getvalue()

        def entry(
            name: str, kind: bytes = tarfile.REGTYPE, link: str = ""
        ) -> tarfile.TarInfo:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.linkname = link
            info.size = 1 if kind == tarfile.REGTYPE else 0
            return info

        for name, bad in (
            ("dot-dot", entry("../evil")),
            ("absolute", entry("/tmp/evil")),
            ("link out", entry("a/link", tarfile.SYMTYPE, "../../evil")),
            ("absolute link", entry("link", tarfile.SYMTYPE, "/etc/passwd")),
            ("hard link", entry("hard", tarfile.LNKTYPE, "f.txt")),
            ("fifo", entry("pipe", tarfile.FIFOTYPE)),
        ):
            with self.subTest(name):
                out = self.root / f"out-{name}"
                out.mkdir()
                with self.assertRaises(rp.Problem):
                    rp.extract(archive(entry("ok.txt"), bad), out)
                self.assertEqual(list(out.iterdir()), [])
        self.assertFalse((self.root / "evil").exists())
        # Without the data filter (older Python) no link is unpacked at all:
        # chained links could leave the copy.
        chained = archive(
            entry("ok.txt"),
            entry("a", tarfile.SYMTYPE, "."),
            entry("b", tarfile.SYMTYPE, "a/../outside"),
        )
        out = self.root / "out-old-python"
        out.mkdir()
        with patch.object(rp.tarfile, "data_filter", None):
            with self.assertRaisesRegex(rp.Problem, "data filter"):
                rp.extract(chained, out)
        self.assertEqual(list(out.iterdir()), [])
        out = self.root / "out-ok"
        out.mkdir()
        rp.extract(
            archive(entry("d/ok.txt"), entry("d/link", tarfile.SYMTYPE, "ok.txt")), out
        )
        self.assertEqual((out / "d/link").read_text(), "x")

    def test_cli_prints_the_copy(self) -> None:
        action_file = self.root / "action.json"
        action_file.write_text(json.dumps(self.action))
        env = {
            **os.environ,
            "EPIC_ACTION_FILE": str(action_file),
            "EPIC_WORKTREE": str(self.top),
        }
        out = subprocess.run(
            [sys.executable, str(Path(rp.__file__)), "export", "--sha", self.repo.head],
            capture_output=True, text=True, timeout=120, env=env,
        )  # fmt: skip
        self.assertEqual(out.returncode, rp.RUN, out.stderr)
        sha, path = out.stdout.split()[1:3]
        self.assertEqual(sha, self.repo.head)
        self.assertEqual(Path(path, "f.txt").read_text(), "one\nfeature\n")
        env.pop("EPIC_WORKTREE")
        out = subprocess.run(
            [sys.executable, str(Path(rp.__file__)), "export", "--sha", self.repo.head],
            capture_output=True, text=True, timeout=120, env=env,
        )  # fmt: skip
        self.assertEqual(out.returncode, rp.BAD, out.stderr)


class ExportAdmissionTest(unittest.TestCase):
    """permission_gate approves exactly the trusted export call."""

    def rule(self, command: str) -> bool:
        return permission_gate.rule("Bash", {"command": command})[0]

    def test_exact_call_only(self) -> None:
        script = permission_gate.EXPORT_SCRIPT
        self.assertTrue(self.rule(f"python3 {script} export --sha {HEAD}"))
        self.assertTrue(self.rule(f"{sys.executable} {script} export --sha {HEAD}"))
        for command in (
            f"python3 /tmp/rebase_policy.py export --sha {HEAD}",
            f"python3 {script} export --sha {HEAD} --out /tmp/x",
            f"python3 {script} export --sha abc",
            f"python3 {script} export {HEAD}",
            f"python3 {script} prove --sha {HEAD}",
            f"python {script} export --sha {HEAD}",
            f"/bin/sh {script} export --sha {HEAD}",
            f"EPIC_WORKTREE=/x python3 {script} export --sha {HEAD}",
            f"python3 {script} export --sha {HEAD} && rm -rf /tmp/x",
            f"cd /tmp; python3 {script} export --sha {HEAD}",
            f"python3 {os.path.relpath(script)} export --sha {HEAD}",
            f"python3 scripts/epic/rebase_policy.py export --sha {HEAD}",
        ):
            with self.subTest(command):
                self.assertFalse(self.rule(command))


class WorktreeTest(unittest.TestCase):
    """A recovery model starts only from a clean checkout at the PR head or
    from this attempt's recorded rebase. Other work stays untouched."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="recovery-worktree-")
        self.addCleanup(tmp.cleanup)
        self.repo = Repo(Path(os.path.realpath(tmp.name)))
        self.top = self.repo.top

    def record(self, **kw: Any) -> dict[str, Any]:
        data = {
            "pr": 7, "worktree": str(self.top), "base": BASE, "sha": self.repo.head,
            "onto": self.repo.tip, "orig": self.repo.head, "conflicted": [],
        }  # fmt: skip
        data.update(kw)
        return data

    def problem(self, record: dict[str, Any] | None = None) -> str | None:
        return rp.recovery_worktree_problem(
            self.top, "fix/7-x", self.repo.head, self.repo.tip, record
        )

    def test_clean_checkout_at_the_head_is_ready(self) -> None:
        self.assertIsNone(self.problem())

    def test_dirty_and_unpublished_work_stays(self) -> None:
        (self.top / "notes.txt").write_text("untracked\n")
        self.assertIn("untracked", self.problem() or "")
        self.assertTrue((self.top / "notes.txt").exists())
        (self.top / "notes.txt").unlink()
        (self.top / "f.txt").write_text("edited\n")
        self.assertIn("uncommitted", self.problem() or "")
        self.assertEqual((self.top / "f.txt").read_text(), "edited\n")
        git(self.top, "commit", "-q", "-am", "local only")
        local = git(self.top, "rev-parse", "HEAD")
        self.assertIn("unpublished", self.problem(self.record()) or "")
        self.assertEqual(git(self.top, "rev-parse", "HEAD"), local)
        git(self.top, "switch", "-q", "-c", "other")
        self.assertIn("not on fix/7-x", self.problem() or "")

    def test_recorded_rebase_resumes_and_other_rebases_stay(self) -> None:
        # A conflicting rebase left in progress onto the attempt's tip.
        (self.top / "fix.txt").write_text("local\n")
        git(self.top, "add", "fix.txt")
        git(self.top, "commit", "-q", "-m", "clash")
        head = git(self.top, "rev-parse", "HEAD")
        git(self.top, "push", "-q", "origin", "fix/7-x")
        out = subprocess.run(
            ["git", "rebase", f"origin/{BASE}"], cwd=self.top, capture_output=True,
            env={**os.environ, **GIT_ENV},
        )  # fmt: skip
        self.assertNotEqual(out.returncode, 0)
        own = self.record(sha=head, orig=head)
        problem = rp.recovery_worktree_problem(
            self.top, "fix/7-x", head, self.repo.tip, own
        )
        self.assertIsNone(problem)
        # The attempt pins a newer tip: the model aborts this recorded rebase
        # through the gate and starts again (the existing old-tip rule).
        problem = rp.recovery_worktree_problem(self.top, "fix/7-x", head, "f" * 40, own)
        self.assertIsNone(problem)
        for name, record in (
            ("no record", None),
            ("aborted", {**own, "aborted": True}),
            ("other onto", {**own, "onto": self.repo.head}),
            ("other head", {**own, "sha": self.repo.head, "orig": self.repo.head}),
            ("other worktree", {**own, "worktree": "/elsewhere"}),
            ("not a record", "x"),
        ):
            with self.subTest(name):
                problem = rp.recovery_worktree_problem(
                    self.top, "fix/7-x", head, self.repo.tip, record
                )
                self.assertIn("did not record", problem or "")
        self.assertTrue((self.top / ".git" / "rebase-merge").exists())

    def test_finished_recorded_rebase_awaits_its_push(self) -> None:
        rebased = self.repo.rebase()
        self.assertIsNone(self.problem(self.record()))
        self.assertIn("unpublished", self.problem() or "")
        self.assertIn("unpublished", self.problem(self.record(onto=NEW_TIP)) or "")
        # Finished onto a tip older than the attempt's: it cannot be aborted,
        # so it stays for Anton and no model starts.
        older = rp.recovery_worktree_problem(
            self.top, "fix/7-x", self.repo.head, "f" * 40, self.record()
        )
        self.assertIn("older tip", older or "")
        self.assertEqual(git(self.top, "rev-parse", "HEAD"), rebased)

    def test_records_of_another_pr_or_base_never_start_a_model(self) -> None:
        state = self.repo.root / "state"
        state.mkdir()
        recovery = {**evidence(7), "head": self.repo.head, "tip": self.repo.tip}
        action = {"action": "resolve-conflict", "pr": 7, "sha": self.repo.head,
                  "recovery": recovery}  # fmt: skip
        rebased = self.repo.rebase()  # finished onto the tip, not pushed
        for name, record in (
            ("this PR and base", self.record()),
            ("other PR", self.record(pr=8)),
            ("other base", self.record(base="dev/streaming-architecture")),
        ):
            with self.subTest(name, state="completed"):
                (state / "claude-rebases.json").write_text(
                    json.dumps({"fix/7-x": record})
                )
                with patch.object(runner_worktree, "STATE_DIR", state):
                    if name == "this PR and base":
                        runner_worktree.recovery_ready(
                            "claude", action, self.top, "fix/7-x"
                        )
                        continue
                    with self.assertRaisesRegex(runner_worktree.Stop, "unpublished"):
                        runner_worktree.recovery_ready(
                            "claude", action, self.top, "fix/7-x"
                        )
        self.assertEqual(git(self.top, "rev-parse", "HEAD"), rebased)
        # Active: a rebase in progress that another PR or base recorded.
        git(self.top, "reset", "-q", "--hard", self.repo.head)
        (self.top / "fix.txt").write_text("local\n")
        git(self.top, "add", "fix.txt")
        git(self.top, "commit", "-q", "-m", "clash")
        head = git(self.top, "rev-parse", "HEAD")
        subprocess.run(["git", "rebase", f"origin/{BASE}"], cwd=self.top,
                       capture_output=True, env={**os.environ, **GIT_ENV})  # fmt: skip
        action = {**action, "sha": head, "recovery": {**recovery, "head": head}}
        own = self.record(sha=head, orig=head)
        for name, record in (
            ("other PR", {**own, "pr": 8}),
            ("other base", {**own, "base": "dev/streaming-architecture"}),
        ):
            with self.subTest(name, state="active"):
                (state / "claude-rebases.json").write_text(
                    json.dumps({"fix/7-x": record})
                )
                with patch.object(runner_worktree, "STATE_DIR", state):
                    with self.assertRaisesRegex(runner_worktree.Stop, "did not record"):
                        runner_worktree.recovery_ready(
                            "claude", action, self.top, "fix/7-x"
                        )
        (state / "claude-rebases.json").write_text(json.dumps({"fix/7-x": own}))
        with patch.object(runner_worktree, "STATE_DIR", state):
            runner_worktree.recovery_ready("claude", action, self.top, "fix/7-x")

    def test_runner_worktree_refuses_before_the_model(self) -> None:
        state = self.repo.root / "state"
        state.mkdir()
        action = {"action": "resolve-conflict", "pr": 7, "sha": self.repo.head,
                  "recovery": {**evidence(7), "head": self.repo.head, "tip": self.repo.tip}}  # fmt: skip
        with patch.object(runner_worktree, "STATE_DIR", state):
            runner_worktree.recovery_ready("claude", action, self.top, "fix/7-x")
            (self.top / "f.txt").write_text("edited\n")
            with self.assertRaisesRegex(runner_worktree.Stop, "recovery cannot start"):
                runner_worktree.recovery_ready("claude", action, self.top, "fix/7-x")
            # A plain conflict keeps today's resume.
            plain = {k: v for k, v in action.items() if k != "recovery"}
            runner_worktree.recovery_ready("claude", plain, self.top, "fix/7-x")
            (state / "claude-rebases.json").write_text("not json")
            with self.assertRaisesRegex(runner_worktree.Stop, "unreadable"):
                runner_worktree.recovery_ready("claude", action, self.top, "fix/7-x")


if __name__ == "__main__":
    unittest.main()
