"""Claude's local guard for the architecture epic rules (docs/implementation/epic-rules.md).

Reads a PreToolUse payload on stdin. Exit 2 blocks the tool call (message on
stderr), exit 0 allows it. This is a guard against mistakes, not a security
boundary; the required server-side check is separate.

Rules:
  1. Never a review verdict in Codex's name, in any tool input.
  2. A shell command that creates or merges a pull request must be exactly
     one plain `gh` command: optional `VAR=value` assignments, then `gh`,
     then the subcommand (no global flags before it), with no shell
     operators, wrappers, `$` or backticks outside single quotes, no flags
     between `pr` and its subcommand, and short options written with a
     separate value (`-B main`, not `-Bmain`). It may end with one heredoc
     whose delimiter is quoted (data, e.g. `--body-file -`).
     Backslash-newline continuations are joined first.
  3. `gh pr create` needs an explicit base; base `main` only from
     `dev/streaming-architecture` (release) or the approved setup branch.
  4. `gh pr merge` needs `--match-head-commit <40-char SHA>`.
  5. Pull request writes through `gh api` are refused; use `gh pr create/merge`.
  6. MCP pull request creation follows rule 3; MCP merges are refused.

A command "creates or merges a pull request" when its text, outside the
bodies of quoted-delimiter heredocs fed to a data command (gh, git, cat,
tee), mentions `gh ... pr ... create`, `gh ... pr ... merge` or
`gh ... api ... pulls`. Text such as
`git commit -m "... gh pr merge ..."` is therefore refused too; use a file or
a quoted heredoc for such messages.

Override for an approved `main` hotfix PR (rule 3 only): the operator sets
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
from typing import NoReturn

RELEASE_HEAD = "dev/streaming-architecture"
SETUP_HEADS = {"ci/312-epic-guard"}  # approved epic-guard setup PR to main
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
FORGED_VERDICT = re.compile(
    r"Review: (APPROVED|CHANGES REQUESTED) by Codex at [0-9a-f]{40}"
)
GUARDED = re.compile(
    r"\bgh\b.*?(\bpr\b.*?\b(create|merge)\b|\bapi\b.*?\bpulls\b)", re.S
)
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
HEREDOC = re.compile(r"<<-?[ \t]*(['\"])([A-Za-z_][A-Za-z0-9_]*)\1[ \t]*$")
# Commands that read a heredoc body as data. Any other consumer (a shell,
# `command bash`, `env bash`, ...) may run it, so its body stays in detection.
DATA_CONSUMERS = {"gh", "git", "cat", "tee"}
PUNCTUATION = set(";&|<>()\n")
# gh flags that take a value, normalised to one name so the last one wins.
CREATE_FLAGS = {
    "-B": "--base",
    "--base": "--base",
    "-H": "--head",
    "--head": "--head",
    "-t": "--title",
    "--title": "--title",
    "-b": "--body",
    "--body": "--body",
    "-F": "--body-file",
    "--body-file": "--body-file",
    "-a": "--assignee",
    "--assignee": "--assignee",
    "-l": "--label",
    "--label": "--label",
    "-m": "--milestone",
    "--milestone": "--milestone",
    "-p": "--project",
    "--project": "--project",
    "-r": "--reviewer",
    "--reviewer": "--reviewer",
    "-R": "--repo",
    "--repo": "--repo",
    "-T": "--template",
    "--template": "--template",
    "--recover": "--recover",
}
MERGE_FLAGS = {
    "-b": "--body",
    "--body": "--body",
    "-F": "--body-file",
    "--body-file": "--body-file",
    "-t": "--subject",
    "--subject": "--subject",
    "-A": "--author-email",
    "--author-email": "--author-email",
    "--match-head-commit": "--match-head-commit",
    "-R": "--repo",
    "--repo": "--repo",
}


class Blocked(Exception):
    pass


def block(*lines: str) -> NoReturn:
    raise Blocked("\n".join(lines))


def continues(line: str) -> bool:
    """True if the line ends with an unescaped backslash."""
    return (len(line) - len(line.rstrip("\\"))) % 2 == 1


def join_continuations(text: str) -> str:
    lines = text.split("\n")
    out = [lines[0]]
    for line in lines[1:]:
        if continues(out[-1]):
            out[-1] = out[-1][:-1] + " " + line
        else:
            out.append(line)
    return "\n".join(out)


def split_heredoc(cmd: str) -> tuple[str, str | None]:
    """Split `head <<'D'\\nbody\\nD` into (head, body).

    Continuations are joined in the head only; the quoted body is literal.
    No heredoc: (cmd with continuations joined, None).
    """
    lines = cmd.split("\n")
    first, i = lines[0], 1
    while continues(first) and i < len(lines):
        first = first[:-1] + " " + lines[i]
        i += 1
    rest = "\n".join(lines[i:])
    m = HEREDOC.search(first)
    if not m:
        return join_continuations(cmd), None
    tag = m.group(2)
    lines = rest.split("\n")
    if tag not in [line.strip() for line in lines]:
        return cmd, None
    end = [line.strip() for line in lines].index(tag)
    if any(line.strip() for line in lines[end + 1 :]):
        return cmd, None  # commands after the heredoc: not the supported form
    return first[: m.start()].rstrip(), "\n".join(lines[:end])


def detection_text(cmd: str) -> str:
    """The command text that can run, for deciding whether it is guarded."""
    head, body = split_heredoc(cmd)
    if body is None:
        return cmd
    try:
        words = shlex.split(head)
    except ValueError:
        return cmd
    words = [w for w in words if not ASSIGNMENT.match(w)]
    if words and os.path.basename(words[0]) in DATA_CONSUMERS:
        return head
    return cmd  # the body may run as code


def outside_single_quotes(text: str) -> str:
    out, single, i = [], False, 0
    while i < len(text):
        c = text[i]
        if c == "'" and not single:
            single = True
        elif c == "'" and single:
            single = False
        elif not single:
            if c == "\\" and i + 1 < len(text):
                out.append(" ")
                i += 2
                continue
            out.append(c)
        i += 1
    return "".join(out)


def parse(args: list[str], flags: dict[str, str]) -> dict[str, str]:
    values: dict[str, str] = {}
    i = 0
    while i < len(args):
        arg = args[i]
        name, sep, value = arg.partition("=") if arg.startswith("--") else (arg, "", "")
        if name in flags:
            if not sep:
                if i + 1 >= len(args):
                    block(f"Missing value for {name}.")
                value = args[i + 1]
                i += 1
            values[flags[name]] = value
        i += 1
    return values


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
    if not GUARDED.search(detection_text(cmd)):
        return
    unsupported = (
        "Pull request create/merge commands must be one plain gh command: "
        "no wrappers, shell operators, substitutions or flags before the subcommand."
    )
    head, body = split_heredoc(cmd)
    code = outside_single_quotes(head)
    if "$" in code or "`" in code:
        block(unsupported)
    try:
        lexer = shlex.shlex(head, posix=True, punctuation_chars=";&|<>()\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        words = list(lexer)
    except ValueError:
        block(unsupported)
    if any(w and set(w) <= PUNCTUATION for w in words):
        block(unsupported)
    while words and ASSIGNMENT.match(words[0]):
        words = words[1:]
    if (
        not words
        or os.path.basename(words[0]) != "gh"
        or len(words) < 2
        or words[1].startswith("-")
        or (words[1] == "pr" and (len(words) < 3 or words[2].startswith("-")))
    ):
        block(unsupported)
    if any(w.startswith("-") and not w.startswith("--") and len(w) > 2 for w in words):
        block(
            "Write short options with a separate value (-B main, not -Bmain), "
            "or use the long form."
        )
    if words[1:3] == ["pr", "create"]:
        values = parse(words[3:], CREATE_FLAGS)
        check_base(values.get("--base"), values.get("--head") or current_branch(cwd))
    elif words[1:3] == ["pr", "merge"]:
        sha = parse(words[3:], MERGE_FLAGS).get("--match-head-commit")
        if not sha or not SHA_RE.match(sha):
            block(
                "gh pr merge needs --match-head-commit <40-char head SHA>.",
                "Merge only the head the other agent approved; a mismatch aborts the merge.",
            )
    elif words[1] == "api" and any("pulls" in w for w in words[2:]):
        writes = {"-f", "-F", "--field", "--raw-field", "--input"}
        method = (
            parse(words[2:], {"-X": "-X", "--method": "-X"}).get("-X", "GET").upper()
        )
        if method != "GET" or any(w.split("=", 1)[0] in writes for w in words[2:]):
            block(
                "Pull request writes through gh api are refused. Use gh pr create or gh pr merge."
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
