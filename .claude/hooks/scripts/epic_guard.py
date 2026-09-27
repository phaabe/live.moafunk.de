"""Claude's local guard for the architecture epic rules (docs/implementation/epic-rules.md).

Reads a PreToolUse payload on stdin. Exit 2 blocks the tool call (message on
stderr), exit 0 allows it.

Blocks:
  1. A review verdict written in Codex's name, in any tool input.
  2. A pull request without an explicit base, or with base `main` unless the
     head is `dev/streaming-architecture` (release) or the approved setup
     branch. Covers `gh pr create`, `gh api .../pulls` and the MCP tool.
  3. A merge without its own expected head SHA: `gh pr merge` needs
     `--match-head-commit <sha>`, `gh api .../pulls/<n>/merge` needs
     `sha=<sha>`. MCP merges are blocked.
  4. `gh` pull request commands the guard cannot see into: inside command
     substitution, or run through eval, a shell, xargs or source.

Parsing is quote-aware: separators, heredocs and substitutions are only
recognised outside single quotes, and every simple command is checked on its
own real arguments.

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
GH_PR_TEXT = re.compile(r"\bgh\b.*\b(pr\s+(create|merge)|api\b.*\bpulls\b)", re.S)
SEPARATORS = {"&&", "||", ";", ";;", "|", "|&", "&", "\n", "(", ")"}
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Wrappers that run the rest of the line as a command, with their value options.
WRAPPERS = {
    "env": {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"},
    "command": set(),
    "builtin": set(),
    "exec": {"-a"},
    "nohup": set(),
    "time": set(),
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-U"},
    "nice": {"-n"},
    "timeout": {"-s", "--signal", "-k", "--kill-after"},
    "gtimeout": {"-s", "--signal", "-k", "--kill-after"},
}
TAKES_DURATION = {"timeout", "gtimeout"}
INTERPRETERS = {"eval", "bash", "sh", "zsh", "dash", "ksh", "xargs", "source", "."}
DATA_OWNERS = {"gh", "cat", "tee"}
PR_CREATE_VALUE_FLAGS = {
    "-t",
    "--title",
    "-b",
    "--body",
    "-F",
    "--body-file",
    "-B",
    "--base",
    "-H",
    "--head",
    "-a",
    "--assignee",
    "-l",
    "--label",
    "-m",
    "--milestone",
    "-p",
    "--project",
    "-r",
    "--reviewer",
    "-R",
    "--repo",
    "-T",
    "--template",
}
PR_MERGE_VALUE_FLAGS = {
    "-b",
    "--body",
    "-F",
    "--body-file",
    "-t",
    "--subject",
    "-A",
    "--author-email",
    "--match-head-commit",
    "-R",
    "--repo",
}
API_VALUE_FLAGS = {
    "-X",
    "--method",
    "-f",
    "--raw-field",
    "-F",
    "--field",
    "-H",
    "--header",
    "--input",
    "-q",
    "--jq",
    "-t",
    "--template",
    "--hostname",
    "--cache",
    "-p",
    "--preview",
}


class Blocked(Exception):
    pass


def block(*lines: str) -> None:
    raise Blocked("\n".join(lines))


# ------------------------------------------------------------------ scanning
def scan(cmd: str) -> tuple[str, list[tuple[str, bool, str]], list[str]]:
    """Split a command into code, heredocs and substitutions, respecting quotes.

    Returns (code without heredoc bodies, [(owner line, delimiter quoted, body)],
    [contents of $(...) and backtick substitutions found outside single quotes]).
    """
    code: list[str] = []
    heredocs: list[tuple[str, bool, str]] = []
    subs: list[str] = []
    pending: list[tuple[str, bool]] = []  # heredoc delimiters waiting for end of line
    line_start = 0
    i, n = 0, len(cmd)
    single = double = False
    while i < n:
        c = cmd[i]
        if single:
            if c == "'":
                single = False
            code.append(c)
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            code.append(cmd[i : i + 2])
            i += 2
            continue
        if c == "'" and not double:
            single = True
        elif c == '"':
            double = not double
        elif c == "`":
            end = cmd.find("`", i + 1)
            end = n if end == -1 else end
            subs.append(cmd[i + 1 : end])
            code.append(cmd[i : end + 1])
            i = end + 1
            continue
        elif c == "$" and cmd.startswith("$(", i):
            depth, j = 1, i + 2
            while j < n and depth:
                depth += {"(": 1, ")": -1}.get(cmd[j], 0)
                j += 1
            subs.append(cmd[i + 2 : j - 1])
            code.append(cmd[i:j])
            i = j
            continue
        elif (
            c == "<"
            and not double
            and cmd.startswith("<<", i)
            and not cmd.startswith("<<<", i)
        ):
            m = re.match(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1", cmd[i:])
            if m:
                pending.append((m.group(2), bool(m.group(1))))
                code.append(m.group(0))
                i += m.end()
                continue
        elif c == "\n" and not double and pending:
            owner = cmd[line_start:i]
            code.append("\n")
            i += 1
            for tag, quoted in pending:
                body: list[str] = []
                while i < n:
                    end = cmd.find("\n", i)
                    end = n if end == -1 else end
                    line = cmd[i:end]
                    i = end + 1
                    if line.strip() == tag:
                        break
                    body.append(line)
                heredocs.append((owner, quoted, "\n".join(body)))
            pending = []
            line_start = i
            continue
        if c == "\n" and not double:
            line_start = i + 1
        code.append(c)
        i += 1
    return "".join(code), heredocs, subs


def simple_commands(code: str) -> list[list[str]]:
    lexer = shlex.shlex(code, posix=True, punctuation_chars=";&|()\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    commands, current = [], []
    for token in lexer:
        if token in SEPARATORS or (token and set(token) <= set(";&|()\n")):
            if current:
                commands.append(current)
            current = []
        else:
            current.append(token)
    if current:
        commands.append(current)
    return commands


def strip_wrappers(words: list[str]) -> list[str]:
    """Drop env assignments and wrapper commands such as env, command, sudo, timeout."""
    i = 0
    while i < len(words):
        word = words[i]
        name = os.path.basename(word)
        if ASSIGNMENT.match(word):
            i += 1
        elif name in WRAPPERS:
            i += 1
            while i < len(words) and (
                words[i].startswith("-") or ASSIGNMENT.match(words[i])
            ):
                i += 2 if words[i] in WRAPPERS[name] else 1
            if name in TAKES_DURATION and i < len(words):
                i += 1  # the duration
        else:
            break
    return words[i:]


def parse(args: list[str], value_flags: set[str]) -> dict[str, list[str]]:
    """Collect flag values; value flags consume the next argument."""
    values: dict[str, list[str]] = {}
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("--") and "=" in arg:
            flag, value = arg.split("=", 1)
            values.setdefault(flag, []).append(value)
        elif arg in value_flags and i + 1 < len(args):
            values.setdefault(arg, []).append(args[i + 1])
            i += 1
        i += 1
    return values


def last(values: dict[str, list[str]], *flags: str) -> str | None:
    found = [v for f in flags for v in values.get(f, [])]
    return found[-1] if found else None


# ------------------------------------------------------------------ rules
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


def require_sha(sha: str | None, how: str) -> None:
    if not sha or not SHA_RE.match(sha):
        block(
            f"A merge needs its own expected head SHA: {how} <40-char head SHA>.",
            "Merge only the head the other agent approved; a mismatch aborts the merge.",
        )


def check_gh(words: list[str], cwd: str) -> None:
    if len(words) >= 3 and words[1] == "pr" and words[2] == "create":
        values = parse(words[3:], PR_CREATE_VALUE_FLAGS)
        head = last(values, "--head", "-H") or current_branch(cwd)
        check_base(last(values, "--base", "-B"), head)
    elif len(words) >= 3 and words[1] == "pr" and words[2] == "merge":
        require_sha(
            last(parse(words[3:], PR_MERGE_VALUE_FLAGS), "--match-head-commit"),
            "--match-head-commit",
        )
    elif len(words) >= 2 and words[1] == "api":
        values = parse(words[2:], API_VALUE_FLAGS)
        paths = [w for w in words[2:] if "pulls" in w and not w.startswith("-")]
        fields = dict(
            f.split("=", 1)
            for f in values.get("-f", [])
            + values.get("--raw-field", [])
            + values.get("-F", [])
            + values.get("--field", [])
            if "=" in f
        )
        if any(re.search(r"pulls/\d+/merge\b", p) for p in paths):
            require_sha(fields.get("sha"), "gh api ... -f sha=")
        elif any(re.search(r"/pulls/?$", p) for p in paths) and (
            "base" in fields or "head" in fields
        ):
            check_base(fields.get("base"), fields.get("head"))


def check_command(cmd: str, cwd: str, depth: int = 0) -> None:
    if depth > 3:
        block("Nested commands are too deep for the guard to check.")
    code, heredocs, subs = scan(cmd)
    for sub in subs:
        if GH_PR_TEXT.search(sub):
            block(
                "The guard cannot check a gh pull request command inside $(...) or backticks.",
                "Run the gh command directly.",
            )
    try:
        commands = simple_commands(code)
    except ValueError:
        if GH_PR_TEXT.search(code):
            block(
                "The guard cannot parse this command. Run the gh command as a plain command."
            )
        return
    for words in commands:
        words = strip_wrappers(words)
        if not words:
            continue
        name = os.path.basename(words[0])
        if name in INTERPRETERS and GH_PR_TEXT.search(" ".join(words[1:])):
            block(
                f"The guard cannot check a gh pull request command run through `{name}`.",
                "Run the gh command directly.",
            )
        if name == "gh":
            check_gh(words, cwd)
    for owner, quoted, body in heredocs:
        owner_words = strip_wrappers((simple_commands(scan(owner)[0]) or [[]])[-1])
        owner_name = os.path.basename(owner_words[0]) if owner_words else ""
        if owner_name in DATA_OWNERS:
            if not quoted:  # unquoted delimiter: substitutions in the body still run
                for sub in scan(body.replace("'", " "))[2]:
                    if GH_PR_TEXT.search(sub):
                        block(
                            "A heredoc body runs a gh pull request command in a substitution."
                        )
        elif owner_name in INTERPRETERS or owner_name in {"python", "python3", "node"}:
            if GH_PR_TEXT.search(body):
                block(
                    f"The guard cannot check gh pull request commands fed to `{owner_name}`.",
                    "Run the gh command directly.",
                )
        else:
            check_command(body, cwd, depth + 1)


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
