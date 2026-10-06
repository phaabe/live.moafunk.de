"""Conflicts between selection and model start, through both real runners.

claude-tick.sh and .codex/codex-tick.sh run unchanged, in both reader modes
(EPIC_SHARED_READER=0 and 1). The selector is the real next_action.py on a
saved state (the GitHub view at selection time). The repeat gate is the real
tick_gate.py; it reads the PR again from a fake `gh` (GitHub after selection).
The model is a stub that only logs its start. The --recheck (mode 1 only) is a
stub that returns "still valid": the gate must stop the tick on its own,
because in mode 0 there is no recheck at all.

Unchanged: COMMON and FILES below. Stubs (run as scripts only, no real module
imports them): next_action.py, gitnexus_noise.py, runner_worktree.py,
tick_verify.py, and the Codex steps feature_worktree.py, assignment.py,
review_delivery.py and review_worktree.py. test_fixture_files.py checks that
the runner finds every file it calls.
"""

from __future__ import annotations

import isolated_env  # noqa: F401  (first: hides live runner state)

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
REPO = "repos/phaabe/live.moafunk.de"
ISSUES = "https://github.com/phaabe/live.moafunk.de/issues"
A = "a" * 40
B = "b" * 40
UPDATED = "2026-09-30T04:00:00Z"
REAL_GIT = shutil.which("git")

# Runner files copied as they are; everything else is a stub below.
COMMON = (
    "scripts/epic/github_quota.py",
    "scripts/epic/github_state.py",
    "scripts/epic/agents.py",
    "scripts/epic/tick_events.py",
    "scripts/epic/target_lock.py",
    "scripts/epic/tick_gate.py",
    "scripts/epic/routing.py",
    "scripts/epic/refinement.py",
    "scripts/epic/rebase_policy.py",
    ".codex/epic_lock.py",
)
FILES = {
    "claude": (
        "scripts/epic/claude-tick.sh",
        "scripts/epic/tick_cooldown.py",
        "scripts/epic/claude-result-schema.json",
        # Passed to the model only; the stub model ignores them.
        "scripts/epic/claude-runner-settings.json",
        "scripts/epic/claude-mcp-config.json",
        "scripts/epic/permission_gate.py",
        "scripts/epic/runtime.py",
        "scripts/epic/lockhold",
        ".claude/commands/epic/epic-tick.md",
    ),
    "codex": (
        ".codex/codex-tick.sh",
        "scripts/epic/runtime.py",
        ".codex/epic-tick.md",
        ".codex/tick_backoff.py",
        ".codex/tick-result.schema.json",
    ),
}
RUNNER = {"claude": "scripts/epic/claude-tick.sh", "codex": ".codex/codex-tick.sh"}

# The real selector on the saved state; --recheck is logged and passes.
SELECTOR = """\
import json, os, sys
import real_next_action
def log(*entry):
    with open(os.environ['TEST_CALLS'], 'a') as f:
        f.write(json.dumps(list(entry)) + '\\n')
if __name__ != '__main__':
    # github_state.py imports next_action: give it the real module.
    sys.modules[__name__] = real_next_action
elif '--recheck' in sys.argv:
    log('recheck', json.load(open(sys.argv[-1])).get('pr'))
    sys.exit(0)
else:
    log('select')
    sys.argv += ['--state-file', os.environ['TEST_STATE_FILE']]
    sys.exit(real_next_action.main())
"""
# Substring map, longest key first, so `pulls/7 --jq ...` wins over `pulls/7`.
GH = """\
#!/usr/bin/env python3
import json, os, sys
args = ' '.join(sys.argv[1:])
with open(os.environ['TEST_CALLS'], 'a') as f:
    f.write(json.dumps(['gh', args]) + '\\n')
answers = json.load(open(os.environ['TEST_GH_MAP']))
for part in sorted(answers, key=len, reverse=True):
    if part in args:
        print(answers[part])
        sys.exit(0)
print(f'fake gh: no answer for {args}', file=sys.stderr)
sys.exit(1)
"""
GIT = f"""\
#!/usr/bin/env python3
import os, sys
if 'pull' in sys.argv[1:]:
    sys.exit(0)
os.execv({REAL_GIT!r}, [{REAL_GIT!r}, *sys.argv[1:]])
"""
CLAUDE = """\
#!/bin/bash
printf '["model", "claude"]\\n' >> "$TEST_CALLS"
cat > /dev/null
printf '{"type": "result", "structured_output": {"status": "completed", "summary": "x"}}'
"""
CODEX = """\
#!/usr/bin/env python3
import json, os, sys
with open(os.environ['TEST_CALLS'], 'a') as f:
    f.write(json.dumps(['model', 'codex']) + '\\n')
sys.stdin.read()
out = sys.argv[sys.argv.index('--output-last-message') + 1]
open(out, 'w').write(json.dumps({'status': 'completed', 'summary': 'x'}))
"""
EXIT_0 = "import sys\nsys.exit(0)\n"
# Codex review delivery: no saved review to resume; a new one is "published".
REVIEW_DELIVERY = """\
import sys
sys.exit(3 if sys.argv[1] == 'resume' else 0)
"""
# Codex review worktree: `prepare` writes the context the runner reads and
# names an empty worktree; `cleanup` removes it.
REVIEW_WORKTREE = """\
import argparse, json, pathlib, shutil
parser = argparse.ArgumentParser()
parser.add_argument('command', choices=('prepare', 'cleanup'))
parser.add_argument('--runner', required=True)
parser.add_argument('--action-file')
parser.add_argument('--state-dir')
parser.add_argument('--context-file', required=True)
args = parser.parse_args()
context_file = pathlib.Path(args.context_file)
if args.command == 'cleanup':
    shutil.rmtree(json.loads(context_file.read_text())['worktree'], ignore_errors=True)
    raise SystemExit(0)
action = json.loads(pathlib.Path(args.action_file).read_text())
artifact = pathlib.Path(args.state_dir) / 'reviews' / str(action['pr']) / action['sha']
attempt = artifact / 'attempt-0'
attempt.mkdir(parents=True)
worktree = pathlib.Path(args.state_dir) / 'review-worktree'
worktree.mkdir()
context_file.write_text(json.dumps(
    {'worktree': str(worktree), 'artifact_dir': str(artifact), 'attempt_dir': str(attempt)}
))
print(worktree)
"""


def verdict(state: str, by: str, sha: str) -> dict[str, Any]:
    return {
        "body": f"Review: {state} by {by} at {sha}",
        "createdAt": "2026-09-30T03:00:00Z",
        "url": f"{ISSUES}/1#issuecomment-1",
        "includesCreatedEdit": False,
    }


def pr(number: int, owner: str, mergeable: str, **kw: Any) -> dict[str, Any]:
    """A PR as the selector saw it."""
    data = {
        "number": number,
        "title": f"PR {number}",
        "body": f"Issue: {ISSUES}/{900 + number}\nExecutor: {owner}",
        "baseRefName": "dev/312-interim",
        "headRefName": f"feat/{number}-x",
        "headRefOid": A,
        "isDraft": False,
        "labels": [],
        "mergeable": mergeable,
        "statusCheckRollup": [
            {"conclusion": "SUCCESS"},
            {"context": "epic-guard", "state": "SUCCESS"},
        ],
        "comments": [],
        "updatedAt": UPDATED,
    }
    data.update(kw)
    return data


class Routing(unittest.TestCase):
    agent = "claude"
    reader = "0"

    @property
    def peer(self) -> str:
        return "Codex" if self.agent == "claude" else "Claude"

    @property
    def me(self) -> str:
        return self.agent.capitalize()

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="conflict-routing-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))
        self.repo = self.root / "checkout"
        self.state = self.root / "state"
        self.calls = self.root / "calls.jsonl"
        self.gh_map = self.root / "gh.json"
        self.state_file = self.root / "selected-state.json"
        epic = self.repo / "scripts/epic"
        for rel in (*COMMON, *FILES[self.agent]):
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / rel, self.repo / rel)  # keeps the x bit (lockhold)
        shutil.copyfile(
            ROOT / "scripts/epic/next_action.py", epic / "real_next_action.py"
        )
        (epic / "next_action.py").write_text(SELECTOR)
        for stub in (
            "gitnexus_noise.py", "runner_worktree.py", "tick_verify.py", "close_merged.py",
        ):  # fmt: skip
            (epic / stub).write_text(EXIT_0)
        (self.repo / ".codex/feature_worktree.py").write_text(
            f"print({str(self.repo)!r})\n"
        )
        (self.repo / ".codex/review_delivery.py").write_text(REVIEW_DELIVERY)
        (self.repo / ".codex/review_worktree.py").write_text(REVIEW_WORKTREE)
        # The protected-home check is out of scope here (.codex/tests covers it):
        # the stub passes and names the tick's temporary parent.
        (self.repo / ".codex/protected_home.py").write_text(
            "import json, os\n"
            "print(json.dumps({'temporary_parent': os.environ['TEST_TEMP_PARENT']}))\n"
        )
        # Codex's assignment evidence step runs after the gate: it passes here.
        (self.repo / ".codex/assignment.py").write_text(
            "import sys\n"
            "open(sys.argv[sys.argv.index('--output') + 1], 'w').write('{}')\n"
        )
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        for name, text in (
            ("gh", GH),
            ("git", GIT),
            ("claude", CLAUDE),
            ("codex", CODEX),
        ):
            (bin_dir / name).write_text(text)
            (bin_dir / name).chmod(0o755)
        home = self.root / "home"
        home.mkdir()
        self.github: dict[str, str] = {}
        self.env = {
            **os.environ,
            "HOME": str(home),
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "EPIC_STATE_DIR": str(self.state),
            "EPIC_LOCK_DIR": str(self.root / "locks"),
            # Legacy runtime mode, explicit (runtime.py mode).
            "EPIC_RUNTIME_LEGACY": "1",
            "EPIC_RUNTIME_HOME": str(self.root / "runtime-home"),
            "EPIC_WORKTREE_DIR": str(self.root / "worktrees"),
            "EPIC_SHARED_READER": self.reader,
            "EPIC_TICK_TIMEOUT_SECONDS": "30",
            "EPIC_SELECT_TIMEOUT_SECONDS": "30",
            "EPIC_PULL_TIMEOUT_SECONDS": "10",
            "EPIC_SNAPSHOT_LOCK_SECONDS": "1",
            "EPIC_SNAPSHOT_REFRESH_SECONDS": "1",
            "EPIC_RECHECK_TIMEOUT_SECONDS": "10",
            "TEST_CALLS": str(self.calls),
            "TEST_GH_MAP": str(self.gh_map),
            "TEST_STATE_FILE": str(self.state_file),
            "TEST_TEMP_PARENT": str(self.root),
        }

    # GitHub: at selection time (state file) and after it (fake gh)

    def selected(self, *prs: dict[str, Any]) -> None:
        self.state_file.write_text(json.dumps({"prs": list(prs), "items": []}))

    def now_on_github(self, number: int, head: str, mergeable: bool | None) -> None:
        self.github[f"{REPO}/issues/{number} "] = f"{UPDATED}\nopen"
        self.github[f"{REPO}/pulls/{number}"] = json.dumps(
            {"head": {"sha": head}, "mergeable": mergeable}
        )
        # tick_cooldown.py keys resolve-conflict by the base head.
        self.github[f"{REPO}/pulls/{number} --jq .base.ref"] = "dev/312-interim"
        self.github["git/ref/heads/dev/312-interim"] = B
        self.gh_map.write_text(json.dumps(self.github))

    # Runner

    def tick(self) -> int:
        self.calls.unlink(missing_ok=True)
        return subprocess.run(
            ["/bin/bash", str(self.repo / RUNNER[self.agent])],
            env=self.env,
            cwd=self.root,
            timeout=90,
        ).returncode

    def made(self) -> list[list[Any]]:
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def models(self) -> int:
        return sum(1 for c in self.made() if c[0] == "model")

    def log(self) -> str:
        return (self.state / f"{self.agent}.log").read_text()

    def assert_no_model_and_no_record(self) -> None:
        self.assertEqual(self.models(), 0, self.log())
        self.assertFalse((self.state / f"{self.agent}-gate.json").exists())
        self.assertFalse((self.state / f"{self.agent}-gate-seen.json").exists())
        # No success and no blocked record in the runner's own retry state.
        for name in ("claude-cooldown.json", "codex-backoff.json"):
            path = self.state / name
            self.assertTrue(not path.exists() or json.loads(path.read_text()) == {})

    # Cases

    def test_conflict_after_selection_starts_no_review(self) -> None:
        self.selected(pr(7, self.peer, "MERGEABLE"))
        self.now_on_github(7, A, False)
        self.assertEqual(self.tick(), 0)
        self.assertIn(["select"], self.made())
        self.assertIn("gate: skip, the PR conflicts with its base", self.log())
        self.assertNotIn("recheck", [c[0] for c in self.made()])
        self.assert_no_model_and_no_record()

    def test_changed_head_starts_no_model(self) -> None:
        self.selected(pr(7, self.peer, "MERGEABLE"))
        self.now_on_github(7, B, True)
        self.assertEqual(self.tick(), 0)
        self.assertIn("gate: skip, the PR head changed after selection", self.log())
        self.assert_no_model_and_no_record()

    def test_conflict_after_selection_starts_no_fix(self) -> None:
        asked = [verdict("CHANGES REQUESTED", self.peer, A)]
        self.selected(pr(8, self.me, "MERGEABLE", comments=asked))
        self.now_on_github(8, A, False)
        self.assertEqual(self.tick(), 0)
        self.assertIn('"action": "fix"', self.log())
        self.assertIn("gate: skip, the PR conflicts with its base", self.log())
        self.assert_no_model_and_no_record()

    def test_conflict_cleared_after_selection_starts_no_resolve(self) -> None:
        self.selected(pr(8, self.me, "CONFLICTING"))
        self.now_on_github(8, A, True)
        self.assertEqual(self.tick(), 0)
        self.assertIn('"action": "resolve-conflict"', self.log())
        self.assertIn("gate: skip, the PR no longer conflicts", self.log())
        self.assert_no_model_and_no_record()

    def test_cleared_conflict_is_reviewed_on_a_later_selection(self) -> None:
        # Selected while conflicted: no review at all, not even a gate read.
        self.selected(pr(7, self.peer, "CONFLICTING"))
        self.now_on_github(7, A, False)
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.made(), [["select"]])
        self.assert_no_model_and_no_record()
        # The owner rebased onto the base without a new head; GitHub agrees.
        self.selected(pr(7, self.peer, "MERGEABLE"))
        self.now_on_github(7, A, True)
        self.assertEqual(self.tick(), 0, self.log())
        steps = [c[0] for c in self.made() if c[0] != "gh"]
        recheck = ["recheck"] if self.reader == "1" else []
        self.assertEqual(steps, ["select", *recheck, "model"], self.log())
        # The gate read the PR itself, not only the issue.
        self.assertIn(["gh", f"api {REPO}/pulls/7"], self.made())
        self.assertIn('"action": "review"', self.log().rsplit("tick: started", 1)[1])

    def test_unknown_mergeability_does_not_block_the_review(self) -> None:
        self.selected(pr(7, self.peer, "UNKNOWN"))
        self.now_on_github(7, A, None)
        self.assertEqual(self.tick(), 0, self.log())
        self.assertEqual(self.models(), 1, self.log())

    def test_malformed_pr_read_starts_no_model(self) -> None:
        self.selected(pr(7, self.peer, "MERGEABLE"))
        self.now_on_github(7, A, True)
        self.github[f"{REPO}/pulls/7"] = json.dumps({"head": {"sha": A}})
        self.gh_map.write_text(json.dumps(self.github))
        self.assertNotEqual(self.tick(), 0)
        self.assertIn("no valid mergeable state", self.log())
        self.assert_no_model_and_no_record()

    def test_failed_pr_read_starts_no_model(self) -> None:
        self.selected(pr(7, self.peer, "MERGEABLE"))
        self.now_on_github(7, A, True)
        del self.github[f"{REPO}/pulls/7"]
        self.gh_map.write_text(json.dumps(self.github))
        self.assertNotEqual(self.tick(), 0)
        self.assert_no_model_and_no_record()


class ClaudeSharedReader(Routing):
    agent, reader = "claude", "1"


class CodexRouting(Routing):
    agent, reader = "codex", "0"


class CodexSharedReader(Routing):
    agent, reader = "codex", "1"


if __name__ == "__main__":
    unittest.main()
