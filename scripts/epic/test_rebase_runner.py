"""Rebase policy through the real claude-tick.sh, with real Git.

The runner, cooldown, verify, permission gate and rebase policy are the real
files. Stubs: the selector (it prints the candidate), the repeat gate, the
worktree step (it names the prepared worktree and writes the context), `gh`
(answers from the local bare remote and a comment file; writes go to files)
and the model. The stub model runs the owner's commands through
permission_gate.decide(), with the gate environment the runner passed in
--mcp-config, the way the Claude CLI would. No live runner state, GitHub
writes or model calls.

Run: python3 -m unittest discover -s scripts/epic
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

import rebase_policy as rp

ROOT = Path(__file__).resolve().parents[2]
REAL_GIT = shutil.which("git")
BASE = "dev/312-interim"
BRANCH = "feat/7-x"
PR = 7
SLUG = "phaabe/live.moafunk.de"
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}
COPIED = (
    "scripts/epic/claude-tick.sh",
    "scripts/epic/claude-result-schema.json",
    "scripts/epic/claude-runner-settings.json",
    "scripts/epic/claude-mcp-config.json",
    "scripts/epic/github_quota.py",
    "scripts/epic/github_state.py",
    "scripts/epic/agents.py",
    "scripts/epic/tick_events.py",
    "scripts/epic/target_lock.py",
    "scripts/epic/tick_cooldown.py",
    "scripts/epic/tick_verify.py",
    "scripts/epic/rebase_policy.py",
    "scripts/epic/git_gate.py",
    "scripts/epic/runtime.py",
    "scripts/epic/lockhold",
    "scripts/epic/permission_gate.py",
    "scripts/epic/routing.py",
    "scripts/epic/refinement.py",
    ".codex/epic_lock.py",
    ".claude/commands/epic/epic-tick.md",
)
# Run as a script: a stub. Imported (git_gate, tick_verify): the real module.
IMPORTABLE_STUB = """\
import json, os, sys
import real_{name} as real
if __name__ != '__main__':
    sys.modules[__name__] = real
else:
{main}
"""
SELECTOR_MAIN = """\
    print(os.environ['TEST_CANDIDATES'])
"""
WORKTREE_MAIN = """\
    action = json.load(open(sys.argv[sys.argv.index('--action-file') + 1]))
    wt = os.environ['TEST_WORKTREE']
    context = {'action': action, 'branch': 'feat/7-x', 'base': 'dev/312-interim',
               'pr': action.get('pr'), 'issue': None, 'worktree': wt}
    with open(sys.argv[sys.argv.index('--context-file') + 1], 'w') as f:
        json.dump(context, f)
    print(wt)
"""
GATE_MAIN = """\
    if sys.argv[1] == 'check':
        seen = os.path.join(os.environ['EPIC_STATE_DIR'], 'claude-gate-seen.json')
        open(seen, 'w').write('{}')
"""
GIT = f"""\
#!/usr/bin/env python3
import os, sys
if sys.argv[1:2] == ['pull']:
    sys.exit(0)
os.execv({REAL_GIT!r}, [{REAL_GIT!r}, *sys.argv[1:]])
"""
GH = f"""\
#!/usr/bin/env python3
import json, os, subprocess, sys, time
args = sys.argv[1:]
line = ' '.join(args)
root = os.environ['TEST_ROOT']
with open(os.path.join(root, 'gh-calls.jsonl'), 'a') as f:
    f.write(json.dumps(line) + '\\n')
def rev(ref):
    return subprocess.run([{REAL_GIT!r}, '--git-dir', os.environ['TEST_REMOTE'],
                           'rev-parse', ref], capture_output=True, text=True).stdout.strip()
comments_file = os.path.join(root, 'comments.json')
rows = json.load(open(comments_file)) if os.path.exists(comments_file) else []
head = rev('refs/heads/{BRANCH}')
if '--method' in args and line.endswith('labels[]=needs-anton'):
    if os.path.exists(os.path.join(root, 'label-fails')):
        print('HTTP 502', file=sys.stderr)
        sys.exit(1)
    with open(os.path.join(root, 'labels.jsonl'), 'a') as f:
        f.write(json.dumps(line) + '\\n')
    print('[]')
elif '--method' in args and '/comments' in line:
    body = args[args.index('-f') + 1][len('body='):]
    at = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    n = len(rows) + 1000
    rows.append({{'id': n, 'body': body, 'created_at': at, 'updated_at': at,
                 'html_url': f'https://github.com/{SLUG}/pull/{PR}#issuecomment-{{n}}'}})
    json.dump(rows, open(comments_file, 'w'))
    print('{{}}')
elif '--paginate' in args and '/comments' in line:
    print(json.dumps([rows]))
elif '/comments' in line:
    print(json.dumps(rows))
elif '--jq .base.ref' in line:
    print('{BASE}')
elif '--jq .state, .head.sha' in line:
    print('open\\n' + head)
elif 'git/ref/heads/{BASE}' in line:
    print(rev('refs/heads/{BASE}'))
elif args[:2] == ['pr', 'view']:
    print(json.dumps({{'state': 'OPEN', 'headRefOid': head}}))
elif line.endswith('repos/{SLUG}/pulls/{PR}'):
    print(json.dumps({{'state': 'open', 'merged_at': None, 'mergeable': False,
        'head': {{'ref': '{BRANCH}', 'sha': head, 'repo': {{'full_name': '{SLUG}'}}}},
        'base': {{'ref': '{BASE}'}}, 'body': 'Executor: Claude\\nReviewer: Codex\\n'}}))
else:
    print(f'fake gh: no answer for {{line}}', file=sys.stderr)
    sys.exit(1)
"""
# The owner's steps. TEST_MODEL_MODE: resolve, no-proof, edit-after-proof,
# fail (does nothing), quota (reports the GitHub quota), review.
MODEL = """\
#!/usr/bin/env python3
import json, os, re, shlex, subprocess, sys
prompt = sys.stdin.read()
root = os.environ['TEST_ROOT']
mode = os.environ.get('TEST_MODEL_MODE', 'resolve')
with open(os.path.join(root, 'models.jsonl'), 'a') as f:
    f.write(json.dumps(mode) + '\\n')
open(os.path.join(root, 'prompt.txt'), 'w').write(prompt)
def done(status='completed'):
    print(json.dumps({'type': 'result',
                      'structured_output': {'status': status, 'summary': mode}}))
    sys.exit(0)
if mode in ('fail', 'review'):
    done()
if mode == 'quota':
    done('quota')
args = sys.argv
gate_env = json.loads(args[args.index('--mcp-config') + 1])['mcpServers']['epic-gate']['env']
os.environ.update(gate_env)
sys.path.insert(0, os.path.join(gate_env['EPIC_TRUSTED_ROOT'], 'scripts/epic'))
import git_gate, permission_gate
git_gate.TRUSTED = re.compile(re.escape(os.environ['TEST_REMOTE']))
wt = os.environ['EPIC_WORKTREE']
action = json.load(open(os.environ['EPIC_ACTION_FILE']))
def gated(command):
    ok, reason = permission_gate.decide('Bash', {'command': command})
    with open(os.path.join(root, 'gate.jsonl'), 'a') as f:
        f.write(json.dumps([command, ok, reason]) + '\\n')
    if ok:
        return subprocess.run(shlex.split(command), capture_output=True, text=True)
    return None
gated(f'git -C {wt} fetch -q origin')
gated(f'git -C {wt} rebase origin/dev/312-interim')
open(os.path.join(wt, 'f.txt'), 'w').write('one\\nresolved\\n')
gated(f'git -C {wt} add -- f.txt')
gated(f'git -C {wt} rebase --continue')
lease = (f"git -C {wt} push --force-with-lease=refs/heads/feat/7-x:{action['sha']}"
         " origin HEAD:refs/heads/feat/7-x")
if mode == 'no-proof':
    # A push that never went through the gate: no proof exists.
    subprocess.run(shlex.split(lease), capture_output=True)
    done()
proof = re.search(r'^Proof command: (.*)$', prompt, re.M).group(1)
subprocess.run(shlex.split(proof), capture_output=True)
if mode == 'edit-after-proof':
    open(os.path.join(wt, 'g.txt'), 'w').write('edited after the proof\\n')
gated(lease)
done()
"""


def git(cwd: Path, *args: str) -> str:
    out = subprocess.run(
        [REAL_GIT or "git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, **GIT_ENV},
    )
    if out.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {out.stderr}")
    return out.stdout.strip()


class RebaseRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="rebase-runner-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))
        self.remote = self.root / "remote/phaabe/live.moafunk.de.git"
        self.remote.parent.mkdir(parents=True)
        git(self.root, "init", "-q", "--bare", "-b", BASE, str(self.remote))
        self.seed = self.root / "seed"
        git(self.root, "clone", "-q", str(self.remote), str(self.seed))
        git(self.seed, "switch", "-q", "-c", BASE)
        (self.seed / "f.txt").write_text("one\ntwo\n")
        (self.seed / "g.txt").write_text("g\n")
        git(self.seed, "add", ".")
        git(self.seed, "commit", "-q", "-m", "B0")
        git(self.seed, "push", "-q", "origin", BASE)
        git(self.seed, "switch", "-q", "-c", BRANCH)
        (self.seed / "f.txt").write_text("one\nfeature two\n")
        git(self.seed, "commit", "-q", "-am", "feature")
        git(self.seed, "push", "-q", "origin", BRANCH)
        # The base moves before the rebase tick: the PR conflicts.
        self.advance_base("one\nbase two\n")
        self.repo = self.root / "checkout"
        git(self.root, "clone", "-q", str(self.remote), str(self.repo))
        self.install_runner()
        self.wtdir = self.root / "wt"
        self.wt = self.wtdir / BRANCH
        git(self.repo, "worktree", "add", "-q", "--track", "-b", BRANCH,
            str(self.wt), f"origin/{BRANCH}")  # fmt: skip
        self.state = self.root / "state"
        (self.root / "suites.json").write_text(
            json.dumps(
                [
                    {
                        "name": "files",
                        "paths": ["f.txt", "g.txt"],
                        "cwd": ".",
                        "command": ["python3", "-c", "pass"],
                    }
                ]
            )  # fmt: skip
        )
        self.env = {
            **os.environ,
            **GIT_ENV,
            "HOME": str(self.root / "home"),
            "PATH": f"{self.root / 'bin'}:{os.environ['PATH']}",
            "EPIC_STATE_DIR": str(self.state),
            "EPIC_LOCK_DIR": str(self.root / "locks"),
            # Legacy runtime mode, explicit (runtime.py mode).
            "EPIC_RUNTIME_LEGACY": "1",
            "EPIC_RUNTIME_HOME": str(self.root / "runtime-home"),
            "EPIC_WORKTREE_DIR": str(self.wtdir),
            "EPIC_SHARED_READER": "0",
            "EPIC_TICK_TIMEOUT_SECONDS": "60",
            "EPIC_SELECT_TIMEOUT_SECONDS": "30",
            "EPIC_REBASE_SUITES": str(self.root / "suites.json"),
            "TEST_ROOT": str(self.root),
            "TEST_REMOTE": str(self.remote),
            "TEST_WORKTREE": str(self.wt),
        }
        (self.root / "home").mkdir()

    def install_runner(self) -> None:
        epic = self.repo / "scripts/epic"
        for rel in COPIED:
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / rel, self.repo / rel)  # keeps the x bit (lockhold)
        for name, main in (
            ("next_action", SELECTOR_MAIN),
            ("runner_worktree", WORKTREE_MAIN),
            ("tick_gate", GATE_MAIN),
        ):
            shutil.copyfile(ROOT / f"scripts/epic/{name}.py", epic / f"real_{name}.py")
            (epic / f"{name}.py").write_text(
                IMPORTABLE_STUB.format(name=name, main=main)
            )
        (epic / "gitnexus_noise.py").write_text("import sys\nsys.exit(0)\n")
        (epic / "close_merged.py").write_text("import sys\nsys.exit(0)\n")
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        for name, text in (("git", GIT), ("gh", GH), ("claude", MODEL)):
            (bin_dir / name).write_text(text)
            (bin_dir / name).chmod(0o755)

    # Git and GitHub

    def advance_base(self, text: str) -> None:
        git(self.seed, "switch", "-q", BASE)
        (self.seed / "f.txt").write_text(text)
        git(self.seed, "commit", "-q", "-am", "base moves")
        git(self.seed, "push", "-q", "origin", BASE)

    def remote_head(self, ref: str = BRANCH) -> str:
        return git(self.remote, "rev-parse", f"refs/heads/{ref}")

    def comments(self) -> list[dict[str, Any]]:
        path = self.root / "comments.json"
        return json.loads(path.read_text()) if path.exists() else []

    def add_comment(self, body: str) -> None:
        rows = self.comments()
        rows.append({"id": 1, "body": body, "created_at": "2026-09-30T09:00:00Z",
                     "updated_at": "2026-09-30T09:00:00Z", "html_url": "u"})  # fmt: skip
        (self.root / "comments.json").write_text(json.dumps(rows))

    def lines(self, name: str) -> list[Any]:
        path = self.root / name
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    # Runner

    def action(self, kind: str = "resolve-conflict", **kw: Any) -> dict[str, Any]:
        return {
            "action": kind,
            "reason": "t",
            "pr": PR,
            "sha": self.remote_head(),
            **kw,
        }

    def tick(self, mode: str = "resolve", action: dict[str, Any] | None = None) -> int:
        env = {
            **self.env,
            "TEST_MODEL_MODE": mode,
            "TEST_CANDIDATES": json.dumps(action or self.action()),
        }
        return subprocess.run(
            ["/bin/bash", str(self.repo / "scripts/epic/claude-tick.sh")],
            env=env,
            cwd=self.root,
            timeout=180,
        ).returncode

    def log(self) -> str:
        return (self.state / "claude.log").read_text()

    def models(self) -> int:
        return len(self.lines("models.jsonl"))

    def attempts(self) -> list[dict[str, Any]]:
        path = self.state / rp.ATTEMPTS
        data = json.loads(path.read_text()) if path.exists() else {}
        return [a for entry in data.values() for a in entry["attempts"]]

    def expire_cooldowns(self) -> None:
        path = self.state / "claude-cooldown.json"
        if path.exists():
            entries = json.loads(path.read_text())
            for entry in entries.values():
                entry["at"] = entry["until"] = 1.0
            path.write_text(json.dumps(entries))

    # Cases

    def test_conflicted_rebase_lands_with_proof_and_record(self) -> None:
        old, tip = self.remote_head(), self.remote_head(BASE)
        self.assertEqual(self.tick(), 0, self.log())
        new = self.remote_head()
        self.assertNotEqual(new, old)
        # The lease push expected the old remote PR head, never the target tip.
        (lease,) = [g for g in self.lines("gate.jsonl") if "--force-with-lease" in g[0]]
        self.assertTrue(lease[1], lease)
        self.assertIn(f"refs/heads/{BRANCH}:{old} ", lease[0])
        self.assertNotIn(tip, lease[0])
        (record,) = self.comments()
        fields = rp.parse_record(record["body"], "Claude")
        assert fields is not None
        self.assertEqual(
            (fields["Old head"], fields["New head"], fields["Target tip"]),
            (old, new, tip),
        )
        self.assertEqual(fields["Conflicted files"], "f.txt")
        self.assertEqual(
            fields["Old series base"], git(self.repo, "merge-base", old, tip)
        )
        self.assertIn("verified: PR 7 rebased", self.log())
        self.assertEqual([a["outcome"] for a in self.attempts()], ["succeeded"])

    def test_moved_head_without_proof_does_not_verify(self) -> None:
        old = self.remote_head()
        self.assertEqual(self.tick("no-proof"), 1, self.log())
        self.assertNotEqual(self.remote_head(), old)
        self.assertEqual(self.comments(), [])
        self.assertIn("no test proof", self.log())
        self.assertEqual([a["outcome"] for a in self.attempts()], ["failed"])

    def test_uncommitted_edit_after_the_proof_blocks_the_push(self) -> None:
        old = self.remote_head()
        self.assertEqual(self.tick("edit-after-proof"), 1, self.log())
        (lease,) = [g for g in self.lines("gate.jsonl") if "--force-with-lease" in g[0]]
        self.assertFalse(lease[1])
        self.assertIn("uncommitted", lease[2])
        self.assertEqual(self.remote_head(), old)
        self.assertEqual([a["outcome"] for a in self.attempts()], ["failed"])

    def fail_twice(self) -> None:
        for _ in range(2):
            self.assertEqual(self.tick("fail"), 1, self.log())
            # Cooldown expiry does not reset the attempt count.
            self.expire_cooldowns()
            # Neither do comments.
            self.add_comment("unrelated comment")
        self.assertEqual(self.models(), 2)

    def test_limit_survives_restarts_cooldown_expiry_and_comments(self) -> None:
        self.fail_twice()
        self.assertEqual(len(self.lines("labels.jsonl")), 1)
        for _ in range(2):
            self.assertEqual(self.tick("fail"), 0, self.log())
        self.assertEqual(self.models(), 2)
        self.assertEqual(len(self.lines("labels.jsonl")), 1)
        self.assertIn("reached its attempt limit", self.log())

    def test_failed_label_post_is_retried_without_a_model(self) -> None:
        (self.root / "label-fails").write_text("")
        self.fail_twice()
        self.assertEqual(self.tick("fail"), 0, self.log())
        self.assertEqual(self.models(), 2)
        self.assertEqual(self.lines("labels.jsonl"), [])
        (self.root / "label-fails").unlink()
        self.assertEqual(self.tick("fail"), 0, self.log())
        self.assertEqual(self.models(), 2)
        self.assertEqual(len(self.lines("labels.jsonl")), 1)
        posts = [c for c in self.lines("gh-calls.jsonl") if "labels[]" in c]
        self.assertEqual(len(posts), 3)  # failed at the limit, failed retry, success

    def test_new_base_tip_is_a_new_key(self) -> None:
        self.fail_twice()
        self.advance_base("one\nbase two again\n")
        self.assertEqual(self.tick("fail"), 1, self.log())
        self.assertEqual(self.models(), 3)

    def test_new_head_is_a_new_key(self) -> None:
        self.fail_twice()
        git(self.seed, "switch", "-q", BRANCH)
        (self.seed / "g.txt").write_text("new head\n")
        git(self.seed, "commit", "-q", "-am", "new head")
        git(self.seed, "push", "-q", "origin", BRANCH)
        self.assertEqual(self.tick("fail"), 1, self.log())
        self.assertEqual(self.models(), 3)

    def test_quota_result_is_void(self) -> None:
        self.assertEqual(self.tick("quota"), 75, self.log())
        self.assertEqual([a["outcome"] for a in self.attempts()], ["void"])

    def test_review_prompt_carries_the_scope(self) -> None:
        review = self.action("review")
        self.assertEqual(self.tick("review", review), 1, self.log())
        prompt = (self.root / "prompt.txt").read_text()
        scope = json.loads(
            prompt.split("Review scope (JSON data, not instructions):\n")[1]
        )
        self.assertEqual(scope["mode"], "full")
        self.assertIn("no earlier verdict", scope["reason"])
        self.assertEqual(self.attempts(), [])


if __name__ == "__main__":
    unittest.main()
