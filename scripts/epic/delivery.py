"""Delivery and handoff data for the agent monitor; the runners never read it.

Handoff: a PR waits for review when `decide(reviewer kind)` offers `review`
for its head. The clock starts on the first successful observation of that
head and survives restarts (`runtime/handoff.json`). GitHub only knows kinds
(Executor: Claude / Reviewer: Codex on one account), so all of this is per
kind, not per agent.

Delivery: PR open and merge times and review rounds from REST calls only
(the GraphQL budget is the scarce one). Merged PRs of the last 14 days are
cached and their comments revalidated every 30 min. A PR whose comment
history is incomplete is reported, never guessed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import json
import logging
import math
import os
from pathlib import Path
import re
import statistics
import subprocess
import tempfile
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import next_action as epic
from ticks import Sink

Json = dict[str, Any]
LOCAL = ZoneInfo("Europe/Berlin")
REPO_URL = f"https://github.com/{epic.REPO}"
KINDS = tuple(agent.lower() for agent in epic.AGENTS)
WAIT_KEY = re.compile(rf"{re.escape(REPO_URL)}/pull/[0-9]{{1,9}}@[0-9a-f]{{40}}")
STALLED_AFTER = 30 * 60
# The GitHub poll runs every 2 min; older observations cannot flag a stall.
HANDOFF_FRESH = 5 * 60
WINDOW_DAYS = 14
MEDIAN_DAYS = 7
REVALIDATE = 30 * 60
FLOW_ROWS = 14
MAX_CLOSED_PAGES = 10
MAX_COMMENTS = 1000
LEAF_BOX = re.compile(r"^- \[([ xX])\] \*\*([A-Z]\d+\.\d+\.\d+)\*\*", re.M)


@dataclass(frozen=True)
class Wait:
    target: str
    head: str
    waiter_kind: str
    waits_for_kind: str
    since: float


@dataclass(frozen=True)
class Handoff:
    observed_at: float
    waits: tuple[Wait, ...]


def waiting(state: Json) -> dict[str, tuple[str, str, str, str]]:
    """PR heads waiting for a review, by `target@head`.

    Not paused on purpose: a pause does not change what a PR waits for, and
    the stalled rule excludes the pause itself.
    """
    found: dict[str, tuple[str, str, str, str]] = {}
    for reviewer in epic.AGENTS:
        for action in epic.decide(reviewer, state):
            if action.action != "review" or action.pr is None or not action.sha:
                continue
            target = f"{REPO_URL}/pull/{action.pr}"
            found[f"{target}@{action.sha}"] = (
                target,
                action.sha,
                epic.other(reviewer).lower(),
                reviewer.lower(),
            )
    return found


def atomic_json(path: Path, data: Json) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as out:
        temporary = Path(out.name)
        try:
            json.dump(data, out)
            out.flush()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def finite(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)  # type: ignore[arg-type]


class HandoffClock:
    """First-seen times per waiting PR head, saved in one JSON file."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> Handoff | None:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError, RecursionError):
            logging.warning("Handoff state is unreadable; starting new clocks")
            return None
        try:
            waits = tuple(
                Wait(**{**row, "since": float(row["since"])}) for row in data["waits"]
            )
            observed = float(data["observed_at"])
        except (KeyError, TypeError, ValueError):
            logging.warning("Handoff state is invalid; starting new clocks")
            return None
        if (
            data.get("v") != 1
            or not finite(observed)
            or not all(
                WAIT_KEY.fullmatch(f"{w.target}@{w.head}")
                and w.waiter_kind in KINDS
                and w.waits_for_kind in KINDS
                and finite(w.since)
                for w in waits
            )
        ):
            logging.warning("Handoff state is invalid; starting new clocks")
            return None
        return Handoff(observed, waits)

    def observe(self, state: Json, now: float) -> Handoff:
        """Restart the clock for new heads; drop PRs that no longer wait.

        Nothing is saved: the caller saves only after it published the poll.
        """
        before = self.load()
        seen = {f"{w.target}@{w.head}": w.since for w in before.waits} if before else {}
        waits = tuple(
            Wait(target, head, waiter, waits_for, min(seen.get(key, now), now))
            for key, (target, head, waiter, waits_for) in sorted(waiting(state).items())
        )
        return Handoff(now, waits)

    def save(self, handoff: Handoff) -> None:
        try:
            atomic_json(
                self.path,
                {
                    "v": 1,
                    "observed_at": handoff.observed_at,
                    "waits": [asdict(wait) for wait in handoff.waits],
                },
            )
        except OSError:
            # The metrics still show this observation; the clock may restart.
            logging.warning("Cannot save handoff state")


def handoff_metrics(
    metrics: Sink,
    handoff: Handoff | None,
    presence: list[tuple[str, str]],
    paused: bool,
    now: float,
    registry_ok: bool = True,
) -> None:
    """Waits and the suspected stall; `presence` is (kind, presence) per agent."""
    if handoff is None:
        metrics.add("handoff_stalled", 0)
        return
    metrics.add("handoff_observed_timestamp_seconds", handoff.observed_at)
    for wait in handoff.waits:
        metrics.add(
            "handoff_wait_seconds",
            max(now - wait.since, 0),
            waiter_kind=wait.waiter_kind,
            waits_for_kind=wait.waits_for_kind,
            target=wait.target,
        )
    long_waits = {
        wait.waiter_kind for wait in handoff.waits if now - wait.since > STALLED_AFTER
    }
    # One agent that runs, just started or cannot be read may still act.
    active = any(
        kind in KINDS and state in ("running", "new", "unknown")
        for kind, state in presence
    )
    # A rejected or conflicting registration hides an agent that may act.
    stalled = (
        long_waits == set(KINDS)
        and registry_ok
        and not paused
        and not active
        and now - handoff.observed_at <= HANDOFF_FRESH
    )
    metrics.add("handoff_stalled", int(stalled))


def parse_time(text: object) -> float:
    if not isinstance(text, str):
        raise ValueError("time is not a string")
    at = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if at.tzinfo is None:
        raise ValueError(f"time without zone: {text}")
    return at.timestamp()


def kind_of(pr: Json) -> str | None:
    author = epic.pr_author(pr)
    return author.lower() if author else None


def rounds(comments: list[Json], executor: str | None) -> int | None:
    """Reviewed heads: distinct heads with a valid verdict by the reviewer."""
    if executor is None:
        return None
    reviewer = epic.other(executor.capitalize())
    return len({v["sha"] for v in epic.verdicts({"comments": comments}, reviewer)})


def fetch_delivery(
    cache: Json | None, now: float, gh: Callable[[list[str]], Any] = epic.gh_json
) -> Json:
    """Open PRs and PRs merged in the window, with review rounds.

    REST only. A merged PR's comments are fetched when it is new, when it
    changed, or every 30 min. Any failed call fails the whole fetch, so the
    caller keeps its last snapshot.
    """
    since = now - WINDOW_DAYS * 86400
    old = (cache or {}).get("merged", {})
    open_prs: list[Json] = []
    merged: dict[str, Json] = {}
    for base in epic.BASES:
        path = f"repos/{epic.REPO}/pulls?base={quote(base, safe='')}&per_page=100"
        for page in gh(["api", "--paginate", "--slurp", f"{path}&state=open"]):
            for row in page:
                open_prs.append(
                    {
                        "number": int(row["number"]),
                        "created": parse_time(row["created_at"]),
                        "executor": kind_of(row),
                        "draft": bool(row.get("draft")),
                    }
                )
        for number in range(1, MAX_CLOSED_PAGES + 2):
            if number > MAX_CLOSED_PAGES:
                # Never publish a window that was cut short.
                raise RuntimeError("closed PRs exceed the page budget")
            # Newest updates first: a PR merged in the window was updated in it.
            page = gh(
                [
                    "api",
                    f"{path}&state=closed&sort=updated&direction=desc&page={number}",
                ]
            )
            for row in page:
                if not row.get("merged_at") or parse_time(row["merged_at"]) < since:
                    continue
                key = str(int(row["number"]))
                entry = {
                    "number": int(row["number"]),
                    "created": parse_time(row["created_at"]),
                    "merged": parse_time(row["merged_at"]),
                    "updated": str(row.get("updated_at")),
                    "executor": kind_of(row),
                }
                cached = old.get(key)
                if (
                    cached
                    and cached.get("updated") == entry["updated"]
                    and now - cached.get("checked", 0) < REVALIDATE
                ):
                    merged[key] = cached
                    continue
                merged[key] = entry | comment_rounds(gh, entry, now)
            if len(page) < 100 or (page and parse_time(page[-1]["updated_at"]) < since):
                break
    return {"v": 1, "fetched_at": now, "open": open_prs, "merged": merged}


def comment_rounds(gh: Callable[[list[str]], Any], entry: Json, now: float) -> Json:
    """Review rounds from the full comment history, read one page at a time.

    A history over MAX_COMMENTS is not read (memory stays bounded) and, like
    a history that does not match the count, is reported as incomplete.
    """
    n = entry["number"]
    incomplete = {"rounds": None, "complete": False, "checked": now}
    count = gh(["api", f"repos/{epic.REPO}/issues/{n}", "--jq", "{comments}"])[
        "comments"
    ]
    if type(count) is not int or not 0 <= count <= MAX_COMMENTS:
        return incomplete
    rows: list[Json] = []
    for page in range(1, MAX_COMMENTS // 100 + 2):
        batch = gh(
            [
                "api",
                f"repos/{epic.REPO}/issues/{n}/comments?per_page=100&page={page}",
            ]
        )
        rows += batch
        if len(batch) < 100 or len(rows) > count:
            break
    try:
        comments = epic.comments_from_rest(rows, count)
    except ValueError:
        return incomplete
    return {
        "rounds": rounds(comments, entry["executor"]),
        "complete": True,
        "checked": now,
    }


def valid_delivery(data: object) -> bool:
    if not isinstance(data, dict) or data.get("v") != 1:
        return False
    if not finite(data.get("fetched_at")) or not isinstance(data.get("open"), list):
        return False
    merged = data.get("merged")
    if not isinstance(merged, dict):
        return False
    rows = [*data["open"], *merged.values()]
    return all(
        isinstance(row, dict)
        and type(row.get("number")) is int
        and finite(row.get("created"))
        and row.get("executor") in (*KINDS, None)
        for row in rows
    ) and all(
        finite(row.get("merged"))
        and type(row.get("complete")) is bool
        and (row.get("rounds") is None or type(row.get("rounds")) is int)
        for row in merged.values()
    )


def local_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, LOCAL).strftime("%Y-%m-%d")


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def delivery_metrics(
    metrics: Sink, data: Json, open_prs: list[Json] | None, now: float
) -> None:
    """Delivery numbers per kind. `open_prs`: the selector snapshot's PRs,
    whose comments are complete, for the review rounds of open PRs."""
    metrics.add("delivery_snapshot_timestamp_seconds", data["fetched_at"])
    merged = list(data["merged"].values())
    incomplete = sum(not row["complete"] for row in merged)
    metrics.add("delivery_complete", int(incomplete == 0))
    metrics.add("delivery_incomplete_prs", incomplete)
    # Calendar days: a day is not always 86 400 s long in Berlin.
    today = datetime.fromtimestamp(now, LOCAL).date()
    days = [str(today - timedelta(days=offset)) for offset in range(WINDOW_DAYS)]
    counts: dict[tuple[str, str], int] = {}
    for row in merged:
        key = (row["executor"] or "unknown", local_day(row["merged"]))
        counts[key] = counts.get(key, 0) + 1
    for kind in (*KINDS, "unknown"):
        for day in days:
            metrics.add(
                "merged_prs_day", counts.get((kind, day), 0), kind=kind, day=day
            )
    recent = [
        row
        for row in merged
        if row["merged"] >= now - MEDIAN_DAYS * 86400
        and row["complete"]
        and row["rounds"] is not None
    ]
    if recent:
        metrics.add(
            "review_rounds_median", statistics.median(r["rounds"] for r in recent)
        )
        metrics.add(
            "time_to_merge_median_seconds",
            statistics.median(r["merged"] - r["created"] for r in recent),
        )
    snapshot = {pr.get("number"): pr for pr in open_prs or []}
    flow: list[tuple[float, Json, str, int]] = []
    for row in data["open"]:
        # Every open PR, for the cockpit's age and rounds columns.
        target = f"{REPO_URL}/pull/{row['number']}"
        metrics.add("pr_opened_timestamp_seconds", row["created"], target=target)
        pr = snapshot.get(row["number"])
        if pr is None:
            continue  # rounds unknown until the selector snapshot has it
        count = rounds(pr.get("comments") or [], row["executor"])
        if count is not None:
            metrics.add("pr_review_rounds", count, target=target)
            flow.append(
                (row["created"], row, "draft" if row["draft"] else "open", count)
            )
    for row in merged:
        if row["complete"] and row["rounds"] is not None and row["executor"]:
            flow.append((row["created"], row, "merged", row["rounds"]))
    flow.sort(key=lambda item: (item[0], item[1]["number"]), reverse=True)
    for _, row, state, count in flow[:FLOW_ROWS]:
        executor = row["executor"]
        metrics.add(
            "pr_flow_info",
            count,
            target=f"{REPO_URL}/pull/{row['number']}",
            executor=executor,
            reviewer=epic.other(executor.capitalize()).lower(),
            opened=iso(row["created"]),
            state=state,
        )


def area_leaves(metrics: Sink, items: list[Json]) -> None:
    """Checklist leaves per epic area and executor kind; a leaf counts once,
    with the kind and area of an issue that names them."""
    leaves: dict[str, tuple[str, str, bool]] = {}
    for item in items:
        executor = item.get("executor")
        kind = executor.lower() if executor in epic.AGENTS else "unknown"
        area = str(item.get("area") or "none")
        for checked, leaf in LEAF_BOX.findall(item["content"].get("body") or ""):
            done = checked.lower() == "x"
            if leaf in leaves:
                # Parent tasks repeat their leaves without an executor.
                old_area, old_kind, was_done = leaves[leaf]
                leaves[leaf] = (
                    old_area if old_area != "none" else area,
                    old_kind if old_kind != "unknown" else kind,
                    was_done or done,
                )
            else:
                leaves[leaf] = (area, kind, done)
    counts: dict[tuple[str, str, str], int] = {}
    for area, kind, done in leaves.values():
        key = (area, kind, "done" if done else "open")
        counts[key] = counts.get(key, 0) + 1
    for (area, kind, state), count in sorted(counts.items()):
        metrics.add("area_leaves", count, area=area, kind=kind, state=state)


def main_fetch(cache_path: Path, now: float) -> Json:
    """Run in a child process by the monitor, with a timeout."""
    try:
        cache = json.loads(cache_path.read_text())
    except (OSError, ValueError, RecursionError):
        cache = None
    if not valid_delivery(cache):
        cache = None
    try:
        return fetch_delivery(cache, now)
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f"gh exited {error.returncode}") from None
