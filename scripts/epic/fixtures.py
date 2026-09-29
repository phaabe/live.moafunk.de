"""Fixture data for the agent dashboards: the 10 design scenarios.

    python3 scripts/epic/fixtures.py SCENARIO --state-dir DIR [--output DIR]

Builds a runner state dir for SCENARIO, then publishes metrics every 5 s
like the collector, with a fixed GitHub snapshot instead of GitHub. Start
the stack with `tools/agent-monitoring/preview.sh SCENARIO`. It never calls
GitHub and never reads the real state dir.

Scenarios: normal, single, busy, full, late, failing, collision, retired,
paused, stale.
"""

from __future__ import annotations

import argparse
import fcntl
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import random
import shutil
import time
from typing import BinaryIO

import agents
import delivery
import monitor
import tick_events
import ticks

REPO_URL = monitor.REPO_URL
HEAD = "c0ffee" + "0" * 34


@dataclass
class Spec:
    """One fixture agent. `outcomes` are its past ticks, oldest first."""

    id: str
    label: str = ""
    interval: float = 600
    budget: float = 1800
    outcomes: str = "ok ok ok ok blocked ok ok ok ok ok ok ok ok ok ok ok ok ok ok ok"
    last_start_ago: float = 300
    running: int | None = None  # PR (or issue) the running tick works on
    running_action: str = "review"
    running_issue: bool = False  # `running` is an issue number
    running_for: float = 240
    retired: bool = False
    backoff: int = 0
    denials: int = 0
    tokens: list[int] = field(default_factory=list)
    action: str = "review"
    pr: int = 407  # target of the finished ticks
    # The preview keeps a live agent ticking; late and failing ones stay so.
    live: bool = True
    registered_ago: float = 7 * 86400


FAILING = "ok ok ok ok ok ok ok ok ok ok ok ok ok ok ok ok error error error error"
MIXED = "ok ok timeout ok ok ok blocked ok ok ok killed ok ok ok ok error ok ok ok ok"
BLOCKED_LAST = "ok ok ok ok ok blocked ok ok ok ok ok ok ok ok ok ok ok ok ok blocked"


def scenario(name: str) -> tuple[list[Spec], bool]:
    """Agents and whether the loop is paused."""
    two = [
        Spec("claude", "executor", interval=600, running=412, denials=2),
        Spec("codex", "reviewer", interval=180, outcomes=MIXED, backoff=1),
    ]
    if name == "normal":
        # The agents of the design's "normal" frame.
        return [
            # Targets from github_snapshot(), so the Task column has text.
            Spec(
                "claude",
                "epic implementer",
                running=412,
                running_action="fix",
                running_for=560,
            ),
            Spec(
                "claude-2",
                "frontend pair",
                running=303,
                running_issue=True,
                running_action="continue",
                running_for=1030,
            ),
            Spec(
                "codex",
                "backend implementer",
                running=302,
                running_issue=True,
                running_action="claim",
                running_for=1550,
            ),
            Spec(
                "codex-review",
                "reviews Claude PRs",
                running=411,
                running_for=180,
            ),
            Spec(
                "claude-docs",
                "docs and changelog",
                last_start_ago=480,
                action="merge",
                pr=416,
                denials=1,
            ),
            Spec(
                "codex-2",
                "second backend pair",
                interval=300,
                last_start_ago=270,
                outcomes=MIXED,
                action="continue",
                pr=373,
            ),
            Spec(
                "codex-ops",
                "CI and release chores",
                interval=300,
                last_start_ago=240,
                outcomes=BLOCKED_LAST,
                action="claim",
                pr=374,
                backoff=1,
            ),
            # Registered 6 min ago, 10 min interval: "first in 4m".
            Spec("claude-3", "ops scripts", outcomes="", registered_ago=360),
        ], False
    if name == "single":
        return [Spec("claude", "executor", running=412)], False
    if name == "busy":
        return [
            Spec("claude", "executor", running=412),
            Spec("claude-2", "docs", running=415),
            Spec("codex", "reviewer", interval=180, running=409),
            Spec("codex-2", "second reviewer", interval=300, running=411),
        ], False
    if name == "full":
        specs = [*two]
        for i in range(2, 7):
            specs.append(Spec(f"claude-{i}", f"worker {i}", outcomes=MIXED))
            specs.append(Spec(f"codex-{i}", f"reviewer {i}", interval=300))
        return specs, False
    if name == "late":
        return [
            two[0],
            Spec(
                "codex", "reviewer", interval=180, last_start_ago=4 * 3600, live=False
            ),
        ], False
    if name == "failing":
        return [
            two[0],
            Spec("codex", "reviewer", interval=180, outcomes=FAILING, live=False),
        ], False
    if name == "collision":
        return [
            Spec("claude", "executor", running=412),
            Spec("claude-2", "second executor", running=412),
            two[1],
        ], False
    if name == "retired":
        return [*two, Spec("codex-old", "old reviewer", retired=True)], False
    if name == "paused":
        return [
            Spec("claude", "executor"),
            Spec("codex", "reviewer", interval=180),
        ], True
    if name == "stale":
        return two, False
    raise SystemExit(f"unknown scenario {name}; see --help")


SCENARIOS = (
    "normal",
    "single",
    "busy",
    "full",
    "late",
    "failing",
    "collision",
    "retired",
    "paused",
    "stale",
)


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def build_state(root: Path, specs: list[Spec], now: float) -> None:
    """A fresh state dir: registered agents with events, logs and locks.

    Only a dir this tool made (marked `.fixture`) is ever deleted.
    """
    # The runners' default, not EPIC_STATE_DIR: preview.sh sets that to root.
    if root.resolve() == (Path.home() / ".local/state/epic-loop").resolve():
        raise SystemExit("refusing to use the real runner state dir")
    if root.exists():
        if any(root.iterdir()) and not (root / ".fixture").exists():
            raise SystemExit(f"{root} is not a fixture dir; refusing to delete it")
        # Empty it, but keep the dir: containers bind-mount it.
        for child in root.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
    root.mkdir(parents=True, exist_ok=True)
    (root / ".fixture").write_text("made by scripts/epic/fixtures.py\n")
    for spec in specs:
        kind = agents.kind_of(spec.id)
        # The fixture state dir holds registered agents only.
        home = agents.register(
            root,
            spec.id,
            now - spec.registered_ago,
            label=spec.label,
            interval=spec.interval,
            budget=spec.budget,
        )
        outcomes = spec.outcomes.split()
        events, log = home / f"{kind}-ticks.jsonl", home / f"{kind}.log"
        with events.open("w") as ev, log.open("w") as out:
            for i, outcome in enumerate(outcomes):
                ago = spec.last_start_ago + (len(outcomes) - 1 - i) * spec.interval
                start = now - ago
                code = {"ok": 0, "blocked": 75, "timeout": 124, "killed": 143}.get(
                    outcome, 1
                )
                ev.write(
                    json.dumps({"v": 1, "event": "start", "tick": iso(start), "pid": 1})
                    + "\n"
                )
                ev.write(
                    json.dumps(
                        {
                            "v": 1,
                            "event": "finish",
                            "tick": iso(start),
                            "at": iso(start + min(spec.interval / 2, 240)),
                            "exit": code,
                            "outcome": outcome,
                            "phase": "model" if outcome != "ok" else "record",
                            "action": spec.action,
                            "pr": spec.pr,
                            "issue": None,
                            "tokens": 12000 + i * 100 if kind == "codex" else None,
                        }
                    )
                    + "\n"
                )
                out.write(
                    f"\ntick: started {iso(start)} repo=/fixture\n"
                    f'{{"action": "{spec.action}", "pr": {spec.pr}}}\n'
                    f"tick: finished exit={code}\n"
                )
        if spec.running is not None:
            start = now - spec.running_for
            lock = home / f"{kind}.lock"
            lock.mkdir()
            # The fixture's own pid, so the lock owner looks alive.
            (lock / "owner.json").write_text(
                json.dumps(
                    {"pid": os.getpid(), "started_at": start, "max_age": spec.budget}
                )
            )
            (lock / "action.json").write_text(
                json.dumps(
                    {
                        "action": spec.running_action,
                        **(
                            {"issue": f"{REPO_URL}/issues/{spec.running}"}
                            if spec.running_issue
                            else {"pr": spec.running}
                        ),
                    }
                )
            )
            tick_events.append(
                events, {"v": 1, "event": "start", "tick": iso(start), "pid": 1}
            )
            with log.open("a") as out:
                out.write(f"\ntick: started {iso(start)} repo=/fixture\n")
        if spec.backoff:
            (home / "codex-backoff.json").write_text(
                json.dumps(
                    {
                        f"pr:{409 + i}:{HEAD}": {
                            "at": now - 60,
                            "until": now + 840,
                            "reason": "fixture",
                        }
                        for i in range(spec.backoff)
                    }
                )
            )
        if kind == "claude":
            (home / "claude-permissions.log").write_text("")
        if spec.retired:
            agents.retire(root, spec.id, now - 3600)


def last_starts(specs: list[Spec], now: float) -> dict[str, float]:
    """Start time of each agent's newest tick, as build_state wrote it."""

    def ago(spec: Spec) -> float:
        if spec.running is not None:
            return spec.running_for
        # A new agent's first tick is due one interval after registering.
        return spec.last_start_ago if spec.outcomes.split() else spec.registered_ago

    return {spec.id: now - ago(spec) for spec in specs}


def tick(home: Path, kind: str, spec: Spec, start: float, at: float) -> None:
    """One finished ok tick in the events file and the runner log."""
    events, log = home / f"{kind}-ticks.jsonl", home / f"{kind}.log"
    tick_events.append(events, {"v": 1, "event": "start", "tick": iso(start), "pid": 1})
    finish(events, log, spec, start, at)


def finish(events: Path, log: Path, spec: Spec, start: float, at: float) -> None:
    tick_events.append(
        events,
        {
            "v": 1,
            "event": "finish",
            "tick": iso(start),
            "at": iso(at),
            "exit": 0,
            "outcome": "ok",
            "phase": "record",
            "action": spec.action,
            "pr": spec.pr,
            "issue": None,
            "tokens": None,
        },
    )
    with log.open("a") as out:
        out.write(
            f"\ntick: started {iso(start)} repo=/fixture\n"
            f'{{"action": "{spec.action}", "pr": {spec.pr}}}\n'
            "tick: finished exit=0\n"
        )


def advance(
    root: Path, specs: list[Spec], starts: dict[str, float], now: float
) -> None:
    """Keep a long preview normal: a due tick starts, a long one finishes.

    Without this, idle agents turn late and running ones pass their budget.
    """
    for spec in specs:
        idle = spec.running is None
        if not spec.live or spec.retired:
            continue
        kind = agents.kind_of(spec.id)
        home = root / "agents" / spec.id
        start = starts[spec.id]
        if idle and now >= start + spec.interval:
            starts[spec.id] = start + spec.interval
            tick(home, kind, spec, starts[spec.id], min(starts[spec.id] + 60, now))
        elif not idle and now - start > 0.9 * spec.budget:
            events, log = home / f"{kind}-ticks.jsonl", home / f"{kind}.log"
            finish(events, log, spec, start, now)
            starts[spec.id] = now
            tick_events.append(
                events, {"v": 1, "event": "start", "tick": iso(now), "pid": 1}
            )
            owner = home / f"{kind}.lock" / "owner.json"
            owner.write_text(
                json.dumps(
                    {"pid": os.getpid(), "started_at": now, "max_age": spec.budget}
                )
            )


def history(specs: list[Spec], now: float, hours: int = 24) -> str:
    """OpenMetrics for the outcome timeline: `hours` of past ticks, one
    sample a minute, ending 2 min ago. Labels match the preview's scrape."""
    scrape = 'instance="exporter:9100",job="epic-agents"'
    weights = {
        "ok": 80,
        "blocked": 8,
        "error": 5,
        "timeout": 3,
        "killed": 2,
        "interrupted": 2,
    }
    end = int(now // 60 * 60) - 120
    steps = range(end - hours * 3600, end + 1, 60)
    lines = ["# TYPE epic_local_snapshot_timestamp_seconds gauge"]
    lines += [
        f"epic_local_snapshot_timestamp_seconds{{{scrape}}} {t} {t}" for t in steps
    ]
    outcome, presence = [], []
    for spec in specs:
        if spec.retired or (spec.running is None and not spec.outcomes.split()):
            continue
        labels = f'agent="{spec.id}",{scrape}'
        for t in steps:
            # The same outcome for the same tick slot in every run, so a
            # second backfill overlaps the first without conflicts.
            slot = int(t // spec.interval)
            (name,) = random.Random(f"{spec.id}:{slot}").choices(
                list(weights), weights=list(weights.values())
            )
            value = ticks.SEVERITY[name]
            outcome.append(f"epic_tick_last_outcome{{{labels}}} {value} {t}")
            presence.append(f"epic_agent_presence{{{labels}}} 2 {t}")
    return "\n".join(
        [
            *lines,
            "# TYPE epic_tick_last_outcome gauge",
            *outcome,
            "# TYPE epic_agent_presence gauge",
            *presence,
            "# EOF",
            "",
        ]
    )


def add_denials(root: Path, specs: list[Spec], now: float) -> None:
    """Denials after the ledger's baseline, so the 1 h increase shows them."""
    for spec in specs:
        if spec.denials:
            path = root / "agents" / spec.id / "claude-permissions.log"
            with path.open("a") as out:
                for _ in range(spec.denials):
                    out.write(f"{iso(now)} deny Bash(rm -rf fixture)\n")


def github_snapshot() -> monitor.Json:
    """Issues with leaves per area and open PRs, one of them waiting."""

    def item(number: int, executor: str, area: str, status: str, done: int, total: int):
        body = "\n".join(
            f"- [{'x' if i < done else ' '}] **{area[0]}1.{number % 10}.{i + 1}** leaf {i + 1}"
            for i in range(total)
        )
        return {
            "executor": executor,
            "status": status,
            "area": area,
            "level": "Subtask",
            "title": f"[{area[0]}1.{number % 10}] Fixture task {number}",
            "content": {
                "type": "Issue",
                "number": number,
                "url": f"{REPO_URL}/issues/{number}",
                "title": f"Fixture task {number}",
                "body": body,
                "labels": [],
            },
        }

    def pr(number: int, executor: str, title: str, **extra: object) -> monitor.Json:
        return {
            "number": number,
            "title": title,
            "body": f"Executor: {executor}\nIssue: {REPO_URL}/issues/{301 + number % 4}",
            "baseRefName": "dev/312-interim",
            "headRefName": f"feat/{number}",
            "headRefOid": HEAD,
            "isDraft": False,
            "labels": [],
            "mergeable": "MERGEABLE",
            "statusCheckRollup": [{"conclusion": "SUCCESS"}],
            "comments": [],
            **extra,
        }

    return {
        "items": [
            item(301, "Claude", "Backend", "In progress", 3, 6),
            item(302, "Codex", "Operations", "Ready", 1, 5),
            item(303, "Claude", "Frontend", "Done", 4, 4),
            item(304, "", "Coordination", "Backlog", 0, 6),
        ],
        "prs": [
            pr(412, "Claude", "Stream: reconnect after network loss"),
            pr(409, "Codex", "Ops: rotate recorder logs"),
            pr(
                411,
                "Claude",
                "Admin: show live listeners",
                statusCheckRollup=[{"conclusion": "FAILURE", "name": "backend-ci"}],
            ),
        ],
        "merged_prs": [],
        "batch_order": [],
    }


def delivery_data(now: float) -> monitor.Json:
    merged = {}
    for i in range(24):
        executor = ("claude", "codex")[i % 2]
        end = now - (i % 13) * 86400 - 3600 * (i % 5)
        merged[str(380 + i)] = {
            "number": 380 + i,
            "created": end - 3600 * (1 + i % 7),
            "merged": end,
            "updated": iso(end),
            "executor": executor,
            "rounds": 1 + i % 3,
            "complete": True,
            "checked": now,
        }
    return {
        "v": 1,
        "fetched_at": now,
        "open": [
            {"number": n, "created": now - age, "executor": kind, "draft": False}
            for n, age, kind in (
                (412, 7200, "claude"),
                (411, 3000, "claude"),
                (409, 2400, "codex"),
            )
        ],
        "merged": merged,
    }


def publish_remote(output: Path, now: float, stale: bool) -> None:
    at = now - 900 if stale else now
    state = github_snapshot()
    monitor.atomic_write(output / "github.prom", monitor.github_metrics(state, at))
    view = monitor.epic_view(state)
    monitor.LATEST.prs = view["prs"]
    monitor.LATEST.heads = {f"{REPO_URL}/pull/{p['number']}": HEAD for p in view["prs"]}
    # Both kinds wait; the Codex PR has waited over 30 min.
    waits = [
        delivery.Wait(
            target, head, waiter, waits_for, at - (2400 if waiter == "codex" else 600)
        )
        for target, head, waiter, waits_for in delivery.waiting(view).values()
    ]
    monitor.LATEST.handoff = delivery.Handoff(at, tuple(waits))
    metrics = monitor.Metrics()
    delivery.delivery_metrics(metrics, delivery_data(at), view["prs"], at)
    monitor.atomic_write(output / "delivery.prom", metrics.render())
    health = monitor.Metrics()
    health.add("github_collection_success", int(not stale))
    health.add("github_attempt_timestamp_seconds", now)
    monitor.atomic_write(output / "github-health.prom", health.render())


REAL_RUNTIME = Path(__file__).resolve().parents[2] / "tools/agent-monitoring/runtime"


def prepare_runtime(runtime: Path) -> BinaryIO:
    """Lock and empty a preview runtime dir; return the held lock.

    The collector's runtime dir holds checkpoints and the metrics the real
    stack reads, so only a dir marked `.preview` (or a new one) is used.
    """
    if runtime.resolve() == REAL_RUNTIME.resolve():
        raise SystemExit("refusing the collector's runtime dir; use runtime-preview")
    runtime.mkdir(parents=True, exist_ok=True)
    marker = runtime / ".preview"
    if not marker.exists():
        if any(runtime.iterdir()):
            raise SystemExit(f"{runtime} is not a preview runtime dir; refusing it")
        marker.write_text("made by scripts/epic/fixtures.py\n")
    lock = (runtime / "collector.lock").open("ab")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise SystemExit("a collector owns this runtime dir; stop it first") from None
    # Metrics and checkpoints of an earlier scenario.
    for pattern in (
        "metrics/*.prom",
        "ticks-*.json",
        "events-*.json",
        "permissions-*.json",
    ):
        for old in runtime.glob(pattern):
            old.unlink()
    return lock


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("scenario", choices=SCENARIOS)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("tools/agent-monitoring/runtime-preview/metrics"),
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--history",
        type=Path,
        help="write 24 h of timeline history as OpenMetrics to this file and exit",
    )
    args = parser.parse_args(argv)
    specs, paused = scenario(args.scenario)
    if args.history:
        args.history.write_text(history(specs, time.time()))
        return 0
    runtime = args.output.parent
    # Never next to a real collector: it owns this lock while it runs.
    _lock = prepare_runtime(runtime)  # held while the preview runs
    now = time.time()
    build_state(args.state_dir, specs, now)
    pause = args.state_dir / ".epic-pause"
    if paused:
        pause.write_text("fixture\n")
    args.output.mkdir(parents=True, exist_ok=True)
    ledgers = monitor.Ledgers(runtime)
    local = argparse.Namespace(
        state_dir=args.state_dir, pause_file=pause, output=args.output
    )
    starts = last_starts(specs, now)
    cycle = 0
    while True:
        now = time.time()
        advance(args.state_dir, specs, starts, now)
        publish_remote(args.output, now, args.scenario == "stale")
        monitor.publish_local(local, ledgers)
        if cycle == 1:
            add_denials(args.state_dir, specs, now)
        cycle += 1
        # Stale: the collector stops, so the data ages.
        if args.once or (args.scenario == "stale" and cycle > 2):
            return 0
        time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
