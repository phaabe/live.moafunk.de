"""Read local epic runners and GitHub; publish atomic Prometheus text files.

Runs on the host with its existing gh login. Containers receive only metrics.
No prompts, model output, credentials, or repository files are exported.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Callable
import fcntl
import json
import logging
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

import next_action as epic

# GitHub and the existing selector use heterogeneous JSON objects.
Json = dict[str, Any]
AGENTS = ("claude", "codex")
STATUSES = ("Backlog", "Ready", "In progress", "In review", "Done", "Unknown")
ACTIONS = (
    "stop",
    "idle",
    "escalate",
    "merge",
    "fix",
    "fix-checks",
    "resolve-conflict",
    "review",
    "continue",
    "claim",
    "wait",
)
REPO_URL = f"https://github.com/{epic.REPO}"
LEAF_BOX = re.compile(r"^- \[([ xX])\] \*\*([A-Z]\d+\.\d+\.\d+)\*\*", re.M)
LEAF_TEXT = re.compile(r"^- \[[ xX]\] \*\*([A-Z]\d+\.\d+\.\d+)\*\*[ \t]*(.*)$", re.M)
PARENT_LINE = re.compile(r"^Parent:[ \t]*(\S+)", re.M)
PLAN_TITLE = re.compile(r"^\[([A-Z]\d+(?:\.\d+)*)\][ \t]*")
# Leaf text starts with markers such as "**Wave 0 (v3).**" or "v3:".
LEAF_MARKERS = re.compile(r"^(?:\*\*[^*]+\*\*[ \t]*|v\d+(?:[ \t]*\([^)]*\))?:[ \t]*)+")


class Metrics:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.names: set[str] = set()

    def add(self, name: str, value: float, **labels: str) -> None:
        name = f"epic_{name}"
        if not math.isfinite(value):
            raise ValueError("metric must be finite")
        if name not in self.names:
            self.lines.extend(
                [
                    f"# HELP {name} {name.removeprefix('epic_').replace('_', ' ')}",
                    f"# TYPE {name} gauge",
                ]
            )
            self.names.add(name)
        escaped = []
        for key, value_text in sorted(labels.items()):
            text = (
                str(value_text)[:300]
                .replace("\\", "\\\\")
                .replace("\n", "\\n")
                .replace('"', '\\"')
            )
            escaped.append(f'{key}="{text}"')
        suffix = "{" + ",".join(escaped) + "}" if escaped else ""
        self.lines.append(f"{name}{suffix} {float(value):.17g}")

    def render(self) -> str:
        return "\n".join(self.lines) + "\n"


def atomic_write(path: Path, text: str) -> None:
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as out:
        temporary = Path(out.name)
        try:
            out.write(text)
            out.flush()
            # Only the sanitized metrics directory is mounted into Docker.
            temporary.chmod(0o644)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def read_object(path: Path, *, allow_empty: bool = False) -> Json | None:
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    if allow_empty and not text.strip():
        return None
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"expected object: {path.name}")
    return data


def positive_number(data: Json, key: str) -> float:
    value = data[key]
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"invalid {key}")
    return float(value)


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def action_labels(action: Json) -> dict[str, str]:
    if not isinstance(action, dict):
        raise ValueError("action must be an object")
    kind = action.get("action", "unknown")
    kind = kind if kind in ACTIONS else "unknown"
    target = ""
    if type(action.get("pr")) is int and action["pr"] > 0:
        target = f"{REPO_URL}/pull/{action['pr']}"
    elif re.fullmatch(
        re.escape(REPO_URL) + r"/issues/\d+", str(action.get("issue", ""))
    ):
        target = action["issue"]
    return {"action": kind, "target": target}


def short(text: str, limit: int = 75, *, sentence: bool = True) -> str:
    """Plain text cut to `limit` characters; leaf text keeps its first sentence."""
    text = re.sub(r"[`*]", "", LEAF_MARKERS.sub("", text.strip()))
    if sentence:
        text = re.split(r"(?<=[.;:])\s", text, maxsplit=1)[0].rstrip(".;:")
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def plan_title(item: Json) -> str:
    """ "[O1.2] Remove the trigger" -> "O1.2 · Remove the trigger"."""
    title = (item.get("content") or {}).get("title") or item.get("title") or ""
    title = PLAN_TITLE.sub(lambda m: f"{m.group(1)} · ", title)
    return short(title.removeprefix("Epic: "), 80, sentence=False)


def task_contexts(state: Json) -> list[dict[str, str]]:
    """Epic > area > task > subtask > leaf > PR for every issue and PR target.

    Panels join these rows onto action metrics by `target`.
    """
    issues: dict[int, Json] = {}
    for item in state["items"]:
        content = item.get("content") or {}
        if content.get("type") == "Issue" and content.get("url", "").startswith(
            f"{REPO_URL}/issues/"
        ):
            issues[content["number"]] = item
    done = epic.done_leaves(state)
    # The epic's batch tables decide which open leaf an agent takes first.
    batch = [
        leaf
        for comment in state.get("batch_order", [])
        for _, row in epic.BATCH_ROW.findall(comment)
        for leaf in epic.LEAF.findall(row)
    ]

    def batch_rank(leaf: str) -> int:
        return batch.index(leaf) if leaf in batch else len(batch)

    def chain(number: int | None) -> dict[str, str]:
        labels = dict.fromkeys(
            ("epic", "epic_url", "area", "task", "task_url", "subtask", "subtask_url"),
            "",
        )
        if epic_item := issues.get(epic.EPIC):
            labels["epic"] = plan_title(epic_item)
            labels["epic_url"] = epic_item["content"]["url"]
        item = issues.get(number) if number != epic.EPIC else None
        if item is None:
            return labels
        labels["area"] = str(item.get("area") or "")
        parent_line = PARENT_LINE.search(item["content"].get("body") or "")
        parent_number = (
            epic.ISSUE_URL.fullmatch(parent_line.group(1)) if parent_line else None
        )
        parent = issues.get(int(parent_number.group(1))) if parent_number else None
        task, subtask = (
            (parent, item)
            if parent and item.get("level") == "Subtask"
            else (item, None)
        )
        labels["task"], labels["task_url"] = plan_title(task), task["content"]["url"]
        if subtask:
            labels["subtask"] = plan_title(subtask)
            labels["subtask_url"] = subtask["content"]["url"]
        return labels

    def leaf_texts(number: int | None) -> dict[str, str]:
        item = issues.get(number) if number else None
        body = (item or {}).get("content", {}).get("body") or ""
        return {leaf: short(text) for leaf, text in LEAF_TEXT.findall(body)}

    def describe(leaves: list[str], texts: dict[str, str]) -> str:
        if not leaves:
            return ""
        first = (
            f"{leaves[0]} · {texts[leaves[0]]}" if texts.get(leaves[0]) else leaves[0]
        )
        return first + (f" (+{len(leaves) - 1} more)" if len(leaves) > 1 else "")

    rows = []
    for number, item in issues.items():
        if number == epic.EPIC or item.get("level") not in ("Task", "Subtask"):
            continue
        texts = leaf_texts(number)
        open_leaves = sorted(
            (leaf for leaf in texts if leaf not in done), key=batch_rank
        )
        rows.append(
            {
                "target": item["content"]["url"],
                **chain(number),
                "leaf": describe(open_leaves, texts),
                "leaves_done": f"{len(texts) - len(open_leaves)}/{len(texts)}"
                if texts
                else "",
                "pr": "",
            }
        )
    open_numbers = {pr["number"] for pr in state["prs"]}
    merged = [pr for pr in state["merged_prs"] if pr.get("number") not in open_numbers]
    for pr in [*state["prs"], *merged]:
        body = pr.get("body") or ""
        number = min(epic.issue_numbers(body), default=None)
        line = epic.LEAF_IDS_LINE.search(body)
        leaves = epic.LEAF.findall(line.group(1)) if line else []
        leaf = describe(leaves, leaf_texts(number))
        if not leaf and line and "setup" in line.group(1).lower():
            leaf = "setup (loop and rule files)"
        title = pr.get("title") or ""
        rows.append(
            {
                "target": f"{REPO_URL}/pull/{pr['number']}",
                **chain(number),
                "leaf": leaf,
                "leaves_done": "",
                "pr": f"PR {pr['number']}"
                + (f" · {short(title, 80, sentence=False)}" if title else ""),
            }
        )
    return rows


def task_levels(row: dict[str, str]) -> list[dict[str, str]]:
    """One row per level of a target's path, indented so panels read as a tree."""
    levels = (
        ("Epic", row["epic"], row["epic_url"]),
        ("Area", row["area"], ""),
        ("Task", row["task"], row["task_url"]),
        ("Subtask", row["subtask"], row["subtask_url"]),
        # A leaf lives in its issue body; link that issue.
        ("Leaf", row["leaf"], row["subtask_url"] or row["task_url"]),
        ("PR", row["pr"], row["target"] if "/pull/" in row["target"] else ""),
    )
    rows = []
    for level, text, url in levels:
        if text:
            indent = "\u2003" * len(rows) + ("└ " if rows else "")
            rows.append(
                {
                    "target": row["target"],
                    "depth": str(len(rows) + 1),
                    "level": level,
                    "text": indent + text,
                    "url": url,
                }
            )
    return rows


def runner_metrics(
    state_dir: Path,
    paused: bool,
    now: float,
    alive: Callable[[int], bool] = process_alive,
) -> str:
    metrics = Metrics()
    metrics.add("local_snapshot_timestamp_seconds", now)
    metrics.add("pause_requested", int(paused))
    for agent in AGENTS:
        sample = Metrics()
        try:
            lock = state_dir / f"{agent}.lock"
            owner = read_object(lock / "owner.json")
            state = "inactive"
            if owner is not None:
                pid = positive_number(owner, "pid")
                if not pid.is_integer():
                    raise ValueError("invalid pid")
                started = positive_number(owner, "started_at")
                budget = positive_number(owner, "max_age")
                if started > now + 5:
                    raise ValueError("runner timestamp is in the future")
                live = alive(int(pid))
                elapsed = max(0, now - started)
                state = "running" if live else "orphaned"
                if live and elapsed > budget:
                    state = "overdue"
                sample.add("tick_elapsed_seconds", elapsed, agent=agent)
                # Shell redirection creates this file before the selector runs.
                action = read_object(lock / "action.json", allow_empty=True)
                if action is not None:
                    sample.add(
                        "current_action_info", 1, agent=agent, **action_labels(action)
                    )
            elif lock.exists():
                state = "unknown"
            sample.add("runner_state", 1, agent=agent, state=state)
            gate = read_object(state_dir / f"{agent}-gate.json")
            if gate is not None:
                sample.add(
                    "last_session_success_timestamp_seconds",
                    positive_number(gate, "at"),
                    agent=agent,
                )
                sample.add(
                    "last_successful_action_info",
                    1,
                    agent=agent,
                    **action_labels(gate["action"]),
                )
            log = state_dir / f"{agent}.log"
            if log.exists():
                sample.add(
                    "log_modified_timestamp_seconds", log.stat().st_mtime, agent=agent
                )
                # Bounded tail; never publish arbitrary log or model text.
                with log.open("rb") as stream:
                    stream.seek(max(0, log.stat().st_size - 131072))
                    tail = stream.read(131072).decode("utf-8", errors="replace")
                finishes = re.findall(r"^tick: finished exit=(\d+)\s*$", tail, re.M)
                if finishes:
                    sample.add(
                        "last_observed_exit_code", int(finishes[-1]), agent=agent
                    )
            metrics.lines.extend(sample.lines)
            metrics.add("runner_read_success", 1, agent=agent)
        except (OSError, ValueError, KeyError, TypeError, OverflowError):
            logging.warning("Cannot read %s runner metadata", agent)
            metrics.add("runner_read_success", 0, agent=agent)
    # Deduplicate HELP/TYPE lines shared by the two runner samples.
    seen: set[str] = set()
    lines = []
    for line in metrics.lines:
        if line.startswith("#"):
            if line in seen:
                continue
            seen.add(line)
        lines.append(line)
    return "\n".join(lines) + "\n"


def github_metrics(state: Json, now: float) -> str:
    metrics = Metrics()
    # Project boards may contain issues from other repositories.
    items = [
        item
        for item in state["items"]
        if (item.get("content") or {}).get("type") == "Issue"
        and re.fullmatch(
            re.escape(REPO_URL) + r"/issues/\d+",
            (item.get("content") or {}).get("url", ""),
        )
    ]
    prs = [pr for pr in state["prs"] if pr.get("baseRefName") in epic.BASES]
    filtered = {**state, "items": items, "prs": prs}
    metrics.add("github_snapshot_timestamp_seconds", now)
    for agent in epic.AGENTS:
        label = agent.lower()
        mine = [item for item in items if item.get("executor") == agent]
        counts = Counter(
            item.get("status") if item.get("status") in STATUSES else "Unknown"
            for item in mine
        )
        for status in STATUSES:
            metrics.add("issues", counts[status], agent=label, status=status)
        leaves: dict[str, bool] = {}
        for item in mine:
            for checked, leaf in LEAF_BOX.findall(item["content"].get("body") or ""):
                leaves[leaf] = leaves.get(leaf, False) or checked.lower() == "x"
        metrics.add("checklist_leaves", len(leaves), agent=label, state="total")
        metrics.add(
            "checklist_leaves", sum(leaves.values()), agent=label, state="checked"
        )
        for action in epic.decide(agent, filtered, include_waiting=True):
            metrics.add(
                "queued_action_info",
                1,
                agent=label,
                **action_labels(action.__dict__),
                reason=action.reason,
            )
        owned = [pr for pr in prs if epic.pr_author(pr) == agent]
        for checks in ("green", "failed", "pending", "unknown"):
            metrics.add(
                "open_prs",
                sum(
                    (
                        epic.checks_state(pr)
                        if pr.get("statusCheckRollup")
                        else "unknown"
                    )
                    == checks
                    for pr in owned
                ),
                agent=label,
                checks=checks,
            )
        metrics.add(
            "needs_operator",
            sum(epic.ESCALATION_LABEL in epic.labels(item) for item in mine)
            + sum(epic.ESCALATION_LABEL in epic.labels(pr) for pr in owned),
            agent=label,
        )
        metrics.add(
            "merged_prs_in_window",
            sum(epic.pr_author(pr) == agent for pr in state["merged_prs"]),
            agent=label,
        )
        for pr in owned:
            verdicts = epic.verdicts(pr, epic.other(agent))
            latest = verdicts[-1] if verdicts else None
            review = (
                latest["state"].lower()
                if latest and latest["sha"] == pr["headRefOid"]
                else "waiting"
            )
            metrics.add(
                "pr_info",
                1,
                agent=label,
                target=f"{REPO_URL}/pull/{pr['number']}",
                title=pr.get("title", ""),
                review=review,
                checks=epic.checks_state(pr)
                if pr.get("statusCheckRollup")
                else "unknown",
                draft=str(bool(pr.get("isDraft"))).lower(),
            )
    metrics.add("unattributed_prs", sum(epic.pr_author(pr) is None for pr in prs))
    for row in task_contexts(filtered):
        metrics.add("task_context_info", 1, **row)
        for level in task_levels(row):
            metrics.add("task_level_info", 1, **level)
    return metrics.render()


def fetch_snapshot(timeout: float) -> Json:
    command = [sys.executable, str(Path(__file__).resolve()), "--fetch-state"]
    with subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as process:
        try:
            output, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise
        if process.returncode:
            raise RuntimeError(f"GitHub collection exited {process.returncode}")
    data = json.loads(output)
    for key in ("items", "prs", "merged_prs"):
        if not isinstance(data.get(key), list):
            raise ValueError(f"missing GitHub {key}")
    return data


def collect_github(
    output: Path, timeout: float, fetch: Callable[[float], Json] = fetch_snapshot
) -> bool:
    began = time.monotonic()
    ok = False
    try:
        metrics = github_metrics(fetch(timeout), time.time())
        atomic_write(output / "github.prom", metrics)
        ok = True
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as error:
        # Keep the previous snapshot and its timestamp. Never replace failure with zero work.
        logging.error("GitHub collection failed: %s", type(error).__name__)
    health = Metrics()
    health.add("github_collection_success", int(ok))
    health.add("github_collection_duration_seconds", time.monotonic() - began)
    health.add("github_attempt_timestamp_seconds", time.time())
    atomic_write(output / "github-health.prom", health.render())
    return ok


def run(args: argparse.Namespace) -> None:
    stopped = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stopped.set())

    def github_loop() -> None:
        while not stopped.is_set():
            collect_github(args.output, args.github_timeout)
            stopped.wait(args.github_interval)

    thread = threading.Thread(target=github_loop, daemon=True)
    thread.start()
    try:
        while not stopped.is_set():
            atomic_write(
                args.output / "runners.prom",
                runner_metrics(args.state_dir, args.pause_file.exists(), time.time()),
            )
            stopped.wait(args.interval)
    finally:
        stopped.set()
        thread.join(timeout=args.github_timeout + 5)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("tools/agent-monitoring/runtime/metrics")
    )
    parser.add_argument(
        "--state-dir", type=Path, default=Path.home() / ".local/state/epic-loop"
    )
    parser.add_argument("--pause-file", type=Path, default=Path.home() / ".epic-pause")
    parser.add_argument("--interval", type=float, default=5)
    parser.add_argument("--github-interval", type=float, default=120)
    parser.add_argument("--github-timeout", type=float, default=90)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--fetch-state", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    if args.fetch_state:
        print(json.dumps(epic.fetch_state()))
        return
    if any(
        not math.isfinite(value) or value <= 0
        for value in (args.interval, args.github_interval, args.github_timeout)
    ):
        parser.error("intervals and timeout must be finite positive seconds")
    args.output.mkdir(parents=True, exist_ok=True)
    with (args.output.parent / "collector.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("another collector owns this output directory")
        if args.once:
            atomic_write(
                args.output / "runners.prom",
                runner_metrics(args.state_dir, args.pause_file.exists(), time.time()),
            )
            if not collect_github(args.output, args.github_timeout):
                raise SystemExit(1)
        else:
            run(args)


if __name__ == "__main__":
    main()
