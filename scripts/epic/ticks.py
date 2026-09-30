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
from dataclasses import dataclass
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
# Claude session usage of a finish event (see tick_events.py). Kept apart from
# Codex's `tokens`: unlike counters, never summed into one total.
USAGE_COUNTERS = ("input", "output", "cache_read", "cache_write")
USAGE_REASONS = (
    "no-transcript",
    "unreadable",
    "too-large",
    "no-usage",
    "interrupted",
    "malformed",
    "subagents",
)
COVERAGES = ("complete", "partial", "unavailable")


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
    "usage": (dict, type(None)),
}


def valid_usage(value: object) -> bool:
    """None (no Claude model session) or the event's usage object."""
    if value is None:
        return True
    if not isinstance(value, dict) or set(value) != {
        *USAGE_COUNTERS,
        "complete",
        "reason",
    }:
        return False
    counts = [value[name] for name in USAGE_COUNTERS]
    # Partial usage may miss single counters; each is a count or None.
    if not all(n is None or (type(n) is int and 0 <= n < 10**15) for n in counts):
        return False
    if value["complete"] is True:
        return None not in counts and value["reason"] is None
    return value["complete"] is False and value["reason"] in USAGE_REASONS


def coverage(usage: Json) -> str:
    if usage["complete"]:
        return "complete"
    measured = any(usage[name] is not None for name in USAGE_COUNTERS)
    return "partial" if measured else "unavailable"


def compact(n: int | None) -> str:
    if n is None:
        return "?"
    for size, suffix in ((10**6, "M"), (10**3, "k")):
        if n >= size:
            return f"{n / size:.1f}{suffix}"
    return str(n)


def usage_label(usage: Json | None) -> str:
    """Short text for the tick history; the counters stay apart."""
    if usage is None:
        return ""
    if coverage(usage) == "unavailable":
        return f"unavailable: {usage['reason']}"
    text = (
        f"in {compact(usage['input'])} · out {compact(usage['output'])}"
        f" · cache read {compact(usage['cache_read'])}"
        f" · cache write {compact(usage['cache_write'])}"
    )
    return text if usage["complete"] else f"{text} · partial: {usage['reason']}"


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
                "first_start": (float, type(None)),
            },
        )
        and data["v"] == 6
        and data["offset"] >= 0
        and data["baseline"] >= 0
        and (data["open"] is None or typed(data["open"], OPEN_KEYS))
        and all(
            typed(t, TICK_KEYS) and t["outcome"] in SEVERITY and valid_usage(t["usage"])
            for t in data["ticks"]
        )
        and set(data["totals"]) == set(SEVERITY)
        and all(type(n) is int and n >= 0 for n in data["totals"].values())
    )


def migrate_v4(data: Json) -> Json:
    """v4 checkpoints only lack the event ledger's first start."""
    return {**data, "v": 5, "first_start": None}


def migrate_v5(data: Json) -> Json:
    """v5 ticks only lack the Claude session usage."""
    ticks = data.get("ticks")
    if not isinstance(ticks, list):
        return data
    return {
        **data,
        "v": 6,
        "ticks": [t | {"usage": None} if isinstance(t, dict) else t for t in ticks],
    }


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
        # Once the runner writes events, only ticks that started before its
        # first event are counted here; later ones are counted by the events.
        # Asked only after the tick's lines were read (see EventLedger.peek).
        self.count_before: Callable[[], float] = lambda: math.inf

    def fresh(self, now: float, size: int) -> Json:
        return {
            "v": 6,
            "first_start": None,
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
        if isinstance(data, dict) and data.get("v") == 4:
            data = migrate_v4(data)
        if isinstance(data, dict) and data.get("v") == 5:
            data = migrate_v5(data)
        if not self.valid(data):
            logging.warning("Tick checkpoint for %s is invalid; rebuilding", self.agent)
            return None
        return data

    def valid(self, data: object) -> bool:
        return valid_checkpoint(data)

    def update(self, now: float) -> None:
        try:
            stream = self.opener()
        except FileNotFoundError:
            if self.state is None:
                # Keep the saved state while the file is away. Without one, a
                # file that appears later is new, so all its lines count.
                self.state = self.load() or self.fresh(now, 0)
                self.dirty = True
            raise
        with stream:
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
            except (ValueError, TypeError, RecursionError):
                return  # model output can print anything here
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
        self.record(
            tick["tick"],
            # The log has no finish time; a live read is up to one cycle late.
            now if live else None,
            exit_code,
            kind,
            phase,
            tick["action"],
            tick["target"],
            tick["tokens"],
            live and utc(tick["tick"]) < self.count_before(),
        )

    def record(
        self,
        tick: str,
        end: float | None,
        exit_code: int | None,
        kind: str,
        phase: str,
        action: str,
        target: str,
        tokens: int | None,
        counted: bool,
        usage: Json | None = None,
    ) -> None:
        state = self.state
        assert state is not None
        # Starts can share a second; the exported label must stay unique. The
        # export window is this tick plus the RECENT - 1 before it.
        taken = {t["id"] for t in state["ticks"][-(RECENT - 1) :]}
        tick_id, n = tick, 1
        while tick_id in taken:
            n += 1
            tick_id = f"{tick}#{n}"
        state["ticks"].append(
            {
                "id": tick_id,
                "tick": tick,
                "start": utc(tick),
                "end": end,
                "exit": exit_code,
                "outcome": kind,
                "phase": phase,
                "action": action,
                "target": target,
                "tokens": tokens,
                "usage": usage,
            }
        )
        del state["ticks"][:-LEDGER_SIZE]
        if counted:
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


# What a runner may name in an event (see tick_events.py). "interrupted" is
# never written: the ledger infers it from a start without a finish.
EVENT_OUTCOMES = ("ok", "blocked", "timeout", "killed", "error")
PHASES = (
    "lock",
    "refresh",
    "select",
    "quota",
    "backoff",
    "gate",
    "model",
    "result",
    "verify",
    "record",
    "unknown",
)
MAX_EVENT_LINE = 4096  # the runner writes each event below this size
# The log ledger looks this far into an events file it has not read yet.
PEEK_BYTES = 65536
TICK_TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")


class EventLedger(LogLedger):
    """Incremental reader of a runner's tick events (`<kind>-ticks.jsonl`).

    Same checkpoint and counting rules as the log ledger; each line is one
    JSON event. A line is checked after the fact (the lock is gone by then):
    schema and enums, one finish per start, finish after start, starts in
    order. Anything else is rejected, counted and skipped.
    """

    def __init__(self, *args: Any, source: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.source = source
        self.rejected = 0
        self.pending = 0
        self.peeked: float | None = None

    @property
    def active(self) -> bool:
        """The runner writes events, so they are the counted source."""
        return self.first_start() < math.inf

    def first_start(self) -> float:
        """Start of the oldest known event tick; inf before the first event."""
        # Saved once: trimming the ticks list must not move this boundary.
        if not self.state or self.state["first_start"] is None:
            return math.inf
        return self.state["first_start"]

    def peek(self) -> float:
        """First start in the events file, even before it was read.

        The runner writes a tick's start event before the lines the log
        ledger decides on, so asked after those lines were read, this finds
        every tick that has events: the log never counts one of them. A
        start found only on disk is kept in `peeked`, so the monitor can
        check the event ledger read it before the log's decision is saved.
        An unreadable file raises: it must not look like a missing one.
        """
        known = self.first_start()
        if known < math.inf:
            return known
        try:
            with self.opener() as stream:
                data = stream.read(PEEK_BYTES)
        except FileNotFoundError:
            return math.inf
        for raw in data.split(b"\n"):
            start = start_of(raw.decode("utf-8", "replace"))
            if start is not None:
                self.peeked = start
                return start
        return math.inf

    def update(self, now: float) -> None:
        self.pending = 0
        super().update(now)
        # Rejections of a read that was rolled back are not counted.
        self.rejected += self.pending

    def reject(self) -> None:
        self.pending += 1

    def feed(self, line: str, end: int, now: float) -> None:
        state = self.state
        assert state is not None
        # Untrusted: bound the size before parsing, and deep nesting is invalid.
        if len(line) > MAX_EVENT_LINE:
            return self.reject()
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            return self.reject()
        if not isinstance(event, dict) or event.get("v") != 1:
            return self.reject()
        tick = event.get("tick")
        if not isinstance(tick, str) or not TICK_TIME.fullmatch(tick):
            return self.reject()
        try:
            start = utc(tick)
        except ValueError:
            return self.reject()
        if event.get("event") == "start":
            previous = state["open"]["tick"] if state["open"] else None
            if previous is None and state["ticks"]:
                previous = state["ticks"][-1]["tick"]
            if previous is not None and start < utc(previous):
                return self.reject()
            if state["open"] is not None:
                self.finish(None, end, now)  # no finish before the next start
            if state["first_start"] is None:
                state["first_start"] = start
            state["open"] = dict.fromkeys(OPEN_KEYS, False) | {
                "tick": tick,
                "action": "",
                "target": "",
                "tokens": None,
            }
            return None
        if event.get("event") != "finish":
            return self.reject()
        if state["open"] is None or state["open"]["tick"] != tick:
            return self.reject()  # orphan finish
        values = self.finish_values(event, start)
        if values is None:
            return self.reject()
        state["open"] = None
        live = end > state["baseline"]
        *fields, usage = values
        self.record(tick, *fields, live, usage=usage)
        return None

    def finish_values(self, event: Json, start: float) -> tuple[Any, ...] | None:
        at, code, tokens = event.get("at"), event.get("exit"), event.get("tokens")
        if not isinstance(at, str) or not TICK_TIME.fullmatch(at):
            return None
        try:
            finished = utc(at)
        except ValueError:
            return None
        if (
            finished < start
            or type(code) is not int
            or not 0 <= code <= 255
            or event.get("outcome") not in EVENT_OUTCOMES
            or event.get("phase") not in PHASES
            or not (tokens is None or (type(tokens) is int and 0 <= tokens < 10**15))
            # Absent in Codex and older events.
            or not valid_usage(event.get("usage"))
        ):
            return None
        action, target = "", ""
        if event.get("action") is not None:
            try:
                labels = self.normalize(
                    {
                        "action": event["action"],
                        "pr": event.get("pr"),
                        "issue": event.get("issue"),
                    }
                )
            except (ValueError, TypeError):
                return None
            action, target = labels["action"], labels["target"]
        return (
            finished,
            code,
            event["outcome"],
            event["phase"],
            action,
            target,
            tokens,
            event.get("usage"),
        )

    def finish(self, exit_code: int | None, end: int, now: float) -> None:
        """A start without a finish: the runner was stopped hard."""
        state = self.state
        assert state is not None
        tick, state["open"] = state["open"], None
        live = end > state["baseline"]
        self.record(
            tick["tick"], None, None, "interrupted", "unknown", "", "", None, live
        )


DECISION = re.compile(r"\S+ (allow|deny) ")
DECISIONS = ("allow", "deny")


def start_of(line: str) -> float | None:
    """Start time of a well-formed start event line, else None."""
    if len(line) > MAX_EVENT_LINE:
        return None
    try:
        event = json.loads(line)
    except (ValueError, RecursionError):
        return None
    if not (
        isinstance(event, dict)
        and event.get("v") == 1
        and event.get("event") == "start"
        and isinstance(event.get("tick"), str)
        and TICK_TIME.fullmatch(event["tick"])
    ):
        return None
    try:
        return utc(event["tick"])
    except ValueError:
        return None


class DecisionLedger(LogLedger):
    """Counts allow/deny lines of the Claude permission gate's log.

    Only the decision word is read; commands and reasons stay in the file.
    Same incremental reading and baseline as the tick ledgers.
    """

    def fresh(self, now: float, size: int) -> Json:
        return super().fresh(now, size) | {"totals": dict.fromkeys(DECISIONS, 0)}

    def valid(self, data: object) -> bool:
        if not isinstance(data, dict) or set(data.get("totals") or {}) != set(
            DECISIONS
        ):
            return False
        return valid_checkpoint(data | {"totals": dict.fromkeys(SEVERITY, 0)}) and all(
            type(n) is int and n >= 0 for n in data["totals"].values()
        )

    def feed(self, line: str, end: int, now: float) -> None:
        state = self.state
        assert state is not None
        match = DECISION.match(line)
        if match and end > state["baseline"]:
            state["totals"][match[1]] += 1


@dataclass
class TickView:
    """The ticks one agent exports: legacy log history, then events."""

    agent: str
    state: Json


def merge(log: LogLedger, events: EventLedger | None) -> TickView:
    """Log ticks before the first event, then event ticks; totals of both.

    The log ledger counts only ticks that started before the first event, so a
    tick is counted by exactly one of them.
    """
    if events is None or not events.active or events.state is None:
        return TickView(log.agent, log.state or {})
    cutoff = events.first_start()
    older = [t for t in log.ticks if t["start"] < cutoff]
    newer = [t | {"source": events.source} for t in events.state["ticks"]]
    log_state = log.state or {}
    totals = dict.fromkeys(SEVERITY, 0)
    for source in (log_state.get("totals", {}), events.state["totals"]):
        for kind, count in source.items():
            totals[kind] += count
    return TickView(
        log.agent,
        {
            "ticks": (older + newer)[-LEDGER_SIZE:],
            "totals": totals,
            "coverage_start": min(
                log_state.get("coverage_start", math.inf),
                events.state["coverage_start"],
            ),
            "open": events.state["open"],
        },
    )


def hour_label(bucket: float) -> tuple[str, str]:
    """Local day and hour of a UTC hour; the repeated autumn hour gets "b"."""
    local = datetime.fromtimestamp(bucket, tz=LOCAL)
    return local.strftime("%Y-%m-%d"), f"{local.hour:02d}" + ("b" if local.fold else "")


def export(metrics: Sink, ledger: LogLedger | TickView, now: float) -> None:
    """Publish counters and ledger-derived gauges for one agent."""
    state = ledger.state
    if not state:
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
        source=last.get("source", "log"),
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
    # Claude usage: each counter on its own, plus how many model ticks it
    # fully covers. An unavailable count adds nothing and shows as coverage.
    if any(t.get("usage") is not None for t in ticks):
        used = [t["usage"] for t in todays if t.get("usage") is not None]
        for name in USAGE_COUNTERS:
            metrics.add(
                "usage_tokens_today",
                sum(u[name] or 0 for u in used),
                agent=agent,
                counter=name,
            )
        for kind in COVERAGES:
            metrics.add(
                "usage_ticks_today",
                sum(coverage(u) == kind for u in used),
                agent=agent,
                coverage=kind,
            )

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
            usage=usage_label(tick.get("usage")),
            source=tick.get("source", "log"),
        )
