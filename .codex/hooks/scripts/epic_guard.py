"""Check literal PR commands and prevent Codex from writing Claude verdicts."""

from __future__ import annotations

import importlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any
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
FEATURE_HELPER = Path.home() / ".local/libexec/codex-feature-git.py"


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
    anchor = os.environ.get("EPIC_BODY_DIR_ID", "")
    if (
        target is None
        or not body_path.is_absolute()
        or ".." in body_path.parts
        or not action_path.is_absolute()
        or not anchor
    ):
        return False
    # Only the runner's per-tick body directory, so a wrong path cannot become
    # a PR body by mistake. It does not stop a session that copies another file
    # into the directory or swaps the file after this check. It is identified by the device and inode the runner
    # recorded, so a replaced directory or a symlink in its place does not
    # match. The file must be regular with one link: no symlink or hard link.
    parent = os.stat(body_path.parent)
    body = os.lstat(body_path)
    if (
        f"{parent.st_dev}:{parent.st_ino}" != anchor
        or not stat.S_ISREG(body.st_mode)
        or body.st_nlink != 1
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


def helper_push_command(command: str, cwd: Path) -> tuple[str, Path]:
    """Translate the installed publisher into the branch write it performs."""
    if not re.search(r"(?:codex-feature-git|feature_git)\.py", command):
        return command, cwd
    args = shell_words(join_continuations(command))
    literal = (
        not any(arg and set(arg) <= SHELL_PUNCTUATION for arg in args)
        and "$" not in command
        and "`" not in command
    )
    if literal and args and args[0] in ("cat", "rg", "head", "tail", "echo"):
        return command, cwd
    if not literal or args[:3] != ["python3", "-I", str(FEATURE_HELPER)]:
        raise ValueError("Use one literal installed feature-git helper command.")
    tail = args[3:]
    if tail == ["--help"]:
        return "", cwd
    if len(tail) < 3 or tail[0] != "--worktree":
        raise ValueError("The feature-git helper requires --worktree <path> push.")
    if tail[2:] == ["push", "--help"] or tail[2:] == ["commit", "--help"]:
        return "", cwd
    if len(tail) == 5 and tail[2:4] == ["commit", "--message-file"]:
        return "", cwd
    if tail[2:] != ["push"]:
        raise ValueError("The feature-git push command cannot be checked.")
    worktree = (cwd / tail[1]).resolve(strict=True)
    try:
        branch = subprocess.run(
            [
                "/usr/bin/git",
                "-C",
                str(worktree),
                "symbolic-ref",
                "--quiet",
                "--short",
                "HEAD",
            ],
            env={
                key: value
                for key, value in os.environ.items()
                if not key.startswith("GIT_")
            },
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout.strip()
    except subprocess.SubprocessError as error:
        raise ValueError(
            f"Cannot resolve the feature-git push branch: {error}"
        ) from error
    if not branch:
        raise ValueError("The feature-git push branch is empty.")
    return shlex.join(["git", "push", "origin", f"HEAD:refs/heads/{branch}"]), worktree


def runner_write_check(tool: str, tool_input: dict[str, Any], cwd: Path) -> None:
    """Use the runner's trusted checker immediately before a GitHub write."""
    if not os.environ.get("EPIC_ACTION_FILE"):
        return
    shared = os.environ.get("EPIC_SHARED_READER") == "1"
    action: dict[str, Any] = {}
    action_error: OSError | ValueError | None = None
    try:
        action = json.loads(Path(os.environ["EPIC_ACTION_FILE"]).read_text())
        if not isinstance(action, dict):
            raise ValueError("The selected runner action must be an object.")
    except (OSError, ValueError) as error:
        # Read-only calls need no action file; refuse writes below.
        action = {}
        action_error = error
    issue_action = (
        action.get("action") in ("claim", "continue")
        and bool(action.get("issue"))
        and action.get("pr") is None
    )
    if action_error is None and not shared and not issue_action:
        return
    root = Path(
        os.environ.get("EPIC_TRUSTED_ROOT") or Path(__file__).resolve().parents[3]
    )
    sys.path.insert(0, str(root / "scripts" / "epic"))
    try:
        if not (root / "scripts/epic/write_checks.py").is_file():
            raise ValueError("write checker is missing from the trusted checkout")
        checks = importlib.import_module("write_checks")
        if (
            Path(checks.__file__).resolve()
            != (root / "scripts/epic/write_checks.py").resolve()
        ):
            raise ValueError("write checker did not load from the trusted checkout")
    except Exception as error:  # Import failures must block the proposed write.
        raise ValueError(f"Runner write checks are unavailable: {error}") from error

    if tool in ("exec_command", "shell_command"):
        tool = "Bash"
        tool_input = {
            **tool_input,
            "command": tool_input.get("command", tool_input.get("cmd", "")),
        }
    if tool == "Bash":
        command, cwd = helper_push_command(tool_input.get("command", ""), cwd)
        tool_input = {**tool_input, "command": command}
    try:
        writes = checks.tool_writes(tool, tool_input, str(cwd))
    except Exception as error:  # Unclear commands must block with the hook protocol.
        raise ValueError(f"Runner write check failed: {error}") from error
    if not writes:
        return
    if action_error is not None:
        raise ValueError(
            f"Selected runner action is unavailable: {action_error}"
        ) from action_error
    if issue_action:
        try:
            assignment_path = root / ".codex/assignment.py"
            if not assignment_path.is_file():
                raise ValueError(
                    "assignment reader is missing from the trusted checkout"
                )
            sys.path.insert(0, str(root / ".codex"))
            assignment = importlib.import_module("assignment")
            if Path(assignment.__file__).resolve() != assignment_path.resolve():
                raise ValueError(
                    "assignment reader did not load from the trusted checkout"
                )
        except Exception as error:
            raise ValueError(f"Runner write checks are unavailable: {error}") from error
    try:
        if shared:
            kwargs = {"reader": assignment.make_reader} if issue_action else {}
            refused = checks.guard(tool, tool_input, str(cwd), agent="Codex", **kwargs)
        else:
            # The shared-reader switch must stay off for the rest of the runner.
            ctx = checks.Context(action, assignment.make_reader, agent="Codex")
            refused = None
            for write in writes:
                refused = checks.check_write(ctx, write)
                if refused:
                    break
    except Exception as error:  # Malformed fresh data must also fail closed.
        raise ValueError(f"Runner write check failed: {error}") from error
    if refused:
        raise ValueError(
            f"Runner write check: {refused}. Stop this action; the next tick selects again."
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
        cwd = Path(tool_input.get("workdir") or payload.get("cwd") or Path.cwd())
        if payload.get("tool_name") in ("Bash", "exec_command", "shell_command"):
            if not isinstance(command, str):
                raise ValueError("The shell command must be a string.")
            check_command(command, cwd)
        runner_write_check(tool_name, tool_input, cwd)
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
