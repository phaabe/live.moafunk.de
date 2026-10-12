#!/usr/bin/env python3
"""Load the PR base's gate. Read-only by default; CI can publish head statuses."""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

REPO = "phaabe/live.moafunk.de"
EPIC = f"https://github.com/{REPO}/issues/312"
BASES = {"main", "dev/312-interim", "dev/streaming-architecture"}
SHA = re.compile(r"[0-9a-f]{40}\Z")
CONTEXT = "epic-guard"
CHECKING = "Checking counterpart verdict, lanes and checks"
MAX_DESCRIPTION = 140  # GitHub's limit for a status description
ERRORS = (ValueError, TypeError, KeyError, OSError, subprocess.SubprocessError)
Json = dict[str, Any]  # GitHub responses are validated at the API boundary.
Api = Callable[[str], Any]
Publish = Callable[[str, str, str], None]


class GuardRefusal(ValueError):
    """A PR cannot pass the guard, but the refresh can continue."""


def describe(exc: Exception) -> str:
    """Keep gh's stderr: it holds the API's reason for a failed call."""
    if isinstance(exc, subprocess.CalledProcessError) and isinstance(exc.stderr, str):
        return f"{exc} {exc.stderr.strip()[-500:]}".strip()
    return str(exc)


def api(endpoint: str) -> Any:
    command = ["gh", "api", "--method", "GET", endpoint]
    if endpoint.startswith("graphql?"):
        params = parse_qs(endpoint.split("?", 1)[1], strict_parsing=True)
        if set(params) != {"query"} or len(params["query"]) != 1:
            raise ValueError("invalid GraphQL query parameters")
        query = params["query"][0]
        if not re.match(r"query\s*\{", query):
            raise ValueError("only anonymous GraphQL queries are supported")
        # GitHub's GraphQL GET returns its schema; POST executes this query.
        command = ["gh", "api", "graphql", "-f", f"query={query}"]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return json.loads(result.stdout)


def publish(head: str, state: str, description: str) -> None:
    """Use structured JSON, never shell interpolation of PR text."""
    payload = {
        "state": state,
        "context": CONTEXT,
        "description": description[:MAX_DESCRIPTION],
        "target_url": f"https://github.com/{REPO}/actions/runs/{os.environ['GITHUB_RUN_ID']}",
    }
    subprocess.run(
        [
            "gh",
            "api",
            "--method",
            "POST",
            f"repos/{REPO}/statuses/{head}",
            "--input",
            "-",
        ],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )


def open_pulls(gh: Api) -> list[Json]:
    pulls: list[Json] = []
    for page in range(1, 101):
        batch = gh(f"repos/{REPO}/pulls?state=open&per_page=100&page={page}")
        if not isinstance(batch, list) or any(not isinstance(p, dict) for p in batch):
            raise ValueError("invalid open PR page")
        pulls.extend(batch)
        if len(batch) < 100:
            numbers = [p.get("number") for p in pulls]
            if None in numbers or len(numbers) != len(set(numbers)):
                raise ValueError("duplicate or missing PR numbers; retry")
            return pulls
    raise ValueError("open PR pagination limit reached")


def in_scope(pull: Json) -> bool:
    return (
        pull.get("base", {}).get("ref") in BASES - {"main"}
        or pull.get("head", {}).get("ref")
        in {"ci/312-epic-guard", "dev/streaming-architecture"}
        or EPIC in (pull.get("body") or "")
    )


def trusted_file(gh: Api, path: str, base: str) -> bytes:
    blob = gh(f"repos/{REPO}/contents/{path}?ref={base}")
    if blob.get("type") != "file" or blob.get("encoding") != "base64":
        raise ValueError(f"trusted base lacks {path}; install setup first")
    content = base64.b64decode("".join(blob["content"].split()), validate=True)
    actual = hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()
    if actual != blob.get("sha"):
        raise ValueError(f"invalid Git blob for {path}")
    return content


def verify(
    number: int,
    gh: Api = api,
    writer: Publish | None = None,
    expected_head: str | None = None,
) -> list[str]:
    """Use trusted base code and publish verification errors as PR verdicts."""
    pull = gh(f"repos/{REPO}/pulls/{number}")
    head = pull.get("head", {}).get("sha", "")
    if not SHA.fullmatch(head):
        raise ValueError("invalid PR head SHA")
    published = None
    if writer:
        try:
            latest = latest_status(gh, head)
        except ERRORS:
            latest = None  # Unknown, so publish as for a new head.
        if latest is None:
            writer(head, "pending", CHECKING)
            published = ("pending", CHECKING)
        else:
            published = (latest.get("state"), latest.get("description"))
    try:
        base = pull.get("base", {})
        if (
            base.get("ref") not in BASES
            or base.get("repo", {}).get("full_name") != REPO
            or not SHA.fullmatch(base.get("sha", ""))
        ):
            raise GuardRefusal("PR has an untrusted base")
        if expected_head is not None and head != expected_head:
            raise GuardRefusal("PR head differs from expected head")
        # A commit status is shared by every PR at that head. Refuse ambiguity.
        same_head = [p for p in open_pulls(gh) if p.get("head", {}).get("sha") == head]
        if len(same_head) != 1 or same_head[0].get("number") != number:
            raise GuardRefusal("head must belong to exactly one open PR")
        with tempfile.TemporaryDirectory(prefix="epic-guard-") as directory:
            script = Path(directory) / "check.py"
            policy_path = Path(directory) / "epic-lanes.yml"
            script.write_bytes(
                trusted_file(gh, "scripts/epic_guard/check.py", base["sha"])
            )
            policy_path.write_bytes(
                trusted_file(gh, ".github/epic-lanes.yml", base["sha"])
            )
            spec = importlib.util.spec_from_file_location("trusted_epic_check", script)
            if spec is None or spec.loader is None:
                raise ValueError("cannot load trusted checker")
            checker = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(checker)
            policy = checker.load_policy(policy_path)
            snapshot = checker.collect(REPO, number, gh=gh)
            errors = checker.evaluate(policy, snapshot, expected_head=head)
            if not errors:
                # Re-read all inputs before granting success, including comments
                # and check results which can change without a new head commit.
                snapshot = checker.collect(REPO, number, gh=gh)
                errors = checker.evaluate(policy, snapshot, expected_head=head)
            if snapshot["pr"]["base"] != base:
                errors.append("PR base changed during verification; retry")
        current = gh(f"repos/{REPO}/pulls/{number}")
        for key in ("head", "base", "body", "state", "draft", "updated_at"):
            if current.get(key) != snapshot["pr"].get(key):
                errors.append("PR changed before status publication; retry")
                break
        same_head = [p for p in open_pulls(gh) if p.get("head", {}).get("sha") == head]
        if len(same_head) != 1 or same_head[0].get("number") != number:
            errors.append("open PRs sharing this head changed; retry")
        # Older trusted bases return only plain errors; keep them failing closed.
        waiting_type = getattr(checker, "WaitingReason", ())
        failures = [error for error in errors if not isinstance(error, waiting_type)]
    except ERRORS as exc:
        errors = [str(exc)]
        failures = errors
    if writer:
        if failures:
            state, description = "failure", failures[0]
        elif errors:
            state, description = "pending", "waiting for checks: " + "; ".join(errors)
        else:
            state, description = "success", "Verdict, lanes and checks pass"
        description = description[:MAX_DESCRIPTION]
        # GitHub caps the statuses per commit and context. Only this workflow
        # writes the context, one run at a time, so skip an unchanged result.
        if (state, description) != published:
            writer(head, state, description)
    return errors


def latest_status(gh: Api, head: str) -> Json | None:
    """The newest guard status on a head; GitHub lists statuses newest first."""
    if not SHA.fullmatch(head):
        raise ValueError("invalid open PR head")
    for page in range(1, 1001):
        statuses = gh(f"repos/{REPO}/commits/{head}/statuses?per_page=100&page={page}")
        if not isinstance(statuses, list) or any(
            not isinstance(status, dict) for status in statuses
        ):
            raise ValueError("invalid status page")
        ours = [status for status in statuses if status.get("context") == CONTEXT]
        if ours:
            return max(ours, key=lambda status: status.get("id", 0))
        if len(statuses) < 100:
            return None
    raise ValueError("status pagination limit reached")


def previously_checked(gh: Api, head: str) -> bool:
    """Retargeting or removing metadata must not preserve an old green status."""
    return latest_status(gh, head) is not None


def event_numbers(event: Json, gh: Api) -> list[int]:
    """Refresh all open epic PRs; an event's stale payload is never the policy."""
    if event.get("repository", {}).get("full_name") != REPO:
        raise ValueError("event repository mismatch")
    explicit = event.get("inputs", {}).get("pr_number")
    if explicit:
        if not re.fullmatch(r"[1-9][0-9]*", str(explicit)):
            raise ValueError("invalid PR number")
        return [int(explicit)]
    return [
        pull["number"]
        for pull in open_pulls(gh)
        if in_scope(pull) or previously_checked(gh, pull["head"]["sha"])
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pr", type=int)
    group.add_argument("--event", type=Path)
    parser.add_argument("--expected-head")
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    try:
        if args.publish and (
            os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("GITHUB_REPOSITORY") != REPO
        ):
            raise ValueError("status publication is only supported in repository CI")
        if args.pr is not None and args.pr < 1:
            raise ValueError("invalid PR number")
        numbers = (
            [args.pr]
            if args.pr is not None
            else event_numbers(json.loads(args.event.read_text()), api)
        )
        result = {}
        failed = False
        for number in numbers:
            # One PR's API or publication failure must not stop the others.
            try:
                result[str(number)] = verify(
                    number,
                    writer=publish if args.publish else None,
                    expected_head=args.expected_head,
                )
            except ERRORS as exc:
                result[str(number)] = [describe(exc)]
                failed = True
        print(json.dumps(result, indent=2))
        if failed:
            return 1
        return 0 if args.publish else int(any(result.values()))
    except ERRORS as exc:
        print(json.dumps({"errors": [describe(exc)]}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
