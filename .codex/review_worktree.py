"""Prepare detached reviews, preserve evidence and remove only checked worktrees."""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid
from typing import Any

from feature_git import Refused, SHA, common_dir, git, write_json
import epic_lock
import feature_worktree

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/epic"))
import github_quota  # noqa: E402
import github_state  # noqa: E402
import target_lock  # noqa: E402

REPO = feature_worktree.REPO
TEMP_ROOT = Path("/private/tmp")
LEGACY = re.compile(
    r"(?:moafunk-review-|codex-review-|moafunk-codex-review-|live-moafunk-review-)([1-9][0-9]*)(?:-[A-Za-z0-9-]+)?"
)


class ExistingBundle(Refused):
    """Completed evidence must not be replaced by another model run."""


def pull(number: int) -> dict[str, Any]:
    feature_worktree.check_quota()
    try:
        value = json.loads(github_quota.run_gh(["api", f"repos/{REPO}/pulls/{number}"]))
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        raise github_state.ReadBlocked(f"GitHub PR read failed: {error}") from error
    if not isinstance(value, dict) or value.get("state") not in ("open", "closed"):
        raise github_state.ReadBlocked("malformed GitHub PR metadata")
    return value


def metadata(action: dict[str, Any]) -> dict[str, Any]:
    number, sha = action.get("pr"), action.get("sha")
    if (
        action.get("action") != "review"
        or type(number) is not int
        or number <= 0
        or not isinstance(sha, str)
        or not SHA.fullmatch(sha)
    ):
        raise Refused("review requires a positive PR number and full head SHA")
    pr = pull(number)
    if (
        "body" not in pr
        or not isinstance(pr["body"], (str, type(None)))
        or type(pr.get("draft")) is not bool
    ):
        raise github_state.ReadBlocked("malformed PR body or draft state")
    body = pr.get("body") or ""
    owners = re.findall(
        r"^Executor:[ \t]*(.*?)[ \t]*$", "\n".join(body.splitlines()), re.M
    )
    if (
        pr.get("state") != "open"
        or owners != ["Claude"]
        or pr.get("draft") is not False
    ):
        raise Refused("review PR must be open, ready and assigned to Claude")
    for part in ("head", "base"):
        branch = pr.get(part)
        if (
            not isinstance(branch, dict)
            or not isinstance(branch.get("repo"), dict)
            or not isinstance(branch["repo"].get("full_name"), str)
            or not isinstance(branch.get("ref"), str)
            or not isinstance(branch.get("sha"), str)
            or not SHA.fullmatch(branch["sha"])
        ):
            raise github_state.ReadBlocked("malformed PR branch metadata")
        if branch["repo"]["full_name"] != REPO:
            raise Refused("review branches must belong to the expected repository")
    if pr["head"]["sha"] != sha or pr["base"]["ref"] not in feature_worktree.BASES:
        raise Refused("review head changed or base is forbidden")
    if (
        not isinstance(pr.get("title"), str)
        or not isinstance(pr.get("labels"), list)
        or any(
            not isinstance(label, dict) or not isinstance(label.get("name"), str)
            for label in pr["labels"]
        )
    ):
        raise github_state.ReadBlocked("malformed PR review inputs")
    return {
        "version": 1,
        "repo": REPO,
        "reviewer": "Codex",
        "pr": number,
        "sha": sha,
        "head": {key: pr["head"][key] for key in ("ref", "sha")},
        "base": {key: pr["base"][key] for key in ("ref", "sha")},
        "inputs": {
            "title": pr["title"],
            "body": pr.get("body") or "",
            "draft": pr["draft"],
            "labels": sorted(label["name"] for label in pr["labels"]),
        },
    }


def registrations(runner: Path) -> dict[Path, dict[str, str]]:
    result = {}
    for entry in git(runner, "worktree", "list", "--porcelain", "-z").split("\0\0"):
        fields = {
            field.partition(" ")[0]: field.partition(" ")[2]
            for field in entry.split("\0")
            if field
        }
        if "worktree" in fields:
            result[Path(fields["worktree"])] = fields
    return result


def validate_checkout(runner: Path, path: Path, sha: str) -> None:
    if path.parent != TEMP_ROOT or "review" not in path.name or path.resolve() != path:
        raise Refused(
            "review path must be directly under /private/tmp without symlinks"
        )
    record = registrations(runner).get(path)
    if (
        not record
        or "locked" in record
        or "prunable" in record
        or "detached" not in record
    ):
        raise Refused("review checkout must be registered, detached and unlocked")
    if (
        common_dir(path) != common_dir(runner)
        or Path(git(path, "rev-parse", "--show-toplevel")).resolve() != path
    ):
        raise Refused(
            "review checkout belongs to another repository or is not its root"
        )
    if git(path, "branch", "--show-current") or git(path, "rev-parse", "HEAD") != sha:
        raise Refused(
            "review checkout HEAD does not match the selected detached commit"
        )
    if git(path, "status", "--porcelain", "--untracked-files=all"):
        raise Refused("review checkout contains tracked or untracked changes")


def retained_ref(number: int, sha: str) -> str:
    return f"refs/remotes/codex-review/{number}/{sha}"


def retain(runner: Path, ref: str, sha: str) -> None:
    current = git(
        runner, "for-each-ref", "--format=%(refname) %(objectname) %(symref)", ref
    )
    if current:
        if current != f"{ref} {sha}":
            raise Refused("retained review ref already points at another commit")
    else:
        git(runner, "update-ref", "--no-deref", ref, sha, "0" * 40)


def artifact_directory(runner: Path, state: Path, number: int, sha: str) -> Path:
    state = state.absolute()
    artifact = state / "reviews" / str(number) / sha
    if artifact.resolve() != artifact:
        raise Refused("review evidence path must not contain symlinks")
    if any(
        artifact.is_relative_to(root)
        for root in (*registrations(runner), common_dir(runner))
    ):
        raise Refused("review evidence must live outside all repository checkouts")
    return artifact


def prepare(
    runner: Path, action: dict[str, Any], state: Path, context_file: Path
) -> Path:
    feature_worktree.validate_repository(runner)
    snapshot = metadata(action)
    number, sha = snapshot["pr"], snapshot["sha"]
    path = TEMP_ROOT / f"moafunk-review-{number}-{sha}"
    artifact = artifact_directory(runner, state, number, sha)
    artifact.mkdir(parents=True, exist_ok=True)
    attempt = artifact / "attempts" / uuid.uuid4().hex
    if attempt.resolve() != attempt:
        raise Refused("review attempt path must not contain symlinks")
    attempt.mkdir(parents=True)
    ref = retained_ref(number, sha)
    context = {
        **snapshot,
        "runner": str(runner),
        "worktree": str(path),
        "artifact_dir": str(artifact),
        "attempt_dir": str(attempt),
        "ref": ref,
    }
    write_json(context_file, context)
    bundle_path = artifact / "bundle.json"
    if bundle_path.exists():
        previous = json.loads(bundle_path.read_text())
        if not isinstance(previous, dict):
            raise Refused(f"malformed review bundle retained at {bundle_path}")
        if previous.get("status") in {"complete", "published"}:
            raise ExistingBundle(
                f"{previous['status']} review bundle retained at {bundle_path}; publication retry is separate"
            )
        if previous.get("status") != "draft" or any(
            previous.get(key) != value for key, value in snapshot.items()
        ):
            raise ExistingBundle(
                f"stale draft bundle retained at {bundle_path}; revalidation is separate"
            )
    if path.exists() or path.is_symlink() or path in registrations(runner):
        validate_checkout(runner, path, sha)
    else:
        git(
            runner,
            "fetch",
            "--no-tags",
            "--no-prune",
            "--no-recurse-submodules",
            "--refmap=",
            "origin",
            sha,
        )
        if git(runner, "rev-parse", "FETCH_HEAD^{commit}") != sha:
            raise Refused("fetched review commit does not match the selected head")
    retain(runner, ref, sha)
    if not path.exists():
        git(runner, "worktree", "add", "--detach", str(path), sha)
    validate_checkout(runner, path, sha)
    write_json(artifact / "context.json", context)
    if not bundle_path.exists():
        write_json(
            bundle_path,
            {
                **snapshot,
                "status": "draft",
                "findings": [],
                "verdict": None,
                "comments": [],
            },
        )
    return path


def load_context(context_file: Path) -> dict[str, Any]:
    context = json.loads(context_file.read_text())
    if (
        not isinstance(context, dict)
        or context.get("version") != 1
        or context.get("repo") != REPO
        or context.get("reviewer") != "Codex"
        or type(context.get("pr")) is not int
        or context["pr"] <= 0
        or not isinstance(context.get("sha"), str)
        or not SHA.fullmatch(context["sha"])
    ):
        raise Refused("invalid review context")
    sha, number = context["sha"], context["pr"]
    if (
        context.get("worktree") != str(TEMP_ROOT / f"moafunk-review-{number}-{sha}")
        or context.get("ref") != retained_ref(number, sha)
        or Path(context["artifact_dir"]).parts[-3:] != ("reviews", str(number), sha)
    ):
        raise Refused("review context identity does not match its evidence or ref")
    return context


def save_bundle(context_file: Path, bundle_file: Path) -> None:
    context = load_context(context_file)
    path = Path(context["artifact_dir"]) / "bundle.json"
    previous, bundle = json.loads(path.read_text()), json.loads(bundle_file.read_text())
    if not isinstance(bundle, dict) or any(
        bundle.get(key) != context.get(key)
        for key in (
            "version",
            "repo",
            "reviewer",
            "pr",
            "sha",
            "head",
            "base",
            "inputs",
        )
    ):
        raise Refused("bundle must match the reviewed PR, head, base and inputs")
    if (
        bundle.get("status") not in {"complete", "published"}
        or bundle.get("verdict") not in {"APPROVED", "CHANGES REQUESTED"}
        or not isinstance(bundle.get("findings"), list)
    ):
        raise Refused("bundle requires completed findings and an explicit verdict")
    comments = bundle.get("comments")
    if (
        not isinstance(comments, list)
        or not comments
        or any(
            not isinstance(comment, dict)
            or set(comment) != {"body", "url"}
            or not isinstance(comment["body"], str)
            or not comment["body"]
            or (
                comment["url"] is not None
                and (
                    not isinstance(comment["url"], str)
                    or not re.fullmatch(
                        rf"https://github\.com/{re.escape(REPO)}/(?:issues|pull)/{context['pr']}#issuecomment-[0-9]+",
                        comment["url"],
                    )
                )
            )
            for comment in comments
        )
    ):
        raise Refused(
            "bundle requires ordered comments with bodies and GitHub URLs or null"
        )
    expected = f"Review: {bundle['verdict']} by Codex at {context['sha']}"
    if comments[-1]["body"] != expected or any(
        comment["body"].startswith("Review:") for comment in comments[:-1]
    ):
        raise Refused("the final comment must be the standalone verdict")
    if bundle["status"] == "published" and any(
        comment["url"] is None for comment in comments
    ):
        raise Refused("published bundle requires every comment URL")
    if previous.get("status") in {"complete", "published"}:
        immutable = set(previous) - {"status", "comments"}
        if (
            any(bundle.get(key) != previous[key] for key in immutable)
            or len(previous["comments"]) != len(comments)
            or any(
                old["body"] != new["body"]
                or (old["url"] is not None and old["url"] != new["url"])
                for old, new in zip(previous["comments"], comments)
            )
            or (previous["status"] == "published" and bundle["status"] != "published")
        ):
            raise Refused("completed review evidence cannot be overwritten")
    write_json(path, bundle)


def cleanup(runner: Path, context: dict[str, Any]) -> None:
    logging.info("review: cleanup checkout %s", context.get("worktree"))
    feature_worktree.validate_repository(runner)
    if context.get("runner") != str(runner):
        raise Refused("review context belongs to another runner")
    path, sha, ref = Path(context["worktree"]), context["sha"], context["ref"]
    validate_checkout(runner, path, sha)
    retain(runner, ref, sha)
    helper = Path.home() / ".local/libexec/codex-cleanup-git.py"
    errors = []
    try:
        result = subprocess.run(
            [
                "python3",
                "-I",
                str(helper),
                "--worktree",
                str(runner),
                "remove-worktree",
                str(path),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            errors.append(
                result.stderr.strip()
                or result.stdout.strip()
                or "installed cleanup helper failed"
            )
    except (OSError, subprocess.SubprocessError) as error:
        errors.append(str(error))
    try:
        git(runner, "worktree", "prune")
    except Refused as error:
        errors.append(f"worktree prune failed: {error}")
    if errors:
        raise Refused("; ".join(errors))
    if path.exists() or path in registrations(runner):
        raise Refused("cleanup helper did not remove the registered checkout")
    bundle = Path(context["artifact_dir"]) / "bundle.json"
    if bundle.exists() and json.loads(bundle.read_text()).get("status") != "published":
        logging.info("review: removed %s; retained %s for pending evidence", path, ref)
    else:
        git(runner, "update-ref", "--no-deref", "-d", ref, sha)
        logging.info("review: removed %s and retained ref %s", path, ref)


def list_orphan_refs(
    runner: Path, known: dict[Path, dict[str, str]], pulls: dict[int, dict[str, Any]]
) -> None:
    """Report retained evidence without treating its ref as deletion authority."""
    attached = {
        (int(match[1]), entry.get("HEAD"))
        for path, entry in known.items()
        if path.parent == TEMP_ROOT and (match := LEGACY.fullmatch(path.name))
    }
    records = git(
        runner,
        "for-each-ref",
        "--format=%(refname) %(objectname) %(objecttype) %(symref)",
        "refs/remotes/codex-review",
    )
    for record in records.splitlines():
        fields = record.split()
        ref = fields[0]
        match = re.fullmatch(
            r"refs/remotes/codex-review/([1-9][0-9]*)/([0-9a-f]{40})", ref
        )
        if (
            len(fields) != 3
            or not match
            or fields[1] != match[2]
            or fields[2] != "commit"
        ):
            logging.warning(
                "review: retained %s: malformed, mismatched or symbolic ref; manual review required",
                ref,
            )
            continue
        number = int(match[1])
        if (number, fields[1]) in attached:
            continue
        try:
            if number not in pulls:
                pulls[number] = pull(number)
            if pulls[number].get("state") == "closed":
                logging.info(
                    "review: retained %s: closed PR %s, no registered checkout; preserve for pending evidence or manual review",
                    ref,
                    number,
                )
            else:
                logging.info(
                    "review: retained %s: PR is open or its closed state is unknown",
                    ref,
                )
        except feature_worktree.QuotaWait:
            raise
        except (Refused, OSError, ValueError, subprocess.SubprocessError) as error:
            logging.warning("review: retained %s: %s", ref, error)


def sweep(runner: Path, state: Path, apply: bool) -> None:
    feature_worktree.validate_repository(runner)
    state.mkdir(parents=True, exist_ok=True)
    lock = state / "codex.lock"
    if not epic_lock.acquire(lock, os.getpid(), 300):
        raise Refused("runner is active; sweep refused")
    try:
        known = registrations(runner)
        pulls: dict[int, dict[str, Any]] = {}
        list_orphan_refs(runner, known, pulls)
        candidates = set(known) | set(TEMP_ROOT.glob("*review*"))
        for path in sorted(candidates):
            if path.parent != TEMP_ROOT or "review" not in path.name:
                continue
            match = LEGACY.fullmatch(path.name)
            if not match or path not in known:
                logging.info("review: retained %s: unknown name or unregistered", path)
                continue
            number = int(match[1])
            try:
                with ExitStack() as stack:
                    locks = [
                        stack.enter_context(p.open("a"))
                        for p in target_lock.paths(
                            {"pr": number}, target_lock.lock_dir()
                        )
                    ]
                    if not target_lock.acquire([f.fileno() for f in locks]):
                        raise Refused("PR is locked by an active runner")
                    sha = known[path].get("HEAD", "")
                    if not SHA.fullmatch(sha):
                        raise Refused("invalid registered head")
                    canonical = re.fullmatch(
                        r"moafunk-review-[1-9][0-9]*-([0-9a-f]{40})", path.name
                    )
                    if canonical and canonical[1] != sha:
                        raise Refused(
                            "review path head does not match its registered head"
                        )
                    validate_checkout(runner, path, sha)
                    # A listing read may precede this target lock by many PRs.
                    pr = pull(number)
                    if pr.get("state") != "closed":
                        raise Refused("PR is open or its closed state is unknown")
                    if not apply:
                        logging.info(
                            "review: eligible %s (closed PR %s); use --apply",
                            path,
                            number,
                        )
                        continue
                    context = {
                        "runner": str(runner),
                        "worktree": str(path),
                        "sha": sha,
                        "ref": retained_ref(number, sha),
                        "artifact_dir": str(
                            artifact_directory(runner, state, number, sha)
                        ),
                    }
                    cleanup(runner, context)
            except feature_worktree.QuotaWait:
                raise
            except (Refused, OSError, ValueError, subprocess.SubprocessError) as error:
                logging.warning("review: retained %s: %s", path, error)
    finally:
        (lock / "owner.json").unlink()
        lock.rmdir()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "cleanup", "sweep", "save-bundle"):
        command = commands.add_parser(name, allow_abbrev=False)
        if name != "save-bundle":
            command.add_argument("--runner", required=True, type=Path)
        if name in {"prepare", "sweep"}:
            command.add_argument("--state-dir", required=True, type=Path)
        if name != "sweep":
            command.add_argument("--context-file", required=True, type=Path)
        if name == "prepare":
            command.add_argument("--action-file", required=True, type=Path)
        if name == "save-bundle":
            command.add_argument("--bundle-file", required=True, type=Path)
        if name == "sweep":
            command.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    retained_path = "unknown review path"
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        if args.command == "save-bundle":
            save_bundle(args.context_file, args.bundle_file)
        else:
            runner = args.runner.resolve(strict=True)
            if args.command == "prepare":
                print(
                    prepare(
                        runner,
                        json.loads(args.action_file.read_text()),
                        args.state_dir,
                        args.context_file,
                    )
                )
            elif args.command == "cleanup":
                context = load_context(args.context_file)
                retained_path = context["worktree"]
                cleanup(runner, context)
            else:
                sweep(runner, args.state_dir, args.apply)
        return 0
    except ExistingBundle as error:
        logging.warning("review: %s", error)
        return 3
    except feature_worktree.QuotaWait as error:
        logging.warning("review: %s", error)
        return 4
    except github_state.ReadBlocked as error:
        logging.warning("review: GitHub read blocked: %s", error)
        return 5
    except github_quota.QuotaExhausted as error:
        return github_quota.stop_on_quota(error)
    except (
        Refused,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
    ) as error:
        logging.warning(
            "review: retained %s; evidence preserved: %s", retained_path, error
        )
        return 7


if __name__ == "__main__":
    raise SystemExit(main())
