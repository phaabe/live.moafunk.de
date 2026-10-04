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
  7. Headless runner with the shared reader (EPIC_SHARED_READER=1 and
     EPIC_ACTION_FILE set by scripts/epic/claude-tick.sh; with an action file,
     a missing, empty or invalid EPIC_SHARED_READER blocks every write, and an
     explicit 0 keeps only the other rules): each GitHub write
     is checked fresh right before it runs (scripts/epic/write_checks.py,
     loaded from EPIC_RUNTIME_ROOT, the pinned runtime; without pinned mode
     from EPIC_TRUSTED_ROOT, the runner checkout). Pinned mode configured
     without EPIC_RUNTIME_ROOT blocks. A stale target or a failed read blocks
     the write. Interactive sessions are not affected.
  8. Runtime promotion (every session): while
     `<EPIC_LOCK_DIR>/runtime-promotion.json` exists, a write is blocked
     unless it runs inside a tick admitted before the promotion started
     (scripts/epic/runtime.py). Without the marker this costs one stat().

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
INTERIM = "dev/312-interim"  # temporary feature base, epic-rules.md section 0
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

API_FLAGS = {
    "-X": "--method",
    "--method": "--method",
    "-f": "--raw-field",
    "--raw-field": "--raw-field",
    "-F": "--field",
    "--field": "--field",
    "--input": "--input",
    "-H": "--header",
    "--header": "--header",
    "-q": "--jq",
    "--jq": "--jq",
    "-t": "--template",
    "--template": "--template",
    "-p": "--preview",
    "--preview": "--preview",
    "--hostname": "--hostname",
    "--cache": "--cache",
}


class Blocked(Exception):
    pass


def block(*lines: str) -> NoReturn:
    raise Blocked("\n".join(lines))


def logical_line(text: str) -> tuple[str, str, bool]:
    """Read one logical shell line: (line, rest, ended_at_newline).

    Like the shell, a backslash-newline outside single quotes is removed
    with no space, so `ma\\<newline>in` is `main`. A newline inside quotes
    does not end the line.
    """
    out: list[str] = []
    single = double = False
    i = 0
    while i < len(text):
        c = text[i]
        if single:
            single = c != "'"
        elif c == "\\" and i + 1 < len(text):
            if text[i + 1] != "\n":
                out.append(text[i : i + 2])
            i += 2
            continue
        elif c == "'" and not double:
            single = True
        elif c == '"':
            double = not double
        elif c == "\n" and not double:
            return "".join(out), text[i + 1 :], True
        out.append(c)
        i += 1
    return "".join(out), "", False


def join_continuations(text: str) -> str:
    lines = []
    more = True
    while more:
        line, text, more = logical_line(text)
        lines.append(line)
    return "\n".join(lines)


def split_heredoc(cmd: str) -> tuple[str, str | None]:
    """Split `head <<'D'\\nbody\\nD` into (head, body).

    Continuations are joined in the head only; the quoted body is literal.
    No supported heredoc: (cmd with continuations joined, None).
    """
    first, rest, _ = logical_line(cmd)
    m = HEREDOC.search(first)
    if not m:
        return join_continuations(cmd), None
    tag = m.group(2)
    lines = rest.split("\n")
    stripped = [line.strip() for line in lines]
    if tag not in stripped:
        return join_continuations(cmd), None
    end = stripped.index(tag)
    if any(stripped[end + 1 :]):
        # commands after the heredoc: not the supported form
        return join_continuations(cmd), None
    return first[: m.start()].rstrip(), "\n".join(lines[:end])


def shell_words(head: str) -> list[str] | None:
    """Words of one simple command, or None if it has shell syntax."""
    code = outside_single_quotes(head)
    if "$" in code or "`" in code:
        return None
    try:
        lexer = shlex.shlex(head, posix=True, punctuation_chars=";&|<>()\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        words = list(lexer)
    except ValueError:
        return None
    if any(w and set(w) <= PUNCTUATION for w in words):
        return None
    while words and ASSIGNMENT.match(words[0]):
        words = words[1:]
    return words


def detection_text(cmd: str) -> str:
    """The command text that can run, for deciding whether it is guarded."""
    head, body = split_heredoc(cmd)
    if body is None:
        return head
    words = shell_words(head)
    if words and os.path.basename(words[0]) in DATA_CONSUMERS:
        return head  # one simple data command reads the body
    return head + "\n" + body  # the body may run as code


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
    """Read value flags; the last one wins. Refuse attached short values."""
    values: dict[str, str] = {}
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("-") and not arg.startswith("--") and len(arg) > 2:
            block(
                "Write short options with a separate value (-B main, not -Bmain), "
                "or use the long form."
            )
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
            f"Epic feature PRs: --base {INTERIM} (until protection is fixed, then {RELEASE_HEAD}). "
            f"Release PRs: --head {RELEASE_HEAD} --base main.",
        )
    if base == "main" and os.environ.get("CLAUDE_ALLOW_MAIN_PR") != "1":
        if head != RELEASE_HEAD and head not in SETUP_HEADS:
            block(
                f"Only release PRs from {RELEASE_HEAD} (or the approved setup PR) may target main "
                f"(head was '{head or 'unknown'}').",
                f"Target {INTERIM} instead (epic-rules.md section 0).",
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
    head, _ = split_heredoc(cmd)
    words = shell_words(head)
    if words is None:
        block(unsupported)
    if (
        not words
        or os.path.basename(words[0]) != "gh"
        or len(words) < 2
        or words[1].startswith("-")
        or (words[1] == "pr" and (len(words) < 3 or words[2].startswith("-")))
    ):
        block(unsupported)
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
        method = parse(words[2:], API_FLAGS).get("--method", "GET").upper()
        if method != "GET" or any(w.split("=", 1)[0] in writes for w in words[2:]):
            block(
                "Pull request writes through gh api are refused. Use gh pr create or gh pr merge."
            )


def checkout_root() -> str:
    return os.environ.get("EPIC_TRUSTED_ROOT") or os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..")
    )


def runtime_home() -> str:
    return os.environ.get("EPIC_RUNTIME_HOME") or os.path.expanduser(
        "~/.local/share/epic-runtime"
    )


def code_root() -> str:
    """Where runner code loads from: EPIC_RUNTIME_ROOT in a pinned tick.
    Pinned mode configured without it: blocked, never the checkout."""
    root = os.environ.get("EPIC_RUNTIME_ROOT")
    if root:
        return root
    home = runtime_home()
    if any(
        os.path.exists(os.path.join(home, n)) for n in ("configured.json", "pin.json")
    ):
        block(
            "Pinned runtime mode is configured but EPIC_RUNTIME_ROOT is not set.",
            "Start runner ticks through the epic-tick launcher.",
        )
    return checkout_root()


def load_write_checks(root: str, what: str):
    sys.path.insert(0, os.path.join(root, "scripts", "epic"))
    try:
        import write_checks
    except Exception as exc:  # any import failure must block, not allow
        block(f"{what} are unavailable: {exc!r}")
    return write_checks


def promotion_barrier(tool: str, tool_input: dict, cwd: str) -> None:
    """Rule 8. Fails closed while a promotion marker exists."""
    lock_dir = os.environ.get("EPIC_LOCK_DIR") or os.path.expanduser(
        "~/.local/state/epic-loop/target-locks"
    )
    if not os.path.exists(os.path.join(lock_dir, "runtime-promotion.json")):
        return
    root = os.environ.get("EPIC_RUNTIME_ROOT") or checkout_root()
    write_checks = load_write_checks(root, "Promotion write checks")
    try:
        refused = write_checks.promotion_refusal(tool, tool_input, cwd)
    except Exception as exc:  # exit 1 would let the write through
        block(f"Promotion write check failed: {exc!r}")
    if refused:
        block(
            f"Runtime promotion: {refused}",
            "Wait until the promotion has finished, then try again.",
        )


def runner_write_check(tool: str, tool_input: dict, cwd: str) -> None:
    """Rule 7. Fails closed: if the check cannot load, the write is blocked."""
    # No action context: interactive, unchanged. Explicit 0: the runner turned
    # the reader off. Anything else (1, missing, empty, invalid) goes to
    # write_checks.guard(), which refuses writes on a bad setting.
    if not os.environ.get("EPIC_ACTION_FILE"):
        return
    if os.environ.get("EPIC_SHARED_READER") == "0":
        return
    write_checks = load_write_checks(code_root(), "Runner write checks")
    try:
        refused = write_checks.guard(tool, tool_input, cwd, agent="Claude")
    except Exception as exc:  # exit 1 would let the write through
        block(f"Runner write check failed: {exc!r}")
    if refused:
        block(
            f"Runner write check: {refused}",
            "Stop this action; the next tick selects again from fresh GitHub state.",
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
        promotion_barrier(tool, tool_input, cwd)
        runner_write_check(tool, tool_input, cwd)
    except Blocked as exc:
        print("BLOCKED by .claude/hooks/scripts/epic-guard.sh", file=sys.stderr)
        print(exc, file=sys.stderr)
        print("Rules: docs/implementation/epic-rules.md", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
