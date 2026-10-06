"""Refinement record and Definition of Ready tests.

Run: python3 -m unittest discover -s scripts/epic.
"""

from __future__ import annotations

import isolated_env  # noqa: F401  (first: hides live runner state)

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import next_action
import rebase_policy as rp
import refinement as r

ISSUE = 900
BASE = f"https://github.com/phaabe/live.moafunk.de/issues/{ISSUE}"


def comment(
    cid: int, body: str, minute: int, edited: bool = False
) -> dict[str, object]:
    return {
        "id": cid,
        "body": body,
        "createdAt": f"2026-10-06T10:{minute:02d}:00Z",
        "url": f"{BASE}#issuecomment-{cid}",
        "includesCreatedEdit": edited,
    }


def proposal_data(**over: object) -> dict[str, object]:
    data: dict[str, object] = {
        "request": "> make the thing",
        "proposer": "Claude",
        "executor": "Codex",
        "leaves": ["setup"],
        "files": ["scripts/epic/x.py"],
        "depends_on": [],
        "labels": ["type::ci", "project::AgentSetup"],
        "acceptance_criteria": ["x works"],
        "scope": "x only",
    }
    data.update(over)
    return data


def proposal_body(data: dict[str, object], pretty: bool = False) -> str:
    text = json.dumps(data, indent=2 if pretty else None)
    return f"{r.PROPOSAL_MARKER}\n```json\n{text}\n```"


def verdict_body(state: str, by: str, digest: str) -> str:
    return f"Refinement: {state} by {by} at {digest}"


def item(**over: object) -> dict[str, object]:
    found: dict[str, object] = {
        "content": {"type": "Issue", "number": ISSUE, "url": BASE},
        "executor": "Codex",
        "status": "Backlog",
        "labels": ["type::ci", "project::AgentSetup", "refinement"],
    }
    found.update(over)
    return found


def approved(data: dict[str, object] | None = None) -> list[dict[str, object]]:
    rows = [comment(10, proposal_body(data or proposal_data()), 1)]
    p, _ = r.latest_proposal(ISSUE, rows)
    assert p is not None
    rows.append(comment(11, verdict_body("APPROVED", "Codex", p.digest), 2))
    return rows


def ready_problems(rows: list[dict[str, object]], **over: object) -> list[str]:
    p, _ = r.latest_proposal(ISSUE, rows)
    v = r.current_verdict(p, rows) if p else None
    return next_action.definition_of_ready(item(**over), p, v)


class Digest(unittest.TestCase):
    def test_stable_under_key_order(self) -> None:
        data = proposal_data()
        flipped = dict(reversed(list(data.items())))
        self.assertEqual(r.digest(ISSUE, 1, data), r.digest(ISSUE, 1, flipped))

    def test_new_comment_same_content_new_digest(self) -> None:
        data = proposal_data()
        self.assertNotEqual(r.digest(ISSUE, 1, data), r.digest(ISSUE, 2, data))

    def test_pretty_and_compact_json_same_digest(self) -> None:
        a, _ = r.latest_proposal(ISSUE, [comment(5, proposal_body(proposal_data()), 1)])
        b, _ = r.latest_proposal(
            ISSUE, [comment(5, proposal_body(proposal_data(), pretty=True), 1)]
        )
        assert a and b
        self.assertEqual(a.digest, b.digest)


class Proposals(unittest.TestCase):
    def test_no_proposal(self) -> None:
        self.assertEqual(r.latest_proposal(ISSUE, [comment(1, "hi", 1)]), (None, None))

    def test_edited_newest_blocks_without_fallback(self) -> None:
        rows = [
            comment(1, proposal_body(proposal_data()), 1),
            comment(2, proposal_body(proposal_data(scope="y")), 2, edited=True),
        ]
        p, problem = r.latest_proposal(ISSUE, rows)
        self.assertIsNone(p)
        self.assertIn("edited", problem or "")

    def test_malformed_and_missing_keys(self) -> None:
        bad = f"{r.PROPOSAL_MARKER}\nnot json"
        self.assertIsNone(r.latest_proposal(ISSUE, [comment(1, bad, 1)])[0])
        data = proposal_data()
        del data["scope"]
        _, problem = r.latest_proposal(ISSUE, [comment(1, proposal_body(data), 1)])
        self.assertIn("scope", problem or "")

    def test_bad_proposer(self) -> None:
        body = proposal_body(proposal_data(proposer="Anton"))
        self.assertIsNone(r.latest_proposal(ISSUE, [comment(1, body, 1)])[0])

    def test_enrolled_by_label_or_proposal(self) -> None:
        rows = [comment(1, proposal_body(proposal_data()), 1)]
        self.assertTrue(r.is_enrolled({"refinement"}, []))
        # Label removed after a proposal: still enrolled.
        self.assertTrue(r.is_enrolled(set(), rows))
        self.assertFalse(r.is_enrolled(set(), [comment(1, "hi", 1)]))


class Verdicts(unittest.TestCase):
    def test_edited_verdict_ignored(self) -> None:
        rows = approved()
        rows[1]["includesCreatedEdit"] = True
        p, _ = r.latest_proposal(ISSUE, rows)
        assert p
        self.assertIsNone(r.current_verdict(p, rows))

    def test_verdict_by_proposer_ignored(self) -> None:
        rows = [comment(10, proposal_body(proposal_data()), 1)]
        p, _ = r.latest_proposal(ISSUE, rows)
        assert p
        rows.append(comment(11, verdict_body("APPROVED", "Claude", p.digest), 2))
        self.assertIsNone(r.current_verdict(p, rows))

    def test_change_after_approval_needs_new_verdict(self) -> None:
        rows = approved()
        rows.append(comment(12, proposal_body(proposal_data(scope="more")), 3))
        p, _ = r.latest_proposal(ISSUE, rows)
        assert p
        self.assertIsNone(r.current_verdict(p, rows))
        self.assertIn("no current approval by the other agent", ready_problems(rows))

    def test_newest_verdict_wins(self) -> None:
        rows = approved()
        p, _ = r.latest_proposal(ISSUE, rows)
        assert p
        rows.append(
            comment(12, verdict_body("CHANGES REQUESTED", "Codex", p.digest), 3)
        )
        v = r.current_verdict(p, rows)
        self.assertEqual(v.state if v else None, "CHANGES REQUESTED")

    def test_trailing_newline_is_no_verdict(self) -> None:
        rows = approved()
        rows[1]["body"] = str(rows[1]["body"]) + "\n"
        self.assertEqual(r.verdicts(rows), [])


def rejection(cid: int, minute: int, rows: list[dict[str, object]]) -> None:
    """Post a new proposal and a CHANGES REQUESTED verdict for it."""
    rows.append(comment(cid, proposal_body(proposal_data(scope=str(cid))), minute))
    p, _ = r.latest_proposal(ISSUE, rows)
    assert p
    rows.append(
        comment(
            cid + 1, verdict_body("CHANGES REQUESTED", "Codex", p.digest), minute + 1
        )
    )


class Escalation(unittest.TestCase):
    def test_rounds_count_across_revisions(self) -> None:
        rows: list[dict[str, object]] = []
        for n in range(3):
            rejection(10 + 2 * n, 1 + 2 * n, rows)
        self.assertEqual(r.rejected_rounds(rows), r.MAX_REJECTED_ROUNDS)

    def test_reset_names_newest_escalation_only(self) -> None:
        esc1 = comment(20, f"{r.ESCALATION_MARKER}\nthree rounds", 10)
        reset1 = comment(21, f"Refinement reset: Anton for {esc1['url']}", 11)
        esc2 = comment(22, f"{r.ESCALATION_MARKER}\nagain", 12)
        rows = [esc1, reset1]
        self.assertFalse(r.escalated(rows))
        rows.append(esc2)
        # The old reset does not clear the newer escalation.
        self.assertTrue(r.escalated(rows))
        rows.append(comment(23, f"Refinement reset: Anton for {esc1['url']}", 13))
        self.assertTrue(r.escalated(rows))
        rows.append(comment(24, f"Refinement reset: Anton for {esc2['url']}", 14))
        self.assertFalse(r.escalated(rows))

    def test_edited_reset_ignored(self) -> None:
        esc = comment(20, f"{r.ESCALATION_MARKER}\nx", 10)
        reset = comment(
            21, f"Refinement reset: Anton for {esc['url']}", 11, edited=True
        )
        self.assertTrue(r.escalated([esc, reset]))

    def test_reset_with_trailing_newline_ignored(self) -> None:
        esc = comment(20, f"{r.ESCALATION_MARKER}\nx", 10)
        reset = comment(21, f"Refinement reset: Anton for {esc['url']}\n", 11)
        self.assertTrue(r.escalated([esc, reset]))

    def test_rounds_count_after_reset(self) -> None:
        rows: list[dict[str, object]] = []
        for n in range(3):
            rejection(10 + 2 * n, 1 + 2 * n, rows)
        esc = comment(30, f"{r.ESCALATION_MARKER}\nx", 20)
        rows += [esc, comment(31, f"Refinement reset: Anton for {esc['url']}", 21)]
        self.assertEqual(r.rejected_rounds(rows), 0)
        rejection(40, 22, rows)
        self.assertEqual(r.rejected_rounds(rows), 1)


class AttemptKey(unittest.TestCase):
    def test_revision_zero_and_reset_id(self) -> None:
        self.assertEqual(r.attempt_key(ISSUE, [], None), f"refine:{ISSUE}:0:none")
        esc = comment(30, f"{r.ESCALATION_MARKER}\nx", 20)
        rows = [esc, comment(31, f"Refinement reset: Anton for {esc['url']}", 21)]
        self.assertEqual(r.attempt_key(ISSUE, rows, None), f"refine:{ISSUE}:0:31")

    def test_revision_is_proposal_comment(self) -> None:
        rows = approved()
        p, _ = r.latest_proposal(ISSUE, rows)
        self.assertEqual(r.attempt_key(ISSUE, rows, p), f"refine:{ISSUE}:10:none")


class Exempt(unittest.TestCase):
    def test_newest_unedited_list(self) -> None:
        old = comment(1, f"{r.EXEMPT_START}\n{BASE}", 1)
        new = comment(2, f"{r.EXEMPT_START}\n- {BASE[:-3]}901", 2)
        self.assertEqual(r.exempt_issues([old, new]), frozenset({BASE[:-3] + "901"}))
        new["includesCreatedEdit"] = True
        self.assertEqual(r.exempt_issues([old, new]), frozenset())


class DefinitionOfReady(unittest.TestCase):
    def test_approved_proposal_is_ready(self) -> None:
        self.assertEqual(ready_problems(approved()), [])

    def test_no_proposal(self) -> None:
        self.assertEqual(
            next_action.definition_of_ready(item(), None, None), ["no proposal"]
        )

    def test_issue_without_executor_and_criteria_reaches_ready(self) -> None:
        # The issue starts empty; refine writes Executor and labels from the
        # approved proposal, then it is Ready.
        rows = approved()
        self.assertIn(
            "issue Executor is not Codex", ready_problems(rows, executor=None)
        )
        self.assertEqual(ready_problems(rows, executor="Codex"), [])

    def test_executor_from_proposal_not_body(self) -> None:
        rows = approved()
        problems = ready_problems(rows, executor="Claude")
        self.assertIn("issue Executor is not Codex", problems)

    def test_empty_criteria_and_labels(self) -> None:
        data = proposal_data(acceptance_criteria=[" "], labels=["type::ci"])
        problems = ready_problems(approved(data))
        self.assertIn("no acceptance criteria", problems)
        self.assertIn("needs exactly one project::* label", problems)

    def test_missing_issue_label(self) -> None:
        problems = ready_problems(approved(), labels=["type::ci"])
        self.assertIn("issue lacks labels project::AgentSetup", problems)

    def test_added_issue_label_outside_the_proposal(self) -> None:
        labels = ["type::ci", "project::AgentSetup", "project::Stream"]
        problems = ready_problems(approved(), labels=labels)
        self.assertIn("issue needs exactly one project::* label", problems)
        self.assertIn("issue labels not in the proposal: project::Stream", problems)
        # Lifecycle labels are not compared.
        labels = ["type::ci", "project::AgentSetup", "waiting", "priority::high"]
        self.assertEqual(ready_problems(approved(), labels=labels), [])

    def test_added_label_blocks_the_claim(self) -> None:
        labels = ["type::ci", "project::AgentSetup", "project::Stream"]
        entry = board(approved(), status="Ready", labels=labels)
        self.assertNotIn("claim", kinds("Codex", entry))

    def test_needs_anton_blocks(self) -> None:
        labels = ["type::ci", "project::AgentSetup", "needs-anton"]
        self.assertIn("label needs-anton", ready_problems(approved(), labels=labels))

    def test_dependency_outside_proposal(self) -> None:
        dep = "https://github.com/phaabe/live.moafunk.de/issues/77"
        problems = ready_problems(approved(), readiness=f"Ready. Start after {dep}.")
        self.assertIn(f"dependencies not in the proposal: {dep}", problems)
        ok = approved(proposal_data(depends_on=[dep]))
        self.assertEqual(ready_problems(ok, readiness=f"Ready. Start after {dep}."), [])

    def test_route_problem_and_board(self) -> None:
        rows = approved()
        p, _ = r.latest_proposal(ISSUE, rows)
        v = r.current_verdict(p, rows) if p else None
        problems = next_action.definition_of_ready(
            item(), p, v, route_problem="owners differ", on_board=False
        )
        self.assertEqual(
            problems, ["file ownership: owners differ", "not on the board"]
        )


if __name__ == "__main__":
    unittest.main()


RULES = [{"pattern": "scripts/epic/*", "owners": ["Codex"], "lanes": ["setup"]}]
ALL = frozenset(next_action.REFINEMENT_ACTIONS)


def board(rows: list[dict[str, object]], **over: object) -> dict[str, object]:
    found = item(**over)
    found["refinement_comments"] = rows
    return found


def actions(
    agent: str,
    entry: dict[str, object],
    enabled: frozenset[str] = ALL,
    exempt: list[str] | None = None,
) -> list[next_action.Action]:
    state = {"prs": [], "items": [entry], "refinement_exempt": exempt or []}
    found = next_action.decide(agent, state, enabled=enabled, rules=RULES)
    return [a for a in found if a.action != "idle"]


def kinds(agent: str, entry: dict[str, object], **kw: object) -> list[str]:
    return [a.action for a in actions(agent, entry, **kw)]  # type: ignore[arg-type]


class DecideRefinement(unittest.TestCase):
    def test_draft_without_executor_goes_to_claude_for_refine(self) -> None:
        entry = board([], executor=None, labels=["refinement"])
        self.assertEqual(kinds("Claude", entry), ["refine"])
        self.assertEqual(kinds("Codex", entry), [])

    def test_not_enrolled_backlog_issue_gets_nothing(self) -> None:
        self.assertEqual(kinds("Codex", board([], labels=["type::ci"])), [])

    def test_label_removed_after_proposal_stays_enrolled(self) -> None:
        rows = [comment(10, proposal_body(proposal_data()), 1)]
        entry = board(rows, labels=["type::ci", "project::AgentSetup"])
        found = actions("Codex", entry)
        self.assertEqual([a.action for a in found], ["review-refinement"])
        p, _ = r.latest_proposal(ISSUE, rows)
        assert p is not None
        self.assertEqual(found[0].digest, p.digest)

    def test_phase_label_alone_enrolls_from_rest_rows(self) -> None:
        # Read path and stage: the label decides the read, REST rows the stage.
        for phase in r.PHASE_LABELS:
            with self.subTest(phase=phase):
                entry = item(executor=None, labels=[phase])
                self.assertTrue(next_action.reads_comments(entry, frozenset(), True))
                rows = [{"id": 5, "body": "hi", "created_at": "2026-10-01T00:00:00Z",
                         "updated_at": "2026-10-01T00:00:00Z", "html_url": "u"}]  # fmt: skip
                next_action.set_comments(entry, rows, True)
                self.assertEqual(kinds("Claude", entry), ["refine"])

    def test_proposer_never_reviews(self) -> None:
        rows = [comment(10, proposal_body(proposal_data(proposer="Codex")), 1)]
        self.assertEqual(kinds("Codex", board(rows)), [])
        self.assertEqual(kinds("Claude", board(rows)), ["review-refinement"])

    def test_changes_requested_routes_back_to_refine(self) -> None:
        rows = [comment(10, proposal_body(proposal_data()), 1)]
        p, _ = r.latest_proposal(ISSUE, rows)
        assert p is not None
        rows.append(
            comment(11, verdict_body("CHANGES REQUESTED", "Codex", p.digest), 2)
        )
        self.assertEqual(kinds("Codex", board(rows)), ["refine"])

    def test_third_rejection_escalates(self) -> None:
        rows: list[dict[str, object]] = []
        for n in range(3):
            rows.append(comment(10 + 2 * n, proposal_body(proposal_data()), 1 + 2 * n))
            p, _ = r.latest_proposal(ISSUE, rows)
            assert p is not None
            rows.append(
                comment(
                    11 + 2 * n,
                    verdict_body("CHANGES REQUESTED", "Codex", p.digest),
                    2 + 2 * n,
                )
            )
        found = actions("Codex", board(rows))
        self.assertEqual([a.action for a in found], ["escalate"])
        self.assertEqual(found[0].issue, BASE)

    def test_escalation_blocks_until_reset(self) -> None:
        rows = [comment(10, proposal_body(proposal_data()), 1)]
        rows.append(comment(20, f"{r.ESCALATION_MARKER}\nask Anton", 3))
        self.assertEqual(kinds("Codex", board(rows)), [])
        self.assertEqual(kinds("Claude", board(rows)), [])
        rows.append(
            comment(21, f"Refinement reset: Anton for {BASE}#issuecomment-20", 4)
        )
        self.assertEqual(kinds("Codex", board(rows)), ["review-refinement"])

    def test_approved_backlog_issue_gets_set_ready(self) -> None:
        found = actions("Codex", board(approved()))
        self.assertEqual([a.action for a in found], ["set-ready"])
        self.assertTrue(found[0].digest)

    def test_set_ready_never_touches_in_progress_or_done(self) -> None:
        for status in ("In progress", "Done"):
            self.assertNotIn(
                "set-ready", kinds("Codex", board(approved(), status=status))
            )

    def test_approved_and_ready_is_claimed_with_digest(self) -> None:
        found = actions("Codex", board(approved(), status="Ready"))
        self.assertEqual([a.action for a in found], ["claim"])
        self.assertTrue(found[0].digest)

    def test_change_after_approval_routes_to_refine(self) -> None:
        entry = board(approved(), status="Ready", executor="Claude")
        found = actions("Claude", entry)
        self.assertEqual([a.action for a in found], ["refine"])
        self.assertIn("Executor is not Codex", found[0].reason)

    def test_manually_ready_ticket_routes_to_refine(self) -> None:
        entry = board([], status="Ready", labels=["type::ci"])
        self.assertEqual(kinds("Codex", entry), ["refine"])

    def test_exempt_ready_ticket_is_claimed(self) -> None:
        entry = board([], status="Ready", labels=["type::ci"])
        self.assertEqual(kinds("Codex", entry, exempt=[BASE]), ["claim"])

    def test_exempt_ticket_loses_exemption_with_a_proposal(self) -> None:
        rows = [comment(10, proposal_body(proposal_data()), 1)]
        entry = board(rows, status="Ready")
        self.assertEqual(kinds("Claude", entry, exempt=[BASE]), [])
        self.assertEqual(kinds("Codex", entry, exempt=[BASE]), ["review-refinement"])

    def test_without_refine_enabled_claims_stay_as_before(self) -> None:
        entry = board([], status="Ready", labels=["type::ci"])
        self.assertEqual(kinds("Codex", entry, enabled=frozenset()), ["claim"])

    def test_depends_on_blocks_the_claim(self) -> None:
        entry = board(approved(proposal_data(depends_on=["Z1.1.1"])), status="Ready")
        self.assertEqual(kinds("Codex", entry), [])
        state = {"prs": [], "items": [entry], "refinement_exempt": []}
        waits = next_action.decide(
            "Codex", state, include_waiting=True, enabled=ALL, rules=RULES
        )
        self.assertIn("Z1.1.1", waits[0].reason)

    def test_depends_on_ticket_is_read_and_follows_completion(self) -> None:
        dep = "https://github.com/phaabe/live.moafunk.de/issues/77"
        entry = board(approved(proposal_data(depends_on=[dep])), status="Ready")
        ticket = {"state": "closed", "state_reason": "completed"}
        calls: list[int] = []

        def read(n: int) -> dict[str, object]:
            calls.append(n)
            return dict(ticket)

        def decided() -> list[next_action.Action]:
            state = {
                "prs": [],
                "items": [entry],
                "refinement_exempt": [],
                "completed_tickets": True,
                "tickets": next_action.read_tickets([entry], read),
            }
            return next_action.decide(
                "Codex",
                state,
                include_waiting=True,
                enabled=ALL,
                rules=RULES,
                completed_tickets=True,
            )

        self.assertEqual([a.action for a in decided()], ["claim"])
        self.assertEqual(calls, [77])
        ticket["state"] = "open"  # reopened: blocks again, no new comment needed
        waits = decided()
        self.assertEqual(waits[0].action, "wait")
        self.assertIn(dep, waits[0].reason)

    def test_files_with_no_owner_block(self) -> None:
        rows = approved(proposal_data(files=["nowhere/x"]))
        self.assertEqual(kinds("Codex", board(rows, executor=None)), [])
        self.assertEqual(kinds("Claude", board(rows, executor=None)), [])

    def test_unread_comments_never_claim(self) -> None:
        entry = item(status="Ready")
        state = {"prs": [], "items": [entry]}
        found = next_action.decide("Codex", state, enabled=ALL, rules=RULES)
        self.assertEqual(found[0].action, "idle")


class ReadsComments(unittest.TestCase):
    def reads(
        self, refine: bool = True, focus: frozenset[str] = frozenset(), **over: object
    ) -> bool:
        return next_action.reads_comments(item(**over), focus, refine)

    def test_ready_issues_are_always_read(self) -> None:
        self.assertTrue(self.reads(refine=False, status="Ready", labels=[]))

    def test_enrolled_or_focus_issues_before_ready_are_read_to_refine(self) -> None:
        self.assertTrue(self.reads())
        self.assertTrue(self.reads(status=None, labels=[r.PHASE_CHANGES]))
        self.assertTrue(self.reads(status="Todo", labels=["x"], focus=frozenset({"x"})))
        self.assertFalse(self.reads(labels=[]))
        self.assertFalse(self.reads(refine=False))

    def test_past_ready_and_pull_requests_are_not_read(self) -> None:
        self.assertFalse(self.reads(status="In progress"))
        self.assertFalse(self.reads(content={"type": "PullRequest", "number": 1}))

    def test_exempt_list_from_rest_rows(self) -> None:
        url = "https://github.com/phaabe/live.moafunk.de/issues/77"
        row = {
            "body": f"{r.EXEMPT_START}\n- {url}",
            "created_at": "2026-10-06T10:00:00Z",
            "updated_at": "2026-10-06T10:00:00Z",
            "html_url": f"{BASE}#issuecomment-1",
        }
        self.assertEqual(next_action.exempt_list([row]), [url])
        edited = {**row, "updated_at": "2026-10-06T11:00:00Z"}
        self.assertEqual(next_action.exempt_list([edited]), [])
        self.assertEqual(next_action.exempt_list([]), [])


class AttemptLimit(unittest.TestCase):
    """Failed refine runs in the shared attempt store (rebase_policy.py)."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="refine-attempts-")
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.posts: list[int] = []
        self.ticks = 0
        # The issue's comments: the runner posts its escalation here and
        # reads them back over REST (rebase_policy.comments).
        self.rows: list[dict[str, object]] = []
        for name, value in (
            ("comments", self.rest_comments),
            ("post_comment", self.post_comment),
        ):
            patcher = patch.object(rp, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def post(self, number: int) -> None:
        self.posts.append(number)

    def next_minute(self) -> int:
        return 1 + max((int(str(c["createdAt"])[14:16]) for c in self.rows), default=0)

    def post_comment(self, issue: int, body: str) -> None:
        self.assertEqual(issue, ISSUE)
        self.rows.append(comment(100 + len(self.rows), body, self.next_minute()))

    def rest_comments(self, issue: int) -> list[dict[str, object]]:
        return [
            {"id": c["id"], "body": c["body"], "created_at": c["createdAt"],
             "updated_at": c["createdAt"], "html_url": c["url"]}
            for c in self.rows
        ]  # fmt: skip

    def action(
        self, rows: list[dict[str, object]], agent: str = "Claude"
    ) -> next_action.Action:
        found = actions(agent, board(rows, executor=None, labels=["refinement"]))
        self.assertEqual([a.action for a in found], ["refine"])
        return found[0]

    def run_tick(self, action: next_action.Action, outcome: str | None) -> int:
        """check, start and (unless the session crashed) finish one tick."""
        self.ticks += 1
        out = self.dir / "attempt.json"
        data = json.loads(action.to_json())
        code = rp.attempt_check(self.dir, "claude", data, out, self.post)
        if code != rp.RUN:
            return code
        pin = rp.load_attempt(out)
        self.assertEqual(pin["pr"], ISSUE)
        tick = f"t{self.ticks}"
        self.assertEqual(rp.attempt_start(self.dir, pin, tick), rp.RUN)
        if outcome:
            rp.attempt_finish(self.dir, pin, tick, outcome, self.post)
        return rp.RUN

    def suppressed_after_two(
        self, rows: list[dict[str, object]], agent: str = "Claude"
    ) -> None:
        action = self.action(rows, agent)
        self.assertEqual(self.run_tick(action, "failed"), rp.RUN)
        self.assertEqual(self.run_tick(action, None), rp.RUN)  # crash counts
        # Repeated ticks stay suppressed and post the escalation once.
        for _ in range(2):
            self.assertEqual(self.run_tick(action, "failed"), rp.SKIP)
        self.assertEqual(self.posts[-1], ISSUE)
        escalations = [c for c in rows if r.ESCALATION_MARKER in str(c["body"])]
        self.assertIn(f"`{action.attempt_key}`", str(escalations[-1]["body"]))
        # A fresh decide() (a restart) sees it and waits for Anton's reset.
        entry = board(rows, executor=None, labels=["refinement"])
        self.assertEqual(kinds(agent, entry), [])

    def test_key_in_the_action_json(self) -> None:
        data = json.loads(self.action([]).to_json())
        self.assertEqual(data["attempt_key"], f"refine:{ISSUE}:0:none")

    def reset(self, cid: int) -> None:
        """Anton resets the escalation the runner posted last."""
        escalations = [c for c in self.rows if r.ESCALATION_MARKER in str(c["body"])]
        url = escalations[-1]["url"]
        self.rows.append(
            comment(cid, f"Refinement reset: Anton for {url}", self.next_minute())
        )

    def test_revision_zero_and_later_revision_with_reset(self) -> None:
        rows = self.rows
        self.suppressed_after_two(rows)
        self.assertTrue(r.escalated(rows))
        # Anton resets it: a new key, two more runs, then suppressed again.
        self.reset(31)
        self.assertEqual(self.action(rows).attempt_key, f"refine:{ISSUE}:0:31")
        self.suppressed_after_two(rows)
        # A rejected later revision is its own key (its files route to Codex).
        self.reset(32)
        rejection(40, self.next_minute(), rows)
        key = self.action(rows, "Codex").attempt_key
        self.assertEqual(key, f"refine:{ISSUE}:40:32")
        self.suppressed_after_two(rows, "Codex")

    def test_succeeded_and_void_runs_do_not_count(self) -> None:
        action = self.action([])
        for outcome in ("succeeded", "void", "void", "failed"):
            self.assertEqual(self.run_tick(action, outcome), rp.RUN)
        self.assertEqual(self.run_tick(action, "failed"), rp.RUN)
        self.assertEqual(self.run_tick(action, "failed"), rp.SKIP)

    def test_key_must_name_the_issue(self) -> None:
        data = json.loads(self.action([]).to_json())
        for bad in ("refine:901:0:none", "pr:1:head:x", None):
            with self.assertRaises(ValueError):
                rp.attempt_check(
                    self.dir,
                    "claude",
                    {**data, "attempt_key": bad},
                    self.dir / "a.json",
                    self.post,
                )
