"""Resume ordered review comments from completed evidence without another model."""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any

from feature_git import Refused, SHA, write_json
import review_worktree as review

github_quota = review.github_quota
github_state = review.github_state
feature_worktree = review.feature_worktree
target_lock = review.target_lock

IDENTITY = ("version", "repo", "reviewer", "pr", "sha", "head", "base", "inputs")
VERDICT = re.compile(r"Review: (APPROVED|CHANGES REQUESTED) by Codex at ([0-9a-f]{40})")


class FreshReview(Refused):
    """No reusable completed evidence exists for the current reviewed inputs."""


class NoCompletedReview(FreshReview):
    """The model has not saved completed analysis; preserve its retry cooldown."""


def local_gate(number: int) -> None:
    """Check local stops and the inherited PR lock immediately before a write."""
    if (Path.home() / ".epic-pause").exists():
        raise Refused("review publication paused")
    feature_worktree.check_quota()
    path = target_lock.lock_dir() / f"{number}.lock"
    try:
        expected = path.stat()
    except FileNotFoundError as error:
        raise Refused("review publication requires the runner's target lock") from error
    for fd in (8, 9):
        try:
            actual = os.fstat(fd)
        except OSError:
            continue
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise Refused("review target is locked by another runner") from error
        return
    raise Refused("review publication requires the matching inherited target lock")


def api(args: list[str]) -> str:
    """REST failures stop delivery; an uncertain write is reconciled next tick."""
    feature_worktree.check_quota()
    try:
        return github_quota.run_gh(["api", *args])
    except (subprocess.CalledProcessError, github_quota.QuotaExhausted) as error:
        message = (
            (error.stderr or "") + (error.stdout or "")
            if isinstance(error, subprocess.CalledProcessError)
            else str(error)
        )
        if (
            isinstance(error, github_quota.QuotaExhausted)
            or github_quota.QUOTA_TEXT.search(message)
            or "secondary rate limit" in message.lower()
        ):
            wait = github_quota.record(
                github_quota.STATE_DIR, time.time(), lookup=lambda: None
            )
            raise feature_worktree.QuotaWait(
                f"REST rate limit; retry at {wait['retry_at']}"
            ) from error
        raise github_state.ReadBlocked(
            f"GitHub comment request failed: {error}"
        ) from error
    except (OSError, subprocess.TimeoutExpired) as error:
        raise github_state.ReadBlocked(
            f"GitHub comment request failed: {error}"
        ) from error


def comments(number: int) -> list[dict[str, Any]]:
    """Read all REST pages; incomplete or malformed history cannot permit POST."""
    try:
        pages = json.loads(
            api(
                [
                    f"repos/{review.REPO}/issues/{number}/comments?per_page=100",
                    "--paginate",
                    "--slurp",
                ]
            )
        )
        if (
            not isinstance(pages, list)
            or not pages
            or any(not isinstance(page, list) for page in pages)
        ):
            raise ValueError("invalid comment pages")
        result = [comment for page in pages for comment in page]
        for comment in result:
            if (
                not isinstance(comment, dict)
                or type(comment.get("id")) is not int
                or comment["id"] <= 0
                or not isinstance(comment.get("body"), str)
                or not isinstance(comment.get("html_url"), str)
                or not isinstance(comment.get("user"), dict)
                or not isinstance(comment["user"].get("login"), str)
                or not re.fullmatch(
                    rf"https://github\.com/{re.escape(review.REPO)}/(?:issues|pull)/{number}#issuecomment-{comment['id']}",
                    comment["html_url"],
                )
            ):
                raise ValueError("invalid comment identity")
            for key in ("created_at", "updated_at"):
                github_quota.parse_iso(comment[key])
        if len({comment["id"] for comment in result}) != len(result):
            raise ValueError("duplicate comment IDs in history")
        return result
    except Refused:
        raise
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise github_state.ReadBlocked(f"malformed GitHub comments: {error}") from error


def reconcile(
    bundle: dict[str, Any], remote: list[dict[str, Any]], trusted: set[str]
) -> bool:
    """Confirm exact bodies and URLs, refusing newer opposite review results."""
    started = github_quota.parse_iso(bundle["review_started_at"])
    remote = [comment for comment in remote if comment["user"]["login"] in trusted]
    for comment in remote:
        match = VERDICT.fullmatch(comment["body"])
        if (
            match
            and match[2] == bundle["sha"]
            and match[1] != bundle["verdict"]
            and github_quota.parse_iso(comment["updated_at"]) >= started
        ):
            raise Refused(
                "a newer conflicting Codex verdict blocks this pending bundle"
            )
    changed = False
    for index, item in enumerate(bundle["comments"]):
        matches = [comment for comment in remote if comment["body"] == item["body"]]
        if index == len(bundle["comments"]) - 1:
            matches = [
                comment
                for comment in matches
                if comment["created_at"] == comment["updated_at"]
                and github_quota.parse_iso(comment["created_at"]) >= started
            ]
        # Fresh analysis may repeat an existing finding after a base change.
        # Only the verdict must come from this review's publication window.
        if item["url"] is not None:
            if not any(comment["html_url"] == item["url"] for comment in matches):
                raise Refused(
                    "a previously confirmed review comment is missing or edited"
                )
        elif matches:
            item["url"] = min(matches, key=lambda comment: comment["id"])["html_url"]
            changed = True
    if bundle["comments"][-1]["url"] and any(
        item["url"] is None for item in bundle["comments"][:-1]
    ):
        raise Refused("remote verdict exists without all saved findings")
    return changed


def trusted_reviewers(runner: Path) -> set[str]:
    config = json.loads((runner / ".github/epic-lanes.yml").read_text())
    if not isinstance(config, dict) or not isinstance(
        config.get("trusted_reviewers"), dict
    ):
        raise Refused("runner has no valid trusted Codex reviewer policy")
    logins = config["trusted_reviewers"].get("Codex")
    if (
        config.get("repository") != review.REPO
        or not isinstance(logins, list)
        or not logins
        or any(not isinstance(login, str) or not login for login in logins)
    ):
        raise Refused("runner has no valid trusted Codex reviewer policy")
    return set(logins)


def check_formal_reviews(bundle: dict[str, Any], trusted: set[str]) -> None:
    try:
        pages = json.loads(
            api(
                [
                    f"repos/{review.REPO}/pulls/{bundle['pr']}/reviews?per_page=100",
                    "--paginate",
                    "--slurp",
                ]
            )
        )
        if (
            not isinstance(pages, list)
            or not pages
            or any(not isinstance(page, list) for page in pages)
        ):
            raise ValueError("invalid review pages")
        for item in (item for page in pages for item in page):
            if (
                not isinstance(item, dict)
                or item.get("state")
                not in {
                    "APPROVED",
                    "CHANGES_REQUESTED",
                    "COMMENTED",
                    "DISMISSED",
                    "PENDING",
                }
                or not isinstance(item.get("user"), dict)
                or not isinstance(item["user"].get("login"), str)
                or not isinstance(item.get("commit_id"), str)
                or not SHA.fullmatch(item["commit_id"])
            ):
                raise ValueError("invalid formal review")
            if item["state"] == "PENDING":
                continue
            submitted = github_quota.parse_iso(item["submitted_at"])
            verdict = item["state"].replace("_", " ")
            if (
                verdict in {"APPROVED", "CHANGES REQUESTED"}
                and verdict != bundle["verdict"]
                and item["commit_id"] == bundle["sha"]
                and item["user"]["login"] in trusted
                and submitted >= github_quota.parse_iso(bundle["review_started_at"])
            ):
                raise Refused(
                    "a newer conflicting GitHub review blocks this pending bundle"
                )
    except Refused:
        raise
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise github_state.ReadBlocked(f"malformed GitHub reviews: {error}") from error


def current(bundle: dict[str, Any]) -> None:
    local_gate(bundle["pr"])
    try:
        snapshot = review.metadata(
            {"action": "review", "pr": bundle["pr"], "sha": bundle["sha"]}
        )
    except Refused as error:
        if str(error) == "review head changed or base is forbidden":
            raise FreshReview(str(error)) from error
        raise
    if any(bundle.get(key) != snapshot.get(key) for key in IDENTITY):
        raise FreshReview(
            "reviewed head, base or metadata changed; fresh review required"
        )


def deliver(context_file: Path) -> None:
    context = review.load_context(context_file)
    runner = Path(context["runner"]).resolve(strict=True)
    feature_worktree.validate_repository(runner)
    artifact = Path(context["artifact_dir"])
    expected = review.artifact_directory(
        runner, artifact.parents[2], context["pr"], context["sha"]
    )
    if artifact != expected or context_file.resolve() != artifact / "context.json":
        raise Refused("delivery context must be the retained artifact context")
    path = artifact / "bundle.json"
    if path.is_symlink():
        raise Refused("review bundle must not be a symlink")
    bundle = json.loads(path.read_text())
    if isinstance(bundle, dict) and bundle.get("status") == "draft":
        raise NoCompletedReview("model did not save a completed review bundle")
    if not isinstance(bundle, dict) or bundle.get("status") not in {
        "complete",
        "published",
    }:
        raise Refused("invalid review bundle status")
    if not bundle.get("review_started_at"):
        raise FreshReview("legacy review evidence has no conflict baseline")
    review.validate_bundle(context, bundle)
    if github_quota.parse_iso(bundle["review_started_at"]) > time.time():
        raise Refused("review conflict baseline is in the future")
    trusted = trusted_reviewers(runner)
    # Save each request before sending it. An uncertain outcome remains blocked
    # across restarts until a fresh read confirms it, even if visibility is late.
    for _ in range(len(bundle["comments"]) + 1):
        current(bundle)
        remote = comments(bundle["pr"])
        check_formal_reviews(bundle, trusted)
        changed = reconcile(bundle, remote, trusted)
        if "pending_comment" in bundle:
            pending = bundle["pending_comment"]
            if bundle["comments"][pending]["url"] is not None:
                del bundle["pending_comment"]
                changed = True
        if changed:
            write_json(path, bundle)
        if "pending_comment" in bundle:
            raise Refused(
                "GitHub did not confirm the pending review comment; request retained without retry"
            )
        missing = next(
            (item for item in bundle["comments"] if item["url"] is None), None
        )
        if missing is None:
            current(bundle)
            local_gate(bundle["pr"])
            bundle["status"] = "published"
            write_json(path, bundle)
            logging.info(
                "review: confirmed findings and current-head Codex verdict for PR %s",
                bundle["pr"],
            )
            return
        current(bundle)
        local_gate(bundle["pr"])
        bundle["pending_comment"] = bundle["comments"].index(missing)
        write_json(path, bundle)
        try:
            local_gate(bundle["pr"])
        except (Refused, OSError, ValueError):
            # No request was sent when the final local check stopped us.
            del bundle["pending_comment"]
            write_json(path, bundle)
            raise
        try:
            api(
                [
                    f"repos/{review.REPO}/issues/{bundle['pr']}/comments",
                    "--method",
                    "POST",
                    "--raw-field",
                    f"body={missing['body']}",
                ]
            )
        except feature_worktree.QuotaWait:
            # A local wait or explicit rate-limit rejection proves no POST
            # was accepted. Transport failures retain the unresolved request.
            del bundle["pending_comment"]
            write_json(path, bundle)
            raise
    raise github_state.ReadBlocked("GitHub did not confirm the posted review comment")


def resume(runner: Path, action: dict[str, Any], state: Path) -> None:
    if (
        not isinstance(action, dict)
        or action.get("action") != "review"
        or type(action.get("pr")) is not int
        or action["pr"] <= 0
        or not isinstance(action.get("sha"), str)
        or not SHA.fullmatch(action["sha"])
    ):
        raise Refused("delivery requires a review action with PR and full head SHA")
    feature_worktree.validate_repository(runner)
    artifact = review.artifact_directory(runner, state, action["pr"], action["sha"])
    if not (artifact / "bundle.json").exists():
        raise FreshReview("no saved review bundle")
    context_file = artifact / "context.json"
    if not context_file.exists():
        raise FreshReview("saved review has no completed context")
    context = review.load_context(context_file)
    if context.get("runner") != str(runner):
        raise Refused("saved review belongs to another runner")
    deliver(context_file)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("resume", allow_abbrev=False)
    command.add_argument("--runner", required=True, type=Path)
    command.add_argument("--action-file", required=True, type=Path)
    command.add_argument("--state-dir", required=True, type=Path)
    command = commands.add_parser("publish", allow_abbrev=False)
    command.add_argument("--context-file", required=True, type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        if args.command == "resume":
            resume(
                args.runner.resolve(strict=True),
                json.loads(args.action_file.read_text()),
                args.state_dir,
            )
        else:
            context = review.load_context(args.context_file)
            deliver(Path(context["artifact_dir"]) / "context.json")
        return 0
    except NoCompletedReview as error:
        logging.info("review: %s", error)
        return 3 if args.command == "resume" else 8
    except FreshReview as error:
        logging.info("review: %s", error)
        return 3 if args.command == "resume" else 7
    except feature_worktree.QuotaWait as error:
        logging.warning("review: pending delivery retained: %s", error)
        return 4
    except github_quota.QuotaExhausted as error:
        return github_quota.stop_on_quota(error)
    except github_state.ReadBlocked as error:
        logging.warning("review: pending delivery retained: %s", error)
        return 5
    except (
        Refused,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
    ) as error:
        logging.warning("review: pending delivery retained: %s", error)
        return 7


if __name__ == "__main__":
    sys.exit(main())
