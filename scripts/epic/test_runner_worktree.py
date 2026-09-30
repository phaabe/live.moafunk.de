"""runner_worktree.py against real Git repositories (no GitHub calls)."""

from __future__ import annotations

import isolated_env  # noqa: F401  (first: hides live runner state)

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

import runner_worktree as rw

BRANCH = "feat/77-ring-buffer"
SHA = "a" * 40


def run(cwd: Path, *args: str) -> str:
    return subprocess.run(
        list(args), cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


class RealGitTest(unittest.TestCase):
    """An origin named like the real repository, the runner clone and its dir."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="runner-wt-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        origin = self.tmp / "phaabe" / "live.moafunk.de.git"
        origin.parent.mkdir()
        run(self.tmp, "git", "init", "-q", "--bare", str(origin))
        seed = self.tmp / "seed"
        run(self.tmp, "git", "clone", "-q", str(origin), str(seed))
        for key, value in (("user.name", "t"), ("user.email", "t@t")):
            run(seed, "git", "config", key, value)
        (seed / "README").write_text("base\n")
        run(seed, "git", "add", "README")
        run(seed, "git", "commit", "-q", "-m", "base")
        run(seed, "git", "push", "-q", "origin", "HEAD:refs/heads/dev/312-interim")
        run(seed, "git", "push", "-q", "origin", f"HEAD:refs/heads/{BRANCH}")
        self.repo = self.tmp / "runner"
        run(self.tmp, "git", "clone", "-q", str(origin), str(self.repo))
        for key, value in (("user.name", "t"), ("user.email", "t@t")):
            run(self.repo, "git", "config", key, value)
        self.root = self.tmp / "claude-wt"
        self.reads: list[str] = []

    def read(self, pr: dict[str, Any] | None = None, title: str = "") -> Any:
        def reader(endpoint: str) -> Any:
            self.reads.append(endpoint)
            if "/pulls/" in endpoint:
                return {
                    "state": "open",
                    "head": {
                        "ref": BRANCH,
                        "repo": {"full_name": "phaabe/live.moafunk.de"},
                    },
                    "base": {"ref": "dev/312-interim"},
                    "body": "Executor: Claude\nReviewer: Codex\n",
                    **(pr or {}),
                }
            return {"title": title}

        return reader

    def fix(self) -> dict[str, Any]:
        return {"action": "fix", "reason": "t", "pr": 5, "sha": SHA}

    def prepare(self, action: dict[str, Any], **read: Any) -> Path | None:
        return rw.prepare("claude", action, self.repo, self.root, self.read(**read))

    def human_checkout(self) -> Path:
        """A human session holds the branch, with unfinished work in it."""
        human = self.tmp / "human"
        run(self.repo, "git", "worktree", "add", "-q", str(human), BRANCH)
        (human / "draft.txt").write_text("human work\n")
        return human

    def snapshot(self, path: Path) -> tuple[str, str, str]:
        return (
            run(path, "git", "rev-parse", "HEAD"),
            run(path, "git", "status", "--porcelain"),
            (path / "draft.txt").read_text(),
        )

    def test_new_worktree_for_a_pr_branch_on_origin(self) -> None:
        path = self.prepare(self.fix())
        self.assertEqual(path, self.root / BRANCH)
        assert path is not None
        self.assertEqual(run(path, "git", "branch", "--show-current").strip(), BRANCH)
        self.assertEqual(self.reads, ["repos/phaabe/live.moafunk.de/pulls/5"])

    def test_resume_keeps_unfinished_work(self) -> None:
        path = self.prepare(self.fix())
        assert path is not None
        (path / "wip.txt").write_text("unfinished\n")
        head = run(path, "git", "rev-parse", "HEAD")
        self.assertEqual(self.prepare(self.fix()), path)
        self.assertEqual((path / "wip.txt").read_text(), "unfinished\n")
        self.assertEqual(run(path, "git", "rev-parse", "HEAD"), head)

    def conflicting_rebase(self, path: Path) -> None:
        """Stop a rebase of the branch in `path` on a conflict."""
        (path / "README").write_text("branch\n")
        run(path, "git", "commit", "-q", "-am", "branch change")
        base = run(self.repo, "git", "rev-parse", "origin/dev/312-interim").strip()
        run(self.repo, "git", "branch", "moved-base", base)
        other = self.tmp / "other"
        run(self.repo, "git", "worktree", "add", "-q", str(other), "moved-base")
        (other / "README").write_text("base change\n")
        run(other, "git", "commit", "-q", "-am", "base change")
        rebase = subprocess.run(
            ["git", "rebase", "moved-base"], cwd=path, capture_output=True, text=True
        )
        self.assertNotEqual(rebase.returncode, 0)
        self.assertEqual(run(path, "git", "branch", "--show-current"), "")

    def test_resume_keeps_an_unfinished_rebase(self) -> None:
        # Codex review on https://github.com/phaabe/live.moafunk.de/pull/516:
        # a rebase stopped by a conflict detaches HEAD; the runner refused it.
        path = self.prepare({**self.fix(), "action": "resolve-conflict"})
        assert path is not None
        self.conflicting_rebase(path)
        status = run(path, "git", "status", "--porcelain")
        self.assertEqual(
            self.prepare({**self.fix(), "action": "resolve-conflict"}), path
        )
        self.assertEqual(run(path, "git", "status", "--porcelain"), status)
        rebase_dir = run(
            path,
            "git",
            "rev-parse",
            "--path-format=absolute",
            "--git-path",
            "rebase-merge",
        ).strip()
        self.assertTrue(Path(rebase_dir).is_dir())

    def test_rebase_in_a_human_checkout_needs_handoff(self) -> None:
        human = self.human_checkout()
        (human / "draft.txt").unlink()
        self.conflicting_rebase(human)
        with self.assertRaises(rw.Stop) as stop:
            self.prepare(self.fix())
        self.assertEqual(str(stop.exception), f"handoff needed: {BRANCH} in {human}")
        self.assertFalse((self.root / BRANCH).exists())

    def test_deleted_runner_checkout_is_not_resumed(self) -> None:
        path = self.prepare(self.fix())
        assert path is not None
        subprocess.run(["rm", "-rf", str(path)], check=True)
        with self.assertRaisesRegex(rw.Stop, "registered for .* but missing"):
            self.prepare(self.fix())
        self.assertFalse(path.exists())

    def test_branch_held_elsewhere_needs_handoff_and_changes_nothing(self) -> None:
        human = self.human_checkout()
        before = self.snapshot(human)
        with self.assertRaises(rw.Stop) as stop:
            self.prepare(self.fix())
        self.assertEqual(str(stop.exception), f"handoff needed: {BRANCH} in {human}")
        self.assertEqual(self.snapshot(human), before)
        self.assertFalse((self.root / BRANCH).exists())

    def test_git_refusal_is_reported_as_handoff(self) -> None:
        # The branch is taken between the listing and `git worktree add`.
        human = self.human_checkout()
        before = self.snapshot(human)
        with self.assertRaises(rw.Stop) as stop:
            rw.add(self.repo, BRANCH, self.root / BRANCH, new=False)
        self.assertEqual(str(stop.exception), f"handoff needed: {BRANCH} in {human}")
        self.assertEqual(self.snapshot(human), before)

    def test_released_branch_is_created_on_a_later_tick(self) -> None:
        human = self.human_checkout()
        with self.assertRaises(rw.Stop):
            self.prepare(self.fix())
        (human / "draft.txt").unlink()
        run(self.repo, "git", "worktree", "remove", str(human))
        self.assertEqual(self.prepare(self.fix()), self.root / BRANCH)

    def test_claim_creates_a_new_branch_from_the_interim_base(self) -> None:
        claim = {
            "action": "claim",
            "reason": "t",
            "issue": "https://github.com/phaabe/live.moafunk.de/issues/429",
        }
        path = self.prepare(claim, title="Keep Claude edits in runner worktrees!")
        self.assertEqual(
            path, self.root / "feat/429-keep-claude-edits-in-runner-worktrees"
        )
        assert path is not None
        self.assertEqual(
            run(path, "git", "rev-parse", "HEAD"),
            run(self.repo, "git", "rev-parse", "origin/dev/312-interim"),
        )

    def test_issue_resumes_its_one_existing_branch(self) -> None:
        cont = {
            "action": "continue",
            "reason": "t",
            "issue": "https://github.com/phaabe/live.moafunk.de/issues/77",
        }
        self.assertEqual(self.prepare(cont), self.root / BRANCH)
        self.assertEqual(self.reads, [])

    def test_issue_with_several_branches_stops(self) -> None:
        run(self.repo, "git", "branch", "fix/77-other", "origin/dev/312-interim")
        cont = {
            "action": "continue",
            "reason": "t",
            "issue": "https://github.com/phaabe/live.moafunk.de/issues/77",
        }
        with self.assertRaisesRegex(rw.Stop, "several branches"):
            self.prepare(cont)

    def test_pr_checks_refuse_foreign_heads_bases_and_owners(self) -> None:
        cases = {
            "not a branch of": {"head": {"ref": BRANCH, "repo": {"full_name": "x/y"}}},
            "not an epic base": {"base": {"ref": "main"}},
            "not assigned to claude": {"body": "Executor: Codex\n"},
            "not a feature branch": {
                "head": {"ref": "main", "repo": {"full_name": "phaabe/live.moafunk.de"}}
            },
            "not open": {"state": "closed"},
        }
        for reason, pr in cases.items():
            with self.subTest(reason), self.assertRaisesRegex(rw.Stop, reason):
                self.prepare(self.fix(), pr=pr)
        self.assertFalse(self.root.exists())

    def test_other_repository_is_refused(self) -> None:
        run(
            self.repo, "git", "remote", "set-url", "origin", "git@github.com:x/fork.git"
        )
        with self.assertRaisesRegex(RuntimeError, "not phaabe/live.moafunk.de"):
            self.prepare(self.fix())

    def test_foreign_path_in_the_directory_is_not_touched(self) -> None:
        (self.root / BRANCH).mkdir(parents=True)
        (self.root / BRANCH / "keep.txt").write_text("x")
        with self.assertRaisesRegex(rw.Stop, "does not hold"):
            self.prepare(self.fix())
        self.assertEqual((self.root / BRANCH / "keep.txt").read_text(), "x")

    def test_actions_without_edits_need_no_worktree(self) -> None:
        for kind in ("review", "merge", "adopt", "escalate", "idle"):
            self.assertIsNone(self.prepare({"action": kind, "reason": "t", "pr": 5}))
        self.assertEqual(self.reads, [])


class RememberTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="runner-wt-state-")
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.action = {"action": "fix", "reason": "t", "pr": 5, "sha": SHA}

    def remember(self, message: str | None, now: float, **action: Any) -> bool:
        return rw.remember(
            "claude", {**self.action, **action}, message, self.state, now, ttl=100
        )

    def test_same_stop_is_a_repeat_until_it_changes(self) -> None:
        self.assertFalse(self.remember("handoff needed: b in /p", 0))
        self.assertTrue(self.remember("handoff needed: b in /p", 50))
        # Another holder, a new head or an expired record: report again.
        self.assertFalse(self.remember("handoff needed: b in /q", 60))
        self.assertFalse(self.remember("handoff needed: b in /q", 70, sha="b" * 40))
        self.assertFalse(self.remember("handoff needed: b in /q", 500, sha="b" * 40))

    def test_ready_worktree_clears_the_record(self) -> None:
        self.remember("handoff needed: b in /p", 0)
        self.assertFalse(self.remember(None, 10))
        self.assertFalse(self.remember("handoff needed: b in /p", 20))
        records = json.loads((self.state / "claude-handoff.json").read_text())
        self.assertEqual(list(records), ["5"])


if __name__ == "__main__":
    unittest.main()
