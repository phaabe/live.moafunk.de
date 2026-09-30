"""Shared GitHub REST snapshot for the epic runners, status and monitoring.

Off by default: next_action.fetch_state() uses it only with EPIC_SHARED_READER=1.
The contract is in https://github.com/phaabe/live.moafunk.de/issues/486
("Agreed reader contract").

Snapshot reads (read_snapshot):
  - One snapshot per cache key, shared by every reader on this machine. A
    snapshot younger than EPIC_SNAPSHOT_MAX_AGE_SECONDS (120) is used as is.
  - Otherwise one reader refreshes it under `refresh.lock` (fcntl.flock; the OS
    frees it when the refresher dies). Others wait up to
    EPIC_SNAPSHOT_LOCK_SECONDS (50) and check the age again under the lock.
    One refresh may take EPIC_SNAPSHOT_REFRESH_SECONDS (45).
  - Only a complete, validated snapshot is published (temp file, os.replace).
    A failed refresh keeps the old file and its `fetched_at`.
  - Every page is a conditional REST request with the stored ETag. Only the
    refresher writes ETag entries (`etags/<sha256 of URL>.json`).

Fresh reads (FreshReader, recheck, write_checks.py) never use the snapshot and
never write the cache. They may send a stored ETag; on 304 they use the body
they loaded together with that ETag before the request.

Errors: ReadBlocked (next_action.py exit 5) for lock or refresh timeouts,
failed, partial or malformed pages, a 304 without a stored body, REST rate
limits and suspected loss of authorization. ConfigError (exit 2) for invalid
settings or a missing `github-cache/auth-context` file.

Files under the shared root (EPIC_CACHE_DIR, else EPIC_QUOTA_DIR, else
EPIC_STATE_DIR, else ~/.local/state/epic-loop):
  github-cache/auth-context      nonsecret version string chosen by Anton.
                                 Change it when a token or its grants change.
  github-cache/calls.jsonl       one line per outbound call
  github-cache/v1/<key hash>/    snapshot.json, refresh.lock, etags/,
                                 auth-blocked.json
"""

from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import next_action as na
from github_quota import run_gh

SWITCH_ENV = "EPIC_SHARED_READER"
API = "https://api.github.com/"
API_HOST = "api.github.com"
API_VERSION = "2022-11-28"
MEDIA = "application/vnd.github+json"
SCHEMA = 1
MAX_PAGES = 1000
SHA = re.compile(r"^[0-9a-f]{40}$")
NEXT_LINK = re.compile(r'<([^>]+)>;\s*rel="next"')
MERGEABLE = {True: "MERGEABLE", False: "CONFLICTING", None: "UNKNOWN"}
# Actions whose rule reads a verdict: their recheck adds GraphQL edit evidence.
VERDICT_ACTIONS = {"merge", "fix", "escalate"}
STATE_KEYS = ("prs", "items", "linked_labels", "merged_prs", "batch_order")
# Write hooks run the fresh check with this host timeout (.claude/settings.json,
# .codex/hooks.json). A Claude Code hook that times out does not block the
# write, so the check must end first; the margin covers start-up.
HOOK_TIMEOUT_SECONDS = 90
HOOK_MARGIN_SECONDS = 10
IDENTITY_TIMEOUT_SECONDS = 30


class ConfigError(Exception):
    """Bad settings or a missing auth context. next_action.py exits 2."""


class ReadBlocked(Exception):
    """No complete, current GitHub read. next_action.py exits 5."""


class Gone(Exception):
    """A read allowed to miss (a prerequisite ticket) got 404 or 410."""

    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.status = status


class AuthLost(ReadBlocked):
    """401, or a non-rate-limit 403/404 on a URL that returned 200 before."""


def enabled(env: Mapping[str, str] | None = None) -> bool:
    return (os.environ if env is None else env).get(SWITCH_ENV) == "1"


@dataclass(frozen=True)
class Settings:
    max_age: int
    lock: int
    refresh: int
    recheck: int


def settings(env: Mapping[str, str] | None = None) -> Settings:
    """Read and check the timing settings. Invalid values raise ConfigError."""
    values: Mapping[str, str] = os.environ if env is None else env

    def positive(name: str, default: int) -> int:
        raw = values.get(name)
        if raw is None or raw == "":
            return default
        if not raw.isdigit() or int(raw) < 1:
            raise ConfigError(f"{name} must be a positive integer, got {raw!r}")
        return int(raw)

    found = Settings(
        max_age=positive("EPIC_SNAPSHOT_MAX_AGE_SECONDS", 120),
        lock=positive("EPIC_SNAPSHOT_LOCK_SECONDS", 50),
        refresh=positive("EPIC_SNAPSHOT_REFRESH_SECONDS", 45),
        recheck=positive("EPIC_RECHECK_TIMEOUT_SECONDS", 60),
    )
    select = positive("EPIC_SELECT_TIMEOUT_SECONDS", 120)
    if found.lock + found.refresh >= select:
        raise ConfigError(
            "EPIC_SNAPSHOT_LOCK_SECONDS + EPIC_SNAPSHOT_REFRESH_SECONDS "
            f"({found.lock} + {found.refresh}) must be below "
            f"EPIC_SELECT_TIMEOUT_SECONDS ({select})"
        )
    if found.recheck + HOOK_MARGIN_SECONDS > HOOK_TIMEOUT_SECONDS:
        raise ConfigError(
            f"EPIC_RECHECK_TIMEOUT_SECONDS ({found.recheck}) must be at most "
            f"{HOOK_TIMEOUT_SECONDS - HOOK_MARGIN_SECONDS}, so write checks end "
            f"before the {HOOK_TIMEOUT_SECONDS}s hook timeout"
        )
    return found


def shared_root(env: Mapping[str, str] | None = None) -> Path:
    """One root for runners, monitor and manual commands.

    Registered agents have their own EPIC_STATE_DIR; the runners export the
    shared registry root as EPIC_QUOTA_DIR, so it wins over EPIC_STATE_DIR.
    """
    values: Mapping[str, str] = os.environ if env is None else env
    for name in ("EPIC_CACHE_DIR", "EPIC_QUOTA_DIR", "EPIC_STATE_DIR"):
        if values.get(name):
            return Path(values[name]).expanduser()
    return Path.home() / ".local" / "state" / "epic-loop"


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, prefix=f".{path.name}-", delete=False
    ) as f:
        try:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        except BaseException:
            os.unlink(f.name)
            raise
    try:
        os.replace(f.name, path)
    except BaseException:
        os.unlink(f.name)
        raise


# --- HTTP through `gh api -i` ---


@dataclass
class Response:
    status: int
    headers: dict[str, str]  # lower-case names
    body: str


def parse_gh_i(text: str) -> Response | None:
    """Split `gh api -i` output into status, headers and body.

    gh prints the status line, the headers (CRLF), a blank line and the body.
    A 304 has headers and an empty body. None when there is no status line.
    """
    head, sep, body = text.partition("\n")
    m = re.match(r"HTTP/[0-9.]+ (\d{3})", head)
    if not m:
        return None
    headers: dict[str, str] = {}
    rest = body
    while rest:
        line, _, after = rest.partition("\n")
        line = line.rstrip("\r")
        if not line:
            rest = after
            break
        name, colon, value = line.partition(":")
        if colon:
            headers[name.strip().lower()] = value.strip()
        rest = after
    else:
        rest = ""
    return Response(int(m.group(1)), headers, rest)


Http = Callable[[str, "str | None", float], Response]


def gh_http(url: str, etag: str | None, timeout: float) -> Response:
    """One GET through gh. Reads the HTTP status, not only the exit code:
    gh exits 1 for a 304 and for errors."""
    args = ["gh", "api", "-i", "-H", f"Accept: {MEDIA}"]
    args += ["-H", f"X-GitHub-Api-Version: {API_VERSION}"]
    if etag:
        args += ["-H", f"If-None-Match: {etag}"]
    try:
        out = subprocess.run(
            [*args, url], capture_output=True, text=True, timeout=max(1.0, timeout)
        )
    except subprocess.TimeoutExpired as error:
        raise ReadBlocked(f"GitHub read timed out: {path_of(url)}") from error
    except OSError as error:
        raise ReadBlocked(f"gh failed to start: {error}") from error
    response = parse_gh_i(out.stdout)
    if response is None:
        detail = (out.stderr or "").strip().splitlines()[-1:] or ["no output"]
        raise ReadBlocked(f"no HTTP response for {path_of(url)}: {detail[0][:200]}")
    return response


def full_url(url: str) -> str:
    if url.startswith("https://"):
        if urlsplit(url).netloc != API_HOST:
            raise ReadBlocked(f"refusing a URL outside {API_HOST}: {url}")
        return url
    return API + url.lstrip("/")


def path_of(url: str) -> str:
    parts = urlsplit(url)
    return parts.path.lstrip("/") + (f"?{parts.query}" if parts.query else "")


def next_link(link: str | None) -> str | None:
    m = NEXT_LINK.search(link or "")
    return m.group(1) if m else None


def rate_limited(response: Response) -> bool:
    """GitHub also uses 403 for rate limits; classify those first."""
    if response.status == 429:
        return True
    if response.headers.get("x-ratelimit-remaining") == "0":
        return True
    if "retry-after" in response.headers:
        return True
    return "rate limit" in response.body.lower()


# --- namespace, ETag entries and call log ---


@dataclass(frozen=True)
class Entry:
    url: str
    etag: str
    body: str
    link: str | None


class Namespace:
    """The cache folder for one key. The key has no secret in it."""

    def __init__(self, base: Path, key: dict[str, Any]):
        self.base = base
        self.key = key
        digest = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()
        self.dir = base / "v1" / digest[:24]

    def etag_file(self, url: str) -> Path:
        return self.dir / "etags" / f"{hashlib.sha256(url.encode()).hexdigest()}.json"

    def load_entry(self, url: str) -> Entry | None:
        """URL, ETag, body and Link, read together. Bad or foreign files: None."""
        try:
            data = json.loads(self.etag_file(url).read_text())
            entry = Entry(data["url"], data["etag"], data["body"], data.get("link"))
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if entry.url != url or not isinstance(entry.etag, str) or not entry.etag:
            return None
        if not isinstance(entry.body, str) or not isinstance(entry.link, str | None):
            return None
        return entry

    def save_entry(self, url: str, etag: str, body: str, link: str | None) -> None:
        write_json_atomic(
            self.etag_file(url), {"url": url, "etag": etag, "body": body, "link": link}
        )

    @property
    def auth_marker(self) -> Path:
        return self.dir / "auth-blocked.json"

    def auth_blocked(self) -> bool:
        return self.auth_marker.exists()

    def block_auth(self, reason: str, now: float) -> None:
        write_json_atomic(self.auth_marker, {"at": now, "reason": reason})

    def auth_marker_at(self) -> float | None:
        """When access loss was reported; None without a marker. A marker
        that cannot be read counts as reported now."""
        try:
            at = json.loads(self.auth_marker.read_text()).get("at")
        except FileNotFoundError:
            return None
        except (OSError, ValueError, AttributeError):
            return float("inf")
        return float(at) if isinstance(at, int | float) else float("inf")

    def blocked_since(self, started: float) -> bool:
        """True when access loss was reported at or after `started`."""
        at = self.auth_marker_at()
        return at is not None and at >= started

    def clear_auth(self, started: float) -> None:
        """A refresh that started after the marker proves access again."""
        at = self.auth_marker_at()
        if at is not None and at < started:
            self.auth_marker.unlink(missing_ok=True)

    def log_call(self, record: dict[str, Any]) -> None:
        """Append one line; a failed log write never fails the read."""
        try:
            self.base.mkdir(parents=True, exist_ok=True)
            with (self.base / "calls.jsonl").open("a") as out:
                out.write(json.dumps(record, sort_keys=True) + "\n")
        except OSError:
            pass


def read_auth_context(base: Path) -> str:
    try:
        value = (base / "auth-context").read_text().strip()
    except FileNotFoundError:
        value = ""
    except OSError as error:
        raise ConfigError(f"cannot read {base / 'auth-context'}: {error}") from error
    if not value:
        raise ConfigError(
            f"{base / 'auth-context'} is missing or empty. Write a nonsecret "
            "version string there (for example 2026-09-29-anneoneone-pat1) and "
            "change it whenever a token or its grants change."
        )
    return value


def log_record(
    purpose: str,
    url: str,
    status: int | str,
    result: str,
    started: float,
    scopes: str | None,
) -> dict[str, Any]:
    return {
        "at": iso(time.time()),
        "path": path_of(url) if url.startswith("https://") else url,
        "status": status,
        "result": result,
        "purpose": purpose,
        "ms": int((time.monotonic() - started) * 1000),
        "scopes": scopes,
    }


_LOGINS: dict[str, str] = {}


def resolve_namespace(
    http: Http = gh_http,
    env: Mapping[str, str] | None = None,
    timeout: float = IDENTITY_TIMEOUT_SECONDS,
) -> Namespace:
    """Cache key: API host, gh login, API version, format, repo, project, bases,
    schema and the auth-context version. Resolved before any cache use.
    `timeout` bounds the one login read."""
    values: Mapping[str, str] = os.environ if env is None else env
    if values.get("GH_HOST") not in (None, "", "github.com"):
        raise ConfigError("the shared reader supports github.com only (GH_HOST)")
    base = shared_root(env) / "github-cache"
    context = read_auth_context(base)
    login = _LOGINS.get(context)
    if login is None:
        url = full_url("user")
        started = time.monotonic()
        response = http(url, None, timeout)
        scopes = response.headers.get("x-oauth-scopes")
        result = "200" if response.status == 200 else "error"
        record = log_record("identity", url, response.status, result, started, scopes)
        Namespace(base, {}).log_call(record)
        try:
            login = (
                json.loads(response.body)["login"] if response.status == 200 else None
            )
        except (ValueError, KeyError, TypeError):
            login = None
        if not isinstance(login, str) or not login:
            raise ReadBlocked(f"cannot read the gh login (HTTP {response.status})")
        _LOGINS[context] = login
    key = {
        "host": API_HOST,
        "login": login,
        "api_version": API_VERSION,
        "format": MEDIA,
        "repo": na.REPO,
        "project": na.PROJECT_API,
        "bases": sorted(na.BASES),
        "schema": SCHEMA,
        "auth_context": context,
    }
    return Namespace(base, key)


# --- REST client ---


class Client:
    """Conditional GETs with a deadline. `writable` only for the refresher."""

    def __init__(
        self,
        ns: Namespace,
        purpose: str,
        seconds: float,
        writable: bool,
        http: Http = gh_http,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.ns = ns
        self.purpose = purpose
        self.writable = writable
        self.http = http
        self.clock = clock
        self.deadline = clock() + seconds

    def remaining(self) -> float:
        left = self.deadline - self.clock()
        if left <= 0:
            raise ReadBlocked(f"{self.purpose}: GitHub reads took too long")
        return left

    def get(
        self, url: str, missing: frozenset[int] = frozenset()
    ) -> tuple[str, str | None]:
        """(body, Link header) of one URL. Never a partial or unknown result.

        A status in `missing` raises Gone instead of ReadBlocked or AuthLost:
        the caller blocks only what depends on this URL.
        """
        url = full_url(url)
        left = self.remaining()
        # Keep URL, ETag and body together: the refresher may replace the file
        # while this request runs, and a 304 answers the ETag sent here.
        entry = self.ns.load_entry(url)
        started = time.monotonic()
        response = self.http(url, entry.etag if entry else None, left)
        scopes = response.headers.get("x-oauth-scopes")
        status = response.status
        result = {200: "200", 304: "304"}.get(status, "error")
        self.ns.log_call(log_record(self.purpose, url, status, result, started, scopes))
        if status == 200:
            etag = response.headers.get("etag")
            link = response.headers.get("link")
            if self.writable and etag:
                self.ns.save_entry(url, etag, response.body, link)
            return response.body, link
        if status == 304:
            if entry is None:
                raise ReadBlocked(f"HTTP 304 without a stored body: {path_of(url)}")
            return entry.body, entry.link
        if status in missing:
            raise Gone(status)
        if status in (403, 429) and rate_limited(response):
            raise ReadBlocked(f"REST rate limit (HTTP {status}): {path_of(url)}")
        if status == 401 or (status in (403, 404) and entry is not None):
            reason = f"suspected loss of access (HTTP {status}): {path_of(url)}"
            self.ns.block_auth(reason, time.time())
            raise AuthLost(reason)
        raise ReadBlocked(f"HTTP {status}: {path_of(url)}")

    def json(self, url: str, missing: frozenset[int] = frozenset()) -> Any:
        body, _ = self.get(url, missing)
        try:
            return json.loads(body)
        except ValueError as error:
            raise ReadBlocked(f"malformed JSON: {path_of(full_url(url))}") from error

    def pages(
        self, url: str, key: str | None = None, id_key: str = "id"
    ) -> list[dict[str, Any]]:
        """Every page, following and revalidating each `next` link.

        A `key` names the list inside an object page (search, check runs); its
        `total_count` must stay the same and match the rows. Rows need a unique
        `id_key`. Anything else raises ReadBlocked: no partial list.
        """
        rows: list[dict[str, Any]] = []
        seen: set[Any] = set()
        total: int | None = None
        current: str | None = full_url(url)
        for _ in range(MAX_PAGES):
            if current is None:
                break
            body, link = self.get(current)
            try:
                data = json.loads(body)
            except ValueError as error:
                raise ReadBlocked(f"malformed page: {path_of(current)}") from error
            batch = data
            if key is not None:
                if not isinstance(data, dict) or not isinstance(
                    data.get("total_count"), int
                ):
                    raise ReadBlocked(f"page without total_count: {path_of(current)}")
                if data.get("incomplete_results"):
                    raise ReadBlocked(f"incomplete results: {path_of(current)}")
                if total is not None and total != data["total_count"]:
                    raise ReadBlocked(f"changed during pagination: {path_of(current)}")
                total = data["total_count"]
                batch = data.get(key)
            if not isinstance(batch, list) or any(
                not isinstance(r, dict) for r in batch
            ):
                raise ReadBlocked(f"malformed page: {path_of(current)}")
            for row in batch:
                ident = row.get(id_key)
                if ident is None or ident in seen:
                    raise ReadBlocked(f"missing or duplicate rows: {path_of(current)}")
                seen.add(ident)
            rows.extend(batch)
            current = next_link(link)
            if current is not None:
                current = full_url(current)
        else:
            raise ReadBlocked(f"pagination limit reached: {path_of(full_url(url))}")
        if total is not None and total != len(rows) and key != "items":
            raise ReadBlocked(f"incomplete pagination: {path_of(full_url(url))}")
        return rows


# --- state building (REST only, zero GraphQL) ---


def rollup(client: Client, sha: str) -> list[dict[str, Any]]:
    """statusCheckRollup from REST: the current run per check name and app,
    and the latest status per context."""
    runs = client.pages(
        f"repos/{na.REPO}/commits/{sha}/check-runs?filter=all&per_page=100",
        key="check_runs",
    )
    latest_runs: dict[tuple[str, str], dict[str, Any]] = {}
    for run in runs:
        key = (str(run.get("name") or ""), str((run.get("app") or {}).get("id", "")))
        if key not in latest_runs or run["id"] > latest_runs[key]["id"]:
            latest_runs[key] = run
    statuses = client.pages(f"repos/{na.REPO}/commits/{sha}/statuses?per_page=100")
    latest_status: dict[str, dict[str, Any]] = {}
    for status in statuses:
        name = str(status.get("context") or "")
        if name not in latest_status or status["id"] > latest_status[name]["id"]:
            latest_status[name] = status
    found: list[dict[str, Any]] = []
    for (name, app), run in sorted(latest_runs.items()):
        found.append(
            {
                "__typename": "CheckRun",
                "name": name,
                "appId": app,
                "status": str(run.get("status") or "").upper(),
                "conclusion": str(run.get("conclusion") or "").upper(),
            }
        )
    for name, status in sorted(latest_status.items()):
        found.append(
            {
                "__typename": "StatusContext",
                "context": name,
                "state": str(status.get("state") or "").upper(),
            }
        )
    return found


def pr_from_pull(pull: dict[str, Any]) -> dict[str, Any]:
    """A REST pull request in the `gh pr list --json` shape decide() reads."""
    head = pull["head"]["sha"]
    if not isinstance(head, str) or not SHA.match(head):
        raise ReadBlocked(f"PR {pull.get('number')} has no valid head SHA")
    return {
        "number": pull["number"],
        "title": pull.get("title"),
        "body": pull.get("body") or "",
        "baseRefName": pull["base"]["ref"],
        "headRefName": pull["head"]["ref"],
        "headRefOid": head,
        "isDraft": bool(pull.get("draft")),
        "labels": [{"name": lbl["name"]} for lbl in pull.get("labels") or []],
        "mergeable": MERGEABLE.get(pull.get("mergeable"), "UNKNOWN"),
        "updatedAt": pull.get("updated_at"),
    }


def comment_rows(client: Client, number: int) -> list[dict[str, Any]]:
    return client.pages(f"repos/{na.REPO}/issues/{number}/comments?per_page=100")


def to_comments(rows: list[dict[str, Any]], count: Any) -> list[dict[str, Any]]:
    if not isinstance(count, int):
        raise ReadBlocked("comment count missing")
    try:
        comments = na.comments_from_rest(rows, count)
    except ValueError as error:
        raise ReadBlocked(str(error)) from error
    # Fresh edit evidence (GraphQL lastEditedAt), when collected, also counts.
    for comment, row in zip(comments, rows, strict=True):
        if row.get("last_edited_at"):
            comment["includesCreatedEdit"] = True
    return comments


def read_pr(
    client: Client,
    number: int,
    edit_evidence: Callable[[int, list[dict[str, Any]]], None] | None = None,
    pull: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """(decide-shaped PR with comments, checks and files, raw REST pull).

    `pull` is the REST pull request when the caller has read it already.
    """
    if pull is None:
        pull = client.json(f"repos/{na.REPO}/pulls/{number}")
    if not isinstance(pull, dict):
        raise ReadBlocked(f"malformed PR {number}")
    pr = pr_from_pull(pull)
    rows = comment_rows(client, number)
    if edit_evidence is not None:
        edit_evidence(number, rows)
    pr["comments"] = to_comments(rows, pull.get("comments"))
    pr["statusCheckRollup"] = rollup(client, pr["headRefOid"])
    if pr["baseRefName"] in na.BASES and na.ownerless(pr):
        files = client.pages(
            f"repos/{na.REPO}/pulls/{number}/files?per_page=100", id_key="filename"
        )
        pr["files"] = na.changed_paths(files)
    return pr, pull


def open_pulls(client: Client) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for base in na.BASES:
        rows += client.pages(
            f"repos/{na.REPO}/pulls?state=open&base={quote(base, safe='')}&per_page=100"
        )
    return rows


def board_items(client: Client, rest_ids: bool = False) -> list[dict[str, Any]]:
    """Board items in the fetch_state() shape. `rest_ids` adds each item's
    numeric REST id as `rest_id` (write checks match item IDs with it)."""
    fields = {
        f.get("name"): f.get("id")
        for f in client.pages(f"{na.PROJECT_API}/fields?per_page=100")
    }
    missing = [name for name in na.PROJECT_FIELDS if fields.get(name) is None]
    if missing:
        raise ReadBlocked(f"project board lacks fields: {', '.join(missing)}")
    query = "&".join(f"fields[]={fields[name]}" for name in na.PROJECT_FIELDS)
    items = []
    for row in client.pages(f"{na.PROJECT_API}/items?per_page=100&{query}"):
        item = na.item_from_rest(row)
        if rest_ids:
            item["rest_id"] = row.get("id")
        items.append(item)
    return items


def search_focus(client: Client, focus: set[str]) -> list[dict[str, Any]]:
    """Open issues with a focus label, on the board or not. One entry per issue."""
    found: dict[int, dict[str, Any]] = {}
    for label in sorted(focus):
        query = quote(f'repo:{na.REPO} is:issue is:open label:"{label}"')
        for row in client.pages(f"search/issues?q={query}&per_page=100", key="items"):
            if "pull_request" in row or row["number"] in found:
                continue
            found[row["number"]] = {
                "number": row["number"],
                "url": row.get("html_url") or na.issue_url(row["number"]),
                "title": row.get("title"),
                "labels": sorted(na.labels(row)),
                "updated_at": row.get("updated_at"),
            }
    return [found[n] for n in sorted(found)]


def read_ticket(client: Client, number: int) -> dict[str, Any]:
    """One prerequisite ticket. 404 and 410 block only its successors; other
    failures raise ReadBlocked and stop the tick."""
    try:
        issue = client.json(f"repos/{na.REPO}/issues/{number}", na.MISSING_STATUSES)
    except Gone as gone:
        return na.missing_ticket(gone.status)
    if not isinstance(issue, dict):
        raise ReadBlocked(f"malformed issue {number}")
    return na.ticket_from_issue(number, issue)


def merged_pulls(client: Client) -> list[dict[str, Any]]:
    """Merged PRs into the epic bases, as {number, body}."""
    merged: list[dict[str, Any]] = []
    for base in na.BASES:
        rows = client.pages(
            f"repos/{na.REPO}/pulls?state=closed&base={quote(base, safe='')}&per_page=100"
        )
        merged += [
            {"number": r["number"], "body": r.get("body") or ""}
            for r in rows
            if r.get("merged_at")
        ]
    return merged


def issue_comments(client: Client, number: int) -> list[dict[str, Any]]:
    return na.rows_as_comments(comment_rows(client, number))


def build_state(
    client: Client,
    focus: set[str],
    pr_details: bool = True,
    readiness_for: set[int] | None = None,
    completed_tickets: bool = False,
) -> dict[str, Any]:
    """The fetch_state() dict from REST. Raises ReadBlocked on any gap.

    Without `pr_details` open PRs come from the list only (no comments, checks
    or mergeable): enough for claims. `readiness_for` limits the readiness
    comments to these issues. `completed_tickets` adds the prerequisite
    tickets' issue state (`tickets`), read with this client.
    """
    try:
        prs: list[dict[str, Any]] = []
        for row in open_pulls(client):
            if pr_details:
                prs.append(read_pr(client, row["number"])[0])
            else:
                pr = pr_from_pull(row)
                pr.update({"comments": [], "statusCheckRollup": []})
                prs.append(pr)
        merged = merged_pulls(client)
        items = board_items(client)
        on_board = {
            (i.get("content") or {}).get("number")
            for i in items
            if na.ISSUE_URL.fullmatch((i.get("content") or {}).get("url") or "")
        }
        linked_labels: dict[str, list[str]] = {}
        for n in sorted(
            {n for pr in prs for n in na.issue_numbers(pr.get("body") or "")}
        ):
            if n not in on_board:
                issue = client.json(f"repos/{na.REPO}/issues/{n}")
                linked_labels[str(n)] = sorted(na.labels(issue))
        for item in items:
            content = item.get("content") or {}
            if item.get("status") != "Ready" or content.get("type") != "Issue":
                continue
            if readiness_for is not None and content.get("number") not in readiness_for:
                continue
            rows = comment_rows(client, content["number"])
            item["readiness"] = "\n".join(r.get("body") or "" for r in rows)
        batch_order = [
            r.get("body") or ""
            for r in comment_rows(client, na.EPIC)
            if "Scope, in order" in (r.get("body") or "")
        ]
        state: dict[str, Any] = {
            "prs": prs,
            "items": items,
            "linked_labels": linked_labels,
            "merged_prs": merged,
            "batch_order": batch_order,
            "focus_issues": search_focus(client, focus) if focus else [],
        }
        # Every labeled draft PR and In progress issue, also in a claim
        # recheck: any of them can hold or free the claim.
        state["waiting"] = na.read_waiting(
            state, lambda n: issue_comments(client, n), pr_comments=pr_details
        )
        if completed_tickets:
            state["completed_tickets"] = True
            state["tickets"] = na.read_tickets(
                items, lambda n: read_ticket(client, n), na.resume_tickets(state)
            )
        return state
    except (KeyError, TypeError, AttributeError) as error:
        raise ReadBlocked(f"malformed GitHub data: {error!r}") from error


def validate_state(state: Any) -> None:
    """Refuse a state decide() cannot trust."""
    if not isinstance(state, dict):
        raise ReadBlocked("snapshot state is not an object")
    for key in (*STATE_KEYS, "focus_issues"):
        expected = dict if key == "linked_labels" else list
        if not isinstance(state.get(key), expected):
            raise ReadBlocked(f"snapshot state lacks {key}")
    numbers = [p.get("number") for p in state["prs"] if isinstance(p, dict)]
    if len(numbers) != len(state["prs"]) or len(set(numbers)) != len(numbers):
        raise ReadBlocked("snapshot has missing or duplicate PRs")
    for pr in state["prs"]:
        if not isinstance(pr.get("headRefOid"), str) or not SHA.match(pr["headRefOid"]):
            raise ReadBlocked(f"snapshot PR {pr.get('number')} has no head SHA")
        if not isinstance(pr.get("comments"), list) or not isinstance(
            pr.get("statusCheckRollup"), list
        ):
            raise ReadBlocked(f"snapshot PR {pr.get('number')} is partial")
    if state.get("completed_tickets") and not isinstance(state.get("tickets"), dict):
        raise ReadBlocked("snapshot state lacks tickets")
    if not isinstance(state.get("waiting"), dict):
        raise ReadBlocked("snapshot state lacks waiting records")


# --- snapshot ---


@dataclass
class Snapshot:
    state: dict[str, Any]
    fetched_at: str
    age_seconds: float
    source: str  # "cache" or "refresh"


def read_focus_file() -> set[str]:
    return set(na.read_focus(na.FOCUS_FILE))


def load_snapshot(
    ns: Namespace, completed_tickets: bool = False
) -> dict[str, Any] | None:
    """The stored record, or None when missing, corrupt, for another key or
    read in the other completed-tickets mode."""
    try:
        record = json.loads((ns.dir / "snapshot.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("schema") != SCHEMA:
        return None
    if record.get("key") != ns.key:
        return None
    state = record.get("state")
    digest = hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()
    if record.get("digest") != digest or not isinstance(
        record.get("fetched_ts"), int | float
    ):
        return None
    if not isinstance(record.get("focus"), list):
        return None
    try:
        validate_state(state)
    except ReadBlocked:
        return None
    if bool(state.get("completed_tickets")) != completed_tickets:
        return None
    return record


def publish(
    ns: Namespace, state: dict[str, Any], fetched_ts: float, focus: set[str]
) -> dict[str, Any]:
    record = {
        "schema": SCHEMA,
        "key": ns.key,
        "fetched_ts": fetched_ts,
        "fetched_at": iso(fetched_ts),
        "focus": sorted(focus),
        "digest": hashlib.sha256(
            json.dumps(state, sort_keys=True).encode()
        ).hexdigest(),
        "state": state,
    }
    write_json_atomic(ns.dir / "snapshot.json", record)
    return record


def usable(
    record: dict[str, Any] | None, focus: set[str], now: float, max_age: int
) -> bool:
    if record is None:
        return False
    age = now - record["fetched_ts"]
    return 0 <= age <= max_age and focus <= set(record["focus"])


def as_snapshot(
    record: dict[str, Any], focus: set[str], now: float, source: str
) -> Snapshot:
    state = dict(record["state"])
    # Only the caller's focus labels, as fetch_state(focus) returned before.
    state["focus_issues"] = [
        i for i in state["focus_issues"] if focus and set(i.get("labels") or []) & focus
    ]
    return Snapshot(
        state, record["fetched_at"], max(0.0, now - record["fetched_ts"]), source
    )


@contextmanager
def refresh_lock(
    ns: Namespace, seconds: float, sleep: Callable[[float], None] = time.sleep
) -> Generator[None]:
    """flock on refresh.lock; the OS frees it when the holder dies. Never
    taken from a live holder because time passed: a timeout is ReadBlocked."""
    ns.dir.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + seconds
    with (ns.dir / "refresh.lock").open("a") as handle:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ReadBlocked(
                        f"snapshot refresh lock busy for {seconds:g}s"
                    ) from None
                sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_snapshot(
    focus: frozenset[str] | set[str] = frozenset(),
    http: Http = gh_http,
    now: Callable[[], float] = time.time,
    build: Callable[..., dict[str, Any]] = build_state,
    completed_tickets: bool = False,
) -> Snapshot:
    """A snapshot no older than the max age, refreshed under the lock if needed.

    A snapshot read in the other completed-tickets mode is never used.
    """
    config = settings()
    ns = resolve_namespace(http)
    wanted = set(focus)
    if not ns.auth_blocked():
        record = load_snapshot(ns, completed_tickets)
        if usable(record, wanted, now(), config.max_age):
            assert record is not None
            return as_snapshot(record, wanted, now(), "cache")
    with refresh_lock(ns, config.lock):
        # Another reader may have refreshed while this one waited.
        record = load_snapshot(ns, completed_tickets)
        if not ns.auth_blocked() and usable(record, wanted, now(), config.max_age):
            assert record is not None
            return as_snapshot(record, wanted, now(), "cache")
        started = now()
        # The focus file's labels too, so one snapshot serves every reader.
        labels = wanted | read_focus_file()
        client = Client(ns, "refresh", config.refresh, writable=True, http=http)
        if completed_tickets:
            state = build(client, labels, completed_tickets=True)
        else:
            state = build(client, labels)
        validate_state(state)
        # Refuse, and keep the old snapshot, when access loss was reported
        # during this refresh or the result is already older than the max age.
        if ns.blocked_since(started):
            raise ReadBlocked("access loss was reported during the refresh")
        age = now() - started
        if age > config.max_age:
            raise ReadBlocked(
                f"refresh took {age:.0f}s, longer than the max age {config.max_age}s"
            )
        record = publish(ns, state, started, labels)
        ns.clear_auth(started)
        if ns.blocked_since(started):
            raise ReadBlocked("access loss was reported during the refresh")
        return as_snapshot(record, wanted, now(), "refresh")


# --- fresh reads ---


def trusted_root() -> Path:
    """The runner checkout, never a feature worktree."""
    return Path(
        os.environ.get("EPIC_TRUSTED_ROOT") or Path(__file__).resolve().parents[2]
    )


def load_guard(root: Path | None = None) -> ModuleType:
    """The merge guard's check.py from the trusted checkout."""
    path = (root or trusted_root()) / "scripts" / "epic_guard" / "check.py"
    spec = importlib.util.spec_from_file_location("trusted_epic_guard_check", path)
    if spec is None or spec.loader is None:
        raise ConfigError(f"cannot load the merge guard from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FreshReader:
    """Reads for one check right before a write or a model start.

    Never uses the snapshot and never writes ETag entries. `seconds` bounds
    every read, the login read included.
    """

    def __init__(
        self,
        purpose: str,
        seconds: float,
        http: Http = gh_http,
        root: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        deadline = clock() + seconds
        self.ns = resolve_namespace(
            http, timeout=min(IDENTITY_TIMEOUT_SECONDS, seconds)
        )
        left = deadline - clock()
        if left <= 0:
            raise ReadBlocked(f"{purpose}: the gh login read took too long")
        self.client = Client(
            self.ns, purpose, left, writable=False, http=http, clock=clock
        )
        self.purpose = purpose
        self.root = root

    def pull(self, number: int) -> dict[str, Any]:
        pull = self.client.json(f"repos/{na.REPO}/pulls/{number}")
        if not isinstance(pull, dict):
            raise ReadBlocked(f"malformed PR {number}")
        return pull

    def issue(self, number: int) -> dict[str, Any]:
        issue = self.client.json(f"repos/{na.REPO}/issues/{number}")
        if not isinstance(issue, dict):
            raise ReadBlocked(f"malformed issue {number}")
        return issue

    def checks(self, sha: str) -> str:
        """'green', 'failed' or 'pending', as checks_state() on the selector."""
        return na.checks_state({"statusCheckRollup": rollup(self.client, sha)})

    def pulls_for_branch(self, branch: str) -> list[dict[str, Any]]:
        owner = na.REPO.split("/")[0]
        head = quote(f"{owner}:{branch}", safe="")
        return self.client.pages(
            f"repos/{na.REPO}/pulls?state=all&head={head}&per_page=100"
        )

    def board_items(self) -> list[dict[str, Any]]:
        return board_items(self.client, rest_ids=True)

    def graphql(self, endpoint: str) -> Any:
        """GraphQL for the merge guard's edit evidence only. Logged by purpose."""
        if not endpoint.startswith("graphql?"):
            raise ValueError("fresh GraphQL reads take graphql?query=... only")
        query = parse_qs(endpoint.partition("?")[2], strict_parsing=True)["query"][0]
        if not re.match(r"query(?:\s|\{)", query):
            raise ValueError("only GraphQL queries are allowed")
        started = time.monotonic()
        try:
            out = run_gh(
                ["api", "graphql", "-f", f"query={query}"],
                timeout=int(max(1, self.client.remaining())),
            )
        except subprocess.CalledProcessError as error:
            self.ns.log_call(
                log_record(
                    f"{self.purpose}:graphql",
                    "graphql",
                    "error",
                    "error",
                    started,
                    None,
                )
            )
            raise ReadBlocked("GraphQL edit evidence read failed") from error
        except subprocess.TimeoutExpired as error:
            raise ReadBlocked("GraphQL edit evidence read timed out") from error
        self.ns.log_call(
            log_record(
                f"{self.purpose}:graphql", "graphql", 200, "graphql", started, None
            )
        )
        return json.loads(out)

    def rest_for_guard(self, endpoint: str) -> Any:
        if endpoint.startswith("graphql?"):
            return self.graphql(endpoint)
        return self.client.json(endpoint)

    def edit_evidence(self, number: int, rows: list[dict[str, Any]]) -> None:
        """Adds `last_edited_at` to each row with the merge guard's own code.
        Catches same-second edits that REST timestamps miss."""
        guard = load_guard(self.root)
        try:
            guard.comment_edit_markers(self.graphql, na.REPO, number, rows)
        except ValueError as error:
            raise ReadBlocked(f"edit evidence for PR {number}: {error}") from error

    def merge_errors(self, number: int, head: str) -> list[str]:
        """The full merge-guard result for this head: verdict, checks, files."""
        root = self.root or trusted_root()
        guard = load_guard(root)
        try:
            policy = guard.load_policy(root / ".github" / "epic-lanes.yml")
            snapshot = guard.collect(na.REPO, number, self.rest_for_guard)
        except ValueError as error:
            raise ReadBlocked(f"merge guard read for PR {number}: {error}") from error
        return [str(e) for e in guard.evaluate(policy, snapshot, head)]


def same_action(a: na.Action, action: dict[str, Any]) -> bool:
    return (
        a.action == action.get("action")
        and a.pr == action.get("pr")
        and a.issue == action.get("issue")
        and a.sha == action.get("sha")
        and a.lane == action.get("lane")
        and a.body_sha == action.get("body_sha")
    )


def recheck(
    agent: str,
    action: dict[str, Any],
    focus: frozenset[str],
    enabled_actions: frozenset[str],
    paused: bool,
    reader: FreshReader,
    completed_tickets: bool = False,
) -> str | None:
    """None when the selector would still pick this action, else why not.

    Reads only the target fresh and runs decide() on it again, so the rule is
    the selector's own. The target's `updated_at` and open state were already
    checked after the target lock (tick_gate.py check); they are not read twice.
    """
    kind = action.get("action")
    if kind in ("idle", "stop"):
        return None
    try:
        if action.get("pr"):
            number = int(action["pr"])
            evidence = reader.edit_evidence if kind in VERDICT_ACTIONS else None
            pull = reader.pull(number)
            if pull.get("state") != "open":
                return f"PR {number} is {pull.get('state')}"
            pr, _ = read_pr(reader.client, number, evidence, pull)
            linked = {
                str(n): sorted(na.labels(reader.issue(n)))
                for n in sorted(na.issue_numbers(pr["body"]))
            }
            state = {
                "prs": [pr],
                "items": reader.board_items() if kind == "adopt" else [],
                "linked_labels": linked,
                "merged_prs": [],
                "batch_order": [],
            }
            # A draft's own and inherited waits, with fresh dependency evidence.
            state["waiting"] = na.read_waiting(
                state, lambda n: issue_comments(reader.client, n), pr_comments=True
            )
            deps = na.resume_tickets(state)
            if deps and completed_tickets:
                state["completed_tickets"] = True
                state["tickets"] = {
                    str(n): read_ticket(reader.client, n) for n in sorted(deps)
                }
            elif deps:
                state["merged_prs"] = merged_pulls(reader.client)
        else:
            tail = str(action.get("issue") or "").rstrip("/").rsplit("/", 1)[-1]
            if not tail.isdigit():
                raise ValueError("action has no pr and no issue")
            # Prerequisite tickets are read fresh too, never from the snapshot.
            state = build_state(
                reader.client,
                set(),
                pr_details=False,
                readiness_for={int(tail)},
                completed_tickets=completed_tickets,
            )
    except (KeyError, TypeError, AttributeError) as error:
        raise ReadBlocked(f"malformed GitHub data: {error!r}") from error
    now = na.decide(
        agent.capitalize(),
        state,
        paused,
        focus=focus,
        enabled=enabled_actions,
        completed_tickets=completed_tickets,
        # This check is the fresh recheck that makes free claims safe.
        free_claims=True,
    )
    if any(same_action(a, action) for a in now):
        return None
    first = now[0]
    target = f"PR {first.pr}" if first.pr else (first.issue or "")
    return f"GitHub changed: the selector now gives {first.action} {target} ({first.reason})"
