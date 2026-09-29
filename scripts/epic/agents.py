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
from typing import Any, BinaryIO

Json = dict[str, Any]
KINDS = ("claude", "codex")
ID = re.compile(r"(claude|codex)(?:-[a-z0-9]{1,16})?")
# Printable text without markup or control characters.
LABEL = re.compile(r"[^\x00-\x1f\x7f-\x9f<>`\\]{0,40}")
MAX_AGENTS = 12
MAX_RETIRED = 12
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
    # The trusted state dir and the agent folder below it (empty for legacy).
    root: Path
    rel: tuple[str, ...]

    @property
    def home(self) -> Path:
        return self.root.joinpath(*self.rel)

    @property
    def lock_name(self) -> str:
        return f"{self.kind}.lock"

    @property
    def log_name(self) -> str:
        return f"{self.kind}.log"

    @property
    def gate_name(self) -> str:
        return f"{self.kind}-gate.json"

    @property
    def lock(self) -> Path:
        return self.home / self.lock_name

    @property
    def log(self) -> Path:
        return self.home / self.log_name

    @property
    def gate(self) -> Path:
        return self.home / self.gate_name

    @property
    def checkpoint_name(self) -> str:
        # Legacy names match the tick ledger checkpoints written before agents.
        prefix = "ticks-" if self.layout == "legacy" else "ticks-agents-"
        return f"{prefix}{self.id}.json"

    def open(self, *parts: str) -> BinaryIO | None:
        """Open a regular file in the agent folder; never follows a link."""
        return open_file(self.root, *self.rel, *parts)

    def lstat(self, *parts: str) -> os.stat_result | None:
        return lstat_entry(self.root, *self.rel, *parts)


@dataclass
class Registry:
    agents: list[Agent]
    rejected: Counter[str]
    conflicts: list[str]


def kind_of(agent_id: str) -> str:
    return agent_id.split("-", 1)[0]


def walk(root: Path, parts: tuple[str, ...]) -> int:
    """Directory descriptor for root/parts; no part may be a link."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts:
            if part in ("", ".", "..") or "/" in part:
                raise ValueError("invalid path part")
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def open_file(root: Path, *parts: str) -> BinaryIO | None:
    """Open root/parts read-only without following links; None when missing.

    Model-writable folders are opened relative to the trusted root, one part
    at a time, so a link or a swapped folder cannot redirect the read.
    """
    try:
        folder = walk(root, parts[:-1])
    except FileNotFoundError:
        return None
    try:
        fd = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder
        )
    except FileNotFoundError:
        return None
    finally:
        os.close(folder)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError(f"{parts[-1]} is not a regular file")
    return os.fdopen(fd, "rb")


def lstat_entry(root: Path, *parts: str) -> os.stat_result | None:
    try:
        folder = walk(root, parts[:-1])
    except FileNotFoundError:
        return None
    try:
        return os.stat(parts[-1], dir_fd=folder, follow_symlinks=False)
    except FileNotFoundError:
        return None
    finally:
        os.close(folder)


def number(data: Json, key: str, low: float, high: float, default: float) -> float:
    value = data.get(key, default)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        # A huge JSON integer would overflow float().
        or (isinstance(value, int) and abs(value) > 10**15)
        or not math.isfinite(value)
        or not low <= value <= high
    ):
        raise ValueError(f"invalid {key}")
    return float(value)


def timestamp(data: Json, key: str, now: float) -> float | None:
    if data.get(key) is None:
        return None
    value = number(data, key, 0, now + 5, 0)
    if value <= 0:
        raise ValueError(f"invalid {key}")
    return value


def valid_label(label: object) -> bool:
    if not isinstance(label, str) or not LABEL.fullmatch(label):
        return False
    try:
        label.encode("utf-8")  # rejects lone surrogates such as "\ud800"
    except UnicodeEncodeError:
        return False
    return label.isprintable()


def load(state_dir: Path, name: str, now: float) -> Agent:
    """Read and validate one registered agent; raise ValueError when invalid."""
    stream = open_file(state_dir, "agents", name, "agent.json")
    if stream is None:
        raise FileNotFoundError(name)
    with stream:
        raw = stream.read(MAX_FILE + 1)
    if len(raw) > MAX_FILE:
        raise ValueError("agent.json is too large")
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict) or data.get("v") != 1:
        raise ValueError("unknown agent.json version")
    agent_id = data.get("id")
    if not isinstance(agent_id, str) or not ID.fullmatch(agent_id):
        raise ValueError("invalid id")
    if agent_id != name:
        raise ValueError("id does not match its folder")
    kind = kind_of(agent_id)
    if data.get("kind", kind) != kind:
        raise ValueError("kind does not match the id")
    registered = timestamp(data, "registered_at", now)
    if registered is None:
        raise ValueError("missing registered_at")
    # Runner files are read by the monitor and shipped by Alloy; no links.
    for entry in (f"{kind}.lock", f"{kind}.log", f"{kind}-gate.json"):
        found = lstat_entry(state_dir, "agents", name, entry)
        if found is not None and stat.S_ISLNK(found.st_mode):
            raise ValueError(f"{entry} must not be a link")
    label = data.get("label", "")
    return Agent(
        id=agent_id,
        kind=kind,
        # An invalid label is not fatal: the dashboard shows the id instead.
        label=label if valid_label(label) else "",
        interval=number(data, "interval_seconds", *INTERVAL),
        budget=number(data, "budget_seconds", *BUDGET),
        registered_at=registered,
        retired_at=timestamp(data, "retired_at", now),
        layout="registered",
        root=state_dir,
        rel=("agents", agent_id),
    )


# Anything a malformed file can raise while it is read and validated.
INVALID = (OSError, ValueError, UnicodeError, OverflowError, TypeError, RecursionError)


def discover(state_dir: Path, now: float) -> Registry:
    """All agents to show: legacy ones, active registrations, recently retired."""
    rejected: Counter[str] = Counter()
    active: list[Agent] = []
    retired: list[Agent] = []
    try:
        folder = walk(state_dir, ("agents",))
        try:
            names = sorted(os.listdir(folder))
        finally:
            os.close(folder)
    except FileNotFoundError:
        names = []
    except OSError:
        names = []  # A link or a file where the agents folder belongs.
        rejected["invalid"] += 1
    for name in names:
        # Every probe of an entry sits inside the boundary: a folder without
        # read permission or one swapped during the checks rejects only itself.
        try:
            entry = lstat_entry(state_dir, "agents", name)
            if entry is None or not (
                stat.S_ISDIR(entry.st_mode) or stat.S_ISLNK(entry.st_mode)
            ):
                continue  # Not an agent folder.
            if stat.S_ISDIR(entry.st_mode) and not lstat_entry(
                state_dir, "agents", name, "agent.json"
            ):
                continue  # Being created.
            agent = load(state_dir, name, now)
        except INVALID:
            rejected["invalid"] += 1
            continue
        if agent.retired_at is None:
            active.append(agent)
        elif now - agent.retired_at < RETIRED_KEEP:
            retired.append(agent)
    ids = {agent.id for agent in active + retired}
    legacy: list[Agent] = []
    conflicts: list[str] = []
    for kind in KINDS:
        try:
            found = lstat_entry(state_dir, f"{kind}.log") or lstat_entry(
                state_dir, f"{kind}.lock"
            )
        except OSError:
            found = None
        if found is None:
            continue
        if kind in ids:
            conflicts.append(kind)
            rejected["conflict"] += 1
            continue
        interval, budget = LEGACY[kind]
        legacy.append(
            Agent(kind, kind, "", interval, budget, None, None, "legacy", state_dir, ())
        )
    # Oldest registration first; legacy agents never drop. Retired agents have
    # their own cap, so a full set of active agents never hides them early.
    active.sort(key=lambda a: (a.registered_at or 0, a.id))
    retired.sort(key=lambda a: (-(a.retired_at or 0), a.id))
    kept_active = active[: max(0, MAX_AGENTS - len(legacy))]
    kept_retired = retired[:MAX_RETIRED]
    rejected["limit"] += len(active) - len(kept_active)
    rejected["limit"] += len(retired) - len(kept_retired)
    return Registry(legacy + kept_active + kept_retired, rejected, conflicts)


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
    if not valid_label(label):
        raise ValueError("label: at most 40 printable characters")
    settings = {"interval_seconds": interval, "budget_seconds": budget}
    for key, (low, high, _) in (
        ("interval_seconds", INTERVAL),
        ("budget_seconds", BUDGET),
    ):
        number(settings, key, low, high, 0)
    home = state_dir / "agents" / agent_id
    home.mkdir(parents=True, exist_ok=True)
    try:
        os.close(walk(state_dir, ("agents", agent_id)))
    except OSError as error:
        raise ValueError("agent folder must be a real directory") from error
    registered_at = now
    try:
        registered_at = load(state_dir, agent_id, now).registered_at or now
    except INVALID:
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
    agent = load(state_dir, agent_id, now)
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
                {**asdict(agent), "root": str(agent.root), "home": str(agent.home)}
                for agent in registry.agents
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
