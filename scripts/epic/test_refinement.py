"""Refinement record and Definition of Ready tests.

Run: python3 -m unittest discover -s scripts/epic.
"""

from __future__ import annotations

import isolated_env  # noqa: F401  (first: hides live runner state)

import json
import unittest

import next_action
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
