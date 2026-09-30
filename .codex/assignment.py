"""Fresh REST assignment evidence for Codex issue actions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "epic"))
import github_quota  # noqa: E402
import github_state as gs  # noqa: E402
import next_action as na  # noqa: E402

PROJECT_URL = (
    f"https://github.com/users/{na.PROJECT_OWNER}/projects/{na.PROJECT_NUMBER}"
)
LINK = re.compile(r'<([^<>]+)>;\s*rel="([a-z]+)"')


class UncachedNamespace(gs.Namespace):
    """A disabled shared reader does not read or write cache files."""

    def __init__(self) -> None:
        pass

    def load_entry(self, url: str) -> None:
        return None

    def log_call(self, record: dict[str, Any]) -> None:
        pass

    def block_auth(self, reason: str, now: float) -> None:
        pass


def validate_row(row: dict[str, Any], field_ids: dict[str, int]) -> None:
    """Reject partial assignment values before the shared mapper stringifies them."""
    if not isinstance(row.get("node_id"), str) or not row["node_id"]:
        raise gs.ReadBlocked("project item lacks its node ID")
    if type(row.get("id")) is not int or row["id"] <= 0:
        raise gs.ReadBlocked("project item lacks its REST ID")
    if "project_url" in row and row["project_url"] not in (
        PROJECT_URL,
        gs.full_url(na.PROJECT_API),
    ):
        raise gs.ReadBlocked("project item belongs to another project")
    content = row.get("content")
    if row.get("content_type") not in ("Issue", "PullRequest", "DraftIssue"):
        raise gs.ReadBlocked("project item has an unknown content type")
    if not isinstance(content, dict):
        raise gs.ReadBlocked("project item lacks content")
    if row["content_type"] == "Issue":
        number = content.get("number")
        url = content.get("html_url")
        if (
            type(number) is not int
            or number <= 0
            or not isinstance(url, str)
            or not re.fullmatch(
                r"https://github\.com/[^/]+/[^/]+/issues/[1-9][0-9]*", url
            )
            or url.rsplit("/", 1)[-1] != str(number)
        ):
            raise gs.ReadBlocked("project issue has an invalid identity")
    fields = row.get("fields")
    if not isinstance(fields, list):
        raise gs.ReadBlocked("project item lacks fields")
    names: set[str] = set()
    for field in fields:
        if not isinstance(field, dict) or not isinstance(field.get("name"), str):
            raise gs.ReadBlocked("malformed project item field")
        name = field["name"]
        if name in names or "value" not in field:
            raise gs.ReadBlocked("duplicate or partial project item field")
        if name in na.PROJECT_FIELDS and field.get("id") != field_ids.get(name):
            raise gs.ReadBlocked(
                "project item field does not match the project definition"
            )
        names.add(name)
        value = field["value"]
        if (
            name in ("Status", "Executor", "Wave", "Area", "Level")
            and value is not None
        ):
            if (
                not isinstance(value, dict)
                or not isinstance(value.get("name"), dict)
                or not isinstance(value["name"].get("raw"), str)
            ):
                raise gs.ReadBlocked(f"malformed {name} single-select value")
    if not {"Status", "Executor"} <= names:
        raise gs.ReadBlocked("project item lacks assignment fields")


class AssignmentClient(gs.Client):
    """Use shared pagination, with strict project identity and value checks."""

    project_alias: str | None = None

    def project_resource(self, path: str) -> str | None:
        """Match GitHub's login and verified numeric-owner project URLs."""
        canonical = f"/{na.PROJECT_API}/"
        if path.startswith(canonical):
            return path[len(canonical) :]
        numeric = re.fullmatch(
            rf"/user/([1-9][0-9]*)/projectsV2/{re.escape(na.PROJECT_NUMBER)}/(fields|items)",
            path,
        )
        if numeric is None:
            return None
        if self.project_alias is None:
            owner = self.json(f"users/{na.PROJECT_OWNER}")
            if (
                not isinstance(owner, dict)
                or type(owner.get("id")) is not int
                or owner["id"] <= 0
                or not isinstance(owner.get("login"), str)
                or owner["login"].casefold() != na.PROJECT_OWNER.casefold()
            ):
                raise gs.ReadBlocked("cannot verify the project owner's numeric ID")
            self.project_alias = f"/user/{owner['id']}/projectsV2/{na.PROJECT_NUMBER}/"
        return numeric[2] if path == self.project_alias + numeric[2] else None

    def get(
        self, url: str, missing: frozenset[int] = frozenset()
    ) -> tuple[str, str | None]:
        body, link = super().get(url, missing)
        current = urlsplit(gs.full_url(url))
        resource = self.project_resource(current.path)
        if resource is not None and link is not None:
            relations: set[str] = set()
            for part in link.split(","):
                match = LINK.fullmatch(part.strip())
                if match is None or match[2] in relations:
                    raise gs.ReadBlocked("malformed project pagination Link header")
                relations.add(match[2])
                target = urlsplit(match[1])
                if (
                    target.scheme != "https"
                    or target.netloc != gs.API_HOST
                    or self.project_resource(target.path) != resource
                    or target.fragment
                    or target.username
                ):
                    raise gs.ReadBlocked(
                        "project pagination leaves the requested resource"
                    )
                before = parse_qs(current.query, keep_blank_values=True)
                after = parse_qs(target.query, keep_blank_values=True)
                for cursor in ("page", "after", "before"):
                    before.pop(cursor, None)
                    after.pop(cursor, None)
                if before != after:
                    raise gs.ReadBlocked(
                        "project pagination changes the requested fields"
                    )
        return body, link

    def pages(
        self, url: str, key: str | None = None, id_key: str = "id"
    ) -> list[dict[str, Any]]:
        rows = super().pages(url, key, id_key)
        path = urlsplit(gs.full_url(url)).path
        if path == f"/{na.PROJECT_API}/fields":
            field_ids: dict[str, int] = {}
            for row in rows:
                name, ident = row.get("name"), row.get("id")
                if (
                    not isinstance(name, str)
                    or name in field_ids
                    or type(ident) is not int
                    or ident <= 0
                ):
                    raise gs.ReadBlocked(
                        "malformed or duplicate project field definition"
                    )
                field_ids[name] = ident
            self.field_ids = field_ids
        if path == f"/{na.PROJECT_API}/items":
            issues: set[str] = set()
            for row in rows:
                validate_row(row, self.field_ids)
                if row["content_type"] == "Issue":
                    url = row["content"]["html_url"]
                    if url in issues:
                        raise gs.ReadBlocked("duplicate issue identity on the project")
                    issues.add(url)
        return rows


def make_reader(
    purpose: str = "assignment",
    seconds: float | None = None,
    http: gs.Http | None = None,
) -> gs.FreshReader:
    """Keep all FreshReader methods, without requiring cache settings when off."""
    send = http or gs.gh_http

    def checked_http(url: str, etag: str | None, timeout: float) -> gs.Response:
        code, retry_at = github_quota.check(github_quota.STATE_DIR, time.time())
        if code == github_quota.DEFERRED:
            raise github_quota.QuotaExhausted(f"shared quota wait until {retry_at}")
        return send(url, etag, timeout)

    if gs.enabled():
        reader = gs.FreshReader(
            purpose, seconds or gs.settings().recheck, http=checked_http
        )
        remaining = reader.client.remaining()
    else:
        reader = gs.FreshReader.__new__(gs.FreshReader)
        reader.ns = UncachedNamespace()
        reader.purpose = purpose
        reader.root = None
        remaining = seconds or 60
    reader.client = AssignmentClient(
        reader.ns, purpose, remaining, writable=False, http=checked_http
    )
    return reader


def evidence(
    action: dict[str, Any], reader: gs.FreshReader | None = None
) -> dict[str, Any]:
    """Only a complete fresh board read can prove membership or absence."""
    record: dict[str, Any] = {
        "result": "unknown",
        "eligible": False,
        "source": "REST",
        "project_url": PROJECT_URL,
        "project_api": gs.full_url(na.PROJECT_API),
        "repository": na.REPO,
        "issue": action.get("issue"),
        "read_at": gs.iso(time.time()),
        "item_id": None,
        "status": None,
        "executor": None,
    }
    if action.get("action") not in ("claim", "continue") or action.get("pr"):
        return {**record, "result": "not_applicable", "eligible": True}
    try:
        issue = action.get("issue")
        if not isinstance(issue, str) or na.ISSUE_URL.fullmatch(issue) is None:
            raise gs.ReadBlocked("action has no valid repository issue URL")
        items = (reader or make_reader()).board_items()
        matches = [
            item
            for item in items
            if item["content"].get("type") == "Issue"
            and item["content"].get("url") == issue
        ]
        if len(matches) > 1:
            raise gs.ReadBlocked("issue appears more than once on the project")
        if not matches:
            return {
                **record,
                "result": "absent",
                "reason": "issue is absent from the project",
            }
        item = matches[0]
        expected = "Ready" if action["action"] == "claim" else "In progress"
        eligible = item["status"] == expected and item["executor"] == "Codex"
        return {
            **record,
            "result": "confirmed",
            "eligible": eligible,
            "item_id": item["id"],
            "rest_id": item["rest_id"],
            "status": item["status"],
            "executor": item["executor"],
            "reason": "assignment confirmed"
            if eligible
            else "assignment changed or is not Codex",
        }
    except github_quota.QuotaExhausted as error:
        return {**record, "reason": str(error), "reason_code": "github_rate_limit"}
    except (
        gs.ReadBlocked,
        gs.ConfigError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
    ) as error:
        return {**record, "reason": str(error)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        action = json.loads(args.action_file.read_text())
        if not isinstance(action, dict):
            raise ValueError("action must be an object")
        result = evidence(action)
        args.output.write_text(json.dumps(result) + "\n")
    except (OSError, ValueError) as error:
        sys.stderr.write(f"assignment: {error}\n")
        return 5
    if not result["eligible"]:
        sys.stderr.write(f"assignment: {json.dumps(result, sort_keys=True)}\n")
    if result.get("reason_code") == "github_rate_limit":
        return 4
    if result["result"] == "unknown":
        return 5
    return 0 if result["eligible"] else 6


if __name__ == "__main__":
    raise SystemExit(main())
