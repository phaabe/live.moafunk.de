"""Commit and publish only the current issue branch in a configured repository."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any


GIT = "/usr/bin/git"
BASES = {"dev/312-interim"}
SHA = re.compile(r"[0-9a-f]{40}")
BRANCH = re.compile(
    r"(?:feat|fix|chore|docs|refactor|test|perf|ci|build)/[1-9][0-9]*-[a-z0-9]+(?:-[a-z0-9]+)*"
)


class Refused(ValueError):
    """The requested operation is outside the installed policy."""


def git(worktree: Path, *args: str) -> str:
    # Git environment overrides can redirect the repository, index or configuration.
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    env.update(GIT_EDITOR="/usr/bin/true", GIT_SEQUENCE_EDITOR="/usr/bin/true")
    result = subprocess.run(
        [GIT, "-C", str(worktree), *args],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise Refused(result.stderr.strip() or f"git {args[0]} failed")
    return result.stdout.strip()


def common_dir(worktree: Path) -> Path:
    path = Path(git(worktree, "rev-parse", "--git-common-dir"))
    return (worktree / path).resolve()


def configuration(config_path: Path) -> dict[str, Any]:
    config = json.loads(config_path.read_text())
    required = {"trusted_checkout", "allowed_origin_urls"}
    if (
        not isinstance(config, dict)
        or not required <= set(config)
        or set(config) - required - {"runner_checkout", "context_file"}
    ):
        raise Refused("invalid installed configuration")
    trusted = config["trusted_checkout"]
    urls = config["allowed_origin_urls"]
    if (
        not isinstance(trusted, str)
        or not Path(trusted).is_absolute()
        or not isinstance(urls, list)
        or not urls
        or any(not isinstance(url, str) or not url for url in urls)
    ):
        raise Refused("invalid installed repository or origin URLs")
    return config


def validate(worktree: Path, config_path: Path, *, detached: bool = False) -> str:
    config = configuration(config_path)
    trusted, urls = config["trusted_checkout"], config["allowed_origin_urls"]
    if common_dir(worktree) != common_dir(Path(trusted)):
        raise Refused("worktree does not belong to the trusted repository")
    branch = git(worktree, "branch", "--show-current")
    if not (detached and not branch) and not BRANCH.fullmatch(branch):
        raise Refused("current branch must be type/issue-slug")

    # Read configured URLs as well as resolved URLs: insteadOf/pushInsteadOf
    # rewrites and multiple destinations must never change the approved target.
    entries = git(worktree, "config", "--get-regexp", r"^remote\.origin\.")
    settings: dict[str, list[str]] = {}
    for line in entries.splitlines():
        key, _, value = line.partition(" ")
        settings.setdefault(key.lower(), []).append(value)
    fetch = settings.get("remote.origin.url", [])
    push = settings.get("remote.origin.pushurl", fetch)
    if (
        len(fetch) != 1
        or len(push) != 1
        or fetch[0] not in urls
        or push[0] not in urls
        or git(worktree, "remote", "get-url", "--all", "origin") != fetch[0]
        or git(worktree, "remote", "get-url", "--push", "--all", "origin") != push[0]
    ):
        raise Refused("origin must have one approved URL without redirects")
    if "remote.origin.mirror" in settings:
        if git(worktree, "config", "--bool", "remote.origin.mirror") != "false":
            raise Refused("mirror pushes are forbidden")
    return branch


def write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as output:
            os.chmod(temporary, 0o600)
            json.dump(value, output)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def git_path(worktree: Path, name: str) -> Path:
    return Path(
        git(worktree, "rev-parse", "--path-format=absolute", "--git-path", name)
    )


def rebase_context(worktree: Path, config_path: Path) -> tuple[dict[str, Any], Path]:
    config = configuration(config_path)
    for field in ("runner_checkout", "context_file"):
        if (
            not isinstance(config.get(field), str)
            or not Path(config[field]).is_absolute()
        ):
            raise Refused("installed configuration lacks rebase runner context")
    runner = Path(config["runner_checkout"])
    context_path = Path(config["context_file"])
    root = runner.with_name(runner.name.removesuffix("-runner") + "-wt")
    if runner.resolve() != runner or context_path.resolve() != context_path:
        raise Refused("runner and context paths must not contain symlinks")
    for directory in (
        runner,
        root,
        common_dir(worktree),
        Path(config["trusted_checkout"]),
    ):
        if context_path.is_relative_to(directory):
            raise Refused("runner context must be outside repository writable roots")
    context = json.loads(context_path.read_text())
    fields = {
        "version",
        "action",
        "pr",
        "branch",
        "base",
        "expected_head",
        "worktree",
        "runner",
    }
    if not isinstance(context, dict) or set(context) != fields:
        raise Refused("invalid runner context")
    if (
        context["version"] != 1
        or context["action"] != "resolve-conflict"
        or type(context["pr"]) is not int
        or context["pr"] <= 0
        or not isinstance(context["branch"], str)
        or not BRANCH.fullmatch(context["branch"])
        or not isinstance(context["base"], str)
        or context["base"] not in BASES
        or not isinstance(context["expected_head"], str)
        or not SHA.fullmatch(context["expected_head"])
        or context["runner"] != str(runner)
        or context["worktree"] != str(worktree)
        or worktree != root / context["branch"]
        or common_dir(runner) != common_dir(worktree)
        or Path(git(worktree, "rev-parse", "--show-toplevel")).resolve() != worktree
    ):
        raise Refused("request does not match the runner's selected PR and worktree")
    registered = []
    for entry in git(runner, "worktree", "list", "--porcelain", "-z").split("\0\0"):
        fields = dict(
            field.split(" ", 1) for field in entry.split("\0") if " " in field
        )
        if fields.get("worktree") == str(worktree):
            registered.append(fields)
        elif fields.get("branch") == f"refs/heads/{context['branch']}":
            raise Refused("PR branch is held in another worktree")
    if len(registered) != 1:
        raise Refused("selected worktree is not registered")
    state_path = context_path.with_name(f"rebase-{context['pr']}.json")
    if state_path.is_symlink():
        raise Refused("rebase record must not be a symlink")
    return context, state_path


def load_rebase(path: Path, context: dict[str, Any]) -> dict[str, Any] | None:
    if path.is_symlink():
        raise Refused("rebase record must not be a symlink")
    if not path.exists():
        return None
    state = json.loads(path.read_text())
    if (
        not isinstance(state, dict)
        or set(state) != set(context) | {"original_head", "onto"}
        or any(state.get(key) != value for key, value in context.items())
        or any(
            not isinstance(state.get(key), str) or not SHA.fullmatch(state[key])
            for key in ("original_head", "onto")
        )
        or state["original_head"] != context["expected_head"]
    ):
        raise Refused(
            "rebase record does not match the selected PR; preserve it for manual recovery"
        )
    return state


def check_rebase(worktree: Path, state: dict[str, Any] | None) -> bool:
    for name in (
        "rebase-apply",
        "MERGE_HEAD",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
        "sequencer",
        "BISECT_START",
    ):
        if git_path(worktree, name).exists():
            raise Refused("another Git operation is in progress")
    directory = git_path(worktree, "rebase-merge")
    if not directory.exists():
        return False
    if state is None:
        raise Refused("Git rebase has no runner record")
    expected = {
        "head-name": f"refs/heads/{state['branch']}",
        "orig-head": state["original_head"],
        "onto": state["onto"],
    }
    if any(
        (directory / key).read_text().strip() != value
        for key, value in expected.items()
    ):
        raise Refused("Git rebase does not match the runner's recorded rebase")
    if git(worktree, "branch", "--show-current"):
        raise Refused("recorded active rebase must have detached HEAD")
    return True


def completed_rebase(worktree: Path, state: dict[str, Any]) -> None:
    if check_rebase(worktree, state):
        raise Refused("rebase is unfinished; resolve and continue or abort")
    if git(worktree, "branch", "--show-current") != state["branch"]:
        raise Refused("current branch does not match the recorded rebase")
    if git(worktree, "status", "--porcelain", "--untracked-files=all"):
        raise Refused("completed rebase must have a clean worktree")
    git(worktree, "merge-base", "--is-ancestor", state["onto"], "HEAD")


def remote_head(worktree: Path, branch: str) -> str:
    ref = f"refs/heads/{branch}"
    lines = git(worktree, "ls-remote", "--refs", "origin", ref).splitlines()
    if (
        len(lines) != 1
        or len(lines[0].split()) != 2
        or lines[0].split()[1] != ref
        or not SHA.fullmatch(lines[0].split()[0])
    ):
        raise Refused("origin PR branch is missing or ambiguous")
    return lines[0].split()[0]


def rebase_operation(
    worktree: Path, config_path: Path, args: argparse.Namespace
) -> str:
    context, state_path = rebase_context(worktree, config_path)
    # This directory is provisioned outside the model sandbox by the operator.
    with state_path.with_suffix(".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise Refused("another helper operation is active") from error
        state = load_rebase(state_path, context)
        branch = validate(worktree, config_path, detached=state is not None)
        if args.command == "rebase":
            if (
                args.base != context["base"]
                or args.expected_head != context["expected_head"]
            ):
                raise Refused("base and expected head must match the selected PR")
            if state is not None:
                raise Refused("recorded rebase exists; continue, abort or publish it")
            if (
                branch != context["branch"]
                or git(worktree, "rev-parse", "HEAD") != args.expected_head
            ):
                raise Refused("current branch or head does not match the selected PR")
            if git(worktree, "status", "--porcelain", "--untracked-files=all"):
                raise Refused("new rebase requires a clean worktree")
            if any(
                git_path(worktree, name).exists()
                for name in (
                    "rebase-merge",
                    "rebase-apply",
                    "MERGE_HEAD",
                    "CHERRY_PICK_HEAD",
                    "REVERT_HEAD",
                    "sequencer",
                    "BISECT_START",
                )
            ):
                raise Refused("another Git operation is in progress")
            if remote_head(worktree, branch) != args.expected_head:
                raise Refused("origin PR head changed; the pinned lease is stale")
            # Fetch only the actual allowed base; never refresh the PR lease.
            git(
                worktree,
                "fetch",
                "--no-tags",
                "--no-prune",
                "--no-prune-tags",
                "--no-recurse-submodules",
                "--refmap=",
                "origin",
                f"refs/heads/{args.base}",
            )
            onto = git(worktree, "rev-parse", "FETCH_HEAD^{commit}")
            state = {**context, "original_head": args.expected_head, "onto": onto}
            write_json(state_path, state)
            return git(
                worktree,
                "rebase",
                "--merge",
                "--no-autostash",
                "--no-autosquash",
                "--no-update-refs",
                "--no-rebase-merges",
                "--no-fork-point",
                onto,
            )
        if state is None:
            raise Refused("no runner-recorded rebase exists")
        active = check_rebase(worktree, state)
        if args.command == "rebase-abort":
            if not active:
                if (
                    branch != state["branch"]
                    or git(worktree, "rev-parse", "HEAD") != state["original_head"]
                ):
                    raise Refused("rebase already completed; cannot abort")
                if git(worktree, "status", "--porcelain", "--untracked-files=all"):
                    raise Refused(
                        "pending rebase has local changes; preserve for recovery"
                    )
                output = "Cleared rebase interrupted before Git started."
            else:
                output = git(worktree, "rebase", "--abort")
            state_path.unlink()
            return output
        if args.command == "rebase-continue":
            if not active:
                completed_rebase(worktree, state)
                return "Rebase already completed; publish with the original lease."
            return git(worktree, "rebase", "--continue")
        if args.expected_remote_sha != state["expected_head"]:
            raise Refused("lease must equal the original pinned PR head")
        completed_rebase(worktree, state)
        if remote_head(worktree, state["branch"]) != state["expected_head"]:
            raise Refused("origin PR head changed; the pinned lease is stale")
        output = git(
            worktree,
            "push",
            "--no-follow-tags",
            "--recurse-submodules=no",
            f"--force-with-lease=refs/heads/{state['branch']}:{state['expected_head']}",
            "origin",
            f"HEAD:refs/heads/{state['branch']}",
        )
        state_path.unlink()
        return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--worktree", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commit = commands.add_parser("commit", allow_abbrev=False)
    commit.add_argument("--message-file", required=True, type=Path)
    commands.add_parser("push", allow_abbrev=False)
    rebase = commands.add_parser("rebase", allow_abbrev=False)
    rebase.add_argument("--base", required=True)
    rebase.add_argument("--expected-head", required=True)
    commands.add_parser("rebase-continue", allow_abbrev=False)
    commands.add_parser("rebase-abort", allow_abbrev=False)
    lease = commands.add_parser("push-with-lease", allow_abbrev=False)
    lease.add_argument("--expected-remote-sha", required=True)
    args = parser.parse_args()
    try:
        worktree = args.worktree.resolve(strict=True)
        config_path = Path(__file__).resolve().with_suffix(".json")
        if args.command not in {"commit", "push"}:
            if args.worktree.absolute() != worktree:
                raise Refused("worktree must be an exact path without symlinks")
            output = rebase_operation(worktree, config_path, args)
        elif args.command == "commit":
            validate(worktree, config_path)
            message = args.message_file.resolve(strict=True)
            if not message.is_file():
                raise Refused("message file must be a regular file")
            output = git(worktree, "commit", "--file", str(message))
        else:
            branch = validate(worktree, config_path)
            config = configuration(config_path)
            if "context_file" in config:
                records = Path(config["context_file"]).parent.glob("rebase-*.json")
                for record in records:
                    if json.loads(record.read_text()).get("worktree") == str(worktree):
                        raise Refused("recorded rebase requires an explicit lease push")
            output = git(
                worktree,
                "push",
                "--no-follow-tags",
                "--recurse-submodules=no",
                "origin",
                f"HEAD:refs/heads/{branch}",
            )
        if output:
            print(output)
        return 0
    except (Refused, OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        print(f"feature-git: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
