"""Commit and publish only the current issue branch in a configured repository."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys


GIT = "/usr/bin/git"
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


def validate(worktree: Path, config_path: Path) -> str:
    config = json.loads(config_path.read_text())
    if not isinstance(config, dict) or set(config) != {
        "trusted_checkout",
        "allowed_origin_urls",
    }:
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
    if common_dir(worktree) != common_dir(Path(trusted)):
        raise Refused("worktree does not belong to the trusted repository")
    branch = git(worktree, "symbolic-ref", "--quiet", "--short", "HEAD")
    if not BRANCH.fullmatch(branch):
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--worktree", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commit = commands.add_parser("commit", allow_abbrev=False)
    commit.add_argument("--message-file", required=True, type=Path)
    commands.add_parser("push", allow_abbrev=False)
    args = parser.parse_args()
    try:
        worktree = args.worktree.resolve(strict=True)
        branch = validate(worktree, Path(__file__).resolve().with_suffix(".json"))
        if args.command == "commit":
            message = args.message_file.resolve(strict=True)
            if not message.is_file():
                raise Refused("message file must be a regular file")
            output = git(worktree, "commit", "--file", str(message))
        else:
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
    except (Refused, OSError, json.JSONDecodeError) as error:
        print(f"feature-git: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
