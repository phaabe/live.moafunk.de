"""Check literal PR commands and prevent Codex from writing Claude verdicts."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

TRUNK = "dev/streaming-architecture"
VERDICT = re.compile(
    r"Review: (?:APPROVED|CHANGES REQUESTED) by Claude at [0-9a-fA-F]{40}(?![0-9a-fA-F])"
)
PR_MUTATION = re.compile(r"\bgh\s+pr\s+(?:create|merge)\b")
SHELL_PUNCTUATION = frozenset(";&|<>()\n")


def check_verdict(text: str) -> None:
    if VERDICT.search(text):
        raise ValueError("Codex must never write a review verdict in Claude's name.")


def shell_words(command: str) -> list[str]:
    """Tokenize without executing or expanding the proposed command."""
    lexer = shlex.shlex(command.strip(), posix=True, punctuation_chars=";&|<>()\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def check_literal(command: str, args: list[str]) -> None:
    """Accept one literal gh invocation."""
    if (
        not args
        or args[0] != "gh"
        or any(arg and set(arg) <= SHELL_PUNCTUATION for arg in args)
        or "$" in command
        or "`" in command
    ):
        raise ValueError(
            "Use one literal gh command, without wrappers or shell expansion."
        )


def option(args: list[str], names: tuple[str, ...]) -> str | None:
    """Read options without treating body/title values as flags."""
    values: list[str] = []
    value_flags = {
        "--base",
        "-B",
        "--head",
        "-H",
        "--body",
        "-b",
        "--body-file",
        "-F",
        "--title",
        "-t",
        "--repo",
        "-R",
        "--reviewer",
        "-r",
        "--assignee",
        "-a",
        "--label",
        "-l",
        "--milestone",
        "-m",
        "--project",
        "-p",
        "--template",
        "-T",
        "--recover",
        "--match-head-commit",
        "--subject",
        "--author-email",
    }
    index = 0
    while index < len(args):
        arg = args[index]
        key, separator, value = arg.partition("=")
        if key in value_flags:
            if not separator:
                index += 1
                if (
                    index >= len(args)
                    or args[index].startswith("-")
                    and args[index] != "-"
                ):
                    raise ValueError(f"Missing value for {key}.")
                value = args[index]
            if key in names:
                values.append(value)
        elif arg.startswith("-") and not arg.startswith("--") and len(arg) > 2:
            raise ValueError("Use separate short-option values or long options.")
        index += 1
    if len(values) > 1:
        raise ValueError(f"Pass {names[0]} only once.")
    return values[0] if values else None


def check_command(command: str, cwd: Path) -> None:
    args = shell_words(command)
    for arg in args:
        check_verdict(arg)
    if not any(
        arg == "gh" or arg.endswith("/gh") or PR_MUTATION.search(arg) for arg in args
    ):
        return
    # Body files need inspection even for issue comments and PR reviews.
    body_file = any(arg.startswith(("--body-file", "-F")) for arg in args)
    mutation = bool(PR_MUTATION.search(command)) or (
        "pr" in args and ("create" in args or "merge" in args)
    )
    if not mutation and not body_file:
        return
    check_literal(command, args)
    body_path = option(args[1:], ("--body-file", "-F"))
    if body_path is not None:
        if body_path == "-":
            raise ValueError("Use a readable --body-file path, not stdin.")
        check_verdict((cwd / body_path).read_text())
    if args[1:3] == ["pr", "create"]:
        base = option(args[3:], ("--base", "-B"))
        if base not in (TRUNK, "main"):
            raise ValueError(
                f"gh pr create requires --base {TRUNK} (or main for a release)."
            )
        head = option(args[3:], ("--head", "-H"))
        if base == "main":
            if head is None:
                head = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(cwd),
                        "symbolic-ref",
                        "--quiet",
                        "--short",
                        "HEAD",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
            if head != TRUNK:
                raise ValueError(f"Only a release with head {TRUNK} may target main.")
    elif args[1:3] == ["pr", "merge"]:
        sha = option(args[3:], ("--match-head-commit",))
        if sha is None or re.fullmatch(r"[0-9a-fA-F]{40}", sha) is None:
            raise ValueError(
                "gh pr merge requires --match-head-commit <40-char head SHA>."
            )
    elif mutation:
        raise ValueError(
            "Use gh pr create or gh pr merge directly, with flags after the verb."
        )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        tool_input = payload.get("tool_input", payload.get("toolInput", {}))
        check_verdict(json.dumps(tool_input, ensure_ascii=False))
        command = tool_input.get("command", tool_input.get("cmd", ""))
        if payload.get("tool_name") in ("Bash", "exec_command", "shell_command"):
            cwd = Path(tool_input.get("workdir") or payload.get("cwd") or Path.cwd())
            check_command(command, cwd)
    except (
        ValueError,
        OSError,
        TypeError,
        AttributeError,
        subprocess.CalledProcessError,
    ) as error:
        sys.stderr.write(f"BLOCKED by Codex epic guard: {error}\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
