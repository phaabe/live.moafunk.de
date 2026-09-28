"""Registry of epic agents: which runner instances exist and where their files are.

An agent registers by writing `agents/<id>/agent.json` in the state dir. Its
folder then holds the same files as the legacy state dir (`<kind>.lock/`,
`<kind>.log`, `<kind>-gate.json`, ...), so the runner helpers only need
`EPIC_STATE_DIR` pointed at it. Runners without an id keep the legacy files
directly in the state dir; the monitor finds those as legacy agents.

  agents.py register --id claude-2 [--kind claude] [--label text]
                     [--interval 600] [--budget 1930]
  agents.py retire --id claude-2
  agents.py list
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time
from typing import Any

Json = dict[str, Any]
KINDS = ("claude", "codex")
ID = re.compile(r"(claude|codex)(?:-[a-z0-9]{1,16})?")
# Printable text without markup or control characters.
LABEL = re.compile(r"[^\x00-\x1f\x7f-\x9f<>`\\]{0,40}")
MAX_AGENTS = 12
RETIRED_KEEP = 86_400
MAX_FILE = 4096
INTERVAL = (30, 86_400, 600)
BUDGET = (10, 86_400, 3_600)
# Launchd intervals and the runners' default lock budget (120 + 1800 + 10 s).
LEGACY = {"claude": (600, 1_930), "codex": (180, 1_930)}
REASONS = ("invalid", "limit", "conflict")


@dataclass(frozen=True)
class Agent:
    id: str
    kind: str
    label: str
    interval: float
    budget: float
    registered_at: float | None
    retired_at: float | None
    layout: str
    home: Path

    @property
    def lock(self) -> Path:
        return self.home / f"{self.kind}.lock"

    @property
    def log(self) -> Path:
        return self.home / f"{self.kind}.log"

    @property
    def gate(self) -> Path:
        return self.home / f"{self.kind}-gate.json"

    @property
    def checkpoint_name(self) -> str:
        # Legacy names match the tick ledger checkpoints written before agents.
        prefix = "ticks-" if self.layout == "legacy" else "ticks-agents-"
        return f"{prefix}{self.id}.json"


@dataclass
class Registry:
    agents: list[Agent]
    rejected: Counter[str]
    conflicts: list[str]


def kind_of(agent_id: str) -> str:
    return agent_id.split("-", 1)[0]


def number(data: Json, key: str, low: float, high: float, default: float) -> float:
    value = data.get(key, default)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not low <= value <= high
    ):
        raise ValueError(f"invalid {key}")
    return float(value)


def timestamp(data: Json, key: str, now: float) -> float | None:
    value = data.get(key)
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 < value <= now + 5
    ):
        raise ValueError(f"invalid {key}")
    return float(value)


def is_link(path: Path) -> bool:
    try:
        return stat.S_ISLNK(path.lstat().st_mode)
    except FileNotFoundError:
        return False


def load(home: Path, now: float) -> Agent:
    """Read and validate one registered agent; raise ValueError when invalid."""
    if is_link(home) or not home.is_dir():
        raise ValueError("agent folder must be a real directory")
    path = home / "agent.json"
    if is_link(path):
        raise ValueError("agent.json must not be a link")
    with path.open("rb") as stream:
        raw = stream.read(MAX_FILE + 1)
    if len(raw) > MAX_FILE:
        raise ValueError("agent.json is too large")
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict) or data.get("v") != 1:
        raise ValueError("unknown agent.json version")
    agent_id = data.get("id")
    if not isinstance(agent_id, str) or not ID.fullmatch(agent_id):
        raise ValueError("invalid id")
    if agent_id != home.name:
        raise ValueError("id does not match its folder")
    if data.get("kind", kind_of(agent_id)) != kind_of(agent_id):
        raise ValueError("kind does not match the id")
    label = data.get("label", "")
    if not isinstance(label, str) or not LABEL.fullmatch(label):
        raise ValueError("invalid label")
    registered = timestamp(data, "registered_at", now)
    if registered is None:
        raise ValueError("missing registered_at")
    kind = kind_of(agent_id)
    # Runner files are read by the monitor and shipped by Alloy; no links.
    for name in (f"{kind}.lock", f"{kind}.log", f"{kind}-gate.json"):
        if is_link(home / name):
            raise ValueError(f"{name} must not be a link")
    return Agent(
        id=agent_id,
        kind=kind,
        label=label,
        interval=number(data, "interval_seconds", *INTERVAL),
        budget=number(data, "budget_seconds", *BUDGET),
        registered_at=registered,
        retired_at=timestamp(data, "retired_at", now),
        layout="registered",
        home=home,
    )


def discover(state_dir: Path, now: float) -> Registry:
    """All agents to show: registered ones, then legacy ones, capped."""
    rejected: Counter[str] = Counter()
    registered: list[Agent] = []
    folder = state_dir / "agents"
    if not is_link(folder) and folder.is_dir():
        for home in sorted(folder.iterdir()):
            if not (home / "agent.json").exists() and not is_link(home):
                continue  # Being created, or not an agent folder.
            try:
                agent = load(home, now)
            except (OSError, ValueError, UnicodeError):
                rejected["invalid"] += 1
                continue
            if agent.retired_at is None or now - agent.retired_at < RETIRED_KEEP:
                registered.append(agent)
    elif is_link(folder):
        rejected["invalid"] += 1
    ids = {agent.id for agent in registered}
    legacy: list[Agent] = []
    conflicts: list[str] = []
    for kind in KINDS:
        if not (
            (state_dir / f"{kind}.log").exists()
            or (state_dir / f"{kind}.lock").exists()
        ):
            continue
        if kind in ids:
            conflicts.append(kind)
            rejected["conflict"] += 1
            continue
        interval, budget = LEGACY[kind]
        legacy.append(
            Agent(kind, kind, "", interval, budget, None, None, "legacy", state_dir)
        )
    # Active agents first, oldest registration first; legacy agents never drop.
    registered.sort(
        key=lambda a: (a.retired_at is not None, a.registered_at or 0, a.id)
    )
    kept = legacy + registered[: max(0, MAX_AGENTS - len(legacy))]
    rejected["limit"] += len(legacy) + len(registered) - len(kept)
    return Registry(kept, rejected, conflicts)


def write(path: Path, data: Json) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w", dir=path.parent, prefix=".agent-", delete=False
    ) as out:
        temporary = Path(out.name)
        try:
            json.dump(data, out, sort_keys=True)
            out.write("\n")
            out.flush()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def register(
    state_dir: Path,
    agent_id: str,
    now: float,
    *,
    label: str = "",
    interval: float = INTERVAL[2],
    budget: float = BUDGET[2],
) -> Path:
    """Create or refresh an agent; keeps registered_at and clears retired_at."""
    if not ID.fullmatch(agent_id):
        raise ValueError("id must look like claude, claude-2 or codex-review")
    if not LABEL.fullmatch(label):
        raise ValueError("label: at most 40 printable characters")
    settings = {"interval_seconds": interval, "budget_seconds": budget}
    for key, (low, high, _) in (
        ("interval_seconds", INTERVAL),
        ("budget_seconds", BUDGET),
    ):
        number(settings, key, low, high, 0)
    home = state_dir / "agents" / agent_id
    if is_link(state_dir / "agents") or is_link(home):
        raise ValueError("agent folder must be a real directory")
    home.mkdir(parents=True, exist_ok=True)
    registered_at = now
    try:
        registered_at = load(home, now).registered_at or now
    except (OSError, ValueError, UnicodeError):
        pass  # First registration, or replace a broken file.
    data = {
        "v": 1,
        "id": agent_id,
        "kind": kind_of(agent_id),
        "label": label,
        **settings,
        "registered_at": registered_at,
        "retired_at": None,
    }
    write(home / "agent.json", data)
    return home


def retire(state_dir: Path, agent_id: str, now: float) -> None:
    home = state_dir / "agents" / agent_id
    if not ID.fullmatch(agent_id):
        raise ValueError("invalid id")
    agent = load(home, now)
    data = {
        "v": 1,
        "id": agent.id,
        "kind": agent.kind,
        "label": agent.label,
        "interval_seconds": agent.interval,
        "budget_seconds": agent.budget,
        "registered_at": agent.registered_at,
        "retired_at": now,
    }
    write(home / "agent.json", data)


def default_state_dir() -> Path:
    return Path(
        os.environ.get("EPIC_STATE_DIR") or Path.home() / ".local/state/epic-loop"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=default_state_dir())
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("register")
    add.add_argument("--id", required=True)
    add.add_argument("--kind", choices=KINDS)
    add.add_argument("--label", default="")
    add.add_argument("--interval", type=float, default=INTERVAL[2])
    add.add_argument("--budget", type=float, default=BUDGET[2])
    remove = commands.add_parser("retire")
    remove.add_argument("--id", required=True)
    commands.add_parser("list")
    args = parser.parse_args(argv)
    now = time.time()
    try:
        if args.command == "register":
            if args.kind and args.kind != kind_of(args.id):
                raise ValueError(f"id {args.id} is not of kind {args.kind}")
            register(
                args.state_dir,
                args.id,
                now,
                label=args.label,
                interval=args.interval,
                budget=args.budget,
            )
        elif args.command == "retire":
            retire(args.state_dir, args.id, now)
        else:
            registry = discover(args.state_dir, now)
            rows = [
                {**asdict(agent), "home": str(agent.home)} for agent in registry.agents
            ]
            print(
                json.dumps(
                    {
                        "agents": rows,
                        "rejected": dict(registry.rejected),
                        "conflicts": registry.conflicts,
                    },
                    indent=2,
                )
            )
    except (OSError, ValueError, UnicodeError) as error:
        print(f"agents: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
