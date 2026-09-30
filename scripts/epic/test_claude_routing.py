"""Real `claude -p` routes git commands to the runner permission gate.

Opt-in: it starts one model session (network, tokens). Run:

  EPIC_CLAUDE_ROUTING_TEST=1 python3 -m unittest test_claude_routing

Only the remote and the test data are isolated: a local bare repository named
like the real one, a runner checkout with the project's .claude settings and
hooks, and the fixed worktree dir. The CLI, the runner settings
(claude-runner-settings.json), the production gate and the flags are the ones
claude-tick.sh uses. Evidence (CLI version, settings, tool calls, gate log,
refs) goes to EPIC_ROUTING_EVIDENCE, default routing-evidence.json in the
temp dir, which is printed. The gate starts through a small wrapper that
only sets git_gate.TRUSTED to the local bare repository.
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

# The real CLI needs the real login (HOME, config dirs); runner state stays out.
REAL = {k: v for k, v in isolated_env.PARENT.items() if not k.startswith("EPIC_")}

ROOT = Path(__file__).resolve().parents[2]
BRANCH = "feat/424-routing"
CANARY = "feat/424-canary"
BASE = "dev/312-interim"
IDENTITY = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **IDENTITY},
    ).stdout.strip()


def commit(cwd: Path, name: str) -> None:
    (cwd / name).write_text(f"{name}\n")
    git(cwd, "add", name)
    git(cwd, "commit", "-q", "-m", name)


@unittest.skipUnless(
    isolated_env.PARENT.get("EPIC_CLAUDE_ROUTING_TEST") == "1",
    "starts a real claude session",
)
class ClaudeRoutingTest(unittest.TestCase):
    def test_four_forms_reach_the_gate(self) -> None:
        tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="claude-routing-")))
        remote = tmp / "remote/phaabe/live.moafunk.de.git"
        remote.parent.mkdir(parents=True)
        git(tmp, "init", "-q", "--bare", "-b", BASE, str(remote))
        runner = tmp / "runner"
        git(tmp, "clone", "-q", str(remote), str(runner))
        git(runner, "switch", "-q", "-c", BASE)
        commit(runner, "base.txt")
        git(runner, "push", "-q", "origin", BASE)
        # The runner checkout is one commit off the base: a plain rebase that
        # ran would rewrite it.
        git(runner, "switch", "-q", "-c", "runner-local")
        commit(runner, "local.txt")
        git(runner, "switch", "-q", BASE)
        commit(runner, "base2.txt")
        git(runner, "push", "-q", "origin", BASE)
        git(runner, "switch", "-q", "runner-local")
        git(runner, "branch", CANARY)
        runner_head = git(runner, "rev-parse", "HEAD")
        # The project's shared settings and hooks, unchanged.
        shutil.copytree(
            ROOT / ".claude",
            runner / ".claude",
            ignore=shutil.ignore_patterns("worktrees", "settings.local.json"),
        )
        shutil.copytree(ROOT / "scripts", runner / "scripts")
        wtdir = tmp / "runner-wt"
        wt = wtdir / BRANCH
        git(runner, "worktree", "add", "-q", "-b", BRANCH, str(wt), f"origin/{BASE}")
        commit(wt, "feature.txt")
        wt_head = git(wt, "rev-parse", "HEAD")

        state = tmp / "state"
        state.mkdir()
        issue = "https://github.com/phaabe/live.moafunk.de/issues/424"
        action = {"action": "continue", "reason": "routing test", "issue": issue}
        (tmp / "action.json").write_text(json.dumps(action))
        (tmp / "context.json").write_text(
            json.dumps(
                {
                    "action": action,
                    "branch": BRANCH,
                    "base": None,
                    "pr": None,
                    "issue": issue,
                    "worktree": str(wt),
                }
            )
        )
        # The production gate; only the trusted destination is the local bare
        # repository instead of GitHub (the test boundary).
        gate_script = tmp / "gate.py"
        gate_script.write_text(
            "import re, sys\n"
            f"sys.path.insert(0, {str(ROOT / 'scripts/epic')!r})\n"
            "import git_gate\n"
            f"git_gate.TRUSTED = re.compile(re.escape({str(remote)!r}))\n"
            "import permission_gate\n"
            "permission_gate.main()\n"
        )
        gate_env = {
            "EPIC_STATE_DIR": str(state),
            "EPIC_ACTION_FILE": str(tmp / "action.json"),
            "EPIC_TRUSTED_ROOT": str(runner),
            "EPIC_CONTEXT_FILE": str(tmp / "context.json"),
            "EPIC_WORKTREE_DIR": str(wtdir),
        }
        for name, value in REAL.items():
            if name.startswith("GH_") or name in (
                "HOME", "PATH", "USER", "LOGNAME", "TMPDIR", "XDG_CONFIG_HOME",
            ):  # fmt: skip
                gate_env.setdefault(name, value)
        gate_config = {
            "mcpServers": {
                "epic-gate": {
                    "command": "python3",
                    "args": [str(gate_script)],
                    "env": gate_env,
                }
            }
        }
        forms = {
            "C-separate": (f"git -C {wt} push origin {BRANCH}", "allow"),
            "C-attached": (f"git -C{wt} push -u origin {BRANCH}", "allow"),
            "c-config": (f"git -c user.name=x push origin {CANARY}", "deny"),
            "plain-rebase": (f"git rebase origin/{BASE}", "deny"),
        }
        prompt = (
            "Test of a permission hook. Run each command below with the Bash "
            "tool, exactly as written, one tool call each, in this order. Do not "
            "change, fix, retry or chain them, and do not cd. If one is denied or "
            "fails, go on with the next. Then answer `done`.\n\n"
            + "\n".join(f"{i}. {c}" for i, (c, _) in enumerate(forms.values(), 1))
        )
        settings = ROOT / "scripts/epic/claude-runner-settings.json"
        version = subprocess.run(
            ["claude", "--version"], capture_output=True, text=True, check=True
        ).stdout.strip()
        session = subprocess.run(
            [
                "claude",
                "-p",
                "--model",
                "sonnet",
                "--permission-mode",
                "auto",
                "--settings",
                str(settings),
                "--mcp-config",
                json.dumps(gate_config),
                "--permission-prompt-tool",
                "mcp__epic-gate__approve",
                "--add-dir",
                str(wt),
                "--output-format",
                "stream-json",
                "--verbose",
            ],  # fmt: skip
            input=prompt,
            cwd=runner,
            capture_output=True,
            text=True,
            timeout=600,
            env={**REAL, "GIT_EDITOR": "true"},
        )
        calls, results = [], {}
        for line in session.stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            for part in (event.get("message") or {}).get("content") or []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "tool_use" and part.get("name") == "Bash":
                    calls.append(
                        {"id": part["id"], "command": part["input"]["command"]}
                    )
                elif part.get("type") == "tool_result":
                    content = part.get("content")
                    if isinstance(content, list):
                        content = " ".join(str(c.get("text", "")) for c in content)
                    results[part.get("tool_use_id")] = {
                        "error": bool(part.get("is_error")),
                        "text": str(content)[:500],
                    }
        log_file = state / "claude-permissions.log"
        log = log_file.read_text() if log_file.exists() else ""
        refs = git(remote, "for-each-ref", "--format=%(refname) %(objectname)")
        evidence = {
            "cli_version": version,
            "runner_settings": json.loads(settings.read_text()),
            "shared_settings_ask": json.loads(
                (ROOT / ".claude/settings.json").read_text()
            )["permissions"]["ask"],
            "session_exit": session.returncode,
            "tool_calls": [{**c, **results.get(c["id"], {})} for c in calls],
            "gate_log": log.splitlines(),
            "remote_refs": refs.splitlines(),
            "worktree_head": wt_head,
            "runner_head_before": runner_head,
            "runner_head_after": git(runner, "rev-parse", "HEAD"),
        }
        out = Path(
            isolated_env.PARENT.get("EPIC_ROUTING_EVIDENCE")
            or tmp / "routing-evidence.json"
        )
        out.write_text(json.dumps(evidence, indent=1))
        print(f"\nrouting evidence: {out}")
        self.assertEqual(session.returncode, 0, session.stderr[-2000:])

        # Every form reached the gate with the expected decision.
        for name, (command, verdict) in forms.items():
            with self.subTest(form=name):
                self.assertIn(f"{verdict} {command!r}", log)
        # Allowed: the separate -C push ran. Denied: nothing ran.
        self.assertIn(f"refs/heads/{BRANCH} {wt_head}", refs)
        self.assertNotIn(CANARY, refs)
        self.assertEqual(evidence["runner_head_after"], runner_head)
        self.assertFalse((runner / ".git/rebase-merge").exists())
        for call in evidence["tool_calls"]:
            denied = "Runner permission gate" in call.get("text", "")
            expected = next(
                (v for c, v in forms.values() if c == call["command"]), None
            )
            if expected:
                self.assertEqual(denied, expected == "deny", call)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
