"""Prepare only Codex's selected feature checkout before starting a model."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any

from feature_git import BRANCH, Refused, common_dir, git
import tick_backoff

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/epic"))
import github_quota  # noqa: E402
import next_action  # noqa: E402
import github_state  # noqa: E402
import tick_gate  # noqa: E402


EDIT_ACTIONS = {"claim", "continue", "fix", "fix-checks", "resolve-conflict"}
REPO = "phaabe/live.moafunk.de"
BASES = {"dev/312-interim", "dev/streaming-architecture"}
ORIGINS = {
    f"git@github.com:{REPO}.git",
    f"ssh://git@github.com/{REPO}.git",
    f"https://github.com/{REPO}.git",
    f"https://github.com/{REPO}",
}
ISSUE = re.compile(rf"https://github\.com/{re.escape(REPO)}/issues/([1-9][0-9]*)")


class QuotaWait(Refused):
    """Another runner has already stored a quota wait."""


def check_quota() -> None:
    status, retry = github_quota.check(github_quota.STATE_DIR, time.time())
    if status == github_quota.DEFERRED:
        raise QuotaWait(f"GitHub quota wait until {retry}")


def feature_root(runner: Path) -> Path:
    return runner.with_name(runner.name.removesuffix("-runner") + "-wt")


def validate_repository(runner: Path) -> None:
    if Path(git(runner, "rev-parse", "--show-toplevel")).resolve() != runner:
        raise Refused("runner must be a repository root")
    configured = git(runner, "config", "--get-all", "remote.origin.url")
    if (
        configured not in ORIGINS
        or git(runner, "remote", "get-url", "--all", "origin") != configured
    ):
        raise Refused("origin must be the expected repository without redirects")


def metadata(action: dict[str, Any]) -> tuple[str | None, str, int, str | None]:
    """Fresh ownership and branch metadata; never trust selector text as a ref."""
    reader = None
    check_quota()
    if os.environ.get("EPIC_SHARED_READER") == "1":
        reader = github_state.FreshReader("codex-worktree", 60)

    def read(kind: str, number: int) -> dict[str, Any]:
        check_quota()
        if reader:
            value = reader.pull(number) if kind == "pulls" else reader.issue(number)
        else:
            value = json.loads(
                github_quota.run_gh(["api", f"repos/{REPO}/{kind}/{number}"])
            )
        if not isinstance(value, dict):
            raise Refused("malformed GitHub target metadata")
        return value

    if action.get("pr") is not None:
        number = action["pr"]
        if type(number) is not int or number <= 0 or action["action"] == "claim":
            raise Refused("invalid selected PR")
        pr = read("pulls", number)
        raw_body = pr.get("body") or ""
        if not isinstance(raw_body, str):
            raise Refused("malformed PR body")
        body = "\n".join(raw_body.splitlines())
        owners = re.findall(r"^Executor:[ \t]*(.*?)[ \t]*$", body, re.M)
        issues = re.findall(r"^Issue:[ \t]*(.*?)[ \t]*$", body, re.M)
        match = ISSUE.fullmatch(issues[0]) if len(issues) == 1 else None
        if pr.get("state") != "open" or owners != ["Codex"] or not match:
            raise Refused(
                "selected PR must be open, assigned to Codex and link one issue"
            )
        head, base = pr.get("head"), pr.get("base")
        if any(
            not isinstance(part, dict)
            or not isinstance(part.get("repo"), dict)
            or not isinstance(part.get("ref"), str)
            for part in (head, base)
        ):
            raise Refused("malformed PR branch metadata")
        if any(part["repo"].get("full_name") != REPO for part in (head, base)):
            raise Refused("selected PR must use the expected repository, not a fork")
        if base["ref"] not in BASES:
            raise Refused("selected PR has a forbidden base")
        sha = action.get("sha")
        if (
            not isinstance(sha, str)
            or not re.fullmatch(r"[0-9a-f]{40}", sha)
            or head.get("sha") != sha
        ):
            raise Refused("selected PR head changed")
        return head["ref"], base["ref"], int(match[1]), sha

    match = ISSUE.fullmatch(action.get("issue") or "")
    if not match or action["action"] not in {"claim", "continue"}:
        raise Refused(
            "edit action requires a PR or an issue in the expected repository"
        )
    number = int(match[1])
    issue = read("issues", number)
    if issue.get("state") != "open" or "pull_request" in issue:
        raise Refused("selected issue is not open")
    check_quota()
    items = reader.board_items() if reader else next_action.project_items()
    matches = [
        item
        for item in items
        if item.get("content", {}).get("url") == action["issue"]
        and item["content"].get("type") == "Issue"
    ]
    status = "Ready" if action["action"] == "claim" else "In progress"
    if (
        len(matches) != 1
        or matches[0].get("executor") != "Codex"
        or matches[0].get("status") != status
    ):
        raise Refused(f"selected issue must be {status} and assigned to Codex")
    return None, "dev/312-interim", number, None


def refs(runner: Path) -> dict[str, str]:
    return dict(
        line.split(" ", 1)
        for line in git(
            runner,
            "for-each-ref",
            "--format=%(refname) %(objectname)",
            "refs/heads/",
            "refs/remotes/origin/",
        ).splitlines()
    )


def worktrees(runner: Path) -> dict[Path, str]:
    result = {}
    for entry in git(runner, "worktree", "list", "--porcelain", "-z").split("\0\0"):
        fields = dict(
            field.split(" ", 1) for field in entry.split("\0") if " " in field
        )
        if "worktree" in fields:
            result[Path(fields["worktree"]).resolve()] = fields.get("branch", "")
    return result


def handoff_record(
    action: dict[str, Any], branch: str, path: Path, state: Path
) -> bool:
    """Reuse the repeat gate, with local occupancy included in its fingerprint."""
    seen_path = state / "codex-gate-seen.json"
    seen = json.loads(seen_path.read_text())
    if seen.get("fingerprint") != tick_gate.fingerprint(action):
        raise Refused("worktree check does not match the selected gate snapshot")
    handoff = {
        **action,
        "action": "worktree-handoff",
        "branch": branch,
        "path": str(path),
    }
    target = {"updated_at": seen["updated_at"]}
    now = time.time()
    ttl = int(os.environ.get("EPIC_REPEAT_TTL_SECONDS", tick_gate.DEFAULT_TTL))
    if (
        tick_gate.check("codex", handoff, lambda _: target, now, ttl, state)
        == tick_gate.SKIP
    ):
        return False
    # The gate keeps one entry per target, so this replaces its previous result.
    # Normal action fingerprints differ and still reach the local availability check.
    tick_gate.record("codex", handoff, now, state, ttl)
    return True


def repeated_handoff(
    runner: Path, action: dict[str, Any], state: Path
) -> tuple[str, Path] | None:
    """Skip ownership reads only while a checked handoff is still unchanged."""
    records_file, seen_file = state / "codex-gate.json", state / "codex-gate-seen.json"
    if not records_file.exists() or not seen_file.exists():
        return None
    records = json.loads(records_file.read_text())
    seen = json.loads(seen_file.read_text())
    if not isinstance(records, dict) or not isinstance(seen, dict):
        raise ValueError("invalid handoff gate state")
    record = tick_gate.target_record(records, action)
    if record is not None and not isinstance(record, dict):
        raise ValueError("invalid handoff gate record")
    if not record or seen.get("fingerprint") != tick_gate.fingerprint(action):
        return None
    previous = record.get("action", {})
    if not isinstance(previous, dict):
        raise ValueError("invalid handoff gate action")
    if previous.get("action") != "worktree-handoff":
        return None
    branch, path = previous.get("branch"), previous.get("path")
    if (
        not isinstance(branch, str)
        or not BRANCH.fullmatch(branch)
        or not isinstance(path, str)
    ):
        return None
    handoff = {**action, "action": "worktree-handoff", "branch": branch, "path": path}
    ttl = int(os.environ.get("EPIC_REPEAT_TTL_SECONDS", tick_gate.DEFAULT_TTL))
    if not tick_gate.should_skip(
        handoff, seen.get("updated_at"), record, time.time(), ttl
    ):
        return None
    occupied = Path(path)
    if (
        occupied != feature_root(runner) / branch
        and worktrees(runner).get(occupied) == f"refs/heads/{branch}"
    ):
        return branch, occupied
    return None


def record_refusal(action: dict[str, Any], state: Path, reason: str) -> None:
    """Use the normal target cooldown without recording a completed session."""
    ttl = tick_backoff.positive_int(os.environ.get("EPIC_BLOCKED_RETRY_SECONDS", "900"))
    now = time.time()
    entries = tick_backoff.load_entries(state)
    entries[tick_backoff.target_key(action)] = {
        "at": now,
        "until": now + ttl,
        "reason": f"worktree refused: {reason}",
    }
    tick_backoff.save_entries(state, entries)


class Handoff(Refused):
    def __init__(self, branch: str, path: Path) -> None:
        message = f"handoff needed: {branch} in {path}"
        if not path.exists():
            message += "; checkout is missing; after checking its registration, run git worktree prune"
        super().__init__(message)
        self.branch = branch
        self.path = path


def prepare(runner: Path, action: dict[str, Any]) -> Path:
    branch, base, number, sha = metadata(action)
    known = refs(runner)
    if branch is None:
        # Issue numbers identify the work, not the owner of arbitrary shared refs.
        # Noncanonical branches are resumed only through a validated Codex PR.
        branch = f"feat/{number}-codex-work"
    if (
        not isinstance(branch, str)
        or not BRANCH.fullmatch(branch)
        or not branch.split("/", 1)[1].startswith(f"{number}-")
    ):
        raise Refused("selected branch must be type/issue-slug for the linked issue")
    base_ref = f"refs/remotes/origin/{base}"
    local_ref, remote_ref = f"refs/heads/{branch}", f"refs/remotes/origin/{branch}"
    if base_ref not in known:
        raise Refused("allowed base is missing; refresh origin before retrying")
    if sha is not None and known.get(remote_ref) != sha:
        raise Refused(
            "origin branch does not match the selected PR head; refresh before retrying"
        )
    start = (
        local_ref
        if local_ref in known
        else remote_ref
        if remote_ref in known
        else base_ref
    )
    root = feature_root(runner)
    destination = root / branch
    if destination.resolve() != destination:
        raise Refused("feature worktree path must not contain symlinks")
    registered = worktrees(runner)
    held_elsewhere = any(
        ref == local_ref and path != destination for path, ref in registered.items()
    )
    # Let Git report a held branch even when the human has unpublished history.
    if not held_elsewhere:
        git(runner, "merge-base", start, base_ref)
        if sha is not None:
            git(runner, "merge-base", "--is-ancestor", sha, start)
    if destination in registered:
        if registered[destination] != local_ref or common_dir(
            destination
        ) != common_dir(runner):
            raise Refused("runner worktree has the wrong repository or branch")
        if (
            Path(git(destination, "rev-parse", "--show-toplevel")).resolve()
            != destination
        ):
            raise Refused("runner worktree root does not match its registration")
        return destination
    if destination.exists():
        raise Refused(
            "feature destination exists but is not the matching registered worktree"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        if local_ref in known:
            git(runner, "worktree", "add", str(destination), branch)
        else:
            git(runner, "worktree", "add", "-b", branch, str(destination), start)
    except Refused:
        for path, ref in worktrees(runner).items():
            if ref == local_ref and path != destination:
                raise Handoff(branch, path) from None
        raise
    if (
        common_dir(destination) != common_dir(runner)
        or git(destination, "symbolic-ref", "--short", "HEAD") != branch
    ):
        raise Refused(
            "created worktree does not match the selected repository and branch"
        )
    # A human may have released the branch after the initial worktree listing.
    git(runner, "merge-base", local_ref, base_ref)
    if sha is not None:
        git(runner, "merge-base", "--is-ancestor", sha, local_ref)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--runner", required=True, type=Path)
    parser.add_argument("--action-file", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        runner = args.runner.resolve(strict=True)
        action = json.loads(args.action_file.read_text())
        if not isinstance(action, dict):
            raise Refused("selected action must be an object")
        if action.get("action") not in EDIT_ACTIONS:
            print(runner)
            return 0
        # A wrong runner repository is a global error, not one blocked target.
        validate_repository(runner)
        try:
            held = repeated_handoff(runner, action, args.state_dir)
            if held is not None:
                raise Handoff(*held)
            destination = prepare(runner, action)
        except Handoff as error:
            if not handoff_record(action, error.branch, error.path, args.state_dir):
                return 3
            print(error, file=sys.stderr)
            return 75
        except QuotaWait:
            raise
        except Refused as error:
            record_refusal(action, args.state_dir, str(error))
            print(f"worktree: target refused: {error}", file=sys.stderr)
            return 7
        print(destination)
        return 0
    except github_quota.QuotaExhausted as error:
        return github_quota.stop_on_quota(error)
    except QuotaWait as error:
        print(f"worktree: {error}", file=sys.stderr)
        return 4
    except github_state.ReadBlocked as error:
        print(f"worktree: {error}", file=sys.stderr)
        return 5
    except (
        github_state.ConfigError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
    ) as error:
        print(f"worktree: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
