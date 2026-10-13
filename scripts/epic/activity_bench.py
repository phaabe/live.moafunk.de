"""Scale benchmark of the activity ledger (leaf A681.1.2 of issue 681).

Explicitly invoked, never part of a default test part:

  python3 scripts/epic/activity_bench.py --agents 12 --days 30

It stream-generates tick events for `--agents` agents at one attempt per
`--tick` seconds: model work, idle ticks, short failures, agent-wide and
target-only waits, one retired agent and its replacement, a collector
outage and a missing producer file. Each simulated collector cycle (every
`--cycle` seconds) appends only the events due by then, rotates each
producer file once a simulated day, and runs `Collector.cycle`: the ledger
is processed incrementally and never loaded into memory as a whole.

It prints one JSON object: the command, the parameters, the counts, the
runtime, peak memory, disk size of the ledger and query timings. The
default suite runs a small deterministic sample (test_activity_history).
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import random
import resource
import sqlite3
import sys
import tempfile
import time
from typing import Any

import activity_history as ah

DAY = 86_400
# A 2 h-aligned UTC start, so every resolution starts on a bucket edge.
START = 1_791_000_000 - 1_791_000_000 % 7200


def iso(at: int) -> str:
    return datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def event(kind: str, tick: int, at: int, **extra: Any) -> dict[str, Any]:
    return {"event": kind, "tick": iso(tick), "at": iso(at), **extra}


def attempt(rng: random.Random, tick: int) -> list[dict[str, Any]]:
    """The tick events of one attempt that starts at `tick`."""
    roll = rng.random()
    if roll < 0.05:  # a short failure
        return [
            event("start", tick, tick, action="claim"),
            event("finish", tick, tick + 2, action="claim", outcome="error"),
        ]
    if roll < 0.10:  # an agent-wide wait with a retry time
        retry = iso(tick + 600)
        return [
            event("start", tick, tick, action="claim"),
            event(
                "wait",
                tick,
                tick + 1,
                action="claim",
                reason_code="github_quota",
                scope="agent",
                retry_at=retry,
            ),
            event("finish", tick, tick + 2, action="claim", outcome="blocked"),
        ]
    if roll < 0.15:  # a target-only wait
        return [
            event("start", tick, tick, action="fix"),
            event(
                "wait",
                tick,
                tick + 1,
                action="fix",
                reason_code="target_lock",
                scope="target",
                target="pr:723",
            ),
            event("finish", tick, tick + 2, action="fix", outcome="blocked"),
        ]
    if roll < 0.55:  # nothing to do
        return [
            event("start", tick, tick, action="idle"),
            event("finish", tick, tick + 1, action="idle", outcome="ok"),
        ]
    return [  # model work
        event("start", tick, tick, action="continue", target="pr:723"),
        event("model-start", tick, tick + 3, action="continue"),
        event("model-end", tick, tick + 25, action="continue"),
        event("finish", tick, tick + 27, action="continue", outcome="ok"),
    ]


class Producer:
    """One agent's tick-event file, written as the simulated time passes."""

    def __init__(self, name: str, home: Path, first_tick: int, seed: int) -> None:
        self.name = name
        self.path = home / f"{name}-events.jsonl"
        self.rng = random.Random(f"{seed}:{name}")
        self.next_tick = first_tick
        self.due: list[dict[str, Any]] = []
        self.attempts = 0
        self.events = 0
        self.day: int | None = None

    def advance(self, now: int, tick_s: int, writing: bool) -> None:
        while self.next_tick <= now:
            if writing:
                self.due.extend(attempt(self.rng, self.next_tick))
                self.attempts += 1
            self.next_tick += tick_s
        if not writing:
            return
        self.due.sort(key=lambda e: e["at"])
        ready = [e for e in self.due if ah.epoch(e["at"]) <= now]  # type: ignore[operator]
        if not ready:
            return
        self.due = self.due[len(ready) :]
        with self.path.open("a") as handle:
            handle.writelines(json.dumps(e) + "\n" for e in ready)
        self.events += len(ready)

    def rotate(self, now: int) -> None:
        """Start a new file each day (a new inode; the ledger reads it anew)."""
        day = (now - START) // DAY
        if self.day is not None and day != self.day and self.path.exists():
            fresh = self.path.with_suffix(".new")
            fresh.touch()
            os.replace(fresh, self.path)
        self.day = day


def peak_rss_bytes() -> int:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes, Linux kibibytes.
    return peak if sys.platform == "darwin" else peak * 1024


def timed(fn: Any, *args: Any, **kwargs: Any) -> tuple[Any, float]:
    begin = time.perf_counter()
    value = fn(*args, **kwargs)
    return value, round(time.perf_counter() - begin, 4)


def run(
    root: Path,
    *,
    agents: int = 12,
    days: float = 30,
    tick_s: int = 30,
    cycle_s: int = 30,
    seed: int = 681,
) -> dict[str, Any]:
    """Run the benchmark in `root` and return its measurements."""
    span = int(days * DAY)
    end = START + span
    home = root / "agents"
    home.mkdir(parents=True, exist_ok=True)
    producers = [
        Producer(f"a{index:02d}", home, START + (index * 7) % tick_s, seed)
        for index in range(agents)
    ]
    # The last agent retires a third in; its replacement starts then.
    retire_at = START + span // 3
    replacement = Producer("a99", home, retire_at + 5 % tick_s, seed)
    # The collector is down for up to an hour; one producer file is missing
    # for up to half an hour. Both stay unknown time in the ledger.
    down = min(3_600, span // 12)
    outage = (START + span // 2, START + span // 2 + down)
    missing = (START + span * 2 // 3, START + span * 2 // 3 + down // 2)
    collector = ah.Collector(root)
    cycles = 0
    unhealthy = 0
    begin = time.perf_counter()
    for now in range(START, end + 1, cycle_s):
        active = [p for p in producers if p is not producers[-1] or now < retire_at]
        if now >= retire_at:
            active.append(replacement)
        hidden = producers[0].path.with_suffix(".hidden")
        gone = missing[0] <= now < missing[1]
        if gone and producers[0].path.exists():
            os.replace(producers[0].path, hidden)
        elif not gone and hidden.exists():
            os.replace(hidden, producers[0].path)
        for producer in active:
            producer.rotate(now)
            # The agent with the missing file writes nothing meanwhile.
            producer.advance(
                now, tick_s, writing=not (gone and producer is producers[0])
            )
        if outage[0] <= now < outage[1]:
            continue
        collector.cycle([(p.name, p.path) for p in active], now)
        cycles += 1
        unhealthy += not collector.healthy
    runtime = round(time.perf_counter() - begin, 2)
    collector.close()
    conn = ah.open_ledger(ah.default_path(root))
    try:
        result = measure(conn, producers[1].name, end)
        rows = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("events", "buckets", "coverage", "outbox")
        }
    finally:
        conn.close()
    everyone = producers + [replacement]
    return {
        "command": " ".join(
            [
                "python3 scripts/epic/activity_bench.py",
                f"--agents {agents} --days {days:g} --tick {tick_s}",
                f"--cycle {cycle_s} --seed {seed}",
            ]
        ),
        "agents": agents,
        "days": days,
        "attempts": sum(p.attempts for p in everyone),
        "events_written": sum(p.events for p in everyone),
        "cycles": cycles,
        "unhealthy_cycles": unhealthy,
        "rows": rows,
        "runtime_s": runtime,
        "peak_rss_bytes": peak_rss_bytes(),
        "disk_bytes": ah.disk_bytes(ah.default_path(root)),
        "queries_s": result,
    }


def measure(conn: sqlite3.Connection, agent: str, end: int) -> dict[str, float]:
    """Query timings at the end of the run, over the full retained range."""
    start = max(START, end - ah.RETENTION_S)
    name = ah.resolution(end - start)
    width = ah.RESOLUTIONS[name]
    rows, overview = timed(
        lambda: conn.execute(
            "SELECT record FROM buckets WHERE agent = ? AND resolution = ?"
            " AND start >= ? AND start < ?",
            (agent, name, start - start % width, end),
        ).fetchall()
    )
    _, detail_hour = timed(ah.intervals, conn, agent, end - 3_600, end)
    _, detail_range = timed(ah.intervals, conn, agent, start, end)
    _, bucket = timed(
        ah.build_bucket, conn, agent, name, end - width - end % width, sealed_until=end
    )
    _, waits = timed(ah.current_waits, conn, agent)
    return {
        "resolution": name,  # type: ignore[dict-item]
        "overview_buckets": len(rows),  # type: ignore[dict-item]
        "overview_one_agent": overview,
        "detail_one_hour": detail_hour,
        "detail_full_range": detail_range,
        "build_one_bucket": bucket,
        "current_waits": waits,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scale benchmark of the activity ledger."
    )
    parser.add_argument("--agents", type=int, default=12)
    parser.add_argument("--days", type=float, default=30)
    parser.add_argument("--tick", type=int, default=30, help="seconds per attempt")
    parser.add_argument("--cycle", type=int, default=30, help="seconds per cycle")
    parser.add_argument("--seed", type=int, default=681)
    parser.add_argument("--dir", type=Path, help="keep the ledger here")
    args = parser.parse_args(argv)
    if args.agents < 2 or args.days <= 0 or args.tick <= 0 or args.cycle <= 0:
        parser.error("need --agents >= 2 and positive --days, --tick, --cycle")
    options = dict(
        agents=args.agents,
        days=args.days,
        tick_s=args.tick,
        cycle_s=args.cycle,
        seed=args.seed,
    )
    if args.dir:
        args.dir.mkdir(parents=True, exist_ok=True)
        result = run(args.dir, **options)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            result = run(Path(tmp), **options)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["unhealthy_cycles"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
