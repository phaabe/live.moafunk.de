"""Answer permission prompts of the headless Claude runner.

`claude -p` cannot show a prompt. The project settings ask before every
`git push`, `git rebase` and `gh pr merge`; the runner settings
(claude-runner-settings.json) also ask before every `git -<option> ...`, such
as `git -C <path> push` or `git -c k=v push`. claude-tick.sh passes this MCP
server as `--permission-prompt-tool`. It approves only:

  git commands of the runner contract in git_gate.py: push, lease push and
      rebase of the action's own branch in its runner worktree, the merged
      branch's delete, and narrow `git -C` reads and local writes
  gh pr merge <n> --repo phaabe/live.moafunk.de --squash [--delete-branch]
      --match-head-commit <40-hex SHA>
  gh api --method PATCH repos/phaabe/live.moafunk.de/pulls/<n> -F body=@<file>
      only while the selected action (EPIC_ACTION_FILE) is `adopt` of PR <n>,
      with <file> a regular file directly in the tick's body directory
      (EPIC_BODY_DIR, checked by its device and inode EPIC_BODY_DIR_ID)

A feature branch starts with feat/, fix/, chore/, docs/, test/ or refactor/.
Everything else is denied with a reason, including plain `git push` and
`git rebase`: the gate cannot see the shell's directory, so the runner uses
`git -C <worktree>`. The project hooks (epic-guard and
others) still run for every approved command.

With the shared reader (EPIC_SHARED_READER=1), an approved command is also
checked fresh against GitHub right before it runs (write_checks.py): a push
needs its PR open or its issue In progress for Claude, a merge the full merge
guard. A failed read denies it.

Speaks MCP over stdio: one JSON-RPC message per line, standard library only.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import stat
import sys
import time
from pathlib import Path
from typing import Any

REPO = "phaabe/live.moafunk.de"
TOOL = "approve"
BRANCH = re.compile(r"^(feat|fix|chore|docs|test|refactor)/[A-Za-z0-9._/-]+$")
SHA = re.compile(r"^[0-9a-f]{40}$")
# Anything that could chain, redirect or substitute a second command.
SHELL_META = re.compile(r"[;&|<>`$\n\\]|\(|\)")


def decide(tool_name: str, tool_input: dict[str, Any]) -> tuple[bool, str]:
    """(allowed, reason) for one permission prompt."""
    allowed, reason = rule(tool_name, tool_input)
    if allowed and os.environ.get("EPIC_SHARED_READER") == "1":
        # Imported here: standard library only while the switch is off.
        import write_checks

        refused = write_checks.guard(tool_name, tool_input, os.getcwd(), agent="Claude")
        if refused:
            return False, f"fresh check: {refused}"
    return allowed, reason


def rule(tool_name: str, tool_input: dict[str, Any]) -> tuple[bool, str]:
    """(allowed, reason) from the command shape alone."""
    if tool_name != "Bash":
        return False, f"the runner does not approve {tool_name} prompts"
    command = str(tool_input.get("command", "")).strip()
    if SHELL_META.search(command):
        return False, "only one plain command is approved, without shell operators"
    try:
        words = shlex.split(command)
    except ValueError:
        return False, "the command cannot be parsed"
    if words[:1] == ["git"]:
        # Imported here: it reads Git and the runner context only for git.
        import git_gate

        return git_gate.decide(words)
    if words[:3] == ["gh", "pr", "merge"]:
        return merge(words[3:])
    if words[:2] == ["gh", "api"]:
        return body_edit(words[2:], selected_action())
    return (
        False,
        (
            "only the runner's git contract, head-pinned squash merges and the "
            "adopt PR-body edit are approved"
        ),
    )


def merge(args: list[str]) -> tuple[bool, str]:
    if not args or not args[0].isdigit():
        return False, "merge needs a PR number first"
    flags = args[1:]
    seen: dict[str, str] = {}
    i = 0
    while i < len(flags):
        flag = flags[i]
        if flag in ("--squash", "--delete-branch"):
            seen[flag] = ""
            i += 1
        elif flag in ("--repo", "--match-head-commit") and i + 1 < len(flags):
            seen[flag] = flags[i + 1]
            i += 2
        else:
            return False, f"merge flag {flag} is not approved"
    if seen.get("--repo") != REPO:
        return False, f"merge needs --repo {REPO}"
    if "--squash" not in seen:
        return False, "merge needs --squash"
    if not SHA.match(seen.get("--match-head-commit", "")):
        return False, "merge needs --match-head-commit <40-char SHA>"
    return True, f"squash merge of PR {args[0]} at {seen['--match-head-commit'][:7]}"


def selected_action() -> dict[str, Any]:
    """The action of this tick, from EPIC_ACTION_FILE. Empty when unreadable."""
    path = os.environ.get("EPIC_ACTION_FILE")
    try:
        action = json.loads(Path(path).read_text()) if path else {}
    except (OSError, ValueError):
        return {}
    return action if isinstance(action, dict) else {}


def in_body_dir(body_file: str) -> bool:
    """The file sits directly in the runner's per-tick body directory.

    The directory is identified by the device and inode the runner recorded
    (EPIC_BODY_DIR_ID), not by its path, so replacing it with a symlink or a
    new directory does not match. The file itself must be a regular file with
    one link: no symlink, no hard link to a file elsewhere.
    Otherwise any file the session can read could become a PR body.
    """
    anchor = os.environ.get("EPIC_BODY_DIR_ID", "")
    path = Path(body_file)
    if not anchor or not path.is_absolute() or ".." in path.parts:
        return False
    try:
        parent = os.stat(path.parent)
        body = os.lstat(path)
    except OSError:
        return False
    return (
        f"{parent.st_dev}:{parent.st_ino}" == anchor
        and stat.S_ISREG(body.st_mode)
        and body.st_nlink == 1
    )


def body_edit(args: list[str], action: dict[str, Any]) -> tuple[bool, str]:
    """`adopt` writes the PR body through REST: one exact command shape."""
    shape = "`gh api --method PATCH repos/<repo>/pulls/<n> -F body=@<file>`"
    if (
        len(args) != 5
        or args[:2] != ["--method", "PATCH"]
        or args[3] != "-F"
        or not args[4].startswith("body=@")
    ):
        return False, f"gh api is approved only as {shape}"
    prefix = f"repos/{REPO}/pulls/"
    number = args[2][len(prefix) :] if args[2].startswith(prefix) else ""
    if not number.isdigit():
        return False, f"body edit must target a PR of {REPO}"
    if not in_body_dir(args[4][len("body=@") :]):
        return False, "body file must be a regular file directly in EPIC_BODY_DIR"
    if action.get("action") != "adopt" or action.get("pr") != int(number):
        return False, f"PR {number} body may change only in an adopt tick for it"
    return True, f"adopt body edit of PR {number}"


def log_decision(tool_input: dict[str, Any], allowed: bool, reason: str) -> None:
    state = Path(
        os.environ.get("EPIC_STATE_DIR") or Path.home() / ".local/state/epic-loop"
    )
    try:
        with (state / "claude-permissions.log").open("a") as out:
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            verdict = "allow" if allowed else "deny"
            command = str(tool_input.get("command", ""))[:300]
            out.write(f"{stamp} {verdict} {command!r} ({reason})\n")
    except OSError:
        pass


def answer(arguments: dict[str, Any]) -> str:
    tool_input = arguments.get("input") or {}
    allowed, reason = decide(str(arguments.get("tool_name", "")), tool_input)
    log_decision(tool_input, allowed, reason)
    if allowed:
        return json.dumps({"behavior": "allow", "updatedInput": tool_input})
    return json.dumps(
        {"behavior": "deny", "message": f"Runner permission gate: {reason}."}
    )


def handle(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    if "id" not in message:
        return None  # notification, e.g. notifications/initialized
    if method == "initialize":
        result: dict[str, Any] = {
            "protocolVersion": message.get("params", {}).get(
                "protocolVersion", "2025-06-18"
            ),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "epic-permission-gate", "version": "1"},
        }
    elif method == "tools/list":
        result = {
            "tools": [
                {
                    "name": TOOL,
                    "description": "Approve or deny a permission prompt of the epic runner.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "tool_name": {"type": "string"},
                            "input": {"type": "object"},
                            "tool_use_id": {"type": "string"},
                        },
                        "required": ["tool_name", "input"],
                    },
                }
            ]
        }
    elif method == "tools/call" and message.get("params", {}).get("name") == TOOL:
        text = answer(message["params"].get("arguments") or {})
        result = {"content": [{"type": "text", "text": text}]}
    elif method == "ping":
        result = {}
    else:
        return {
            "jsonrpc": "2.0",
            "id": message["id"],
            "error": {"code": -32601, "message": f"unknown method {method}"},
        }
    return {"jsonrpc": "2.0", "id": message["id"], "result": result}


def main() -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        reply = handle(json.loads(line))
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
