"""Focus items: routing, adopt, discovery and --status reasons.

Run: python3 -m unittest discover -s scripts/epic
"""

from __future__ import annotations

import isolated_env  # noqa: F401  (first: hides live runner state)

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import github_quota
import next_action
import permission_gate
import tick_verify
from next_action import body_digest, decide, read_actions, status
from routing import load_rules, route
from test_next_action import A, R, item, pr

EPIC_URL = "https://github.com/phaabe/live.moafunk.de/issues/312"
RULES = [
    {"pattern": ".claude/**", "owners": ["Claude"], "lanes": ["setup"]},
    {"pattern": ".codex/**", "owners": ["Codex"], "lanes": ["setup"]},
    {"pattern": "scripts/epic/**", "owners": ["Claude"], "lanes": ["setup"]},
    {"pattern": "backend/src/**", "owners": ["Claude"], "lanes": ["backend"]},
    {
        "pattern": "docs/implementation/**",
        "owners": ["Claude", "Codex"],
        "lanes": ["setup"],
    },
]
FOCUS = frozenset({"project::AgentMonitoring"})
ADOPT = frozenset({"adopt"})
ORIGINAL = "Adds the cockpit.\r\n\r\nDetails here."


def ownerless(number: int = 484, **kw) -> dict:
    fields = {
        "body": ORIGINAL,
        "labels": [{"name": "project::AgentMonitoring"}],
        "files": ["scripts/epic/monitor.py"],
        "updatedAt": "2026-09-29T10:00:00Z",
        **kw,
    }
    return pr(number, "Claude", **fields)


def actions(agent: str, prs: list, focus=FOCUS, enabled=ADOPT, **state) -> list:
    return decide(
        agent, {"prs": prs, **state}, focus=focus, enabled=enabled, rules=RULES
    )


class RouteTest(unittest.TestCase):
    def test_executor_always_wins(self) -> None:
        got = route("Codex", ["scripts/epic/x.py"], RULES, "adopt")
        self.assertEqual(got.agent, "Codex")

    def test_one_owner_for_all_files(self) -> None:
        got = route(None, ["scripts/epic/a.py", ".claude/b.md"], RULES, "adopt")
        self.assertEqual((got.agent, got.lane), ("Claude", "setup"))

    def test_shared_file_follows_the_other_files(self) -> None:
        got = route(None, [".codex/x.sh", "docs/implementation/y.md"], RULES, "adopt")
        self.assertEqual((got.agent, got.lane), ("Codex", "setup"))

    def test_owners_differ_needs_anton(self) -> None:
        got = route(None, ["scripts/epic/a.py", ".codex/b.sh"], RULES, "adopt")
        self.assertIsNone(got.agent)
        self.assertTrue(got.reason.startswith("needs-anton"))

    def test_only_shared_files_need_anton(self) -> None:
        got = route(None, ["docs/implementation/y.md"], RULES, "adopt")
        self.assertIsNone(got.agent)

    def test_file_without_rule_needs_anton(self) -> None:
        got = route(None, ["frontend/x.ts"], RULES, "adopt")
        self.assertIsNone(got.agent)
        self.assertIn("frontend/x.ts", got.reason)

    def test_lanes_differ_needs_anton(self) -> None:
        got = route(None, ["scripts/epic/a.py", "backend/src/b.rs"], RULES, "adopt")
        self.assertIsNone(got.agent)
        self.assertIn("lanes", got.reason)

    def test_no_files_only_claude_refines(self) -> None:
        self.assertEqual(route(None, None, RULES, "refine").agent, "Claude")
        self.assertIsNone(route(None, [], RULES, "adopt").agent)

    def test_real_lane_map_loads(self) -> None:
        got = route(None, ["scripts/epic/next_action.py"], load_rules(), "adopt")
        self.assertEqual((got.agent, got.lane), ("Claude", "setup"))


class AdoptDecideTest(unittest.TestCase):
    def test_routed_agent_adopts(self) -> None:
        got = actions("Claude", [ownerless()])
        adopt = [a for a in got if a.action == "adopt"]
        self.assertEqual(len(adopt), 1)
        a = adopt[0]
        self.assertEqual((a.pr, a.sha, a.lane), (484, A, "setup"))
        self.assertEqual(a.body_sha, body_digest(ORIGINAL))
        self.assertEqual(a.updated_at, "2026-09-29T10:00:00Z")
        self.assertIn('"action": "adopt"', a.to_json())

    def test_other_agent_does_not_adopt(self) -> None:
        got = actions("Codex", [ownerless()])
        self.assertNotIn("adopt", [a.action for a in got])

    def test_owners_differ_nobody_adopts(self) -> None:
        mixed = ownerless(files=["scripts/epic/a.py", ".codex/b.sh"])
        for agent in ("Claude", "Codex"):
            self.assertNotIn("adopt", [a.action for a in actions(agent, [mixed])])

    def test_pr_with_any_owner_line_is_never_adopted(self) -> None:
        for line in ("Executor: Claude", "Author: Codex", "Reviewer: Codex"):
            with self.subTest(line=line):
                owned = ownerless(body=f"{ORIGINAL}\n{line}")
                got = actions("Claude", [owned]) + actions("Codex", [owned])
                self.assertNotIn("adopt", [a.action for a in got])

    def test_draft_of_other_agent_is_not_adopted_or_continued(self) -> None:
        draft = ownerless(body="Executor: Codex", isDraft=True)
        got = actions("Claude", [draft])
        self.assertEqual([a.action for a in got], ["idle"])

    def test_board_executor_wins_over_files(self) -> None:
        board = {
            "executor": "Codex",
            "content": {
                "type": "PullRequest",
                "number": 484,
                "url": "https://github.com/phaabe/live.moafunk.de/pull/484",
            },
        }
        got = actions("Codex", [ownerless()], items=[board])
        self.assertIn("adopt", [a.action for a in got])
        self.assertNotIn(
            "adopt", [a.action for a in actions("Claude", [ownerless()], items=[board])]
        )

    def test_foreign_board_pr_does_not_override_the_owner(self) -> None:
        # Codex review on PR 497: a PR of another repository with the same number.
        foreign = {
            "executor": "Codex",
            "content": {
                "type": "PullRequest",
                "number": 484,
                "url": "https://github.com/other/repo/pull/484",
            },
        }
        self.assertIn(
            "adopt",
            [a.action for a in actions("Claude", [ownerless()], items=[foreign])],
        )
        self.assertNotIn(
            "adopt",
            [a.action for a in actions("Codex", [ownerless()], items=[foreign])],
        )

    def test_unknown_owner_line_is_never_adopted(self) -> None:
        # Codex review on PR 497: any owner line blocks adopt, known agent or not.
        for line in (
            "Executor: Anton",
            "Author: human",
            "Reviewer: TBD",
            "Executor: REPLACE",
            "Executor:",
            "  Reviewer: TBD",
        ):
            with self.subTest(line=line):
                owned = ownerless(body=f"{ORIGINAL}\n{line}")
                got = actions("Claude", [owned]) + actions("Codex", [owned])
                self.assertNotIn("adopt", [a.action for a in got])
                text = status({"prs": [owned]}, False, FOCUS, ADOPT, RULES)
                self.assertIn("owner line names no known agent", text)

    def test_cross_owner_rename_needs_anton(self) -> None:
        # Codex review on PR 497: the old path of a rename counts, as in epic-guard.
        rows = [
            {
                "filename": "scripts/epic/foo.py",
                "previous_filename": ".codex/foo.py",
                "status": "renamed",
            },
            {"filename": "scripts/epic/bar.py", "status": "modified"},
        ]
        files = next_action.changed_paths(rows)
        self.assertEqual(
            files, [".codex/foo.py", "scripts/epic/bar.py", "scripts/epic/foo.py"]
        )
        moved = ownerless(files=files)
        for agent in ("Claude", "Codex"):
            self.assertNotIn("adopt", [a.action for a in actions(agent, [moved])])
        text = status({"prs": [moved]}, False, FOCUS, ADOPT, RULES)
        self.assertIn("needs-anton: files have different owners", text)

    def test_adopt_only_when_listed(self) -> None:
        got = actions("Claude", [ownerless()], enabled=frozenset())
        self.assertNotIn("adopt", [a.action for a in got])

    def test_empty_focus_adopts_nothing(self) -> None:
        got = actions("Claude", [ownerless()], focus=frozenset())
        self.assertNotIn("adopt", [a.action for a in got])

    def test_pr_outside_focus_is_not_adopted(self) -> None:
        other = ownerless(labels=[{"name": "project::Stream"}])
        self.assertNotIn("adopt", [a.action for a in actions("Claude", [other])])

    def test_needs_anton_label_blocks_adopt(self) -> None:
        blocked = ownerless(
            labels=[{"name": "project::AgentMonitoring"}, {"name": "needs-anton"}]
        )
        self.assertNotIn("adopt", [a.action for a in actions("Claude", [blocked])])

    def test_pause_comes_first(self) -> None:
        got = decide(
            "Claude", {"prs": [ownerless()]}, paused=True, focus=FOCUS, enabled=ADOPT
        )
        self.assertEqual([a.action for a in got], ["stop"])

    def test_adopt_sits_between_continue_and_claim(self) -> None:
        draft = pr(
            10, "Claude", isDraft=True, labels=[{"name": "project::AgentMonitoring"}]
        )
        ready = item(20, "Claude", "Ready", labels=["project::AgentMonitoring"])
        got = actions("Claude", [ownerless(), draft], items=[ready])
        self.assertEqual([a.action for a in got][:2], ["continue", "adopt"])
        got = actions("Claude", [ownerless()], items=[ready])
        self.assertEqual([a.action for a in got], ["adopt", "claim"])

    def test_read_actions(self) -> None:
        self.assertEqual(read_actions("adopt, refine,close-out"), ADOPT)
        self.assertEqual(read_actions(None), frozenset())
        self.assertEqual(read_actions("merge"), frozenset())


class BodyDigestTest(unittest.TestCase):
    def test_added_owner_lines_keep_the_digest(self) -> None:
        adopted = (
            f"Epic: {EPIC_URL}\nExecutor: Claude\nLane: setup\nReviewer: Codex\n"
            f"Leaf IDs: setup\nIssue: {R}/488\n\n{ORIGINAL}"
        )
        self.assertEqual(body_digest(adopted), body_digest(ORIGINAL))

    def test_changed_text_changes_the_digest(self) -> None:
        self.assertNotEqual(body_digest("Adds the cockpit."), body_digest(ORIGINAL))


class StatusReasonTest(unittest.TestCase):
    def status_lines(self, state: dict, enabled=frozenset()) -> str:
        return status(state, False, FOCUS, enabled, RULES)

    def test_every_focus_item_without_action_has_a_reason(self) -> None:
        state = {
            "prs": [
                ownerless(),
                ownerless(485, files=["scripts/epic/a.py", ".codex/b.sh"]),
                pr(
                    486,
                    "Codex",
                    isDraft=True,
                    labels=[{"name": "project::AgentMonitoring"}],
                ),
                pr(
                    487,
                    "Claude",
                    labels=[
                        {"name": "project::AgentMonitoring"},
                        {"name": "needs-anton"},
                    ],
                ),
            ],
            "items": [
                item(30, "Claude", "Backlog", labels=["project::AgentMonitoring"]),
            ],
            "focus_issues": [
                {"number": 31, "url": f"{R}/31", "labels": ["project::AgentMonitoring"]}
            ],
        }
        text = self.status_lines(state)
        claude = text.split("\nCodex:")[0]
        self.assertIn("PR 484", claude)
        self.assertIn("adopt is not in EPIC_FOCUS_ACTIONS", claude)
        self.assertIn("needs-anton: files have different owners", claude)
        self.assertIn("draft of Codex", claude)
        self.assertIn("PR 487", claude)
        self.assertIn("blocked: status Backlog", claude)
        self.assertIn(f"{R}/31", claude)
        self.assertIn("not on board", claude)
        codex = text.split("\nCodex:")[1]
        self.assertIn("routed to Claude", codex)

    def test_adopted_pr_is_listed_as_action(self) -> None:
        text = self.status_lines({"prs": [ownerless()]}, ADOPT)
        self.assertIn("adopt", text.split("\nCodex:")[0])
        self.assertNotIn("no action", text.split("\nCodex:")[0])

    def test_no_reasons_without_focus(self) -> None:
        text = status({"prs": [ownerless()]}, False, frozenset(), ADOPT, RULES)
        self.assertNotIn("no action", text)


class FocusSearchTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        patcher = patch.object(next_action, "STATE_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls: list[list[str]] = []

    def fetch(self, pages_by_label: dict[str, list[dict]]):
        def fetch(args: list[str]):
            self.calls.append(args)
            label = next(k for k in pages_by_label if k.replace(":", "%3A") in args[-1])
            return pages_by_label[label]

        return fetch

    @staticmethod
    def row(n: int, **kw) -> dict:
        return {"number": n, "html_url": f"{R}/{n}", "labels": [], **kw}

    def test_pages_are_joined_without_duplicates_or_prs(self) -> None:
        pages = {
            "project::A": [
                {"items": [self.row(3), self.row(1)]},
                {"items": [self.row(2), self.row(4, pull_request={})]},
            ],
            "project::B": [{"items": [self.row(1), self.row(5)]}],
        }
        got = next_action.focus_issues(
            frozenset(pages), self.fetch(pages), now=lambda: 0.0
        )
        self.assertEqual([i["number"] for i in got], [1, 2, 3, 5])
        self.assertTrue(all("--paginate" in c for c in self.calls))
        self.assertTrue(all("is%3Aissue" in c[-1] for c in self.calls))

    def test_incomplete_search_raises(self) -> None:
        pages = {"project::A": [{"items": [], "incomplete_results": True}]}
        with self.assertRaises(ValueError):
            next_action.focus_issues(
                frozenset(pages), self.fetch(pages), now=lambda: 0.0
            )

    def test_stored_quota_wait_stops_before_any_search(self) -> None:
        github_quota.record(self.dir, 1000.0, "2099-01-01T00:00:00Z")
        pages = {"project::A": [{"items": []}]}
        with self.assertRaises(next_action.QuotaWait):
            next_action.focus_issues(
                frozenset(pages), self.fetch(pages), now=lambda: 1000.0
            )
        self.assertEqual(self.calls, [])


class VerifyAdoptTest(unittest.TestCase):
    ACTION = {
        "action": "adopt",
        "pr": 484,
        "sha": A,
        "lane": "setup",
        "body_sha": body_digest(ORIGINAL),
    }
    GOOD = (
        f"Epic: {EPIC_URL}\nExecutor: Claude\nLane: setup\nReviewer: Codex\n"
        f"Leaf IDs: setup\nIssue: {R}/488\n\n{ORIGINAL}"
    )

    def landed(self, body: str, agent: str = "claude") -> bool:
        return tick_verify.landed(
            agent, self.ACTION, "2026-09-29T00:00:00Z", lambda _: {"body": body}
        )[0]

    def test_owner_lines_and_original_body_land(self) -> None:
        self.assertTrue(self.landed(self.GOOD))

    def test_missing_line_does_not_land(self) -> None:
        for key in ("Epic", "Executor", "Lane", "Reviewer", "Leaf IDs", "Issue"):
            with self.subTest(key=key):
                body = "\n".join(
                    line
                    for line in self.GOOD.splitlines()
                    if not line.startswith(f"{key}:")
                )
                self.assertFalse(self.landed(body))

    def test_lost_original_does_not_land(self) -> None:
        self.assertFalse(self.landed(self.GOOD.replace("Details here.", "")))

    def test_wrong_agent_does_not_land(self) -> None:
        self.assertFalse(self.landed(self.GOOD, agent="codex"))

    def test_unknown_action_never_passes(self) -> None:
        with self.assertRaises(ValueError):
            tick_verify.landed("claude", {"action": "refine"}, "x", lambda _: {})

    def test_known_unchecked_actions_pass(self) -> None:
        for kind in ("continue", "claim", "escalate", "idle", "stop"):
            self.assertTrue(
                tick_verify.landed("claude", {"action": kind}, "x", lambda _: {})[0]
            )


class PermissionBodyEditTest(unittest.TestCase):
    PATCH = "gh api --method PATCH repos/phaabe/live.moafunk.de/pulls/484 -F body=@"
    ADOPT = {"action": "adopt", "pr": 484}

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.body_dir = self.tmp / "body"
        self.body_dir.mkdir()
        self.body = self.body_dir / "body.md"
        self.body.write_text("Executor: Claude\n")
        self.command = self.PATCH + str(self.body)
        # What the runner records when it creates the directory.
        info = os.stat(self.body_dir)
        self.anchor = f"{info.st_dev}:{info.st_ino}"

    def allowed(
        self, command: str, action: dict | None, anchor: str | None = None
    ) -> bool:
        env = {"EPIC_BODY_DIR_ID": self.anchor if anchor is None else anchor}
        if action is not None:
            path = self.tmp / "action.json"
            path.write_text(json.dumps(action))
            env["EPIC_ACTION_FILE"] = str(path)
        with patch.dict(os.environ, env, clear=False):
            if action is None:
                os.environ.pop("EPIC_ACTION_FILE", None)
            return permission_gate.decide("Bash", {"command": command})[0]

    def test_adopt_tick_may_edit_its_pr_body(self) -> None:
        self.assertTrue(self.allowed(self.command, self.ADOPT))

    def test_other_pr_or_action_is_denied(self) -> None:
        self.assertFalse(self.allowed(self.command, {"action": "adopt", "pr": 485}))
        self.assertFalse(self.allowed(self.command, {"action": "fix", "pr": 484}))
        self.assertFalse(self.allowed(self.command, None))

    def test_body_file_only_directly_in_body_dir(self) -> None:
        outside = self.tmp / "secret.txt"
        outside.write_text("token")
        nested = self.body_dir / "sub"
        nested.mkdir()
        (nested / "b.md").write_text("x")
        link = self.body_dir / "link.md"
        link.symlink_to(outside)
        for path in (
            outside,
            nested / "b.md",
            link,
            self.body_dir / "missing.md",
            nested,
            self.body_dir / "sub" / ".." / "body.md",
        ):
            with self.subTest(path=path):
                self.assertFalse(self.allowed(self.PATCH + str(path), self.ADOPT))
        self.assertFalse(self.allowed(self.PATCH + "body.md", self.ADOPT))

    def test_body_dir_anchor_must_match(self) -> None:
        other = self.tmp / "other"
        other.mkdir()
        info = os.stat(other)
        for anchor in ("", "x", f"{info.st_dev}:{info.st_ino}"):
            with self.subTest(anchor=anchor):
                self.assertFalse(self.allowed(self.command, self.ADOPT, anchor))

    def test_body_dir_given_through_a_symlink_still_matches(self) -> None:
        alias = self.tmp / "alias"
        alias.symlink_to(self.body_dir)
        self.assertTrue(self.allowed(self.PATCH + str(alias / "body.md"), self.ADOPT))

    def test_replaced_body_dir_is_denied(self) -> None:
        # The session renames the runner's directory away and puts a symlink
        # to another directory (or a new directory) at the same path.
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "body.md").write_text("secret")
        self.body_dir.rename(self.tmp / "saved-body")
        self.body_dir.symlink_to(outside)
        self.assertFalse(self.allowed(self.command, self.ADOPT))
        self.body_dir.unlink()
        self.body_dir.mkdir()
        (self.body_dir / "body.md").write_text("new")
        self.assertFalse(self.allowed(self.command, self.ADOPT))

    def test_hard_link_to_another_file_is_denied(self) -> None:
        outside = self.tmp / "secret.txt"
        outside.write_text("token")
        linked = self.body_dir / "linked.md"
        os.link(outside, linked)
        self.assertFalse(self.allowed(self.PATCH + str(linked), self.ADOPT))

    def test_other_api_shapes_are_denied(self) -> None:
        b = self.body
        for command in (
            "gh api repos/phaabe/live.moafunk.de/pulls/484",
            f"gh api --method PATCH repos/phaabe/live.moafunk.de/pulls/484 -f body=@{b}",
            f"gh api --method PATCH repos/phaabe/live.moafunk.de/pulls/484 -F title=@{b}",
            f"gh api --method PATCH repos/other/repo/pulls/484 -F body=@{b}",
            f"gh api --method PATCH repos/phaabe/live.moafunk.de/issues/484 -F body=@{b}",
            f"gh api --method PUT repos/phaabe/live.moafunk.de/pulls/484/merge -F body=@{b}",
            "gh api --method PATCH repos/phaabe/live.moafunk.de/pulls/484 -F body=@../b",
            f"gh api --method PATCH repos/phaabe/live.moafunk.de/pulls/484 -F body=@{b} -F state=closed",
        ):
            with self.subTest(command=command):
                self.assertFalse(self.allowed(command, self.ADOPT))


if __name__ == "__main__":
    unittest.main()
