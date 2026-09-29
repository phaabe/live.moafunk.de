"""Check literal PR commands and prevent Codex from writing Claude verdicts."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

TRUNK = "dev/streaming-architecture"
INTERIM = "dev/312-interim"
MAIN_HEADS = (TRUNK, "ci/312-epic-guard")
VERDICT = re.compile(
    r"Review: (?:APPROVED|CHANGES REQUESTED) by Claude at [0-9a-fA-F]{40}(?![0-9a-fA-F])"
)
PR_MUTATION = re.compile(r"\bgh\b.*\bpr\b.*\b(?:create|merge)\b", re.DOTALL)
API_COMMAND = re.compile(r"\bgh\b.*\bapi\b", re.DOTALL)
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


def join_continuations(command: str) -> str:
    """Join shell continuations before detection, keeping single quotes literal."""
    result: list[str] = []
    quote = ""
    index = 0
    while index < len(command):
        char = command[index]
        if char == "\\" and quote != "'" and index + 1 < len(command):
            following = command[index + 1]
            if following != "\n":
                result.extend((char, following))
            index += 2
            continue
        if char == quote:
            quote = ""
        elif not quote and char in ("'", '"'):
            quote = char
        result.append(char)
        index += 1
    return "".join(result)


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
                if index >= len(args):
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


def check_base(base: str | None, head: str | None) -> None:
    if base not in (TRUNK, INTERIM, "main"):
        raise ValueError(
            f"PR creation requires base {INTERIM} or {TRUNK} (or an approved main PR)."
        )
    if base == "main" and head not in MAIN_HEADS:
        raise ValueError(f"Only heads {TRUNK} and ci/312-epic-guard may target main.")


def selected_adopt_body_edit(args: list[str]) -> bool:
    """Allow one body-file PATCH for the runner's selected adopt target."""
    if (
        len(args) != 5
        or args[:2] != ["--method", "PATCH"]
        or args[3] != "-F"
        or not args[4].startswith("body=@")
    ):
        return False
    target = re.fullmatch(
        r"repos/phaabe/live\.moafunk\.de/pulls/([1-9][0-9]*)", args[2]
    )
    body_path = Path(args[4][len("body=@") :])
    action_path = Path(os.environ.get("EPIC_ACTION_FILE", ""))
    if (
        target is None
        or not body_path.is_absolute()
        or ".." in body_path.parts
        or not action_path.is_absolute()
    ):
        return False
    action = json.loads(action_path.read_text())
    if (
        not isinstance(action, dict)
        or action.get("action") != "adopt"
        or type(action.get("pr")) is not int
        or action["pr"] != int(target[1])
        or not isinstance(action.get("sha"), str)
        or re.fullmatch(r"[0-9a-fA-F]{40}", action["sha"]) is None
        or not isinstance(action.get("body_sha"), str)
        or re.fullmatch(r"[0-9a-fA-F]{64}", action["body_sha"]) is None
    ):
        return False
    # Keep file inspection here so this exception cannot bypass verdict checks.
    check_verdict(body_path.read_text())
    return True


def check_api(args: list[str], cwd: Path) -> None:
    """Inspect typed file fields and limit REST PR writes to selected adoption."""
    value_flags = {
        "-f",
        "--raw-field",
        "-F",
        "--field",
        "-X",
        "--method",
        "--input",
        "-H",
        "--header",
        "--hostname",
        "--cache",
        "-q",
        "--jq",
        "-p",
        "--preview",
        "-t",
        "--template",
    }
    switches = {
        "-i",
        "--include",
        "--paginate",
        "--silent",
        "--slurp",
        "--verbose",
        "--help",
    }
    has_fields = False
    endpoints: list[str] = []
    method = None
    body_input = False
    index = 0
    while index < len(args):
        arg = args[index]
        flag, separator, value = arg.partition("=")
        if flag in value_flags:
            if not separator:
                index += 1
                if index >= len(args):
                    raise ValueError(f"Missing value for {flag}.")
                value = args[index]
            if flag in ("-f", "--raw-field", "-F", "--field"):
                has_fields = True
                if flag in ("-F", "--field"):
                    _, _, field_value = value.partition("=")
                    if field_value.startswith("@"):
                        path = field_value[1:]
                        if path == "-":
                            raise ValueError("Use a readable API field file, not @-.")
                        check_verdict((cwd / path).read_text())
            elif flag in ("-X", "--method"):
                if method is not None:
                    raise ValueError("Pass the API method only once.")
                method = value.upper()
            elif flag == "--input":
                body_input = True
                if value == "-":
                    raise ValueError("Use a readable API input file, not stdin.")
                check_verdict((cwd / value).read_text())
        elif arg not in switches:
            if arg.startswith("-"):
                raise ValueError(
                    "Use separate API short-option values or long options."
                )
            endpoints.append(arg)
        index += 1
    if len(endpoints) != 1:
        raise ValueError("Use one literal gh api endpoint.")
    endpoint = urlsplit(endpoints[0])
    path = unquote(endpoint.path).rstrip("/")
    method = method or ("POST" if has_fields or body_input else "GET")
    if re.search(r"/pulls(?:/|$)", path) and (method != "GET" or body_input):
        if selected_adopt_body_edit(args):
            return
        raise ValueError("Use gh pr commands instead of gh api for PR writes.")


def check_command(command: str, cwd: Path) -> None:
    command = join_continuations(command)
    args = shell_words(command)
    for arg in args:
        check_verdict(arg)
    if not any(
        arg == "gh"
        or arg.endswith("/gh")
        or PR_MUTATION.search(arg)
        or API_COMMAND.search(arg)
        for arg in args
    ):
        return
    # Body files need inspection even for issue comments and PR reviews.
    body_command = any(arg in ("pr", "issue") for arg in args) and any(
        arg in ("create", "comment", "edit", "review", "merge") for arg in args
    )
    body_file = body_command and any(
        arg.startswith(("--body-file", "-F")) for arg in args
    )
    api = "api" in args or bool(API_COMMAND.search(command))
    if args[:2] in (["gh", "pr"], ["gh", "issue"]):
        api = False
    mutation = bool(PR_MUTATION.search(command)) or (
        "pr" in args and ("create" in args or "merge" in args)
    )
    if not mutation and not body_file and not api:
        return
    check_literal(command, args)
    if api:
        if args[1:2] != ["api"]:
            raise ValueError("Use gh api directly, with flags after api.")
        check_api(args[2:], cwd)
        return
    body_path = option(args[1:], ("--body-file", "-F")) if body_file else None
    if body_path is not None:
        if body_path == "-":
            raise ValueError("Use a readable --body-file path, not stdin.")
        check_verdict((cwd / body_path).read_text())
    if args[1:3] == ["pr", "create"]:
        base = option(args[3:], ("--base", "-B"))
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
        check_base(base, head)
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
        tool_name = payload.get("tool_name", "")
        if tool_name.endswith("merge_pull_request"):
            raise ValueError(
                "Use gh pr merge with --match-head-commit instead of MCP merge."
            )
        if tool_name.endswith("create_pull_request"):
            check_base(tool_input.get("base"), tool_input.get("head"))
        command = tool_input.get("command", tool_input.get("cmd", ""))
        if payload.get("tool_name") in ("Bash", "exec_command", "shell_command"):
            if not isinstance(command, str):
                raise ValueError("The shell command must be a string.")
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
