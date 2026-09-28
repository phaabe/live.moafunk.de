"""Answer permission prompts of the headless Claude runner.

`claude -p` cannot show a prompt. The project settings ask before every
`git push` and `gh pr merge`, so claude-tick.sh passes this MCP server as
`--permission-prompt-tool`. It approves only:

  git push [-u] origin <feature-branch>
  git push origin --delete <feature-branch>
  gh pr merge <n> --repo phaabe/live.moafunk.de --squash [--delete-branch]
      --match-head-commit <40-hex SHA>

A feature branch starts with feat/, fix/, chore/, docs/, test/ or refactor/.
Everything else is denied with a reason. The project hooks (epic-guard and
others) still run for every approved command.

Speaks MCP over stdio: one JSON-RPC message per line, standard library only.
"""

from __future__ import annotations

import json
import os
import re
import shlex
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
    if tool_name != "Bash":
        return False, f"the runner does not approve {tool_name} prompts"
    command = str(tool_input.get("command", "")).strip()
    if SHELL_META.search(command):
        return False, "only one plain command is approved, without shell operators"
    try:
        words = shlex.split(command)
    except ValueError:
        return False, "the command cannot be parsed"
    if words[:2] == ["git", "push"]:
        return push(words[2:])
    if words[:3] == ["gh", "pr", "merge"]:
        return merge(words[3:])
    return (
        False,
        "only feature-branch pushes and head-pinned squash merges are approved",
    )


def push(args: list[str]) -> tuple[bool, str]:
    if args[:1] == ["-u"]:
        args = args[1:]
    if args[:2] == ["origin", "--delete"]:
        args = ["origin", *args[2:]]
    if len(args) != 2 or args[0] != "origin":
        return (
            False,
            "push must be `git push [-u] origin <branch>` or `--delete <branch>`",
        )
    if not BRANCH.match(args[1]) or ".." in args[1]:
        return False, f"{args[1]} is not a feature branch"
    return True, f"feature branch {args[1]}"


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
