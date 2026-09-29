"""Read local epic runners and GitHub; publish atomic Prometheus text files.

Runs on the host with its existing gh login. Containers receive only metrics.
No prompts, model output, credentials, or repository files are exported.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
import fcntl
import json
import logging
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, BinaryIO

import agents
import next_action as epic
import ticks

# GitHub and the existing selector use heterogeneous JSON objects.
Json = dict[str, Any]
# Presence severity; retired is 0 so "worst presence" ignores it.
# unknown: the runner's lock or gate could not be read this cycle.
PRESENCE = {"retired": 0, "running": 1, "idle": 2, "new": 3, "late": 4, "unknown": 5}
PRESENCE_ORDER = ("running", "idle", "new", "late", "unknown", "retired")
MAX_STATE_FILE = 65_536
TAIL = 131_072
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
    """Prometheus text, grouped by metric family.

    The exposition format needs each family's samples together, after one
    HELP and TYPE line; samples are kept per family in first-use order.
    """

    def __init__(self) -> None:
        self.families: dict[str, list[str]] = {}

    def add(
        self, name: str, value: float, *, metric_type: str = "gauge", **labels: str
    ) -> None:
        name = f"epic_{name}"
        if not math.isfinite(value):
            raise ValueError("metric must be finite")
        family = self.families.get(name)
        if family is None:
            family = self.families[name] = [
                f"# HELP {name} {name.removeprefix('epic_').replace('_', ' ')}",
                f"# TYPE {name} {metric_type}",
            ]
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
        family.append(f"{name}{suffix} {float(value):.17g}")

    def merge(self, other: Metrics) -> None:
        for name, family in other.families.items():
            if name in self.families:
                self.families[name].extend(family[2:])
            else:
                self.families[name] = list(family)

    @property
    def lines(self) -> list[str]:
        return [line for family in self.families.values() for line in family]

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


def read_agent_object(
    agent: agents.Agent, *parts: str, allow_empty: bool = False
) -> Json | None:
    """Like read_object, for files in a model-writable agent folder."""
    stream = agent.open(*parts)
    if stream is None:
        return None
    with stream:
        raw = stream.read(MAX_STATE_FILE + 1)
    if len(raw) > MAX_STATE_FILE:
        raise ValueError(f"{parts[-1]} is too large")
    if allow_empty and not raw.strip():
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except RecursionError as error:  # deep nesting in an untrusted file
        raise ValueError(f"too deeply nested: {parts[-1]}") from error
    if not isinstance(data, dict):
        raise ValueError(f"expected object: {parts[-1]}")
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


@dataclass
class Runner:
    """What the lock and gate files say about one agent's runner."""

    state: str | None = None
    started: float | None = None
    budget: float | None = None
    action: dict[str, str] | None = None
    gate_action: dict[str, str] | None = None


@dataclass
class Row:
    agent: agents.Agent
    presence: str
    action: dict[str, str] | None = None
    outcome_text: str = ""


class Ledgers:
    """Tick ledgers per agent, created and dropped as agents come and go."""

    def __init__(self, runtime: Path) -> None:
        self.runtime = runtime
        self.ledgers: dict[tuple[str, Path], ticks.LogLedger] = {}

    def opener(self, agent: agents.Agent, name: str) -> Callable[[], BinaryIO]:
        def opener() -> BinaryIO:
            stream = agent.open(name)
            if stream is None:
                raise FileNotFoundError(agent.home / name)
            return stream

        return opener

    def get(self, agent: agents.Agent) -> ticks.LogLedger:
        """The legacy log ledger."""
        key = (agent.id, agent.log)
        if key not in self.ledgers:
            self.ledgers[key] = ticks.LogLedger(
                agent.id,
                agent.log,
                self.runtime / agent.checkpoint_name,
                action_labels,
                self.opener(agent, agent.log_name),
            )
        return self.ledgers[key]

    def events(self, agent: agents.Agent) -> ticks.EventLedger:
        """The runner's tick events. Codex cannot write the state dir; the
        Claude model might, so its events are marked unverified."""
        path = agent.home / agent.events_name
        key = (agent.id, path)
        if key not in self.ledgers:
            self.ledgers[key] = ticks.EventLedger(
                agent.id,
                path,
                self.runtime / agent.events_checkpoint_name,
                action_labels,
                self.opener(agent, agent.events_name),
                source="events" if agent.kind == "codex" else "events_unverified",
            )
        ledger = self.ledgers[key]
        assert isinstance(ledger, ticks.EventLedger)
        return ledger

    def permissions(self, agent: agents.Agent) -> ticks.DecisionLedger:
        """Allow/deny counts of the Claude permission gate."""
        path = agent.home / PERMISSIONS_FILE
        key = (agent.id, path)
        if key not in self.ledgers:
            self.ledgers[key] = ticks.DecisionLedger(
                agent.id,
                path,
                self.runtime / agent.permissions_checkpoint_name,
                action_labels,
                self.opener(agent, PERMISSIONS_FILE),
            )
        ledger = self.ledgers[key]
        assert isinstance(ledger, ticks.DecisionLedger)
        return ledger

    def prune(self, active: list[agents.Agent]) -> None:
        """Drop ledgers and checkpoints of agents that are gone.

        A vanished agent starts fresh if it comes back: its log may return
        with a new inode, which would count old ticks again. This also covers
        agents that vanished while the collector was stopped.
        """
        keep = {(a.id, a.log) for a in active} | {
            (a.id, a.home / name)
            for a in active
            for name in (a.events_name, PERMISSIONS_FILE)
        }
        for key in [key for key in self.ledgers if key not in keep]:
            del self.ledgers[key]
        names = {
            name
            for a in active
            for name in (
                a.checkpoint_name,
                a.events_checkpoint_name,
                a.permissions_checkpoint_name,
            )
        }
        for pattern in ("ticks-*.json", "events-*.json", "permissions-*.json"):
            for path in self.runtime.glob(pattern):
                try:
                    if path.name not in names:
                        path.unlink()
                except OSError:
                    logging.warning("Cannot remove an old tick checkpoint")


def runner_sample(
    metrics: Metrics, agent: agents.Agent, now: float, alive: Callable[[int], bool]
) -> Runner:
    """Lock, gate and log tail of one agent. A read failure drops its sample."""
    sample = Metrics()
    runner = Runner()
    name = agent.id
    try:
        owner = read_agent_object(agent, agent.lock_name, "owner.json")
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
            runner.started, runner.budget = started, budget
            sample.add("tick_elapsed_seconds", elapsed, agent=name)
            # Whole runner budget: selector, model and kill grace.
            sample.add("tick_budget_seconds", budget, agent=name)
            # Shell redirection creates this file before the selector runs.
            action = read_agent_object(
                agent, agent.lock_name, "action.json", allow_empty=True
            )
            if action is not None:
                runner.action = action_labels(action)
                sample.add("current_action_info", 1, agent=name, **runner.action)
        elif agent.lstat(agent.lock_name) is not None:
            state = "unknown"
        sample.add("runner_state", 1, agent=name, state=state)
        gate = read_agent_object(agent, agent.gate_name)
        if gate is not None:
            sample.add(
                "last_session_success_timestamp_seconds",
                positive_number(gate, "at"),
                agent=name,
            )
            runner.gate_action = action_labels(gate["action"])
            sample.add(
                "last_successful_action_info", 1, agent=name, **runner.gate_action
            )
        stream = agent.open(agent.log_name)
        if stream is not None:
            with stream:
                info = os.fstat(stream.fileno())
                sample.add("log_modified_timestamp_seconds", info.st_mtime, agent=name)
                # Bounded tail; never publish arbitrary log or model text.
                stream.seek(max(0, info.st_size - TAIL))
                tail = stream.read(TAIL).decode("utf-8", errors="replace")
            finishes = re.findall(r"^tick: finished exit=(\d+)\s*$", tail, re.M)
            if finishes:
                sample.add("last_observed_exit_code", int(finishes[-1]), agent=name)
        metrics.merge(sample)
        metrics.add("runner_read_success", 1, agent=name)
        runner.state = state
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        logging.warning("Cannot read %s runner metadata", name)
        metrics.add("runner_read_success", 0, agent=name)
        return Runner()
    return runner


BACKOFF_FILE = "codex-backoff.json"
PERMISSIONS_FILE = "claude-permissions.log"


def permission_metrics(
    metrics: Metrics, ledger: ticks.DecisionLedger, now: float
) -> None:
    """Allow/deny counts; a gate that never ran has no file yet."""
    ok = True
    try:
        ledger.update(now)
        ledger.save()
    except FileNotFoundError:
        # Never read before: no gate decision yet. Read before: it vanished.
        ok = ledger.state is None or ledger.state["inode"] is None
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        ok = False
    if not ok:
        logging.warning("Cannot read %s permission log", ledger.agent)
    metrics.add("permission_read_success", int(ok), agent=ledger.agent)
    totals = (
        ledger.state["totals"] if ledger.state else dict.fromkeys(ticks.DECISIONS, 0)
    )
    for decision in ticks.DECISIONS:
        metrics.add(
            "permission_decisions_total",
            totals[decision],
            metric_type="counter",
            agent=ledger.agent,
            decision=decision,
        )


# The Codex runner's default retry delay, for entries stored without `until`.
BACKOFF_DEFAULT = 900
BACKOFF_KEY = re.compile(
    rf"pr:([1-9][0-9]{{0,8}}):([0-9a-f]{{7,40}})|issue:({re.escape(REPO_URL)}/issues/[1-9][0-9]{{0,8}})"
)


class Heads:
    """PR heads from the last successful GitHub poll (None before the first)."""

    def __init__(self) -> None:
        self.heads: dict[str, str] | None = None


LATEST = Heads()


def backoff_metrics(
    metrics: Metrics, agent: agents.Agent, now: float, heads: dict[str, str] | None
) -> None:
    """Active retry delays of the Codex runner; never the reason text."""
    if agent.kind != "codex":
        return
    try:
        entries = read_agent_object(agent, BACKOFF_FILE) or {}
        rows = []
        for key, entry in entries.items():
            match = BACKOFF_KEY.fullmatch(key)
            if match is None or not isinstance(entry, dict):
                raise ValueError("invalid backoff entry")
            at = positive_number(entry, "at")
            estimated = "until" not in entry
            until = (
                at + BACKOFF_DEFAULT if estimated else positive_number(entry, "until")
            )
            if until <= now:
                continue
            if match[1]:
                target, head = f"{REPO_URL}/pull/{match[1]}", match[2]
                current = "unknown"
                if heads is not None:
                    current = str(heads.get(target) == head).lower()
            else:
                target, head, current = match[3], "", "unknown"
            rows.append((until, target, head, str(estimated).lower(), current))
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        logging.warning("Cannot read %s backoff", agent.id)
        metrics.add("backoff_read_success", 0, agent=agent.id)
        return
    metrics.add("backoff_read_success", 1, agent=agent.id)
    for until, target, head, estimated, current in rows:
        metrics.add(
            "backoff_info",
            until,
            agent=agent.id,
            target=target,
            head=head,
            estimated=estimated,
            current_head=current,
        )


def last_start(view: ticks.TickView | None) -> float | None:
    state = view.state if view else None
    if not state:
        return None
    try:
        if state["open"] is not None:
            return ticks.utc(state["open"]["tick"])
    except ValueError:
        return None
    return state["ticks"][-1]["start"] if state["ticks"] else None


def agent_metrics(
    metrics: Metrics,
    agent: agents.Agent,
    paused: bool,
    now: float,
    alive: Callable[[int], bool],
    ledgers: Ledgers | None,
    heads: dict[str, str] | None = None,
) -> Row:
    name = agent.id
    metrics.add(
        "agent_info",
        agent.registered_at or 0,
        agent=name,
        kind=agent.kind,
        label=agent.label,
        layout=agent.layout,
    )
    metrics.add("agent_interval_seconds", agent.interval, agent=name)
    if agent.retired_at is not None:
        return Row(agent, "retired")
    runner = runner_sample(metrics, agent, now, alive)
    backoff_metrics(metrics, agent, now, heads)
    if ledgers is not None and agent.kind == "claude":
        permission_metrics(metrics, ledgers.permissions(agent), now)
    view = None
    if ledgers is not None:
        view = ledger_metrics(metrics, ledgers.get(agent), ledgers.events(agent), now)
    budget = runner.budget or agent.budget
    metrics.add("agent_budget_seconds", budget, agent=name)
    started = max(
        (t for t in (runner.started, last_start(view)) if t is not None),
        default=None,
    )
    # No tick starts during a pause, so a pause never makes an agent late.
    overdue = 2 * agent.interval + budget
    if runner.state in (None, "unknown"):
        # A failed read or a lock without owner never looks idle or late.
        presence = "unknown"
    elif runner.state in ("running", "overdue"):
        presence = "running"
    elif started is None:
        since = agent.registered_at
        late = since is not None and now - since > overdue and not paused
        presence = "late" if late else "new"
    else:
        presence = "late" if now - started > overdue and not paused else "idle"
    if started is not None:
        metrics.add("agent_last_start_timestamp_seconds", started, agent=name)
        if presence in ("idle", "late") and not paused:
            metrics.add(
                "agent_next_tick_seconds", started + agent.interval - now, agent=name
            )
    row = Row(agent, presence, runner.action if presence == "running" else None)
    recent = view.state.get("ticks", [])[-ticks.RECENT :] if view else []
    for age, tick in enumerate(reversed(recent)):
        metrics.add(
            "agent_recent",
            ticks.SEVERITY[tick["outcome"]],
            agent=name,
            slot=f"{ticks.RECENT - age:02d}",
        )
    if recent:
        last = recent[-1]
        exit_text = "" if last["exit"] is None else f" {last['exit']}"
        phase = f" · {last['phase']}" if last["phase"] else ""
        row.outcome_text = f"{last['outcome']}{exit_text}{phase}"
        if presence != "running" and last["action"]:
            row.action = {"action": last["action"], "target": last["target"]}
    # A running tick shows only its current action, so an old target never
    # makes a collision; before the selector writes one, it is "selecting".
    if row.action is None and presence != "running":
        row.action = runner.gate_action
    return row


def action_text(row: Row, collision: bool) -> str:
    if not row.action:
        return "selecting" if row.presence == "running" else ""
    prefix = (
        "collision · " if collision else "" if row.presence == "running" else "last: "
    )
    target = row.action["target"]
    match = re.search(r"/(pull|issues)/(\d+)$", target)
    ref = f" {'PR' if match[1] == 'pull' else 'issue'} {match[2]}" if match else ""
    return f"{prefix}{row.action['action']}{ref}"


def runner_metrics(
    state_dir: Path,
    paused: bool,
    now: float,
    alive: Callable[[int], bool] = process_alive,
    ledgers: Ledgers | None = None,
    registry: agents.Registry | None = None,
    heads: dict[str, str] | None = None,
) -> str:
    metrics = Metrics()
    metrics.add("local_snapshot_timestamp_seconds", now)
    metrics.add("pause_requested", int(paused))
    for kind, severity in ticks.SEVERITY.items():
        metrics.add("outcome_severity", severity, outcome=kind)
    if registry is None:
        registry = agents.discover(state_dir, now)
    for reason in agents.REASONS:
        metrics.add("agent_registry_rejected", registry.rejected[reason], reason=reason)
    for name in registry.conflicts:
        metrics.add("agent_conflict", 1, agent=name)
    if ledgers is not None:
        ledgers.prune(registry.agents)
    rows = [
        agent_metrics(metrics, agent, paused, now, alive, ledgers, heads)
        for agent in registry.agents
    ]
    rows.sort(
        key=lambda row: (
            PRESENCE_ORDER.index(row.presence),
            row.agent.kind,
            row.agent.id,
        )
    )
    counts = Counter(row.presence for row in rows)
    for presence in PRESENCE_ORDER:
        metrics.add("agents_registered", counts[presence], presence=presence)
    for kind in agents.KINDS:
        metrics.add(
            "agents_registered_kind",
            sum(row.agent.kind == kind and row.presence != "retired" for row in rows),
            kind=kind,
        )
    running = Counter(
        row.action["target"]
        for row in rows
        if row.presence == "running" and row.action and row.action["target"]
    )
    for order, row in enumerate(rows):
        name = row.agent.id
        target = row.action["target"] if row.action else ""
        collision = row.presence == "running" and running[target] > 1
        if collision:
            metrics.add("agent_collision", 1, agent=name, target=target)
        metrics.add("agent_order", order, agent=name)
        metrics.add("agent_presence", PRESENCE[row.presence], agent=name)
        metrics.add("agent_presence_info", 1, agent=name, presence=row.presence)
        metrics.add(
            "agent_row_info",
            1,
            agent=name,
            action_text=action_text(row, collision),
            target=target,
            outcome_text=row.outcome_text,
        )
    return metrics.render()


def alloy_targets(registry: agents.Registry) -> str:
    """The logs Alloy may ship: regular files of known agents, never links.

    Alloy follows links when it opens a file, so it must not glob folders the
    model can write. It reads only this list, which the collector rebuilds
    every cycle.
    """
    rows = []
    for agent in registry.agents:
        try:
            entry = agent.lstat(agent.log_name)
        except OSError:
            continue
        if entry is None or not stat.S_ISREG(entry.st_mode):
            continue
        path = "/".join(("/logs", *agent.rel, agent.log_name))
        rows.append(
            {"targets": ["localhost"], "labels": {"__path__": path, "agent": agent.id}}
        )
    for agent in registry.agents:
        # The permission gate's decisions: a second stream per Claude agent.
        if agent.kind != "claude":
            continue
        try:
            entry = agent.lstat(PERMISSIONS_FILE)
        except OSError:
            continue
        if entry is None or not stat.S_ISREG(entry.st_mode):
            continue
        path = "/".join(("/logs", *agent.rel, PERMISSIONS_FILE))
        rows.append(
            {
                "targets": ["localhost"],
                "labels": {
                    "__path__": path,
                    "agent": agent.id,
                    "stream": "permissions",
                },
            }
        )
    return json.dumps(rows, indent=2) + "\n"


def publish_local(args: argparse.Namespace, ledgers: Ledgers) -> None:
    """One collection cycle: agents, Alloy's log list, then runner metrics."""
    now = time.time()
    registry = agents.discover(args.state_dir, now)
    targets = args.output.parent / "alloy" / "targets.json"
    text = alloy_targets(registry)
    try:
        current = targets.read_text()
    except FileNotFoundError:
        current = None
    if text != current:
        targets.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(targets, text)
    atomic_write(
        args.output / "runners.prom",
        runner_metrics(
            args.state_dir,
            args.pause_file.exists(),
            now,
            ledgers=ledgers,
            registry=registry,
            heads=LATEST.heads,
        ),
    )


def ledger_metrics(
    metrics: Metrics,
    log: ticks.LogLedger,
    events: ticks.EventLedger,
    now: float,
) -> ticks.TickView:
    """Read new events and log lines, persist checkpoints, publish the ticks.

    Events are read first: once a runner writes them, the log ledger stops
    counting, so a tick is never counted twice.
    """
    name = log.agent
    ok = True
    # After a restart the log's saved boundary must be known before the
    # events are read, or a tick the log counted is counted again.
    log.restore()
    events.log_counted_until = log.last_counted()
    try:
        events.update(now)
        events.save()
    except FileNotFoundError:
        # A runner without events yet is fine; the log still covers it. A
        # file that vanished after events were read is a read failure.
        ok = not events.active
        if not ok:
            logging.warning("Tick events of %s are missing", name)
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        logging.warning("Cannot read %s tick events", name)
        ok = False
    metrics.add("tick_events_read_success", int(ok), agent=name)
    metrics.add(
        "tick_events_rejected_total", events.rejected, metric_type="counter", agent=name
    )
    log.count_before = events.first_start()
    ok = True
    try:
        log.update(now)
        log.save()
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        # Keep the last ledger; a missing or unreadable log is reported, not zeroed.
        logging.warning("Cannot read %s tick ledger", name)
        ok = False
    metrics.add("tick_ledger_read_success", int(ok), agent=name)
    view = ticks.merge(log, events)
    sample = Metrics()
    try:
        ticks.export(sample, view, now)
    except (ValueError, KeyError, TypeError, OverflowError):
        logging.warning("Cannot export %s tick ledger", name)
        metrics.add("tick_ledger_export_success", 0, agent=name)
        return view
    metrics.merge(sample)
    metrics.add("tick_ledger_export_success", 1, agent=name)
    return view


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
        state = fetch(timeout)
        metrics = github_metrics(state, time.time())
        atomic_write(output / "github.prom", metrics)
        # Read by the runner loop to tell whether a backoff head is current.
        LATEST.heads = {
            f"{REPO_URL}/pull/{pr['number']}": pr.get("headRefOid", "")
            for pr in state["prs"]
            if type(pr.get("number")) is int
        }
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
    ledgers = Ledgers(args.output.parent)
    try:
        while not stopped.is_set():
            publish_local(args, ledgers)
            stopped.wait(args.interval)
    finally:
        stopped.set()
        thread.join(timeout=args.github_timeout + 5)


def default_state_dir() -> Path:
    """Match the runners and the Alloy log mount in compose.yaml."""
    return Path(
        os.environ.get("EPIC_STATE_DIR") or Path.home() / ".local/state/epic-loop"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("tools/agent-monitoring/runtime/metrics")
    )
    parser.add_argument("--state-dir", type=Path, default=default_state_dir())
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
            publish_local(args, Ledgers(args.output.parent))
            if not collect_github(args.output, args.github_timeout):
                raise SystemExit(1)
        else:
            run(args)


if __name__ == "__main__":
    main()
