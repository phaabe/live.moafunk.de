#!/usr/bin/env python3
"""PreToolUse hook for the Bash tool.

Blocks CI watchers that burn the GitHub GraphQL budget:
  - gh pr checks ... --watch   (GraphQL poll every 10 s)
  - gh run watch ...           (GraphQL lookup + poll every 3 s, one run only)

Use instead: python3 scripts/gh_checks/wait_checks.py <pr>  (REST, 60 s)

Override: CLAUDE_ALLOW_GH_WATCH=1

stdin -> JSON, stderr -> message back to Claude, exit 2 -> block, exit 0 -> allow.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys

SEPARATORS = {";", "&", "&&", "|", "||", "(", ")", "\n"}
# Words that run the next word as a command.
WRAPPERS = {
    "env",
    "command",
    "exec",
    "time",
    "nohup",
    "nice",
    "sudo",
    "xargs",
    "builtin",
}
SHELLS = {"bash", "sh", "zsh", "dash"}
# Shell words that come before a command: `if x; then gh ...`, `do gh ...`.
CONTROL_WORDS = {"if", "then", "else", "elif", "while", "until", "do", "!", "{"}
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")

MESSAGE = """\
BLOCKED by .claude/hooks/scripts/gh-watch-guard.py
`gh pr checks --watch` and `gh run watch` poll GitHub GraphQL and use up the hourly budget.

Use instead (REST, polls every 60 s, exits non-zero on failure):
  python3 scripts/gh_checks/wait_checks.py <pr-number>
Then merge with the printed SHA:
  gh pr merge <pr-number> --squash --delete-branch --match-head-commit <sha>

Override (use sparingly): CLAUDE_ALLOW_GH_WATCH=1"""


HEREDOC = re.compile(r"(?<!<)<<(-?)(?!<)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")


def drop_heredoc_bodies(command: str) -> tuple[str, list[str]]:
    """Split heredoc bodies off the command.

    Bodies are data, not commands. But with an unquoted delimiter the shell still
    runs `...` and $(...) in them, so those bodies are returned for checking.
    """
    out: list[str] = []
    expanded: list[str] = []
    # (delimiter, leading tabs allowed, body is expanded)
    waiting: list[tuple[str, bool, bool]] = []
    for line in command.split("\n"):
        if waiting:
            delim, strip_tabs, expands = waiting[0]
            if (line.lstrip("\t") if strip_tabs else line) == delim:
                waiting.pop(0)
            elif expands:
                expanded.append(line)
            continue
        out.append(line)
        waiting += [
            (m.group(3), m.group(1) == "-", not m.group(2))
            for m in HEREDOC.finditer(line)
        ]
    return "\n".join(out), expanded


def split_substitutions(command: str) -> tuple[str, list[str]]:
    """Pull `...` and $(...) bodies out of the command, except inside single quotes."""
    rest: list[str] = []
    bodies: list[str] = []
    quote = ""
    i = 0
    while i < len(command):
        c = command[i]
        if c == "\\" and quote != "'":
            rest.append(command[i : i + 2])
            i += 2
            continue
        if quote != "'" and c == "`":
            end = command.find("`", i + 1)
            end = len(command) if end == -1 else end
            bodies.append(command[i + 1 : end])
            rest.append(" ")
            i = end + 1
            continue
        if quote != "'" and command.startswith("$(", i):
            depth, j = 1, i + 2
            while j < len(command) and depth:
                depth += {"(": 1, ")": -1}.get(command[j], 0)
                j += 1
            bodies.append(command[i + 2 : j - 1 if depth == 0 else j])
            rest.append(" ")
            i = j
            continue
        if c in "'\"" and quote in ("", c):
            quote = "" if quote else c
        rest.append(c)
        i += 1
    return "".join(rest), bodies


def tokens(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    return list(lexer)


def simple_commands(command: str) -> list[list[str]]:
    out: list[list[str]] = [[]]
    for tok in tokens(command):
        if tok in SEPARATORS or set(tok) <= set(";&|()\n"):
            out.append([])
        else:
            out[-1].append(tok)
    return [c for c in out if c]


def shell_command_string(args: list[str]) -> str | None:
    """The string a shell runs with -c, also in combined flags like -lc or -ec."""
    has_c = False
    for arg in args:
        if arg.startswith("-") and not arg.startswith("--") and len(arg) > 1:
            has_c = has_c or "c" in arg[1:]
            continue
        if arg.startswith("--"):
            continue
        return arg if has_c else None
    return None


def is_watcher(argv: list[str], depth: int = 0) -> bool:
    i = 0
    while i < len(argv) and (ASSIGNMENT.match(argv[i]) or argv[i] in CONTROL_WORDS):
        i += 1
    argv = argv[i:]
    if not argv:
        return False
    # Wrapper options differ (`env -u NAME`, `sudo -u user`), so jump to the first
    # word that is gh or a shell.
    if os.path.basename(argv[0]) in WRAPPERS:
        starts = [
            j for j, a in enumerate(argv) if os.path.basename(a) in SHELLS | {"gh"}
        ]
        if not starts:
            return False
        argv = argv[starts[0] :]
    prog = os.path.basename(argv[0])
    if prog in SHELLS:
        body = shell_command_string(argv[1:])
        return body is not None and depth < 3 and blocked(body, depth + 1)
    if prog != "gh":
        return False
    args = argv[1:]
    if args[:2] == ["run", "watch"]:
        return True
    return args[:2] == ["pr", "checks"] and any(
        a == "--watch" or a.startswith("--watch=") for a in args
    )


def blocked(command: str, depth: int = 0) -> bool:
    command, expanded = drop_heredoc_bodies(command)
    rest, bodies = split_substitutions(command)
    for line in expanded:
        bodies += split_substitutions(line)[1]
    if depth < 3 and any(blocked(body, depth + 1) for body in bodies):
        return True
    try:
        commands = simple_commands(rest)
    except ValueError:  # unbalanced quotes: let the shell report it
        return False
    return any(is_watcher(argv, depth) for argv in commands)


def main() -> int:
    if os.environ.get("CLAUDE_ALLOW_GH_WATCH") == "1":
        return 0
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    tool_input = data.get("tool_input") or data.get("toolInput") or {}
    command = tool_input.get("command") or ""
    if command and blocked(command):
        print(MESSAGE, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
