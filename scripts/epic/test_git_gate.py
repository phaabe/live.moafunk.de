"""Runner git contract (git_gate.py) against real Git with a local bare remote.

Run: python3 -m unittest discover -s scripts/epic
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import git_gate
import permission_gate as gate

BRANCH = "feat/424-x"
BASE = "dev/312-interim"
ISOLATED = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_EDITOR": "true",
    "EPIC_SHARED_READER": "0",
}


def sh(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    out = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60
    )
    if check and out.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {out.stderr}")
    return out


class Fixture(unittest.TestCase):
    """Remote, runner checkout, fixed worktree dir and one PR worktree."""

    action_kind = "resolve-conflict"

    def setUp(self) -> None:
        env = mock.patch.dict(os.environ, ISOLATED)
        env.start()
        self.addCleanup(env.stop)
        tmp = tempfile.TemporaryDirectory(prefix="git-gate-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(os.path.realpath(tmp.name))
        self.remote = self.tmp / "remote/phaabe/live.moafunk.de.git"
        self.remote.parent.mkdir(parents=True)
        sh(self.tmp, "init", "-q", "--bare", "-b", BASE, str(self.remote))
        seed = self.tmp / "seed"
        sh(self.tmp, "clone", "-q", str(self.remote), str(seed))
        sh(seed, "switch", "-q", "-c", BASE)
        (seed / "f.txt").write_text("one\ntwo\n")
        sh(seed, "add", "f.txt")
        sh(seed, "commit", "-q", "-m", "base")
        sh(seed, "push", "-q", "origin", BASE)
        sh(seed, "switch", "-q", "-c", BRANCH)
        (seed / "g.txt").write_text("feature\n")
        sh(seed, "add", "g.txt")
        sh(seed, "commit", "-q", "-m", "feature")
        sh(seed, "push", "-q", "origin", BRANCH)
        self.seed = seed
        self.root = self.tmp / "runner"
        sh(self.tmp, "clone", "-q", str(self.remote), str(self.root))
        self.wtdir = self.tmp / "runner-wt"
        self.wt = self.wtdir / BRANCH
        sh(self.root, "worktree", "add", "-q", "--track", "-b", BRANCH, str(self.wt),
           f"origin/{BRANCH}")  # fmt: skip
        self.head = self.remote_head()
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.pr: dict[str, Any] = {
            "number": 7,
            "state": "open",
            "merged_at": None,
            "head": {
                "ref": BRANCH,
                "sha": self.head,
                "repo": {"full_name": "phaabe/live.moafunk.de"},
            },
            "base": {"ref": BASE},
            "body": "Executor: Claude\nReviewer: Codex\n",
        }
        reader = mock.patch.object(git_gate, "read_pr", self.read_pr)
        reader.start()
        self.addCleanup(reader.stop)
        self.set_action({"action": self.action_kind, "reason": "t", "pr": 7,
                         "sha": self.head})  # fmt: skip
        paths = mock.patch.dict(
            os.environ,
            {
                "EPIC_ACTION_FILE": str(self.tmp / "action.json"),
                "EPIC_CONTEXT_FILE": str(self.tmp / "context.json"),
                "EPIC_STATE_DIR": str(self.state),
                "EPIC_TRUSTED_ROOT": str(self.root),
                "EPIC_WORKTREE_DIR": str(self.wtdir),
            },
        )
        paths.start()
        self.addCleanup(paths.stop)

    def read_pr(self, number: int) -> dict[str, Any]:
        self.assertEqual(number, 7)
        return json.loads(json.dumps(self.pr))

    def set_action(self, action: dict[str, Any], **context: Any) -> None:
        self.action = action
        (self.tmp / "action.json").write_text(json.dumps(action))
        ctx = {
            "action": action,
            "branch": BRANCH,
            "base": BASE if action.get("pr") else None,
            "pr": action.get("pr"),
            "issue": action.get("issue"),
            "worktree": str(self.wt),
            **context,
        }
        (self.tmp / "context.json").write_text(json.dumps(ctx))

    def remote_head(self, branch: str = BRANCH) -> str:
        return sh(self.remote, "rev-parse", f"refs/heads/{branch}").stdout.strip()

    def decide(self, command: str) -> tuple[bool, str]:
        return gate.decide("Bash", {"command": command})

    def allowed(self, command: str) -> bool:
        return self.decide(command)[0]

    def run_approved(self, command: str) -> subprocess.CompletedProcess[str]:
        """Run the command only if the gate approves it, like the CLI does."""
        ok, reason = self.decide(command)
        self.assertTrue(ok, f"{command}: {reason}")
        return subprocess.run(
            shlex.split(command), capture_output=True, text=True, timeout=60
        )

    def advance_base(self, text: str) -> None:
        sh(self.seed, "switch", "-q", BASE)
        (self.seed / "f.txt").write_text(text)
        sh(self.seed, "commit", "-q", "-am", "base moves")
        sh(self.seed, "push", "-q", "origin", BASE)
        sh(self.wt, "fetch", "-q", "origin")

    def lease(self, sha: str | None = None) -> str:
        return (
            f"git -C {self.wt} push --force-with-lease=refs/heads/{BRANCH}:"
            f"{sha or self.head} origin HEAD:refs/heads/{BRANCH}"
        )


class RebaseFlowTest(Fixture):
    def test_rebase_and_lease_push(self) -> None:
        self.advance_base("zero\none\ntwo\n")
        out = self.run_approved(f"git -C {self.wt} rebase origin/{BASE}")
        self.assertEqual(out.returncode, 0, out.stderr)
        out = self.run_approved(self.lease())
        self.assertEqual(out.returncode, 0, out.stderr)
        new = self.remote_head()
        self.assertNotEqual(new, self.head)
        self.assertEqual(sh(self.wt, "rev-parse", "HEAD").stdout.strip(), new)

    def test_quiet_forms(self) -> None:
        self.advance_base("zero\none\ntwo\n")
        out = self.run_approved(f"git -C {self.wt} rebase -q origin/{BASE}")
        self.assertEqual(out.returncode, 0, out.stderr)
        lease = self.lease().replace(" push ", " push -q ")
        self.assertEqual(self.run_approved(lease).returncode, 0)

    def test_attached_c_path_is_approved_but_git_refuses_it(self) -> None:
        # The contract approves `git -C<path>`; Git 2.50 rejects it as an
        # unknown option, so it changes nothing.
        self.advance_base("zero\none\ntwo\n")
        before = sh(self.wt, "rev-parse", "HEAD").stdout
        out = self.run_approved(f"git -C{self.wt} rebase origin/{BASE}")
        if out.returncode != 0:
            self.assertIn("unknown option", out.stderr)
            self.assertEqual(sh(self.wt, "rev-parse", "HEAD").stdout, before)

    def conflict(self) -> None:
        (self.wt / "f.txt").write_text("one\nfeature two\n")
        sh(self.wt, "commit", "-q", "-am", "touch f")
        sh(self.wt, "push", "-q", "origin", BRANCH)
        self.head = self.remote_head()
        self.pr["head"]["sha"] = self.head
        self.set_action({**self.action, "sha": self.head})
        self.advance_base("one\nbase two\n")
        out = self.run_approved(f"git -C {self.wt} rebase origin/{BASE}")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("CONFLICT", out.stdout + out.stderr)

    def test_conflict_then_continue(self) -> None:
        self.conflict()
        # Detached HEAD with a staged resolution: still the recorded rebase.
        (self.wt / "f.txt").write_text("one\nresolved\n")
        self.assertEqual(
            self.run_approved(f"git -C {self.wt} add -- f.txt").returncode, 0
        )
        out = self.run_approved(f"git -C {self.wt} rebase --continue")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.run_approved(self.lease()).returncode, 0)
        self.assertEqual(
            sh(self.remote, "show", f"{BRANCH}:f.txt").stdout, "one\nresolved\n"
        )

    def test_conflict_then_abort(self) -> None:
        self.conflict()
        out = self.run_approved(f"git -C {self.wt} rebase --abort")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(sh(self.wt, "rev-parse", "HEAD").stdout.strip(), self.head)
        self.assertEqual(sh(self.wt, "symbolic-ref", "--short", "HEAD").stdout.strip(),
                         BRANCH)  # fmt: skip

    def test_skip_during_conflict_is_denied(self) -> None:
        self.conflict()
        self.assertFalse(self.allowed(f"git -C {self.wt} rebase --skip"))

    def test_push_during_rebase_is_denied(self) -> None:
        self.conflict()
        self.assertFalse(self.allowed(self.lease()))
        self.assertFalse(self.allowed(f"git -C {self.wt} push origin {BRANCH}"))

    def test_stale_lease_fails_when_the_remote_moves(self) -> None:
        self.advance_base("zero\none\ntwo\n")
        self.run_approved(f"git -C {self.wt} rebase origin/{BASE}")
        # Someone pushes to the PR branch after the pin.
        other = self.tmp / "other"
        sh(self.tmp, "clone", "-q", "-b", BRANCH, str(self.remote), str(other))
        (other / "h.txt").write_text("late\n")
        sh(other, "add", "h.txt")
        sh(other, "commit", "-q", "-m", "late")
        sh(other, "push", "-q", "origin", BRANCH)
        moved = self.remote_head()
        # The fresh read sees it: denied, and the SHA is never renewed.
        self.pr["head"]["sha"] = moved
        ok, reason = self.decide(self.lease())
        self.assertFalse(ok)
        self.assertIn("not renewed", reason)
        self.assertFalse(self.allowed(self.lease(moved)))
        # A stale read still cannot win: Git refuses the lease itself.
        self.pr["head"]["sha"] = self.head
        out = self.run_approved(self.lease())
        self.assertNotEqual(out.returncode, 0)
        self.assertEqual(self.remote_head(), moved)


class RebaseRefusalTest(Fixture):
    def test_dirty_tree_blocks_the_rebase(self) -> None:
        self.advance_base("zero\none\ntwo\n")
        (self.wt / "f.txt").write_text("dirty\n")
        ok, reason = self.decide(f"git -C {self.wt} rebase origin/{BASE}")
        self.assertFalse(ok)
        self.assertIn("uncommitted", reason)
        (self.wt / "f.txt").write_text("one\ntwo\n")
        (self.wt / "new.txt").write_text("untracked\n")
        self.assertFalse(self.allowed(f"git -C {self.wt} rebase origin/{BASE}"))

    def test_unrelated_operation_blocks_the_rebase(self) -> None:
        git_dir = Path(sh(self.wt, "rev-parse", "--absolute-git-dir").stdout.strip())
        (git_dir / "MERGE_HEAD").write_text(self.head + "\n")
        ok, reason = self.decide(f"git -C {self.wt} rebase origin/{BASE}")
        self.assertFalse(ok)
        self.assertIn("unfinished operation", reason)

    def test_unrecorded_rebase_cannot_continue_or_abort(self) -> None:
        self.advance_base("one\nbase two\n")
        (self.wt / "f.txt").write_text("one\nfeature two\n")
        sh(self.wt, "commit", "-q", "-am", "touch f")
        sh(self.wt, "rebase", f"origin/{BASE}", check=False)  # a human's rebase
        for step in ("--continue", "--abort"):
            with self.subTest(step=step):
                ok, reason = self.decide(f"git -C {self.wt} rebase {step}")
                self.assertFalse(ok)
                self.assertIn("no rebase", reason)

    def test_other_rebase_than_recorded_cannot_continue(self) -> None:
        self.advance_base("one\nbase two\n")
        (self.wt / "f.txt").write_text("one\nfeature two\n")
        sh(self.wt, "commit", "-q", "-am", "touch f")
        sh(self.wt, "push", "-q", "origin", BRANCH)
        self.head = self.remote_head()
        self.pr["head"]["sha"] = self.head
        self.set_action({**self.action, "sha": self.head})
        self.run_approved(f"git -C {self.wt} rebase origin/{BASE}")
        sh(self.wt, "rebase", "--abort")
        sh(self.wt, "rebase", "HEAD~1", check=False)  # onto something else
        sh(self.wt, "rebase", "--abort", check=False)
        sh(self.wt, "rebase", f"origin/{BASE}~1", check=False)
        ok, _ = self.decide(f"git -C {self.wt} rebase --continue")
        self.assertFalse(ok)

    def test_wrong_base_is_denied(self) -> None:
        for base in ("main", "dev/streaming-architecture"):
            with self.subTest(base=base):
                self.assertFalse(self.allowed(f"git -C {self.wt} rebase origin/{base}"))
        self.pr["base"]["ref"] = "main"
        self.assertFalse(self.allowed(f"git -C {self.wt} rebase origin/main"))

    def test_wrong_pr_branch_is_denied(self) -> None:
        self.pr["head"]["ref"] = "feat/999-other"
        ok, reason = self.decide(f"git -C {self.wt} rebase origin/{BASE}")
        self.assertFalse(ok)
        self.assertIn("head is feat/999-other", reason)

    def test_foreign_head_repo_or_owner_is_denied(self) -> None:
        self.pr["head"]["repo"]["full_name"] = "someone/fork"
        self.assertFalse(self.allowed(f"git -C {self.wt} rebase origin/{BASE}"))
        self.pr["head"]["repo"]["full_name"] = "phaabe/live.moafunk.de"
        self.pr["body"] = "Executor: Codex\n"
        self.assertFalse(self.allowed(f"git -C {self.wt} rebase origin/{BASE}"))

    def test_moved_head_since_selection_is_denied(self) -> None:
        self.pr["head"]["sha"] = "b" * 40
        ok, reason = self.decide(f"git -C {self.wt} rebase origin/{BASE}")
        self.assertFalse(ok)
        self.assertIn("moved", reason)

    def test_missing_or_stale_context_blocks(self) -> None:
        command = f"git -C {self.wt} rebase origin/{BASE}"
        (self.tmp / "context.json").unlink()
        self.assertIn("missing", self.decide(command)[1])
        self.set_action(self.action)
        (self.tmp / "action.json").write_text(json.dumps({**self.action, "pr": 8}))
        self.assertIn("stale", self.decide(command)[1])

    def test_failed_fresh_read_blocks(self) -> None:
        def broken(_: int) -> dict[str, Any]:
            raise subprocess.CalledProcessError(1, "gh")

        with mock.patch.object(git_gate, "read_pr", broken):
            ok, reason = self.decide(f"git -C {self.wt} rebase origin/{BASE}")
        self.assertFalse(ok)
        self.assertIn("fresh read", reason)

    def test_action_without_a_pr_does_not_rebase(self) -> None:
        self.set_action(
            {
                "action": "claim",
                "reason": "t",
                "issue": "https://github.com/phaabe/live.moafunk.de/issues/424",
            }
        )
        self.assertFalse(self.allowed(f"git -C {self.wt} rebase origin/{BASE}"))
        self.set_action({"action": "review", "reason": "t", "pr": 7, "sha": self.head})
        self.assertFalse(self.allowed(f"git -C {self.wt} rebase origin/{BASE}"))

    def test_lease_without_recorded_rebase_is_denied(self) -> None:
        ok, reason = self.decide(self.lease())
        self.assertFalse(ok)
        self.assertIn("no rebase", reason)

    def test_lease_with_another_sha_is_denied(self) -> None:
        self.advance_base("zero\none\ntwo\n")
        self.run_approved(f"git -C {self.wt} rebase origin/{BASE}")
        self.assertFalse(self.allowed(self.lease("c" * 40)))


class WorktreeTest(Fixture):
    def test_foreign_and_human_worktrees_are_denied(self) -> None:
        human = self.tmp / "human"
        sh(self.root, "worktree", "add", "-q", "-b", "feat/424-human", str(human))
        other = self.wtdir / "feat/425-y"
        sh(self.root, "worktree", "add", "-q", "-b", "feat/425-y", str(other))
        for path in (human, other, self.root, self.tmp / "nowhere"):
            for command in (
                f"git -C {path} push origin {BRANCH}",
                f"git -C {path} rebase origin/{BASE}",
                f"git -C {path} add -- f.txt",
            ):
                with self.subTest(command=command):
                    self.assertFalse(self.allowed(command))

    def test_symlink_into_the_worktree_dir_resolves_to_its_target(self) -> None:
        human = self.tmp / "human"
        sh(self.root, "worktree", "add", "-q", "-b", "feat/424-human", str(human))
        link = self.wtdir / "feat/424-link"
        link.symlink_to(human)
        self.assertFalse(self.allowed(f"git -C {link} status"))

    def test_untrusted_push_url_is_denied(self) -> None:
        sh(self.wt, "remote", "set-url", "--push", "origin", str(self.tmp / "evil.git"))
        self.assertFalse(self.allowed(f"git -C {self.wt} push origin {BRANCH}"))

    def test_worktree_not_on_the_branch_is_denied(self) -> None:
        sh(self.wt, "switch", "-q", "--detach")
        self.assertFalse(self.allowed(f"git -C {self.wt} push origin {BRANCH}"))


class PushTest(Fixture):
    action_kind = "fix"

    def test_normal_push_forms(self) -> None:
        (self.wt / "g.txt").write_text("more\n")
        sh(self.wt, "commit", "-q", "-am", "more")
        for command in (
            f"git -C {self.wt} push origin {BRANCH}",
            f"git -C {self.wt} push -u origin {BRANCH}",
            f"git -C {self.wt} push -q origin {BRANCH}",
            f"git -C {self.wt} push -u -q origin {BRANCH}",
            f"git -C{self.wt} push origin {BRANCH}",
            f"git -C '{self.wt}' push origin '{BRANCH}'",
        ):
            with self.subTest(command=command):
                self.assertTrue(self.allowed(command), self.decide(command)[1])
        out = self.run_approved(f"git -C {self.wt} push -q origin {BRANCH}")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(
            self.remote_head(), sh(self.wt, "rev-parse", "HEAD").stdout.strip()
        )

    def test_first_push_of_a_claimed_issue_needs_no_pr(self) -> None:
        self.set_action(
            {
                "action": "claim",
                "reason": "t",
                "issue": "https://github.com/phaabe/live.moafunk.de/issues/424",
            }
        )
        self.assertTrue(self.allowed(f"git -C {self.wt} push -u origin {BRANCH}"))
        self.set_action({"action": "claim", "reason": "t", "issue": "https://github.com/phaabe/live.moafunk.de/issues/424"},
                        branch="feat/424-other")  # fmt: skip
        self.assertFalse(self.allowed(f"git -C {self.wt} push -u origin {BRANCH}"))

    def test_refused_push_forms(self) -> None:
        wt = self.wt
        lease = f"--force-with-lease=refs/heads/{BRANCH}:{self.head}"
        for command in (
            f"git push origin {BRANCH}",
            f"git push -u origin {BRANCH}",
            f"git -C {wt} push",
            f"git -C {wt} push origin",
            f"git -C {wt} push -q -u origin {BRANCH}",
            f"git -C {wt} push -u -u origin {BRANCH}",
            f"git -C {wt} push -q -q origin {BRANCH}",
            f"git -C {wt} push origin {BRANCH} -q",
            f"git -C {wt} push --force origin {BRANCH}",
            f"git -C {wt} push -f origin {BRANCH}",
            f"git -C {wt} push origin +{BRANCH}",
            f"git -C {wt} push origin {BRANCH}:main",
            f"git -C {wt} push origin HEAD:{BRANCH}",
            f"git -C {wt} push origin main",
            f"git -C {wt} push origin {BASE}",
            f"git -C {wt} push origin feat/999-other",
            f"git -C {wt} push upstream {BRANCH}",
            f"git -C {wt} push origin {BRANCH} {BRANCH}",
            f"git -C {wt} push --tags origin {BRANCH}",
            f"git -C {wt} push --all origin",
            f"git -C {wt} push --mirror origin",
            f"git -C {wt} push --force-with-lease origin {BRANCH}",
            f"git -C {wt} push --force-with-lease={BRANCH} origin HEAD:refs/heads/{BRANCH}",
            f"git -C {wt} push {lease[:-1]} origin HEAD:refs/heads/{BRANCH}",
            f"git -C {wt} push -u {lease} origin HEAD:refs/heads/{BRANCH}",
            f"git -C {wt} push {lease} origin HEAD:refs/heads/{BRANCH} extra",
            f"git -C {wt} push {lease} origin HEAD:refs/heads/main",
            f"git -C {wt} push {lease} origin +HEAD:refs/heads/{BRANCH}",
            f"git -C {wt} push {lease} {lease} origin HEAD:refs/heads/{BRANCH}",
            f"git -C {wt} push {lease} --force origin HEAD:refs/heads/{BRANCH}",
            f"git -C {wt} push {lease} upstream HEAD:refs/heads/{BRANCH}",
            f"git -C {wt} push {lease.replace(BRANCH, 'main')} origin HEAD:refs/heads/main",
        ):
            with self.subTest(command=command):
                self.assertFalse(self.allowed(command))

    def test_global_options_are_denied(self) -> None:
        wt = self.wt
        for command in (
            f"git -c user.name=x -C {wt} push origin {BRANCH}",
            f"git -C {wt} -c user.name=x push origin {BRANCH}",
            f"git -cuser.name=x -C {wt} push origin {BRANCH}",
            f"git -c core.sshCommand=x push origin {BRANCH}",
            f"git -C {wt} -C {wt} push origin {BRANCH}",
            f"git -C{wt} -C{wt} push origin {BRANCH}",
            f"git --git-dir={wt}/.git push origin {BRANCH}",
            f"git --no-pager -C {wt} push origin {BRANCH}",
            f"git -C {wt} --no-pager log",
            f"git -P -C {wt} status",
            f"git -C relative/path push origin {BRANCH}",
            f"git -C ~/x push origin {BRANCH}",
            f"git -C={wt} push origin {BRANCH}",
            "git -C",
            f"git -C {wt}",
        ):
            with self.subTest(command=command):
                self.assertFalse(self.allowed(command))

    def test_shell_wrappers_and_chains_are_denied(self) -> None:
        wt = self.wt
        for command in (
            f"cd {wt} && git push origin {BRANCH}",
            f"git -C {wt} push origin {BRANCH}; git push origin main",
            f"git -C {wt} push origin {BRANCH} | cat",
            f"GIT_DIR=x git -C {wt} push origin {BRANCH}",
            f"env GIT_EDITOR=vi git -C {wt} rebase origin/{BASE}",
            f"command git -C {wt} push origin {BRANCH}",
            f"bash -c 'git -C {wt} push origin {BRANCH}'",
            f"sh -c 'git -C {wt} push origin {BRANCH}'",
            f"xargs git -C {wt} push origin {BRANCH}",
            f"/usr/bin/git -C {wt} push origin {BRANCH}",
            f"git -C {wt} push origin $(echo {BRANCH})",
            f"git -C {wt} push origin `echo {BRANCH}`",
            f"git -C {wt} push origin {BRANCH} > /tmp/x",
            f"git -C {wt} push origin '{BRANCH}",
            f"git -C {wt} push origin {BRANCH} #x",
            f"git -C {wt} add -- *.txt",
        ):
            with self.subTest(command=command):
                self.assertFalse(self.allowed(command))

    def test_rebase_options_are_denied(self) -> None:
        wt = self.wt
        for command in (
            f"git -C {wt} rebase",
            f"git -C {wt} rebase -i origin/{BASE}",
            f"git -C {wt} rebase --interactive origin/{BASE}",
            f"git -C {wt} rebase --exec true origin/{BASE}",
            f"git -C {wt} rebase -x true origin/{BASE}",
            f"git -C {wt} rebase --onto origin/{BASE} HEAD~1",
            f"git -C {wt} rebase --skip",
            f"git -C {wt} rebase -q -q origin/{BASE}",
            f"git -C {wt} rebase origin/{BASE} -q",
            f"git -C {wt} rebase {BASE}",
            f"git -C {wt} rebase upstream/{BASE}",
            f"git -C {wt} rebase --continue --abort",
            f"git rebase origin/{BASE}",
            "git rebase --continue",
        ):
            with self.subTest(command=command):
                self.assertFalse(self.allowed(command))


class DeleteTest(Fixture):
    action_kind = "merge"

    def test_merged_pr_branch_delete(self) -> None:
        command = f"git -C {self.wt} push origin --delete {BRANCH}"
        ok, reason = self.decide(command)
        self.assertFalse(ok)
        self.assertIn("not merged", reason)
        self.pr["merged_at"] = "2026-09-30T00:00:00Z"
        self.pr["state"] = "closed"
        out = self.run_approved(command)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(sh(self.remote, "branch", "--list", BRANCH).stdout, "")

    def test_refused_deletes(self) -> None:
        self.pr["merged_at"] = "2026-09-30T00:00:00Z"
        for command in (
            f"git push origin --delete {BRANCH}",
            f"git -C {self.wt} push origin --delete main",
            f"git -C {self.wt} push origin --delete {BASE}",
            f"git -C {self.wt} push origin --delete feat/999-other",
            f"git -C {self.wt} push origin --delete {BRANCH} feat/999-other",
            f"git -C {self.root} push origin --delete {BRANCH}",
        ):
            with self.subTest(command=command):
                self.assertFalse(self.allowed(command))
        self.set_action({"action": "fix", "reason": "t", "pr": 7, "sha": self.head})
        self.assertFalse(
            self.allowed(f"git -C {self.wt} push origin --delete {BRANCH}")
        )


class LocalCommandTest(Fixture):
    action_kind = "fix"

    def test_read_only_commands(self) -> None:
        sha = self.head
        for command in (
            f"git -C {self.wt} status",
            f"git -C {self.wt} status --short",
            f"git -C {self.wt} status --porcelain",
            f"git -C {self.wt} log --oneline -5",
            f"git -C {self.wt} log -n 3 --oneline",
            f"git -C {self.wt} log --oneline origin/{BASE}..HEAD",
            f"git -C {self.wt} diff origin/{BASE}...HEAD --stat",
            f"git -C {self.wt} diff --name-only {sha}",
            f"git -C {self.wt} diff --name-status --no-color HEAD -- f.txt",
            f"git -C {self.wt} show --stat {sha}",
            f"git -C {self.wt} rev-parse --abbrev-ref HEAD",
            f"git -C {self.wt} rev-parse --show-toplevel",
            f"git -C {self.wt} ls-files",
            f"git -C {self.wt} merge-base HEAD origin/{BASE}",
            f"git -C {self.root} log --oneline -3",
            f"git -C{self.wt} status",
        ):
            with self.subTest(command=command):
                self.assertTrue(self.allowed(command), self.decide(command)[1])

    def test_read_only_needs_no_context(self) -> None:
        (self.tmp / "context.json").unlink()
        self.assertTrue(self.allowed(f"git -C {self.wt} status"))

    def test_refused_read_only_forms(self) -> None:
        wt = self.wt
        for command in (
            f"git -C {wt} diff --textconv",
            f"git -C {wt} diff --ext-diff",
            f"git -C {wt} diff --output=/tmp/x",
            f"git -C {wt} log -o /tmp/x",
            f"git -C {wt} log --format=%H",
            f"git -C {wt} log -n",
            f"git -C {wt} log -n x",
            f"git -C {wt} show HEAD~1",
            f"git -C {wt} show origin/main",
            f"git -C {wt} diff origin/{BASE}..",
            f"git -C {wt} diff f.txt",
            f"git -C {wt} diff -- ../outside",
            f"git -C {wt} diff -- /etc/passwd",
            f"git -C {wt} diff -- :(top)f.txt",
            f"git -C {wt} grep x",
            f"git -C {wt} blame f.txt",
            f"git -C {wt} config --list",
            f"git -C {wt} reset --hard",
            f"git -C {wt} checkout main",
            f"git -C {self.tmp} status",
        ):
            with self.subTest(command=command):
                self.assertFalse(self.allowed(command))

    def test_local_writes(self) -> None:
        message = self.tmp / "msg.txt"
        message.write_text("change\n")
        (self.wt / "f.txt").write_text("changed\n")
        self.assertEqual(
            self.run_approved(f"git -C {self.wt} add -- f.txt").returncode, 0
        )
        out = self.run_approved(f"git -C {self.wt} commit --file {message}")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(self.allowed(f"git -C {self.wt} fetch origin"))
        self.assertEqual(
            self.run_approved(f"git -C {self.wt} fetch -q origin").returncode, 0
        )

    def test_refused_local_writes(self) -> None:
        wt = self.wt
        message = self.tmp / "msg.txt"
        message.write_text("change\n")
        link = self.tmp / "link.txt"
        link.symlink_to(message)
        for command in (
            f"git -C {wt} add f.txt",
            f"git -C {wt} add -A",
            f"git -C {wt} add --",
            f"git -C {wt} add -- ../x",
            f"git -C {wt} add -f -- f.txt",
            f"git -C {wt} commit -m x",
            f"git -C {wt} commit --amend --file {message}",
            f"git -C {wt} commit --file={message}",
            f"git -C {wt} commit --file {self.tmp}/missing.txt",
            f"git -C {wt} commit --file {self.tmp}",
            f"git -C {wt} commit --file {link}",
            f"git -C {wt} commit --file {message} --no-verify",
            f"git -C {wt} fetch",
            f"git -C {wt} fetch upstream",
            f"git -C {wt} fetch origin main:main",
            f"git -C {wt} fetch -q -q origin",
            f"git -C {wt} fetch origin -q",
            f"git -C {self.root} add -- f.txt",
        ):
            with self.subTest(command=command):
                self.assertFalse(self.allowed(command))

    def test_fetch_with_changed_refspec_is_denied(self) -> None:
        sh(
            self.wt,
            "config",
            "--add",
            "remote.origin.fetch",
            "+refs/heads/main:refs/heads/main",
        )
        self.assertFalse(self.allowed(f"git -C {self.wt} fetch origin"))

    def test_writes_need_the_context(self) -> None:
        (self.tmp / "context.json").unlink()
        self.assertFalse(self.allowed(f"git -C {self.wt} add -- f.txt"))


if __name__ == "__main__":
    unittest.main()
