"""Upstream test recovery through the real claude-tick.sh, with real Git.

The runner, attempt check, cooldown, verify, permission gate and rebase
policy are the real files (as in test_rebase_runner.py, whose stubs this
reuses). The PR does not conflict: the base gained a fix that a required
suite needs, and the owner's request names that suite. Stubs: the selector
(it prints the candidate), the repeat gate, the worktree step, `gh` (answers
from the local bare remote and a comment file, GraphQL edit evidence
included) and the model. No live runner state, GitHub writes or model calls.

Run: python3 -m unittest discover -s scripts/epic
"""

from __future__ import annotations

import isolated_env  # noqa: F401  (first: hides live runner state)

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

import next_action as na
import rebase_policy as rp
import test_rebase_runner as rr

ROOT = rr.ROOT
BASE, BRANCH, PR, SLUG = rr.BASE, rr.BRANCH, rr.PR, rr.SLUG
git = rr.git
ON = "upstream-recovery"
# The line the runner appends with the opt-in (the prompt text quotes it too).
ON_LINE = "\nUpstream test recovery: on\n"

GH = r"""#!/usr/bin/env python3
import json, os, subprocess, sys, time
args = sys.argv[1:]
line = ' '.join(args)
root = os.environ['TEST_ROOT']
remote = os.environ['TEST_REMOTE']
with open(os.path.join(root, 'gh-calls.jsonl'), 'a') as f:
    f.write(json.dumps(line) + '\n')
def rev(ref):
    out = subprocess.run([GIT, '--git-dir', remote, 'rev-parse', '--verify', '-q', ref],
                         capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 else ''
def known(sha):
    return rev(sha + '^{commit}') == sha
def ancestor(a, b):
    return subprocess.run([GIT, '--git-dir', remote, 'merge-base', '--is-ancestor', a, b],
                          capture_output=True).returncode == 0
comments_file = os.path.join(root, 'comments.json')
rows = json.load(open(comments_file)) if os.path.exists(comments_file) else []
head = rev('refs/heads/BRANCH')
base = rev('refs/heads/BASE')
def out(data):
    print(data if isinstance(data, str) else json.dumps(data))
    sys.exit(0)
if args[:2] == ['api', 'graphql']:
    if 'pullRequest(number:' in line:
        nodes = [{'databaseId': r['id'], 'lastEditedAt': r.get('last_edited'),
                  'body': r['body'], 'createdAt': r['created_at'],
                  'updatedAt': r['updated_at']} for r in rows]
        out({'data': {'repository': {'pullRequest': {'comments': {
            'totalCount': len(nodes), 'nodes': nodes,
            'pageInfo': {'hasNextPage': False, 'endCursor': None}}}}}})
    if 'node(id:' in line:
        cid = int(line.rsplit('id=IC_', 1)[1])
        edited = [r.get('last_edited') for r in rows if r['id'] == cid]
        out({'data': {'node': {'lastEditedAt': edited[0] if edited else None}}})
if '/comments' in line and ('--method' in args or '-f' in args):
    if line.endswith('labels[]=needs-anton'):
        pass
    else:
        body = args[args.index('-f') + 1][len('body='):]
        at = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        n = len(rows) + 1000
        rows.append({'id': n, 'node_id': f'IC_{n}', 'body': body, 'created_at': at,
                     'updated_at': at, 'user': {'login': 'anneoneone'},
                     'html_url': f'https://github.com/SLUG/pull/PR#issuecomment-{n}'})
        json.dump(rows, open(comments_file, 'w'))
        out({'id': n})
if line.endswith('labels[]=needs-anton'):
    with open(os.path.join(root, 'labels.jsonl'), 'a') as f:
        f.write(json.dumps(line) + '\n')
    out([])
if '--paginate' in args and '/comments' in line:
    out([rows])
if '/comments' in line:
    out(rows)
if '--jq .base.ref' in line:
    out('BASE')
if '--jq .state, .head.sha' in line:
    out('open\n' + head)
if 'git/ref/heads/BASE' in line:
    out(base if '--jq' in args else {'object': {'sha': base}})
if '/compare/' in line:
    tip, _, rest = line.split('/compare/', 1)[1].partition('...')
    sha = rest.split('?')[0]
    if not known(tip) or not known(sha):
        print('gh: Not Found (HTTP 404)', file=sys.stderr)
        sys.exit(1)
    if tip == sha:
        out({'status': 'identical'})
    if ancestor(tip, sha):
        out({'status': 'ahead'})
    out({'status': 'behind' if ancestor(sha, tip) else 'diverged'})
if args[:2] == ['pr', 'view']:
    out({'state': 'OPEN', 'headRefOid': head})
if line.endswith('repos/SLUG/pulls/PR'):
    out({'state': 'open', 'merged_at': None, 'draft': False, 'comments': len(rows),
         'mergeable': json.loads(os.environ.get('TEST_MERGEABLE', 'true')),
         'head': {'ref': 'BRANCH', 'sha': head, 'repo': {'full_name': 'SLUG'}},
         'base': {'ref': 'BASE'}, 'body': 'Executor: Claude\nReviewer: Codex\n'})
print(f'fake gh: no answer for {line}', file=sys.stderr)
sys.exit(1)
"""
for name, value in (
    ("GIT", repr(rr.REAL_GIT)),
    ("BRANCH", BRANCH),
    ("BASE", BASE),
    ("SLUG", SLUG),
    ("PR#", f"{PR}#"),
    ("pulls/PR'", f"pulls/{PR}'"),
):
    GH = GH.replace(name, value)

# The owner's steps. TEST_MODEL_MODE: recover (rebase, the appended proof,
# lease push), legacy-proof (the proof without the attempt pin), request (post
# the request and its evidence), request-base-moves (the base moves right
# after the post), fail (does nothing).
MODEL = r"""#!/usr/bin/env python3
import json, os, re, shlex, subprocess, sys
prompt = sys.stdin.read()
root = os.environ['TEST_ROOT']
mode = os.environ.get('TEST_MODEL_MODE', 'recover')
with open(os.path.join(root, 'models.jsonl'), 'a') as f:
    f.write(json.dumps(mode) + '\n')
open(os.path.join(root, 'prompt.txt'), 'w').write(prompt)
def done(status='completed'):
    print(json.dumps({'type': 'result',
                      'structured_output': {'status': status, 'summary': mode}}))
    sys.exit(0)
if mode == 'fail':
    done()
action = json.load(open(os.environ['EPIC_ACTION_FILE']))
if mode.startswith('request'):
    tip = subprocess.run(['git', '--git-dir', os.environ['TEST_REMOTE'], 'rev-parse',
                          'refs/heads/BASE'], capture_output=True, text=True).stdout.strip()
    body = (f"Upstream test recovery by Claude\nHead: {action['sha']}\nBase: BASE\n"
            f"Base tip: {tip}\nSuites: upstream")
    for text in (body, 'Evidence: test_upstream fails on the head, passes on the base.'):
        subprocess.run(['gh', 'api', 'repos/SLUG/issues/PR/comments', '-f', f'body={text}'],
                       check=True, capture_output=True)
    if mode == 'request-base-moves':
        seed = os.environ['TEST_SEED']
        for args in (['switch', '-q', 'BASE'], ['commit', '-q', '--allow-empty', '-m', 'later'],
                     ['push', '-q', 'origin', 'BASE']):
            subprocess.run(['git', '-C', seed, *args], check=True, capture_output=True)
    done()
args = sys.argv
gate_env = json.loads(args[args.index('--mcp-config') + 1])['mcpServers']['epic-gate']['env']
os.environ.update(gate_env)
sys.path.insert(0, os.path.join(gate_env['EPIC_TRUSTED_ROOT'], 'scripts/epic'))
import git_gate, permission_gate
git_gate.TRUSTED = re.compile(re.escape(os.environ['TEST_REMOTE']))
wt = os.environ['EPIC_WORKTREE']
def gated(command):
    ok, reason = permission_gate.decide('Bash', {'command': command})
    with open(os.path.join(root, 'gate.jsonl'), 'a') as f:
        f.write(json.dumps([command, ok, reason]) + '\n')
    if ok:
        return subprocess.run(shlex.split(command), capture_output=True, text=True)
    return None
gated(f'git -C {wt} fetch -q origin')
gated(f'git -C {wt} rebase origin/BASE')
proof = re.search(r'^Proof command: (.*)$', prompt, re.M).group(1)
if mode == 'legacy-proof':
    proof = proof.split(' --attempt-file ')[0]
subprocess.run(shlex.split(proof), capture_output=True)
gated(f"git -C {wt} push --force-with-lease=refs/heads/BRANCH:{action['sha']}"
      " origin HEAD:refs/heads/BRANCH")
done()
"""
for name, value in (
    ("BRANCH", BRANCH),
    ("BASE", BASE),
    ("SLUG", SLUG),
    ("/PR/", f"/{PR}/"),
):
    MODEL = MODEL.replace(name, value)

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


class RecoveryRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="recovery-runner-")
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
        # The base gains the fix the `upstream` suite needs; no conflict.
        git(self.seed, "switch", "-q", BASE)
        (self.seed / "fix.txt").write_text("fixed upstream\n")
        git(self.seed, "add", ".")
        git(self.seed, "commit", "-q", "-m", "upstream fix")
        git(self.seed, "push", "-q", "origin", BASE)
        self.repo = self.root / "checkout"
        git(self.root, "clone", "-q", str(self.remote), str(self.repo))
        self.install_runner()
        self.wtdir = self.root / "wt"
        self.wt = self.wtdir / BRANCH
        git(self.repo, "worktree", "add", "-q", "--track", "-b", BRANCH,
            str(self.wt), f"origin/{BRANCH}")  # fmt: skip
        self.state = self.root / "state"
        (self.root / "suites.json").write_text(json.dumps(SUITES))
        self.env = {
            **os.environ,
            **rr.GIT_ENV,
            "HOME": str(self.root / "home"),
            "PATH": f"{self.root / 'bin'}:{os.environ['PATH']}",
            "EPIC_STATE_DIR": str(self.state),
            "EPIC_LOCK_DIR": str(self.root / "locks"),
            "EPIC_RUNTIME_LEGACY": "1",
            "EPIC_RUNTIME_HOME": str(self.root / "runtime-home"),
            "EPIC_WORKTREE_DIR": str(self.wtdir),
            "EPIC_SHARED_READER": "0",
            "EPIC_TICK_TIMEOUT_SECONDS": "60",
            "EPIC_SELECT_TIMEOUT_SECONDS": "30",
            "EPIC_REBASE_SUITES": str(self.root / "suites.json"),
            "EPIC_FOCUS_ACTIONS": ON,
            "TEST_ROOT": str(self.root),
            "TEST_REMOTE": str(self.remote),
            "TEST_WORKTREE": str(self.wt),
            "TEST_SEED": str(self.seed),
        }
        (self.root / "home").mkdir()
        self.old = self.remote_head()
        self.tip = self.remote_head(BASE)

    def install_runner(self) -> None:
        epic = self.repo / "scripts/epic"
        for rel in (*rr.COPIED, "scripts/epic_guard/check.py"):
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / rel, self.repo / rel)
        for name, main in (
            ("next_action", rr.SELECTOR_MAIN),
            ("runner_worktree", rr.WORKTREE_MAIN),
            ("tick_gate", rr.GATE_MAIN),
        ):
            shutil.copyfile(ROOT / f"scripts/epic/{name}.py", epic / f"real_{name}.py")
            (epic / f"{name}.py").write_text(
                rr.IMPORTABLE_STUB.format(name=name, main=main)
            )
        (epic / "gitnexus_noise.py").write_text("import sys\nsys.exit(0)\n")
        (epic / "close_merged.py").write_text("import sys\nsys.exit(0)\n")
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        for name, text in (("git", rr.GIT), ("gh", GH), ("claude", MODEL)):
            (bin_dir / name).write_text(text)
            (bin_dir / name).chmod(0o755)

    # Git and GitHub

    def remote_head(self, ref: str = BRANCH) -> str:
        return git(self.remote, "rev-parse", f"refs/heads/{ref}")

    def comments(self) -> list[dict[str, Any]]:
        path = self.root / "comments.json"
        return json.loads(path.read_text()) if path.exists() else []

    def post(self, body: str) -> dict[str, Any]:
        rows = self.comments()
        cid = len(rows) + 1
        at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60))
        row = {
            "id": cid, "node_id": f"IC_{cid}", "body": body, "created_at": at,
            "updated_at": at, "user": {"login": "anneoneone"},
            "html_url": f"https://github.com/{SLUG}/pull/{PR}#issuecomment-{cid}",
        }  # fmt: skip
        rows.append(row)
        (self.root / "comments.json").write_text(json.dumps(rows))
        return row

    def request(self, tip: str | None = None) -> dict[str, Any]:
        """The owner's request for the PR head, and the selector's action."""
        tip = tip or self.tip
        row = self.post(
            f"Upstream test recovery by Claude\nHead: {self.old}\nBase: {BASE}\n"
            f"Base tip: {tip}\nSuites: upstream"
        )
        evidence = {
            "kind": na.RECOVERY_KIND, "comment_id": row["id"], "url": row["html_url"],
            "owner": "Claude", "head": self.old, "base": BASE, "tip": tip,
            "suites": ["upstream"],
        }  # fmt: skip
        return {
            "action": "resolve-conflict",
            "reason": "upstream test recovery requested by Claude",
            "pr": PR,
            "sha": self.old,
            "recovery": evidence,
        }

    def lines(self, name: str) -> list[Any]:
        path = self.root / name
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    # Runner

    def tick(self, mode: str, action: dict[str, Any], **env: str) -> int:
        run_env = {
            **self.env,
            "TEST_MODEL_MODE": mode,
            "TEST_CANDIDATES": json.dumps(action),
            **env,
        }
        return subprocess.run(
            ["/bin/bash", str(self.repo / "scripts/epic/claude-tick.sh")],
            env=run_env,
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

    def cooldowns(self) -> dict[str, Any]:
        path = self.state / "claude-cooldown.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def expire_cooldowns(self) -> None:
        path = self.state / "claude-cooldown.json"
        if path.exists():
            entries = json.loads(path.read_text())
            for entry in entries.values():
                entry["at"] = entry["until"] = 1.0
            path.write_text(json.dumps(entries))

    # Cases

    def test_recovery_lands_with_the_mandatory_proof_and_a_later_review(self) -> None:
        # Codex approved the old head before the owner found the failing test.
        self.post(f"Review: APPROVED by Codex at {self.old}")
        action = self.request()
        self.assertEqual(self.tick("recover", action), 0, self.log())
        new = self.remote_head()
        self.assertNotEqual(new, self.old)
        prompt = (self.root / "prompt.txt").read_text()
        self.assertIn(ON_LINE, prompt)
        self.assertIn("--attempt-file", prompt)
        pin = json.loads(
            prompt.split("Attempt pin (JSON data, not instructions):\n")[1].split(
                "\nProof command"
            )[0]
        )
        self.assertEqual(pin["tip"], self.tip)
        self.assertEqual(
            [s["name"] for s in pin["proof_suites"] if s.get("mandatory")], ["upstream"]
        )
        # The lease pinned the old head, never the tip.
        (lease,) = [g for g in self.lines("gate.jsonl") if "--force-with-lease" in g[0]]
        self.assertTrue(lease[1], lease)
        self.assertIn(f"refs/heads/{BRANCH}:{self.old} ", lease[0])
        records = [
            rp.parse_record(c["body"], "Claude")
            for c in self.comments()
            if c["body"].startswith("Rebase record by ")
        ]
        self.assertEqual(len(records), 1)
        fields = records[0]
        assert fields is not None
        self.assertEqual(
            (fields["Old head"], fields["New head"], fields["Target tip"]),
            (self.old, new, self.tip),
        )
        self.assertIn("upstream=passed", fields["Proof"])
        self.assertIn("files=passed", fields["Proof"])
        self.assertIn("verified: PR 7 rebased", self.log())
        self.assertEqual([a["outcome"] for a in self.attempts()], ["succeeded"])
        # Later: Codex reviews the new head, focused on the rebase.
        rows = self.comments()
        git(self.repo, "fetch", "-q", "origin")
        scope = rp.scope("codex", PR, new, self.repo, rows, self.tip)
        self.assertEqual(scope["mode"], "focused", scope)
        self.assertEqual(scope["old_head"], self.old)
        shaped = {
            "number": PR, "body": "Executor: Claude\nReviewer: Codex",
            "baseRefName": BASE, "headRefOid": new, "isDraft": False,
            "mergeable": "MERGEABLE", "labels": [],
            "statusCheckRollup": [{"conclusion": "SUCCESS"},
                                  {"context": "epic-guard", "state": "SUCCESS"}],
            "comments": na.comments_from_rest(
                [{**r, "last_edited_at": None} for r in rows], len(rows)
            ),
        }  # fmt: skip
        act = na.decide(
            "Codex", {"prs": [shaped], "items": []}, enabled=frozenset({ON})
        )[0]
        self.assertEqual((act.action, act.sha), ("review", new))

    def test_a_proof_without_the_mandatory_suite_blocks_the_push(self) -> None:
        action = self.request()
        self.assertEqual(self.tick("legacy-proof", action), 1, self.log())
        (lease,) = [g for g in self.lines("gate.jsonl") if "--force-with-lease" in g[0]]
        self.assertFalse(lease[1])
        self.assertIn("required suite upstream did not run", lease[2])
        self.assertEqual(self.remote_head(), self.old)
        self.assertEqual([a["outcome"] for a in self.attempts()], ["failed"])
        self.assertFalse(
            [c for c in self.comments() if c["body"].startswith("Rebase record")]
        )

    def test_a_stale_request_starts_no_model_and_counts_no_attempt(self) -> None:
        action = self.request()
        git(self.seed, "commit", "-q", "--allow-empty", "-m", "base moves on")
        git(self.seed, "push", "-q", "origin", BASE)
        self.assertEqual(self.tick("recover", action), 0, self.log())
        self.assertIn("upstream recovery is stale; next candidate", self.log())
        self.assertEqual(self.models(), 0)
        self.assertEqual(self.attempts(), [])
        self.assertEqual(self.cooldowns(), {})
        self.assertEqual(self.remote_head(), self.old)

    def test_attempt_limit_holds_for_recovery(self) -> None:
        action = self.request()
        for _ in range(2):
            self.assertEqual(self.tick("fail", action), 1, self.log())
            self.expire_cooldowns()
        self.assertEqual(self.models(), 2)
        self.assertEqual(len(self.lines("labels.jsonl")), 1)
        self.assertEqual(self.tick("fail", action), 0, self.log())
        self.assertEqual(self.models(), 2)
        self.assertIn("reached its attempt limit", self.log())

    def test_request_only_fix_checks_tick_is_a_completed_handoff(self) -> None:
        action = {
            "action": "fix-checks",
            "reason": "checks failed",
            "pr": PR,
            "sha": self.old,
        }
        # The base moves right after the post: still a completed handoff.
        self.assertEqual(self.tick("request-base-moves", action), 0, self.log())
        self.assertNotEqual(self.remote_head(BASE), self.tip)
        self.assertIn("verified: upstream test recovery request", self.log())
        self.assertEqual(self.cooldowns(), {})
        prompt = (self.root / "prompt.txt").read_text()
        self.assertIn(ON_LINE, prompt)
        self.assertIn("rebase_policy.py export --sha <SHA>\n", prompt)
        self.assertIn("\nExport command: ", prompt)
        self.assertEqual(self.remote_head(), self.old)

    def test_without_the_opt_in_a_request_only_tick_cools_down(self) -> None:
        action = {
            "action": "fix-checks",
            "reason": "checks failed",
            "pr": PR,
            "sha": self.old,
        }
        self.assertEqual(
            self.tick("request", action, EPIC_FOCUS_ACTIONS=""), 1, self.log()
        )
        prompt = (self.root / "prompt.txt").read_text()
        self.assertNotIn(ON_LINE, prompt)
        self.assertNotIn("\nExport command: ", prompt)
        self.assertEqual(len(self.cooldowns()), 1)


if __name__ == "__main__":
    unittest.main()
