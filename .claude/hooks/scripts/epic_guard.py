"""Claude's local guard for the architecture epic rules (docs/implementation/epic-rules.md).

Reads a PreToolUse payload on stdin. Exit 2 blocks the tool call (message on
stderr), exit 0 allows it.

Blocks:
  1. A review verdict written in Codex's name, in any tool input.
  2. `gh pr create` without an explicit base, or with base `main` unless the
     head is `dev/streaming-architecture` (release) or the approved setup
     branch. MCP pull-request creation is checked the same way.
  3. `gh pr merge` without its own `--match-head-commit <40-char SHA>`.
     MCP merges are blocked; use `gh pr merge` with the expected head.
  4. Command forms the guard cannot parse, when they mention `gh pr create`
     or `gh pr merge`.

Override for an approved `main` hotfix PR (rule 2 only): the operator sets
CLAUDE_ALLOW_MAIN_PR=1 in the hook environment, for example in
`.claude/settings.local.json` under "env". An inline assignment on the
guarded command does not reach this hook, by design.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys

RELEASE_HEAD = "dev/streaming-architecture"
SETUP_HEADS = {"ci/312-epic-guard"}  # approved epic-guard setup PR to main
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
FORGED_VERDICT = re.compile(
    r"Review: (APPROVED|CHANGES REQUESTED) by Codex at [0-9a-f]{40}"
)
SEPARATORS = {"&&", "||", ";", "|", "&", "\n", "(", ")"}


class Blocked(Exception):
    pass


def block(*lines: str) -> None:
    raise Blocked("\n".join(lines))


def strip_heredocs(cmd: str) -> str:
    """Drop heredoc bodies; they are data, not commands."""
    out, lines, i = [], cmd.split("\n"), 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        m = re.search(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1", line)
        i += 1
        if m:
            tag = m.group(2)
            while i < len(lines) and lines[i].strip() != tag:
                i += 1
            i += 1  # skip the terminator
    return "\n".join(out)


def simple_commands(cmd: str) -> list[list[str]]:
    lexer = shlex.shlex(strip_heredocs(cmd), posix=True, punctuation_chars=";&|()")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    commands, current = [], []
    for token in lexer:
        if token in SEPARATORS or set(token) <= set(";&|()\n"):
            if current:
                commands.append(current)
            current = []
        else:
            current.append(token)
    if current:
        commands.append(current)
    return commands


def gh_pr_args(words: list[str], sub: str) -> list[str] | None:
    """Return the arguments after `gh pr <sub>`, skipping leading env assignments."""
    i = 0
    while i < len(words) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[i]):
        i += 1
    if words[i : i + 3] == ["gh", "pr", sub]:
        return words[i + 3 :]
    return None


def option(args: list[str], *names: str) -> str | None:
    for i, arg in enumerate(args):
        for name in names:
            if arg == name and i + 1 < len(args):
                return args[i + 1]
            if name.startswith("--") and arg.startswith(name + "="):
                return arg.split("=", 1)[1]
    return None


def current_branch(cwd: str) -> str:
    try:
        return subprocess.run(
            ["git", "-C", cwd, "symbolic-ref", "--quiet", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def check_base(base: str | None, head: str | None) -> None:
    if not base:
        block(
            "A pull request needs an explicit base.",
            f"Epic feature PRs: --base {RELEASE_HEAD}. Release PRs: --head {RELEASE_HEAD} --base main.",
        )
    if base == "main" and os.environ.get("CLAUDE_ALLOW_MAIN_PR") != "1":
        if head != RELEASE_HEAD and head not in SETUP_HEADS:
            block(
                f"Only release PRs from {RELEASE_HEAD} (or the approved setup PR) may target main "
                f"(head was '{head or 'unknown'}').",
                f"Target {RELEASE_HEAD} instead.",
                "An approved main hotfix needs the operator to set CLAUDE_ALLOW_MAIN_PR=1 "
                "in the hook environment (.claude/settings.local.json env).",
            )


def check_command(cmd: str, cwd: str) -> None:
    mentions = re.search(r"\bgh\s+pr\s+(create|merge)\b", cmd)
    if mentions and re.search(
        r"\$\(|`|\beval\b|\bbash\s+-c\b|\bsh\s+-c\b|\bxargs\b", strip_heredocs(cmd)
    ):
        block(
            "The guard cannot check `gh pr create/merge` inside $(...), backticks, eval, sh -c or xargs.",
            "Run the gh command directly.",
        )
    try:
        commands = simple_commands(cmd)
    except ValueError:
        if mentions:
            block(
                "The guard cannot parse this command. Run `gh pr create/merge` as a plain command."
            )
        return
    for words in commands:
        args = gh_pr_args(words, "create")
        if args is not None:
            head = option(args, "--head", "-H") or current_branch(cwd)
            check_base(option(args, "--base", "-B"), head)
        args = gh_pr_args(words, "merge")
        if args is not None:
            sha = option(args, "--match-head-commit")
            if not sha or not SHA_RE.match(sha):
                block(
                    "gh pr merge needs its own --match-head-commit <40-char head SHA>.",
                    "Merge only the head the other agent approved; a mismatch aborts the merge.",
                )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    tool = payload.get("tool_name") or payload.get("toolName") or ""
    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    cwd = payload.get("cwd") or payload.get("workingDirectory") or os.getcwd()
    try:
        if FORGED_VERDICT.search(json.dumps(tool_input, ensure_ascii=False)):
            block(
                "Claude must never write a review verdict in Codex's name.",
                "Only Codex posts 'Review: ... by Codex at <sha>'. Claude posts 'by Claude' on Codex's PRs.",
            )
        if tool == "mcp__github__create_pull_request":
            check_base(tool_input.get("base"), tool_input.get("head"))
        if tool == "mcp__github__merge_pull_request":
            block(
                "Merge with `gh pr merge <n> --squash --match-head-commit <sha>`, not the MCP tool."
            )
        command = tool_input.get("command")
        if isinstance(command, str) and command:
            check_command(command, cwd)
    except Blocked as exc:
        print("BLOCKED by .claude/hooks/scripts/epic-guard.sh", file=sys.stderr)
        print(exc, file=sys.stderr)
        print("Rules: docs/implementation/epic-rules.md", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
