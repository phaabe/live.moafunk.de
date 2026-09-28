"""Tick ledger: turn a runner log into finished ticks with named outcomes.

A runner log mixes runner lines with model output, prompts and command
output, so a marker line cannot prove who wrote it. Parsing is best effort
(`source="log"`). Only fixed enums, numbers, timestamps and normalized
action/target values leave this module; no log text does.

Counters count only ticks that finish after a byte baseline: on the first
read (and after a lost checkpoint) the baseline is the end of the log, so
old history fills the ledger but never looks like new activity.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import json
import logging
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Protocol
from zoneinfo import ZoneInfo

Json = dict[str, Any]
LOCAL = ZoneInfo("Europe/Berlin")
LEDGER_SIZE = 2000
RECENT = 20
HOUR_DAYS = 7
# A line longer than this is never a marker; skip it instead of buffering it.
MAX_LINE = 65536
# Ordered by severity, so "worst" is the maximum.
SEVERITY = {
    "ok": 1,
    "blocked": 2,
    "timeout": 3,
    "killed": 4,
    "interrupted": 5,
    "error": 6,
}
FAILURES = ("timeout", "killed", "interrupted", "error")
START = re.compile(r"tick: started (\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ) repo=\S.*")
FINISH = re.compile(r"tick: finished exit=(\d{1,3})")
# Written by .codex/tick_backoff.py only for a valid "blocked" result.
BLOCKED = "backoff: model reported blocked: "
# The selector (next_action.py) failed on a gh call before any model ran.
GH_FAILED = re.compile(r"subprocess\.CalledProcessError: Command '\['gh', ")
TOKENS = re.compile(r"[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+")


class Sink(Protocol):
    def add(
        self, name: str, value: float, *, kind: str = ..., **labels: str
    ) -> None: ...


def outcome(exit_code: int, blocked: bool) -> str:
    if exit_code == 0:
        return "ok"
    if exit_code == 124:
        return "timeout"
    if exit_code == 143:
        return "killed"
    # 75 is also "missing or invalid final result"; only the runner's blocked
    # line makes it a blocked tick.
    if exit_code == 75 and blocked:
        return "blocked"
    return "error"


def utc(text: str) -> float:
    return (
        datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
        .replace(tzinfo=timezone.utc)
        .timestamp()
    )


def nearest_rank(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


class LogLedger:
    """Incremental reader of one runner log, with a persisted checkpoint.

    Per cycle the caller runs `update()`, then `save()`, then publishes. A
    crash in between replays bytes; ticks are deduplicated by start time.
    """

    def __init__(
        self,
        agent: str,
        log: Path,
        checkpoint: Path,
        normalize: Callable[[Json], dict[str, str]],
    ) -> None:
        self.agent, self.log, self.checkpoint = agent, log, checkpoint
        self.normalize = normalize
        self.state: Json | None = None
        self.dirty = False

    def fresh(self, now: float, size: int) -> Json:
        return {
            "v": 1,
            "inode": None,
            "offset": 0,
            "baseline": size,
            "partial": "",
            "skip_line": False,
            "open": None,
            "ticks": [],
            "totals": dict.fromkeys(SEVERITY, 0),
            "coverage_start": now,
        }

    def load(self) -> Json | None:
        try:
            data = json.loads(self.checkpoint.read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            logging.warning(
                "Tick checkpoint for %s is unreadable; rebuilding", self.agent
            )
            return None
        keys = {
            "v",
            "inode",
            "offset",
            "baseline",
            "partial",
            "skip_line",
            "open",
            "ticks",
            "totals",
            "coverage_start",
        }
        if (
            not isinstance(data, dict)
            or set(data) != keys
            or data["v"] != 1
            or set(data["totals"]) != set(SEVERITY)
        ):
            logging.warning("Tick checkpoint for %s is invalid; rebuilding", self.agent)
            return None
        return data

    def update(self, now: float) -> None:
        stat = self.log.stat()
        if self.state is None:
            self.state = self.load() or self.fresh(now, stat.st_size)
            self.dirty = True
        state = self.state
        if state["inode"] is not None and (
            stat.st_ino != state["inode"] or stat.st_size < state["offset"]
        ):
            # Rotated or truncated: every line in the file is new.
            state.update(offset=0, baseline=0, partial="", skip_line=False, open=None)
        state["inode"] = stat.st_ino
        if stat.st_size == state["offset"]:
            return
        with self.log.open("rb") as stream:
            stream.seek(state["offset"])
            data = stream.read(stat.st_size - state["offset"])
        partial = state["partial"].encode("utf-8", "surrogateescape")
        position = state["offset"] - len(partial)
        buffer = partial + data
        lines = buffer.split(b"\n")
        rest = lines.pop()
        for raw in lines:
            if state["skip_line"]:
                state["skip_line"] = False
            else:
                self.feed(raw.decode("utf-8", "replace").rstrip("\r"), position, now)
            position += len(raw) + 1
        if len(rest) > MAX_LINE:
            rest, state["skip_line"] = b"", True
        state["partial"] = rest.decode("utf-8", "surrogateescape")
        state["offset"] = stat.st_size
        self.dirty = True

    def feed(self, line: str, position: int, now: float) -> None:
        state = self.state
        assert state is not None
        started = START.fullmatch(line)
        if started:
            if state["open"] is not None:
                self.finish(None, position, now)
            state["open"] = {
                "tick": started.group(1),
                "first": True,
                "action": "",
                "target": "",
                "blocked": False,
                "gh_failed": False,
                "tokens": None,
                "want_tokens": False,
            }
            return
        tick = state["open"]
        if tick is None:
            return
        first, tick["first"] = tick["first"], False
        if first and line.startswith('{"action"'):
            # The selector prints its JSON decision right after the start line.
            try:
                labels = self.normalize(json.loads(line))
            except (ValueError, TypeError):
                return
            tick["action"], tick["target"] = labels["action"], labels["target"]
            return
        if tick["want_tokens"]:
            tick["want_tokens"] = False
            if TOKENS.fullmatch(line):
                tick["tokens"] = int(line.replace(",", ""))
                return
        if line == "tokens used":
            tick["want_tokens"] = True
        elif line.startswith(BLOCKED):
            tick["blocked"] = True
        elif GH_FAILED.match(line):
            tick["gh_failed"] = True
        else:
            finished = FINISH.fullmatch(line)
            if finished:
                self.finish(int(finished.group(1)), position, now)

    def finish(self, exit_code: int | None, position: int, now: float) -> None:
        state = self.state
        assert state is not None
        tick, state["open"] = state["open"], None
        kind = (
            "interrupted" if exit_code is None else outcome(exit_code, tick["blocked"])
        )
        live = position >= state["baseline"]
        if any(t["tick"] == tick["tick"] for t in state["ticks"][-50:]):
            return
        phase = ""
        if kind in FAILURES:
            phase = "select" if tick["gh_failed"] else "unknown"
        state["ticks"].append(
            {
                "tick": tick["tick"],
                "start": utc(tick["tick"]),
                # The log has no finish time; a live read is up to one cycle late.
                "end": now if live else None,
                "exit": exit_code,
                "outcome": kind,
                "phase": phase,
                "action": tick["action"],
                "target": tick["target"],
                "tokens": tick["tokens"],
            }
        )
        del state["ticks"][:-LEDGER_SIZE]
        if live:
            state["totals"][kind] += 1

    def save(self) -> None:
        if not self.dirty or self.state is None:
            return
        self.checkpoint.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", dir=self.checkpoint.parent, delete=False
        ) as out:
            temporary = Path(out.name)
            try:
                json.dump(self.state, out)
                out.flush()
                os.replace(temporary, self.checkpoint)
            finally:
                temporary.unlink(missing_ok=True)
        self.dirty = False

    @property
    def ticks(self) -> list[Json]:
        return self.state["ticks"] if self.state else []


def hour_label(bucket: float) -> tuple[str, str]:
    """Local day and hour of a UTC hour; the repeated autumn hour gets "b"."""
    local = datetime.fromtimestamp(bucket, tz=LOCAL)
    return local.strftime("%Y-%m-%d"), f"{local.hour:02d}" + ("b" if local.fold else "")


def export(metrics: Sink, ledger: LogLedger, now: float) -> None:
    """Publish counters and ledger-derived gauges for one agent."""
    state = ledger.state
    if state is None:
        return
    agent = ledger.agent
    for kind in SEVERITY:
        metrics.add(
            "ticks_total",
            state["totals"][kind],
            kind="counter",
            agent=agent,
            outcome=kind,
        )
    metrics.add(
        "tick_coverage_start_timestamp_seconds", state["coverage_start"], agent=agent
    )
    ticks = state["ticks"]
    if not ticks:
        return
    last = ticks[-1]
    metrics.add("tick_last_outcome", SEVERITY[last["outcome"]], agent=agent)
    metrics.add(
        "tick_last_info",
        last["end"] if last["end"] is not None else last["start"],
        agent=agent,
        outcome=last["outcome"],
        exit="" if last["exit"] is None else str(last["exit"]),
        phase=last["phase"],
        action=last["action"],
        target=last["target"],
        source="log",
    )
    failures = 0
    for tick in reversed(ticks):
        if tick["outcome"] == "ok":
            break
        if tick["outcome"] in FAILURES:
            failures += 1
    metrics.add("tick_consecutive_failures", failures, agent=agent)

    local_now = datetime.fromtimestamp(now, tz=LOCAL)
    today = local_now.date()
    todays = [
        t for t in ticks if datetime.fromtimestamp(t["start"], tz=LOCAL).date() == today
    ]
    for kind in SEVERITY:
        metrics.add(
            "ticks_today",
            sum(t["outcome"] == kind for t in todays),
            agent=agent,
            outcome=kind,
        )
    durations = [t["end"] - t["start"] for t in todays if t["end"] is not None]
    if durations:
        for q in (0.5, 0.95):
            metrics.add(
                "tick_duration_today_seconds",
                nearest_rank(durations, q),
                agent=agent,
                quantile=str(q),
            )
    if any(t["tokens"] is not None for t in ticks):
        metrics.add("tokens_today", sum(t["tokens"] or 0 for t in todays), agent=agent)

    first_day = datetime.combine(
        today - timedelta(days=HOUR_DAYS - 1), datetime.min.time(), tzinfo=LOCAL
    ).timestamp()
    worst: dict[float, int] = {}
    for tick in ticks:
        if tick["start"] >= first_day:
            bucket = tick["start"] // 3600 * 3600
            worst[bucket] = max(worst.get(bucket, 0), SEVERITY[tick["outcome"]])
    for bucket, severity in sorted(worst.items()):
        day, hour = hour_label(bucket)
        metrics.add("tick_hour_worst", severity, agent=agent, day=day, hour=hour)

    for tick in ticks[-RECENT:]:
        metrics.add(
            "tick_info",
            tick["end"] - tick["start"] if tick["end"] is not None else -1,
            agent=agent,
            tick=tick["tick"],
            action=tick["action"],
            target=tick["target"],
            outcome=tick["outcome"],
            exit="" if tick["exit"] is None else str(tick["exit"]),
            phase=tick["phase"],
            tokens="" if tick["tokens"] is None else str(tick["tokens"]),
            source="log",
        )
