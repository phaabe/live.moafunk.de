"""Durable agent activity history (leaf A681.1.2 of issue 681).

The collector is the only writer of `runtime/agent-activity.sqlite3`. It
stores validated activity records (activity.record), the ingest cursor of
each producer file, coverage boundaries, sealed overview buckets and an
export outbox. Records and their cursor commit in one transaction, and a
stable event ID makes every replay a no-op.

  open_ledger(path)                     the ledger, schema created
  ingest(conn, agent, path, now)        new complete lines of a producer file
  intervals(conn, agent, start, end)    the activity intervals of a window
  bucket(intervals, start, end, ...)    one overview bucket, summary by time
  resolution(range_s)                   the overview bucket width of a range
  late(...)                             the one wait-aware lateness decision
  prune(conn, now)                      30-day retention in bounded batches

Outbox states: `pending` (stored, not written), `staged` (written and
flushed to a spool file; not delivered), `delivered` (confirmed by Loki
read-back), `expired` (dropped undelivered at retention: an export gap).
Leaf A681.1.4 implements the transport against these states.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Iterator

import activity

RETENTION_S = 30 * 86_400
# Overview widths, finest first (seconds).
RESOLUTIONS = {"10s": 10, "1m": 60, "5m": 300, "30m": 1800, "2h": 7200}
MAX_BUCKETS = 400
MAX_RANGE_S = RETENTION_S
# A bucket seals this long after its end, once the producers are read.
SEAL_GRACE_S = 10
PRUNE_BATCH = 5_000
MAX_LINE = 4096
OUTBOX_STATES = ("pending", "staged", "delivered", "expired")
# Fixed tie order after "latest seen": the contract's state order.
STATE_ORDER = {name: index for index, name in enumerate(activity.STATES)}

# Event kinds that end model work but not the tick.
RETURN_TO_RUNNER = frozenset({"model-end"})
# Event kinds that end the tick: the agent is idle between ticks.
TICK_END = frozenset({"finish"})

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    agent TEXT NOT NULL,
    at INTEGER NOT NULL,
    record TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_agent_at ON events (agent, at);
CREATE TABLE IF NOT EXISTS cursors (
    source TEXT PRIMARY KEY,
    device INTEGER NOT NULL,
    inode INTEGER NOT NULL,
    offset INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS coverage (
    agent TEXT NOT NULL,
    start INTEGER NOT NULL,
    end INTEGER NOT NULL,
    kind TEXT NOT NULL,
    PRIMARY KEY (agent, start, kind)
);
CREATE TABLE IF NOT EXISTS buckets (
    agent TEXT NOT NULL,
    resolution TEXT NOT NULL,
    start INTEGER NOT NULL,
    record TEXT NOT NULL,
    PRIMARY KEY (agent, resolution, start)
);
CREATE TABLE IF NOT EXISTS outbox (
    id TEXT PRIMARY KEY,
    stream TEXT NOT NULL,
    ts INTEGER NOT NULL,
    payload TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'staged', 'delivered', 'expired'))
);
CREATE INDEX IF NOT EXISTS outbox_stream_ts ON outbox (stream, ts);
"""


class LedgerError(Exception):
    """The store cannot be used: history publication stops, the file stays."""


def open_ledger(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise LedgerError(f"{path}: integrity check failed")
    except (sqlite3.DatabaseError, LedgerError) as exc:
        conn.close()
        raise LedgerError(f"{path}: {exc}") from exc
    return conn


def epoch(stamp: str | None) -> int | None:
    if activity.utc(stamp) is None:
        return None
    moment = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")  # type: ignore[arg-type]
    return int(moment.replace(tzinfo=timezone.utc).timestamp())


def event_id(agent: str, record: dict[str, Any]) -> str:
    """Stable ID: the same record of the same agent is the same event."""
    body = json.dumps([agent, record], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


@dataclass
class IngestResult:
    stored: int = 0
    duplicates: int = 0
    invalid: int = 0


def ingest(conn: sqlite3.Connection, agent: str, path: Path) -> IngestResult:
    """Store the new complete lines of `path`; one transaction with its cursor.

    A partial last line waits for the next call. A replaced or truncated file
    (other inode, or shorter than the cursor) is read from the start; event
    IDs make that replay safe. Invalid lines are counted, never stored.
    """
    result = IngestResult()
    source = f"{agent}:{path}"
    try:
        info = path.stat()
    except FileNotFoundError:
        return result
    row = conn.execute(
        "SELECT device, inode, offset FROM cursors WHERE source = ?", (source,)
    ).fetchone()
    offset = 0
    if (
        row
        and row[0] == info.st_dev
        and row[1] == info.st_ino
        and row[2] <= info.st_size
    ):
        offset = row[2]
    with path.open("rb") as handle:
        handle.seek(offset)
        data = handle.read()
    end = data.rfind(b"\n")
    if end < 0:
        return result
    complete = data[: end + 1]
    conn.execute("BEGIN IMMEDIATE")
    try:
        for line in complete.splitlines():
            record = parse(line)
            if record is None:
                result.invalid += 1
                continue
            at = epoch(record["at"]) or epoch(record["tick"])
            cursor = conn.execute(
                "INSERT OR IGNORE INTO events (id, agent, at, record)"
                " VALUES (?, ?, ?, ?)",
                (
                    event_id(agent, record),
                    agent,
                    at,
                    json.dumps(record, sort_keys=True),
                ),
            )
            if cursor.rowcount:
                result.stored += 1
            else:
                result.duplicates += 1
        conn.execute(
            "INSERT OR REPLACE INTO cursors (source, device, inode, offset)"
            " VALUES (?, ?, ?, ?)",
            (source, info.st_dev, info.st_ino, offset + len(complete)),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return result


def parse(line: bytes) -> dict[str, Any] | None:
    if len(line) > MAX_LINE:
        return None
    try:
        event = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return None
    return activity.record(event)


def observe(
    conn: sqlite3.Connection, agent: str, start: int, end: int, kind: str = "fresh"
) -> None:
    """Record a coverage span: the agent's sources were read and fresh."""
    conn.execute(
        "INSERT INTO coverage (agent, start, end, kind) VALUES (?, ?, ?, ?)"
        " ON CONFLICT (agent, start, kind) DO UPDATE SET end = max(end, excluded.end)",
        (agent, start, end, kind),
    )


@dataclass(frozen=True)
class Interval:
    state: str
    start: int
    end: int
    reason_code: str | None = None
    scope: str | None = None


def state_after(record: dict[str, Any]) -> str:
    if record["event"] in TICK_END:
        return "idle"
    if record["event"] in RETURN_TO_RUNNER:
        return "runner"
    return record["activity"]


def fresh_spans(
    conn: sqlite3.Connection, agent: str, start: int, end: int
) -> list[tuple[int, int]]:
    rows = conn.execute(
        "SELECT start, end FROM coverage WHERE agent = ? AND kind = 'fresh'"
        " AND end > ? AND start < ? ORDER BY start",
        (agent, start, end),
    ).fetchall()
    merged: list[tuple[int, int]] = []
    for lo, hi in rows:
        lo, hi = max(lo, start), min(hi, end)
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        elif hi > lo:
            merged.append((lo, hi))
    return merged


def intervals(
    conn: sqlite3.Connection, agent: str, start: int, end: int
) -> list[Interval]:
    """Intervals of `[start, end)`; time without fresh coverage is `unknown`.

    The state at `start` comes from the last event before it (the boundary
    state retention keeps). A state never extends past the last fresh
    observation: an outage is unknown, not carried-over work.
    """
    raw: list[Interval] = []
    previous = conn.execute(
        "SELECT record FROM events WHERE agent = ? AND at < ? ORDER BY at DESC, rowid DESC LIMIT 1",
        (agent, start),
    ).fetchone()
    state, reason = ("unknown", None)
    if previous:
        record = json.loads(previous[0])
        state, reason = state_after(record), record
    cursor = start
    for (text,) in conn.execute(
        "SELECT record FROM events WHERE agent = ? AND at >= ? AND at < ? ORDER BY at, rowid",
        (agent, start, end),
    ):
        record = json.loads(text)
        at = epoch(record["at"]) or epoch(record["tick"]) or cursor
        if at > cursor:
            raw.append(make(state, cursor, at, reason))
            cursor = at
        state, reason = state_after(record), record
    if end > cursor:
        raw.append(make(state, cursor, end, reason))
    return clip_to_coverage(raw, fresh_spans(conn, agent, start, end), start, end)


def make(state: str, start: int, end: int, record: dict[str, Any] | None) -> Interval:
    if state == "waiting" and record:
        return Interval(
            state, start, end, record.get("reason_code"), record.get("scope")
        )
    return Interval(state, start, end)


def clip_to_coverage(
    raw: list[Interval], spans: list[tuple[int, int]], start: int, end: int
) -> list[Interval]:
    out: list[Interval] = []
    cursor = start
    for lo, hi in spans + [(end, end)]:
        if lo > cursor:
            out.append(Interval("unknown", cursor, lo))
        for item in raw:
            a, b = max(item.start, lo), min(item.end, hi)
            if b > a:
                out.append(Interval(item.state, a, b, item.reason_code, item.scope))
        cursor = max(cursor, hi)
    return [item for item in out if item.end > item.start]


@dataclass
class Bucket:
    """One overview bucket: the dominant state by elapsed time plus flags."""

    agent: str
    resolution: str
    start: int
    end: int
    state: str
    share: float
    breakdown: dict[str, int] = field(default_factory=dict)
    results: dict[str, int] = field(default_factory=dict)
    partial: bool = False
    late_correction: bool = False
    edge: bool = False
    in_progress: bool = False

    def record(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "resolution": self.resolution,
            "start": self.start,
            "end": self.end,
            "state": self.state,
            "share": round(self.share, 4),
            "breakdown": dict(sorted(self.breakdown.items())),
            "results": dict(sorted(self.results.items())),
            "flags": {
                "partial": self.partial,
                "late_correction": self.late_correction,
                "edge": self.edge,
                "in_progress": self.in_progress,
            },
        }


def dominant(
    items: Iterable[Interval], start: int, end: int
) -> tuple[str, float, dict[str, int]]:
    """The state with the most time in `[start, end)`, its share, the breakdown.

    Ties go to the tied state seen latest in the bucket, then to the fixed
    state order. Unknown time takes part like any other state.
    """
    durations: dict[str, int] = {}
    last_seen: dict[str, int] = {}
    for item in items:
        a, b = max(item.start, start), min(item.end, end)
        if b <= a:
            continue
        durations[item.state] = durations.get(item.state, 0) + (b - a)
        last_seen[item.state] = max(last_seen.get(item.state, a), b)
    total = end - start
    covered = sum(durations.values())
    if covered < total:
        durations["unknown"] = durations.get("unknown", 0) + total - covered
        last_seen.setdefault("unknown", start)
    best = max(
        durations,
        key=lambda s: (
            durations[s],
            last_seen[s],
            -STATE_ORDER.get(s, len(STATE_ORDER)),
        ),
    )
    return best, durations[best] / total, durations


def resolution(range_s: float) -> str:
    """The finest width `w` with ceil(range / w) + 1 <= MAX_BUCKETS.

    Raises ValueError for a range outside (0, 30 days]: never a month of
    10 s buckets.
    """
    if not (0 < range_s <= MAX_RANGE_S):
        raise ValueError(f"range {range_s}s is outside (0, {MAX_RANGE_S}s]")
    for name, width in RESOLUTIONS.items():
        if math.ceil(range_s / width) + 1 <= MAX_BUCKETS:
            return name
    raise ValueError(f"range {range_s}s needs more than {MAX_BUCKETS} buckets")


def bucket_starts(start: int, end: int, width: int) -> Iterator[int]:
    """UTC-aligned bucket starts that enclose `[start, end)`."""
    first = start - start % width
    for at in range(first, end, width):
        yield at


def build_bucket(
    conn: sqlite3.Connection,
    agent: str,
    name: str,
    at: int,
    *,
    sealed_until: int,
    view: tuple[int, int] | None = None,
) -> Bucket:
    width = RESOLUTIONS[name]
    end = at + width
    items = intervals(conn, agent, at, end)
    state, share, breakdown = dominant(items, at, end)
    results: dict[str, int] = {}
    for (text,) in conn.execute(
        "SELECT record FROM events WHERE agent = ? AND at >= ? AND at < ?",
        (agent, at, end),
    ):
        outcome = json.loads(text).get("outcome")
        if outcome:
            results[outcome] = results.get(outcome, 0) + 1
    late_correction = (
        conn.execute(
            "SELECT 1 FROM coverage WHERE agent = ? AND kind = 'late' AND start >= ? AND start < ?",
            (agent, at, end),
        ).fetchone()
        is not None
    )
    return Bucket(
        agent,
        name,
        at,
        end,
        state,
        share,
        breakdown,
        results,
        partial="unknown" in breakdown,
        late_correction=late_correction,
        edge=view is not None and (at < view[0] or end > view[1]),
        in_progress=end + SEAL_GRACE_S > sealed_until,
    )


def seal(
    conn: sqlite3.Connection, agent: str, name: str, at: int, *, now: int
) -> Bucket | None:
    """Publish a finished bucket once; a sealed bucket never changes.

    Returns None while the bucket is in progress. A replay returns the
    stored bucket unchanged, so its outbox row (ID, timestamp, payload) is
    identical and adds nothing twice.
    """
    width = RESOLUTIONS[name]
    if at + width + SEAL_GRACE_S > now:
        return None
    stored = conn.execute(
        "SELECT record FROM buckets WHERE agent = ? AND resolution = ? AND start = ?",
        (agent, name, at),
    ).fetchone()
    if stored:
        return bucket_of(json.loads(stored[0]))
    bucket = build_bucket(conn, agent, name, at, sealed_until=now)
    payload = json.dumps(bucket.record(), sort_keys=True, separators=(",", ":"))
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO buckets (agent, resolution, start, record) VALUES (?, ?, ?, ?)",
            (agent, name, at, payload),
        )
        # Loki timestamp of a bucket: its end.
        conn.execute(
            "INSERT OR IGNORE INTO outbox (id, stream, ts, payload, state)"
            " VALUES (?, 'agent_activity_buckets', ?, ?, 'pending')",
            (f"bucket:{agent}:{name}:{at}", bucket.end, payload),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return bucket


def mark_late(conn: sqlite3.Connection, agent: str, at: int, now: int) -> None:
    """A record arrived for time already sealed: keep it, flag the rollup."""
    sealed = conn.execute(
        "SELECT 1 FROM buckets WHERE agent = ? AND start <= ? LIMIT 1", (agent, at)
    ).fetchone()
    if sealed:
        conn.execute(
            "INSERT OR IGNORE INTO coverage (agent, start, end, kind) VALUES (?, ?, ?, 'late')",
            (agent, at, now),
        )


def bucket_of(data: dict[str, Any]) -> Bucket:
    flags = data["flags"]
    return Bucket(
        data["agent"],
        data["resolution"],
        data["start"],
        data["end"],
        data["state"],
        data["share"],
        data["breakdown"],
        data["results"],
        flags["partial"],
        flags["late_correction"],
        flags["edge"],
        flags["in_progress"],
    )


@dataclass(frozen=True)
class Wait:
    scope: str
    reason_code: str
    since: int
    retry_at: int | None = None
    ended_at: int | None = None


def late(
    *,
    now: float,
    last_start: float | None,
    registered: float | None,
    interval: float,
    budget: float,
    paused: bool,
    waits: Iterable[Wait] = (),
    fresh_s: float | None = None,
) -> bool:
    """The one lateness rule for presence, activity and `AgentLate`.

    Late when the last tick started (or, before the first tick, the agent
    registered) more than `2 * interval + budget` ago, unless the runner is
    paused or a fresh agent-wide wait still holds. A target-only wait never
    suppresses lateness. A wait holds until its retry time or recorded end;
    without either it holds while it is fresh (`fresh_s`, default the
    threshold). After that the age rule applies again.
    """
    anchor = last_start if last_start is not None else registered
    if anchor is None or paused:
        return False
    overdue = 2 * interval + budget
    if now - anchor <= overdue:
        return False
    limit = overdue if fresh_s is None else fresh_s
    for wait in waits:
        if wait.scope != "agent":
            continue
        if wait.ended_at is not None and wait.ended_at <= now:
            continue
        if wait.retry_at is not None:
            if now < wait.retry_at:
                return False
            continue
        if now - wait.since <= limit:
            return False
    return True


def prune(conn: sqlite3.Connection, now: int, batch: int = PRUNE_BATCH) -> int:
    """Delete closed history older than 30 days in bounded batches.

    Keeps each agent's last event before the boundary (the state at the
    start of the window). An undelivered outbox row that expires is marked
    `expired` (an export gap), not silently removed. Returns rows deleted.
    """
    boundary = now - RETENTION_S
    deleted = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = conn.execute(
            "DELETE FROM events WHERE rowid IN ("
            " SELECT e.rowid FROM events e WHERE e.at < ? AND e.id != ("
            "  SELECT k.id FROM events k WHERE k.agent = e.agent AND k.at < ?"
            "  ORDER BY k.at DESC, k.rowid DESC LIMIT 1)"
            " LIMIT ?)",
            (boundary, boundary, batch),
        )
        deleted += cursor.rowcount
        deleted += conn.execute(
            "DELETE FROM buckets WHERE rowid IN ("
            " SELECT rowid FROM buckets WHERE start < ? LIMIT ?)",
            (boundary, batch),
        ).rowcount
        deleted += conn.execute(
            "DELETE FROM coverage WHERE rowid IN ("
            " SELECT rowid FROM coverage WHERE end < ? LIMIT ?)",
            (boundary, batch),
        ).rowcount
        conn.execute(
            "UPDATE outbox SET state = 'expired'"
            " WHERE ts < ? AND state IN ('pending', 'staged')",
            (boundary,),
        )
        deleted += conn.execute(
            "DELETE FROM outbox WHERE rowid IN ("
            " SELECT rowid FROM outbox WHERE ts < ? AND state = 'delivered' LIMIT ?)",
            (boundary, batch),
        ).rowcount
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return deleted


def default_path(runtime: Path) -> Path:
    return runtime / "agent-activity.sqlite3"


def disk_bytes(path: Path) -> int:
    return sum(
        os.path.getsize(f"{path}{suffix}")
        for suffix in ("", "-wal", "-shm")
        if os.path.exists(f"{path}{suffix}")
    )
