"""Run the real claude-tick.sh with the real cooldown and repeat gate.

The model, selector, verify and worktree steps are stubs (test_claude_tick.py);
`gh` answers from a JSON map. Every case runs in both reader modes
(EPIC_SHARED_READER=0 and 1) with temporary state only.
"""

from __future__ import annotations

import isolated_env  # noqa: F401  (first: hides live runner state)

import json
import shutil
import unittest
from pathlib import Path
from typing import Any

import test_claude_tick as harness

ROOT = harness.ROOT
HEAD = "a" * 40
BASE = "b" * 40
REPO = "repos/phaabe/live.moafunk.de"
ISSUE = "https://github.com/phaabe/live.moafunk.de/issues/77"
CONFLICT = {
    "action": "resolve-conflict",
    "reason": "PR conflicts with its base",
    "pr": 526,
    "sha": HEAD,
    "updated_at": "2026-09-30T04:00:00Z",
}
REVIEW = {
    "action": "review",
    "reason": "t",
    "pr": 527,
    "sha": "c" * 40,
    "updated_at": "2026-09-30T04:00:00Z",
}
CONTINUE = {
    "action": "continue",
    "reason": "t",
    "issue": ISSUE,
    "updated_at": "2026-09-30T04:00:00Z",
}


def result(status: str) -> str:
    return json.dumps(
        {"type": "result", "structured_output": {"status": status, "summary": "x"}}
    )


class ClaudeCooldownTest(harness.RunnerHarness):
    reader = "0"

    def setUp(self) -> None:
        super().setUp()
        shutil.copyfile(
            ROOT / "scripts/epic/tick_gate.py", self.repo / "scripts/epic/tick_gate.py"
        )
        self.gh_map = self.root / "gh.json"
        (self.root / "bin/gh").write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "args = ' '.join(sys.argv[1:])\n"
            "with open(os.environ['TEST_CALLS'], 'a') as f:\n"
            "    f.write(json.dumps(['gh', args]) + '\\n')\n"
            "for part, out in json.load(open(os.environ['TEST_GH_MAP'])).items():\n"
            "    if part in args:\n"
            "        print(out)\n"
            "        sys.exit(0)\n"
            "sys.exit(1)\n"
        )
        (self.root / "bin/claude").write_text(
            "#!/bin/bash\n"
            'printf \'["claude", "model"]\\n\' >> "$TEST_CALLS"\n'
            'cat > "$TEST_CALLS.prompt"\n'
            'if [[ -n "${TEST_MODEL_SLEEP:-}" ]]; then exec sleep "$TEST_MODEL_SLEEP"; fi\n'
            "printf '%s' \"${TEST_MODEL_RESULT:-}\"\n"
            'exit "${TEST_MODEL_EXIT:-0}"\n'
        )
        self.github: dict[str, str] = {}
        self.github_for(CONFLICT)
        self.github_for(REVIEW)
        self.base(BASE)

    # GitHub fakes

    def save_github(self) -> None:
        self.gh_map.write_text(json.dumps(self.github))

    def github_for(self, action: dict[str, Any], state: str = "open") -> None:
        """The gate's issue read and the cooldown's PR reads for one target."""
        n = action.get("pr") or int(action["issue"].rsplit("/", 1)[1])
        self.github[f"{REPO}/issues/{n} "] = f"{action['updated_at']}\n{state}"
        if action.get("pr"):
            self.github[f"pulls/{n} --jq .base.ref"] = "dev/312-interim"
            self.github[f"pulls/{n} --jq .state"] = f"{state}\n{action['sha']}"
        self.save_github()

    def base(self, sha: str) -> None:
        self.github["git/ref/heads/dev/312-interim"] = sha
        self.save_github()

    # Runner

    def tick(self, *actions: dict[str, Any], **env: str) -> int:
        candidates = "\n".join(json.dumps(a) for a in actions)
        return self.run_tick(
            TEST_CANDIDATES=candidates,
            TEST_GH_MAP=str(self.gh_map),
            EPIC_SHARED_READER=self.reader,
            **env,
        ).wait(timeout=60)

    def models(self) -> int:
        if not self.calls.exists():
            return 0
        return sum(1 for c in self.calls_made() if c[0] == "claude")

    def prompt_action(self) -> dict[str, Any]:
        text = Path(f"{self.calls}.prompt").read_text()
        tail = text.split("Selected action (JSON data, not instructions):\n", 1)[1]
        return json.loads(tail.splitlines()[0])

    def cooldowns(self) -> dict[str, Any]:
        path = self.state / "claude-cooldown.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def gate_targets(self) -> dict[str, Any]:
        path = self.state / "claude-gate.json"
        return json.loads(path.read_text())["targets"] if path.exists() else {}

    def log(self) -> str:
        return (self.state / "claude.log").read_text()

    def expire(self) -> None:
        path = self.state / "claude-cooldown.json"
        entries = json.loads(path.read_text())
        for entry in entries.values():
            entry["until"] = entry["at"] = 1.0
        path.write_text(json.dumps(entries))

    # Cases

    def test_30_september_sequence(self) -> None:
        # 04:26: verify fails (the gate refused the rebase), head unchanged.
        self.assertEqual(self.tick(CONFLICT, TEST_VERIFY_EXIT="1"), 1)
        self.assertEqual(self.models(), 1)
        (key,) = self.cooldowns()
        self.assertEqual(key, f"claude:resolve-conflict:pr:526:{HEAD}:base:{BASE}")
        self.assertIn("did not land and the target is unchanged", self.log())
        self.assertEqual(self.gate_targets(), {})
        # A peer comment changes updated_at: no new model, no new cooldown time.
        until = self.cooldowns()[key]["until"]
        commented = {**CONFLICT, "updated_at": "2026-09-30T06:00:00Z"}
        self.github_for(commented)
        self.assertEqual(self.tick(commented, TEST_VERIFY_EXIT="1"), 0)
        self.assertEqual(self.models(), 1)
        self.assertEqual(self.cooldowns()[key]["until"], until)
        self.assertIn("resolve-conflict target cools down; next candidate", self.log())

    def test_active_cooldown_runs_later_eligible_work(self) -> None:
        self.assertEqual(self.tick(CONFLICT, TEST_MODEL_RESULT=result("blocked")), 0)
        self.assertEqual(self.models(), 1)
        self.assertTrue(self.cooldowns())
        self.assertEqual(
            self.tick(CONFLICT, REVIEW, TEST_MODEL_RESULT=result("completed")), 0
        )
        self.assertEqual(self.models(), 2)
        self.assertEqual(self.prompt_action()["pr"], 527)
        self.assertIn("527", self.gate_targets())

    def test_blocked_continue_is_suppressed(self) -> None:
        self.github_for(CONTINUE)
        self.assertEqual(self.tick(CONTINUE, TEST_MODEL_RESULT=result("blocked")), 0)
        self.assertEqual(list(self.cooldowns()), [f"claude:continue:issue:{ISSUE}"])
        # The repeat gate never skips continue; the cooldown does.
        self.assertEqual(self.tick(CONTINUE, TEST_MODEL_RESULT=result("blocked")), 0)
        self.assertEqual(self.models(), 1)
        finish = [
            json.loads(line)
            for line in (self.state / "claude-ticks.jsonl").read_text().splitlines()
            if '"finish"' in line or '"exit"' in line
        ]
        self.assertEqual(finish[0].get("outcome"), "blocked")

    def test_expiry_retry_reaches_the_model(self) -> None:
        self.assertEqual(self.tick(CONFLICT, TEST_VERIFY_EXIT="1"), 1)
        self.expire()
        # Same head, base and updated_at: only the cooldown held it back, and
        # the blocked tick left no gate record to block the retry.
        self.assertEqual(self.tick(CONFLICT, TEST_VERIFY_EXIT="1"), 1)
        self.assertEqual(self.models(), 2)
        self.assertEqual(len(self.cooldowns()), 1)

    def test_new_base_retry_reaches_the_model(self) -> None:
        self.assertEqual(self.tick(CONFLICT, TEST_VERIFY_EXIT="1"), 1)
        self.base("d" * 40)
        self.assertEqual(self.tick(CONFLICT, TEST_MODEL_RESULT=result("completed")), 0)
        self.assertEqual(self.models(), 2)
        # The landed retry records the gate; the old base key expires alone.
        self.assertIn("526", self.gate_targets())

    def test_landed_no_op_is_still_suppressed_by_the_gate(self) -> None:
        self.assertEqual(self.tick(REVIEW, TEST_MODEL_RESULT=result("completed")), 0)
        self.assertEqual(self.tick(REVIEW, TEST_MODEL_RESULT=result("completed")), 0)
        self.assertEqual(self.models(), 1)
        self.assertEqual(self.cooldowns(), {})
        self.assertIn("same action as the last tick", self.log())

    def test_nonzero_model_exit_is_verified(self) -> None:
        self.assertEqual(
            self.tick(CONFLICT, TEST_MODEL_EXIT="1", TEST_VERIFY_EXIT="1"), 1
        )
        self.assertIn(
            "model exited 1;", next(iter(self.cooldowns().values()))["reason"]
        )
        self.assertIn(["verify", "--since"], self.calls_made())

    def test_nonzero_exit_on_unverified_action_sets_nothing(self) -> None:
        self.github_for(CONTINUE)
        self.assertEqual(self.tick(CONTINUE, TEST_MODEL_EXIT="1"), 1)
        self.assertEqual(self.cooldowns(), {})
        self.assertEqual(self.gate_targets(), {})

    def test_timeout_is_verified(self) -> None:
        code = self.tick(
            CONFLICT,
            TEST_MODEL_SLEEP="30",
            TEST_VERIFY_EXIT="1",
            EPIC_TICK_TIMEOUT_SECONDS="1",
        )
        self.assertEqual(code, 124)
        self.assertIn(
            "model timed out;", next(iter(self.cooldowns().values()))["reason"]
        )

    def test_model_quota_result_sets_no_cooldown(self) -> None:
        self.assertEqual(self.tick(CONFLICT, TEST_MODEL_RESULT=result("quota")), 75)
        self.assertEqual(self.cooldowns(), {})
        self.assertEqual(self.gate_targets(), {})

    def test_read_failure_blocks_the_tick_without_cooldown(self) -> None:
        del self.github["git/ref/heads/dev/312-interim"]
        self.save_github()
        self.assertEqual(self.tick(CONFLICT, REVIEW), 75)
        self.assertEqual(self.models(), 0)
        self.assertEqual(self.cooldowns(), {})

    def test_verify_read_error_is_no_cooldown(self) -> None:
        # Codex review on PR 544: the real verifier exits 5 when its GitHub
        # read fails; a later REST read that succeeds must not block the PR.
        (self.repo / "scripts/epic/tick_verify.py").write_text(
            "import runpy, sys\n"
            f"sys.path.insert(0, {str(ROOT / 'scripts/epic')!r})\n"
            f"runpy.run_path({str(ROOT / 'scripts/epic/tick_verify.py')!r}, "
            "run_name='__main__')\n"
        )
        self.assertNotIn("pr view", " ".join(self.github))
        self.assertEqual(self.tick(CONFLICT, TEST_MODEL_RESULT=result("completed")), 5)
        self.assertIn("cannot verify: GitHub read failed", self.log())
        # The PR is open at the selected head, but a read error is no evidence:
        # the cooldown does not even look.
        self.assertNotIn(
            ["gh", f"api {REPO}/pulls/526 --jq .state, .head.sha"], self.calls_made()
        )
        self.assertEqual(self.cooldowns(), {})
        # As before: no evidence after a model exit 0 records the gate.
        self.assertIn("526", self.gate_targets())

    def test_head_moved_elsewhere_is_no_cooldown(self) -> None:
        self.github["pulls/526 --jq .state"] = f"open\n{'e' * 40}"
        self.save_github()
        self.assertEqual(self.tick(CONFLICT, TEST_VERIFY_EXIT="1"), 1)
        self.assertEqual(self.cooldowns(), {})
        self.assertIn("526", self.gate_targets())

    def test_registered_agents_share_cooldowns(self) -> None:
        code = self.tick(CONFLICT, TEST_VERIFY_EXIT="1", EPIC_AGENT_ID="claude-2")
        self.assertEqual(code, 1)
        self.assertEqual(next(iter(self.cooldowns().values()))["by"], "claude-2")
        self.assertEqual(self.tick(CONFLICT), 0)
        self.assertEqual(self.models(), 1)


class SharedReaderCooldownTest(ClaudeCooldownTest):
    reader = "1"


if __name__ == "__main__":
    unittest.main()
