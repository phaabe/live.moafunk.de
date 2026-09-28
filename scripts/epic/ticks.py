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
from typing import Any, BinaryIO, Protocol
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
# At most 15 digits: int() of a huge string raises, and no session is that big.
TOKENS = re.compile(r"[0-9]{1,3}(?:,[0-9]{3}){0,4}|[0-9]{1,15}")


OPEN_KEYS = {
    "tick": str,
    "first": bool,
    "action": str,
    "target": str,
    "blocked": bool,
    "gh_failed": bool,
    "tokens": (int, type(None)),
    "want_tokens": bool,
}
TICK_KEYS = {
    "id": str,
    "tick": str,
    "start": float,
    "end": (float, type(None)),
    "exit": (int, type(None)),
    "outcome": str,
    "phase": str,
    "action": str,
    "target": str,
    "tokens": (int, type(None)),
}


def typed(value: object, schema: dict[str, type | tuple[type, ...]]) -> bool:
    """Exact types: `bool` is never an `int`, and `float` also accepts `int`."""

    def ok(item: object, kind: type | tuple[type, ...]) -> bool:
        allowed = kind if isinstance(kind, tuple) else (kind,)
        return type(item) in allowed or (float in allowed and type(item) is int)

    return (
        isinstance(value, dict)
        and set(value) == set(schema)
        and all(ok(value[key], kind) for key, kind in schema.items())
    )


def valid_checkpoint(data: object) -> bool:
    """The whole checkpoint, nested values included; anything else is rebuilt."""
    count = int
    return (
        typed(
            data,
            {
                "v": int,
                "inode": (int, type(None)),
                "offset": count,
                "baseline": count,
                "partial": str,
                "skip_line": bool,
                "open": (dict, type(None)),
                "ticks": list,
                "totals": dict,
                "coverage_start": float,
            },
        )
        and data["v"] == 4
        and data["offset"] >= 0
        and data["baseline"] >= 0
        and (data["open"] is None or typed(data["open"], OPEN_KEYS))
        and all(typed(t, TICK_KEYS) and t["outcome"] in SEVERITY for t in data["ticks"])
        and set(data["totals"]) == set(SEVERITY)
        and all(type(n) is int and n >= 0 for n in data["totals"].values())
    )


class Sink(Protocol):
    def add(
        self, name: str, value: float, *, metric_type: str = ..., **labels: str
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

    Per cycle the caller runs `update()`, then `save()`, then publishes. The
    checkpoint holds the read offset and the ledger in one atomic write, so a
    crash before `save()` replays only ticks that were never saved: nothing
    is counted twice. After truncation or rotation every line is new; the
    runners only append, so a new file never repeats old ticks.
    """

    def __init__(
        self,
        agent: str,
        log: Path,
        checkpoint: Path,
        normalize: Callable[[Json], dict[str, str]],
        opener: Callable[[], BinaryIO] | None = None,
    ) -> None:
        self.agent, self.log, self.checkpoint = agent, log, checkpoint
        self.normalize = normalize
        # The monitor passes a no-follow opener for model-writable folders.
        self.opener = opener or (lambda: self.log.open("rb"))
        self.state: Json | None = None
        self.dirty = False

    def fresh(self, now: float, size: int) -> Json:
        return {
            "v": 4,
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
        if not valid_checkpoint(data):
            logging.warning("Tick checkpoint for %s is invalid; rebuilding", self.agent)
            return None
        return data

    def update(self, now: float) -> None:
        with self.opener() as stream:
            self.update_from(stream, os.fstat(stream.fileno()), now)

    def update_from(self, stream: BinaryIO, stat: os.stat_result, now: float) -> None:
        if self.state is None:
            self.state = self.load() or self.fresh(now, stat.st_size)
            self.dirty = True
        # All or nothing: an exception while parsing restores the old state, so
        # a tick is never counted without its offset being committed.
        before = self.state
        state = self.state = {
            **before,
            "ticks": list(before["ticks"]),
            "totals": dict(before["totals"]),
            "open": dict(before["open"]) if before["open"] else None,
        }
        try:
            self.read(state, stream, stat, now)
        except BaseException:
            self.state = before
            raise

    def read(
        self, state: Json, stream: BinaryIO, stat: os.stat_result, now: float
    ) -> None:
        if state["inode"] is not None and (
            stat.st_ino != state["inode"] or stat.st_size < state["offset"]
        ):
            # Rotated or truncated: every line in the file is new. A half line
            # belongs to the old file; an open tick may continue or be
            # interrupted by the next start.
            state.update(offset=0, baseline=0, partial="", skip_line=False)
        state["inode"] = stat.st_ino
        if stat.st_size == state["offset"]:
            return
        stream.seek(state["offset"])
        data = stream.read(stat.st_size - state["offset"])
        partial = state["partial"].encode("utf-8", "surrogateescape")
        position = state["offset"] - len(partial)
        buffer = partial + data
        lines = buffer.split(b"\n")
        rest = lines.pop()
        for raw in lines:
            # Offset just after this line's newline: a line that ends after
            # the baseline is new, even if it started before it.
            position += len(raw) + 1
            if state["skip_line"]:
                state["skip_line"] = False
            else:
                self.feed(raw.decode("utf-8", "replace").rstrip("\r"), position, now)
        if len(rest) > MAX_LINE:
            rest, state["skip_line"] = b"", True
        state["partial"] = rest.decode("utf-8", "surrogateescape")
        state["offset"] = stat.st_size
        self.dirty = True

    def feed(self, line: str, end: int, now: float) -> None:
        state = self.state
        assert state is not None
        started = START.fullmatch(line)
        if started:
            try:
                utc(started.group(1))
            except ValueError:
                return  # an impossible date is not a runner line
            if state["open"] is not None:
                self.finish(None, end, now)
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
                self.finish(int(finished.group(1)), end, now)

    def finish(self, exit_code: int | None, end: int, now: float) -> None:
        state = self.state
        assert state is not None
        tick, state["open"] = state["open"], None
        kind = (
            "interrupted" if exit_code is None else outcome(exit_code, tick["blocked"])
        )
        live = end > state["baseline"]
        phase = ""
        if kind in FAILURES:
            phase = "select" if tick["gh_failed"] else "unknown"
        # Starts can share a second; the exported label must stay unique. The
        # export window is this tick plus the RECENT - 1 before it.
        taken = {t["id"] for t in state["ticks"][-(RECENT - 1) :]}
        tick_id, n = tick["tick"], 1
        while tick_id in taken:
            n += 1
            tick_id = f"{tick['tick']}#{n}"
        state["ticks"].append(
            {
                "id": tick_id,
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
            metric_type="counter",
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
            tick=tick["id"],
            action=tick["action"],
            target=tick["target"],
            outcome=tick["outcome"],
            exit="" if tick["exit"] is None else str(tick["exit"]),
            phase=tick["phase"],
            tokens="" if tick["tokens"] is None else str(tick["tokens"]),
            source="log",
        )
