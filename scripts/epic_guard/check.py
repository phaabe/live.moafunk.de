#!/usr/bin/env python3
"""Read-only architecture PR gate. Policy must come from a trusted base checkout."""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode

# GitHub's JSON objects vary by endpoint; validation is at the API boundary.
Json = dict[str, Any]
Api = Callable[[str], Any]
SHA = re.compile(r"[0-9a-f]{40}\Z")
VERDICT = re.compile(
    r"Review: (APPROVED|CHANGES REQUESTED) by (Codex|Claude) at ([0-9a-f]{40})\Z"
)
LANES = {"setup", "ops", "backend", "frontend", "coordination"}


class WaitingReason(str):
    """A blocking result waiting for PR readiness, review or checks."""


class RetryCollection(ValueError):
    """Collection raced with an update; the next run must collect again."""


def load_policy(path: str | Path) -> Json:
    """Read JSON-subset YAML without installing a YAML interpreter in CI."""
    policy = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(policy, dict) or policy.get("version") != 1:
        raise ValueError("policy must be an object with version 1")
    for key in ("repository", "epic_url", "setup_branch", "release_branch"):
        if not isinstance(policy.get(key), str) or not policy[key]:
            raise ValueError(f"policy requires {key}")
    for key in ("feature_bases", "required_checks", "ignored_checks"):
        value = policy.get(key)
        if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
            raise ValueError(f"policy {key} must be a string list")
    if set(policy["ignored_checks"]) - {"epic-guard", "epic-guard-runner"}:
        raise ValueError("only the guard's own checks may be ignored")
    if not policy["required_checks"] or set(policy["required_checks"]) & set(
        policy["ignored_checks"]
    ):
        raise ValueError("policy needs non-guard required checks")
    reviewers = policy.get("trusted_reviewers")
    if not isinstance(reviewers, dict):
        raise ValueError("policy needs trusted_reviewers by agent")
    for agent in ("Codex", "Claude"):
        if not isinstance(reviewers.get(agent), list) or not reviewers[agent]:
            raise ValueError(f"policy needs trusted reviewers for {agent}")
        if any(not isinstance(login, str) or not login for login in reviewers[agent]):
            raise ValueError("reviewer logins must be nonempty strings")
    rules = policy.get("file_rules")
    if not isinstance(rules, list) or not rules:
        raise ValueError("policy needs file_rules")
    for rule in rules:
        if not isinstance(rule, dict) or not isinstance(rule.get("pattern"), str):
            raise ValueError("file rule needs a pattern")
        if not rule.get("owners") or set(rule["owners"]) - {"Claude", "Codex"}:
            raise ValueError("file rule has invalid owners")
        if not rule.get("lanes") or set(rule["lanes"]) - LANES:
            raise ValueError("file rule has invalid lanes")
    return policy


def gh_api(endpoint: str) -> Any:
    command = ["gh", "api", "--method", "GET", endpoint]
    if endpoint.startswith("graphql?"):
        fields = parse_qs(endpoint.partition("?")[2], strict_parsing=True)
        if set(fields) != {"query"} or len(fields["query"]) != 1:
            raise ValueError("invalid GraphQL query request")
        query = fields["query"][0]
        if not re.match(r"query(?:\s|\{)", query):
            raise ValueError("only GraphQL queries are allowed")
        # GitHub requires POST even for read-only GraphQL queries.
        command = ["gh", "api", "graphql", "-f", f"query={query}"]
    result = subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=True,
        timeout=60,
    )
    return json.loads(result.stdout)


def pages(api: Api, endpoint: str, key: str | None = None) -> list[Json]:
    """Fetch every page; never accept an incomplete or malformed response."""
    rows: list[Json] = []
    expected: int | None = None
    separator = "&" if "?" in endpoint else "?"
    for page in range(1, 1001):
        response = api(f"{endpoint}{separator}per_page=100&page={page}")
        if key:
            if not isinstance(response, dict) or not isinstance(
                response.get("total_count"), int
            ):
                raise ValueError(f"{endpoint}: missing total_count")
            total = response["total_count"]
            if expected is not None and expected != total:
                raise RetryCollection(f"{endpoint}: changed during pagination; retry")
            expected = total
            batch = response.get(key)
        else:
            batch = response
        if not isinstance(batch, list) or any(
            not isinstance(row, dict) for row in batch
        ):
            raise ValueError(f"{endpoint}: malformed page")
        rows.extend(batch)
        if len(batch) < 100:
            if expected is not None and expected != len(rows):
                raise ValueError(f"{endpoint}: incomplete pagination")
            ids = [row.get("id") for row in rows]
            if None in ids or len(ids) != len(set(ids)):
                raise ValueError(f"{endpoint}: missing or duplicate IDs")
            return rows
    raise ValueError(f"{endpoint}: pagination limit reached")


def comment_edit_markers(api: Api, repo: str, pr: int, comments: list[Json]) -> None:
    """Attach explicit edit evidence; REST timestamps alone miss same-second edits."""
    owner, name = repo.split("/")
    cursor = None
    seen_cursors = set()
    markers: dict[int, Json] = {}
    for _ in range(1000):
        query = (
            "query { repository(owner: "
            + json.dumps(owner)
            + ", name: "
            + json.dumps(name)
            + ") { pullRequest(number: "
            + str(pr)
            + ") { comments(first: 100, after: "
            + json.dumps(cursor)
            + ") { totalCount nodes { databaseId lastEditedAt body createdAt updatedAt } "
            + "pageInfo { hasNextPage endCursor } } } } }"
        )
        response = api("graphql?" + urlencode({"query": query}))
        if not isinstance(response, dict) or response.get("errors"):
            raise ValueError("comment edit evidence query failed")
        try:
            connection = response["data"]["repository"]["pullRequest"]["comments"]
            count, nodes, page = (
                connection["totalCount"],
                connection["nodes"],
                connection["pageInfo"],
            )
        except (KeyError, TypeError) as exc:
            raise ValueError("comment edit evidence missing") from exc
        if not isinstance(nodes, list):
            raise ValueError("comment edit evidence nodes must be a list")
        if count != len(comments):
            raise RetryCollection("comment edit evidence count differs from REST")
        for node in nodes:
            if not isinstance(node, dict) or "lastEditedAt" not in node:
                raise ValueError("comment has no explicit lastEditedAt marker")
            identifier = node.get("databaseId")
            marker = node["lastEditedAt"]
            if not isinstance(identifier, int) or identifier in markers:
                raise ValueError("comment edit evidence has invalid or duplicate IDs")
            if marker is not None and (not isinstance(marker, str) or not marker):
                raise ValueError("comment edit marker must be a timestamp or null")
            markers[identifier] = node
        if not isinstance(page, dict) or not isinstance(page.get("hasNextPage"), bool):
            raise ValueError("comment edit evidence lacks pagination state")
        if not page["hasNextPage"]:
            break
        cursor = page.get("endCursor")
        if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
            raise ValueError("comment edit evidence pagination did not advance")
        seen_cursors.add(cursor)
    else:
        raise ValueError("comment edit evidence pagination limit reached")
    if set(markers) != {comment["id"] for comment in comments}:
        raise ValueError("comment edit evidence IDs differ from REST")
    for comment in comments:
        node = markers[comment["id"]]
        pairs = (
            ("body", "body"),
            ("created_at", "createdAt"),
            ("updated_at", "updatedAt"),
        )
        if any(comment.get(rest) != node.get(graphql) for rest, graphql in pairs):
            raise RetryCollection(
                "comment changed between REST and GraphQL reads; retry"
            )
        comment["last_edited_at"] = node["lastEditedAt"]


def collect(repo: str, pr: int, gh: Api = gh_api) -> Json:
    """Collect a read-only REST/GraphQL snapshot; gh is replaceable in tests."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or pr < 1:
        raise ValueError("invalid repository or PR number")
    root = f"repos/{repo}"
    pull = gh(f"{root}/pulls/{pr}")
    if not isinstance(pull, dict):
        raise ValueError("malformed pull request")
    head = pull.get("head", {}).get("sha", "")
    if not isinstance(head, str) or not SHA.fullmatch(head):
        raise ValueError("missing head SHA")
    count = pull.get("changed_files")
    if not isinstance(count, int) or count < 1 or count >= 3000:
        raise ValueError(
            "changed-files count missing, empty, or at GitHub's 3000-file cap"
        )

    # File responses have no numeric id. Give each its unique filename as identity.
    def file_api(endpoint: str) -> Any:
        response = gh(endpoint)
        if not isinstance(response, list):
            return response
        return [dict(row, id=row.get("filename")) for row in response]

    files = pages(file_api, f"{root}/pulls/{pr}/files")
    if len(files) != count:
        raise ValueError("changed-files response is incomplete")
    comments = pages(gh, f"{root}/issues/{pr}/comments")
    if len(comments) != pull.get("comments"):
        raise ValueError("comments response is incomplete")
    comment_edit_markers(gh, repo, pr, comments)
    checks = pages(gh, f"{root}/commits/{head}/check-runs?filter=all", "check_runs")
    statuses = pages(gh, f"{root}/commits/{head}/statuses")
    current = gh(f"{root}/pulls/{pr}")
    for key in (
        "head",
        "base",
        "body",
        "state",
        "draft",
        "updated_at",
        "changed_files",
        "comments",
    ):
        if current.get(key) != pull.get(key):
            raise RetryCollection("PR changed during collection; retry")
    return {
        "repository": repo,
        "pr": pull,
        "files": files,
        "comments": comments,
        "check_runs": checks,
        "statuses": statuses,
    }


def metadata(body: str) -> tuple[dict[str, str], list[str]]:
    result: dict[str, str] = {}
    errors = []
    for key in ("Executor", "Lane", "Reviewer", "Leaf IDs"):
        values = re.findall(rf"^{re.escape(key)}:[ \t]*(.*)$", body, re.MULTILINE)
        if len(values) != 1:
            errors.append(f"PR body needs exactly one {key}: line")
        else:
            result[key] = values[0].strip()
    return result, errors


def file_errors(policy: Json, files: list[Json], executor: str, lane: str) -> list[str]:
    errors = []
    for file in files:
        paths = [file.get("filename")]
        if file.get("status") == "renamed":
            paths.append(file.get("previous_filename"))
        for path in paths:
            if (
                not isinstance(path, str)
                or not path
                or path.startswith("/")
                or ".." in path.split("/")
            ):
                errors.append("changed file has an invalid or missing path")
                continue
            rule = next(
                (
                    rule
                    for rule in policy["file_rules"]
                    if fnmatch.fnmatchcase(path, rule["pattern"])
                ),
                None,
            )
            if not rule or executor not in rule["owners"] or lane not in rule["lanes"]:
                errors.append(f"{path}: not owned by {executor} in lane {lane}")
    return errors


def verdict_errors(
    policy: Json, comments: list[Json], reviewer: str, head: str
) -> list[str]:
    candidates = []
    edited_at = []
    for comment in comments:
        if "last_edited_at" not in comment:
            return ["comment lacks verified edit evidence"]
        if comment.get("user", {}).get("login") not in policy["trusted_reviewers"].get(
            reviewer, []
        ):
            continue
        if comment.get("last_edited_at") or comment.get("created_at") != comment.get(
            "updated_at"
        ):
            # REST cannot tell whether an edited comment used to be a verdict.
            # Require a new approval after any trusted reviewer's comment edit.
            edited_at.append(
                max(
                    comment.get("last_edited_at") or "",
                    comment.get("updated_at") or "9999",
                )
            )
        body = comment.get("body", "")
        if not isinstance(body, str):
            continue
        match = VERDICT.fullmatch(body)
        # A malformed/edited verdict must not revive an older approval.
        # Ordinary review findings need not use this reserved prefix.
        if (match and match[2] == reviewer) or (
            body.startswith("Review:") and f"by {reviewer}" in body
        ):
            candidates.append((comment, match))
    if not candidates:
        if edited_at:
            return ["reviewer comment was edited; post a new verdict"]
        return [WaitingReason(f"missing standalone verdict by {reviewer}")]
    latest, match = max(
        candidates,
        key=lambda item: (item[0].get("created_at", ""), item[0].get("id", 0)),
    )
    if match is None:
        return ["latest counterpart verdict is not an exact standalone verdict"]
    if any(timestamp >= latest.get("created_at", "") for timestamp in edited_at):
        return ["reviewer comment was edited after the verdict; post a new verdict"]
    if (
        not latest.get("created_at")
        or latest.get("created_at") != latest.get("updated_at")
        or latest.get("last_edited_at")
    ):
        return ["latest counterpart verdict was edited; post a new verdict"]
    if match[3] != head:
        return [WaitingReason("latest counterpart verdict is for a different head")]
    if match[1] != "APPROVED":
        return ["latest counterpart verdict requests changes"]
    return []


def check_errors(policy: Json, snapshot: Json, head: str) -> list[str]:
    errors = []
    latest: dict[tuple[str, str], Json] = {}
    for run in snapshot["check_runs"]:
        name = run.get("name", "")
        if name in policy["ignored_checks"]:
            continue
        if run.get("head_sha") != head:
            errors.append(f"check {name}: stale or missing head SHA")
            continue
        key = (name, str(run.get("app", {}).get("id", "")))
        if key not in latest or run.get("id", 0) > latest[key].get("id", 0):
            latest[key] = run
    observed = set()
    for (name, _), run in latest.items():
        observed.add(name)
        accepted = {"success"}
        if name not in policy["required_checks"]:
            accepted.update({"skipped", "neutral"})
        if run.get("status") != "completed":
            errors.append(WaitingReason(f"check {name}: not completed"))
        elif run.get("conclusion") not in accepted:
            errors.append(f"check {name}: not successful")
    states: dict[str, Json] = {}
    for status in snapshot["statuses"]:
        name = status.get("context", "")
        if name in policy["ignored_checks"]:
            continue
        # REST statuses are requested for the exact commit; they have no SHA field.
        if name not in states or status.get("id", 0) > states[name].get("id", 0):
            states[name] = status
    for name, status in states.items():
        observed.add(name)
        if status.get("state") == "pending":
            errors.append(WaitingReason(f"status {name}: pending"))
        elif status.get("state") != "success":
            errors.append(f"status {name}: not successful")
    for name in policy["required_checks"]:
        if name not in observed:
            errors.append(WaitingReason(f"required check missing: {name}"))
    return errors


def evaluate(
    policy: Json, snapshot: Json, expected_head: str | None = None
) -> list[str]:
    """Return violations. An empty list means this snapshot passes the feature gate."""
    pull = snapshot["pr"]
    head = pull.get("head", {}).get("sha", "")
    errors = []
    if not isinstance(head, str) or not SHA.fullmatch(head):
        return ["invalid head SHA"]
    if expected_head is not None and head != expected_head:
        errors.append("head does not match expected head")
    if pull.get("state") != "open":
        errors.append("PR must be open")
    elif pull.get("draft") is True:
        errors.append(WaitingReason("PR is a draft"))
    elif pull.get("draft") is not False:
        errors.append("PR draft state is missing or invalid")
    if snapshot.get("repository") != policy["repository"] or any(
        pull.get(side, {}).get("repo", {}).get("full_name") != policy["repository"]
        for side in ("head", "base")
    ):
        errors.append("PR must use head and base branches in the policy repository")
    body = pull.get("body") or ""
    meta, metadata_errors = metadata(body)
    errors.extend(metadata_errors)
    if not re.search(re.escape(policy["epic_url"]) + r"(?![A-Za-z0-9_/-])", body):
        errors.append("PR body must link the full epic issue URL")
    executor = meta.get("Executor", "")
    reviewer = meta.get("Reviewer", "")
    lane = meta.get("Lane", "")
    if executor not in ("Codex", "Claude"):
        errors.append("Executor must be Codex or Claude")
    if reviewer != {"Codex": "Claude", "Claude": "Codex"}.get(executor):
        errors.append("Reviewer must be the other agent")
    if lane not in LANES:
        errors.append("unknown lane")
    leaves = meta.get("Leaf IDs", "")
    if lane == "setup":
        if leaves != "setup":
            errors.append("setup lane must use Leaf IDs: setup")
    elif not re.fullmatch(
        r"[A-Z][0-9]+\.[0-9]+\.[0-9]+(?:,\s*[A-Z][0-9]+\.[0-9]+\.[0-9]+)*", leaves
    ):
        errors.append("Leaf IDs must list concrete leaves separated by commas")
    base = pull.get("base", {}).get("ref")
    branch = pull.get("head", {}).get("ref")
    if base == "main":
        if branch == policy["release_branch"]:
            errors.append(
                "release PR requires separate operator approval and qualification"
            )
        elif branch != policy["setup_branch"] or executor != "Codex" or lane != "setup":
            errors.append("main accepts only the approved Codex setup branch")
    elif base not in policy["feature_bases"]:
        errors.append("PR has an unsupported base branch")
    files = snapshot["files"]
    if not files or len(files) != pull.get("changed_files"):
        errors.append("changed-files snapshot is empty or incomplete")
    errors.extend(file_errors(policy, files, executor, lane))
    errors.extend(verdict_errors(policy, snapshot["comments"], reviewer, head))
    errors.extend(check_errors(policy, snapshot, head))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--policy", required=True, type=Path)
    args = parser.parse_args()
    try:
        policy = load_policy(args.policy)
        snapshot = collect(args.repo, args.pr)
        errors = evaluate(policy, snapshot, args.expected_head)
        result = {
            "head": snapshot["pr"]["head"]["sha"],
            "ok": not errors,
            "errors": errors,
        }
    except (
        ValueError,
        OSError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
    ) as exc:
        result = {"ok": False, "errors": [str(exc)]}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
