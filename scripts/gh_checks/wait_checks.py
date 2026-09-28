#!/usr/bin/env python3
"""Wait for a PR's checks using only REST and git, then print the verified head SHA.

Usage: python3 scripts/gh_checks/wait_checks.py <pr-number> [--interval 60] [--timeout 1800]

Exit codes: 0 all checks passed, 1 a check failed, 2 timeout, 3 API/git/usage error.
On success the last stdout line is `HEAD_SHA=<sha>`. Merge with
`gh pr merge <n> --squash --delete-branch --match-head-commit <sha>`.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any

# GitHub's JSON objects vary by endpoint; validation is at the API boundary.
Json = dict[str, Any]
Api = Callable[[str, bool], Any]
Git = Callable[[list[str]], str]

OK_CONCLUSIONS = {"success", "neutral", "skipped"}
RUN_TYPES = {"opened", "synchronize", "reopened"}
FILTER_KEYS = ("branches", "branches-ignore", "paths", "paths-ignore")


class Unknown(Exception):
    """A workflow trigger we can't parse. Callers treat the workflow as expected."""


class ChecksFailed(Exception):
    pass


class TimedOut(Exception):
    pass


class ToolError(Exception):
    pass


# ---- workflow trigger parsing -------------------------------------------------


def _clean(value: str) -> str:
    value = re.sub(r"\s+#.*$", "", value).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    return value


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _significant(lines: list[str]) -> list[str]:
    return [ln for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]


def _children(lines: list[str], start: int) -> list[str]:
    """Lines indented deeper than lines[start], directly after it."""
    base = _indent(lines[start])
    out = []
    for ln in lines[start + 1 :]:
        if _indent(ln) <= base:
            break
        out.append(ln)
    return out


def _flow_list(value: str) -> list[str]:
    if not (value.startswith("[") and value.endswith("]")):
        raise Unknown(f"unsupported value: {value}")
    inner = value[1:-1]
    # Quoted items can hold commas; nested lists/maps are not plain strings.
    if any(ch in inner for ch in "'\"[]{}"):
        raise Unknown(f"quoted or nested flow list: {value}")
    return [_clean(v) for v in value[1:-1].split(",") if _clean(v)]


def _keys(block: list[str]) -> dict[str, tuple[str, list[str]]]:
    """Map each top-level key in block to (inline value, child lines)."""
    if not block:
        return {}
    level = min(_indent(ln) for ln in block)
    out: dict[str, tuple[str, list[str]]] = {}
    for i, ln in enumerate(block):
        if _indent(ln) != level:
            continue
        m = re.match(r"\s*([\w-]+|'[^']*'|\"[^\"]*\")\s*:(.*)$", ln)
        if not m:
            raise Unknown(f"unsupported line: {ln.strip()}")
        out[_clean(m.group(1))] = (_clean(m.group(2)), _children(block, i))
    return out


def _string_list(inline: str, child: list[str]) -> list[str]:
    if inline:
        return _flow_list(inline)
    items = []
    for ln in child:
        m = re.match(r"\s*-\s*(.*)$", ln)
        if not m:
            raise Unknown(f"unsupported list item: {ln.strip()}")
        items.append(_clean(m.group(1)))
    return items


def pull_request_filters(text: str) -> dict[str, list[str]] | None:
    """Return the `pull_request` filters of a workflow, or None if it has no such trigger.

    Raises Unknown when the trigger block uses syntax this parser doesn't cover.
    """
    lines = _significant(text.splitlines())
    for i, ln in enumerate(lines):
        m = re.match(r"""^(on|'on'|"on"|true)\s*:(.*)$""", ln)
        if not m:
            continue
        inline = _clean(m.group(2))
        if inline:
            if inline.startswith("{"):
                raise Unknown(f"flow mapping trigger: {inline}")
            names = _flow_list(inline) if inline.startswith("[") else [inline]
            return {} if "pull_request" in names else None
        triggers = _keys(_children(lines, i))
        if "pull_request" not in triggers:
            return None
        value, child = triggers["pull_request"]
        if value not in ("", "null", "{}", "~"):
            raise Unknown(f"unsupported pull_request value: {value}")
        opts = _keys(child)
        filters: dict[str, list[str]] = {}
        for key, (val, sub) in opts.items():
            if key in FILTER_KEYS:
                filters[key] = _string_list(val, sub)
            elif key == "types":
                if not RUN_TYPES & set(_string_list(val, sub)):
                    return None
        return filters
    raise Unknown("no `on:` block")


def glob_regex(pattern: str) -> re.Pattern[str]:
    """GitHub filter pattern as a regex.

    `*` stays in one path segment, `**` crosses segments, `?` / `+` mean zero-or-one /
    one-or-more of the preceding character, `[...]` is a character class, `\\` escapes.
    Raises Unknown for patterns this doesn't cover.
    """
    out, i = "", 0
    quantifiable = False  # the last emitted atom can take `?` / `+`
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out, i, quantifiable = out + "(?:.*/)?", i + 3, False
        elif pattern.startswith("**", i):
            out, i, quantifiable = out + ".*", i + 2, False
        elif c == "*":
            out, i, quantifiable = out + "[^/]*", i + 1, False
        elif c in "?+":
            if not quantifiable:
                raise Unknown(f"unsupported pattern: {pattern}")
            out, i, quantifiable = out + c, i + 1, False
        elif c == "[":
            end = pattern.find("]", i + 1)
            if end == -1:
                raise Unknown(f"unsupported pattern: {pattern}")
            body = pattern[i + 1 : end].replace("\\", "\\\\")
            out, i, quantifiable = out + f"[{body}]", end + 1, True
        elif c == "\\" and i + 1 < len(pattern):
            out, i, quantifiable = out + re.escape(pattern[i + 1]), i + 2, True
        else:
            out, i, quantifiable = out + re.escape(c), i + 1, True
    try:
        return re.compile(out + r"\Z", re.DOTALL)
    except re.error as exc:
        raise Unknown(f"unsupported pattern: {pattern}") from exc


def _matches(patterns: list[str], value: str) -> bool:
    """Ordered match with `!` negation: the last matching pattern wins."""
    hit = False
    for p in patterns:
        negate = p.startswith("!")
        if glob_regex(p[1:] if negate else p).match(value):
            hit = not negate
    return hit


def workflow_runs_for(
    filters: dict[str, list[str]], base: str, files: list[str]
) -> bool:
    if "branches" in filters and not _matches(filters["branches"], base):
        return False
    if "branches-ignore" in filters and _matches(filters["branches-ignore"], base):
        return False
    if "paths" in filters and not any(_matches(filters["paths"], f) for f in files):
        return False
    if "paths-ignore" in filters and all(
        _matches(filters["paths-ignore"], f) for f in files
    ):
        return False
    return True


def expected_workflows(git: Git, base: str, head: str) -> set[str]:
    """Workflow paths that should run for this PR. Unparseable triggers count as expected.

    GitHub checks path filters against only the first 300 changed files. For bigger PRs a
    workflow that matches a later file may not run; we still expect it and time out.
    """
    diff = git(["diff", "--name-only", "-z", f"origin/{base}...{head}"])
    files = [f for f in diff.split("\0") if f]
    listing = git(["ls-tree", "--name-only", head, ".github/workflows/"]).splitlines()
    expected = set()
    for path in listing:
        if not path.endswith((".yml", ".yaml")):
            continue
        try:
            filters = pull_request_filters(git(["show", f"{head}:{path}"]))
            if filters is not None and workflow_runs_for(filters, base, files):
                expected.add(path)
        except Unknown:
            expected.add(path)
    return expected


# ---- check evaluation ---------------------------------------------------------


def _latest(items: list[Json], key: Callable[[Json], str]) -> dict[str, Json]:
    out: dict[str, Json] = {}
    for item in sorted(items, key=lambda x: x.get("id", 0)):
        out[key(item)] = item
    return out


def _check_key(check: Json) -> str:
    app = (check.get("app") or {}).get("slug", "?")
    return f"{app}/{check['name']}"


def evaluate(
    expected: set[str], runs: list[Json], check_runs: list[Json], status: Json
) -> tuple[str, list[str]]:
    """Return ("pass" | "pending" | "fail", reasons)."""
    failed: list[str] = []
    pending: list[str] = []
    latest_runs = _latest(runs, lambda r: r["path"])
    for path in sorted(expected - latest_runs.keys()):
        pending.append(f"workflow {path} has not started")
    for path, run in sorted(latest_runs.items()):
        if run.get("status") != "completed":
            pending.append(f"workflow {path} is {run.get('status')}")
        elif run.get("conclusion") not in OK_CONCLUSIONS:
            failed.append(f"workflow {path} concluded {run.get('conclusion')}")
    for name, cr in sorted(_latest(check_runs, _check_key).items()):
        if cr.get("status") != "completed":
            pending.append(f"check {name} is {cr.get('status')}")
        elif cr.get("conclusion") not in OK_CONCLUSIONS:
            failed.append(f"check {name} concluded {cr.get('conclusion')}")
    # With no commit statuses GitHub reports "pending"; ignore that case.
    # The combined state covers all statuses, also those past the first page.
    if status.get("total_count", 0) > 0:
        bad = [
            f"{st.get('context')}={st.get('state')}"
            for st in status.get("statuses", [])
            if st.get("state") != "success"
        ]
        detail = f" ({', '.join(bad)})" if bad else ""
        if status.get("state") == "pending":
            pending.append(f"commit statuses pending{detail}")
        elif status.get("state") != "success":
            failed.append(f"commit statuses {status.get('state')}{detail}")
    if failed:
        return "fail", failed
    if pending:
        return "pending", pending
    return "pass", []


# ---- polling ------------------------------------------------------------------


def _pages(api: Api, endpoint: str, field: str) -> list[Json]:
    return [item for page in api(endpoint, True) for item in page.get(field, [])]


def wait(
    api: Api,
    git: Git,
    number: int,
    interval: float,
    timeout: float,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    log: Callable[[str], None] = lambda msg: print(msg, file=sys.stderr),
) -> str:
    repo = "repos/{owner}/{repo}"
    deadline = clock() + timeout
    sha, base_ref = "", ""
    expected: set[str] = set()
    while True:
        pr = api(f"{repo}/pulls/{number}", False)
        if pr.get("state") != "open":
            raise ChecksFailed([f"PR is {pr.get('state')}, not open"])
        if pr.get("mergeable") is False:
            raise ChecksFailed(
                ["PR has merge conflicts; pull_request workflows will not run"]
            )
        head, base = pr["head"]["sha"], pr["base"]["ref"]
        if (head, base) != (sha, base_ref):
            if sha:
                log(
                    f"PR moved {base_ref}@{sha[:7]} -> {base}@{head[:7]}, starting over"
                )
            sha, base_ref = head, base
            git(
                [
                    "fetch",
                    "--quiet",
                    "origin",
                    f"+refs/heads/{base}:refs/remotes/origin/{base}",
                    f"+refs/pull/{number}/head:refs/remotes/origin/pr/{number}",
                ]
            )
            expected = expected_workflows(git, base, sha)
            log(f"head {sha[:7]}, expected workflows: {sorted(expected) or 'none'}")
        runs = _pages(
            api,
            f"{repo}/actions/runs?head_sha={sha}&event=pull_request&per_page=100",
            "workflow_runs",
        )
        check_runs = _pages(
            api, f"{repo}/commits/{sha}/check-runs?per_page=100", "check_runs"
        )
        status = api(f"{repo}/commits/{sha}/status?per_page=100", False)
        verdict, reasons = evaluate(expected, runs, check_runs, status)
        if verdict == "pass" and pr.get("mergeable") is None:
            verdict, reasons = "pending", ["GitHub is still computing mergeability"]
        if verdict == "pass":
            return sha
        if verdict == "fail":
            raise ChecksFailed(reasons)
        if clock() >= deadline:
            raise TimedOut(reasons)
        log("waiting: " + "; ".join(reasons))
        sleep(interval)


def gh_api(endpoint: str, paginate: bool) -> Any:
    cmd = ["gh", "api", endpoint]
    if paginate:
        cmd += ["--paginate", "--slurp"]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ToolError(f"gh api {endpoint} failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def run_git(args: list[str]) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ToolError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Wait for a PR's checks via REST.")
    parser.add_argument("number", type=int)
    parser.add_argument("--interval", type=float, default=60)
    parser.add_argument("--timeout", type=float, default=1800)
    args = parser.parse_args(argv)
    try:
        sha = wait(gh_api, run_git, args.number, args.interval, args.timeout)
    except ChecksFailed as exc:
        print("checks failed: " + "; ".join(exc.args[0]), file=sys.stderr)
        return 1
    except TimedOut as exc:
        print("timed out: " + "; ".join(exc.args[0]), file=sys.stderr)
        return 2
    except (ToolError, json.JSONDecodeError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    print(f"HEAD_SHA={sha}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
