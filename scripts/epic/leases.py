"""Shared work leases: which runner instance owns an issue, PR or review head.

Contract: "Agreed lease contract" in
https://github.com/phaabe/live.moafunk.de/issues/456. Not wired into the
runners yet; nothing calls this module on its own.

One store for all Claude and Codex runners on this machine, next to the target
locks: `<parent of EPIC_LOCK_DIR>/leases/v1/` (default
~/.local/state/epic-loop/leases/v1/). It does not follow EPIC_STATE_DIR.
`leases.json` is guarded by `leases.lock` (fcntl.flock). Every change: lock,
read, check, write a temp file, os.replace, unlock. This module makes no
network call; callers read GitHub before they take the lock.

A missing or corrupt store blocks (exit 5). It never turns into an empty store:
only `init` creates one. `init` refuses while the root pointer
(~/.local/state/epic-loop/leases-root) names another store, and needs
`--recreate` when the pointer shows a store here was lost.
Any record that acquire could not have written (for example an implementation
lease without files, or two leases on one PR) also blocks. Moving the root
needs `handoff --from <old root>`, which copies every record and generation;
after a failure, running it again finishes it.

Keys: `impl:<issue>` (or `impl:pr:<n>` for a PR without an `Issue:` line) for
claim, continue, fix, fix-checks, resolve-conflict, escalate, merge and adopt;
`review:<pr>:<sha>` for reviews. A lease is `active`, `released`, `superseded`
(another review head) or `needs-takeover`. Active and needs-takeover leases
keep their slot and files. Acquiring a review head, or the implementer
renewing with a new head, supersedes the PR's other review leases; a
superseded head can be acquired again with a higher generation. Callers pass
the head they read fresh. One PR belongs to at most one implementation lease.

`acquire` takes the key, the pending PR slot and the file reservation in one
locked step. Slots per agent kind: open PRs plus pending claims, at most
MAX_OPEN_PRS. PR numbers from the caller only add to the count; they never free
a slot a lease still holds, and a PR of a pending claim counts once.
Implementation leases never expire.

Generations: each store has a random incarnation in the high 32 bits; a key's
generation goes up by one per re-acquire or takeover. The root pointer lists
every incarnation it issued, so a recreated store never repeats an old token,
whatever the clock says. Takeover records stay on the key, across generations,
until `clear-recovery` drops them.

`takeover` is a manual command. It refuses unless a live process-evidence
provider proves the old session stopped (see EvidenceProvider), no publication
is uncertain, the target locks are free and the old worktree is pinned by a
recovery ref. Set EPIC_PROCESS_PROVIDER to a Python file with `provider()`.

Usage:
  leases.py init [--recreate]
  leases.py handoff --from OLD_ROOT
  leases.py acquire --action A --owner ID [--issue N] [--pr N] [--head SHA]
                    [--files GLOBS] [--branch B] [--worktree PATH]
                    [--open-pr N[:ISSUE] ...] [--evidence-file F] [--expect-version V]
  leases.py renew --owner ID [--key K --generation G] [--pr N] [--head SHA]
                  [--branch B] [--worktree PATH] [--work] [--evidence-file F]
                  [--uncertain TEXT] [--reconciled TEXT]
  leases.py check --key K --owner ID --generation G [--repo R] [--issue N]
                  [--pr N] [--branch B] [--head SHA]
  leases.py release --key K --owner ID --generation G
                    --outcome done|abandoned|stale
  leases.py extend --key K --owner ID --generation G --files GLOBS
  leases.py flag --key K --reason TEXT
  leases.py list [--owner ID]
  leases.py takeover --key K --to ID
  leases.py clear-recovery --key K --index N

Exit codes: 0 ok, 2 bad input, 3 refused (held, slots full, files overlap,
takeover not proved), 5 store blocked (lock timeout, missing, corrupt or moved
store), 7 lease lost (wrong owner, generation, state or target).
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
import copy
from dataclasses import dataclass, field
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
from typing import Any, Callable, Generator, Mapping, Protocol

from agents import ID
from target_lock import lock_dir

Json = dict[str, Any]
OpenPrs = Mapping[int, "int | None"]

OK, INVALID, REFUSED, BLOCKED, LOST = 0, 2, 3, 5, 7
SCHEMA = 1
# Generation = incarnation << 32 | counter: every store has its own range.
GENERATION_BITS = 32
LOCK_SECONDS = 10.0
MAX_OPEN_PRS = 2
REPO = "phaabe/live.moafunk.de"
IMPL_ACTIONS = frozenset(
    {
        "claim",
        "continue",
        "fix",
        "fix-checks",
        "resolve-conflict",
        "escalate",
        "merge",
        "adopt",
    }
)
STATES = ("active", "released", "superseded", "needs-takeover")
HELD = ("active", "needs-takeover")
OUTCOMES = ("done", "abandoned", "stale")
ALL_FILES = "**"
SHA = re.compile(r"[0-9a-f]{40}")
SEGMENT = re.compile(r"[A-Za-z0-9._@+*?-]+")
FILES_LINE = re.compile(r"^Files:[ \t]*(.*)$", re.MULTILINE)
PROVIDER_ENV = "EPIC_PROCESS_PROVIDER"
RECORD_KEYS = frozenset(
    {
        "key",
        "kind",
        "action",
        "owner",
        "agent",
        "generation",
        "repo",
        "issue",
        "pr",
        "head",
        "files",
        "worktree",
        "branch",
        "evidence",
        "acquired_at",
        "renewed_at",
        "state",
        "slot",
        "tentative",
        "uncertain",
        "outcome",
        "released_at",
        "reason",
        "handoffs",
    }
)


class Blocked(Exception):
    """Store unusable: lock timeout, missing, corrupt or moved. Exit 5."""


class Refused(Exception):
    """Ordinary contention or an unproved takeover. Exit 3."""


class Lost(Exception):
    """The caller does not own the lease for this target. Exit 7."""


# --- Files ----------------------------------------------------------------


def normalize_glob(raw: str) -> str:
    """A repository-relative glob. Supported: literal segments, `*`, `?` and
    a whole `**` segment. Absolute paths, `~`, `..` and backslashes are
    rejected."""
    text = raw.strip().strip("`").strip()
    if not text or text.startswith(("/", "~")) or "\\" in text:
        raise ValueError(f"not a repository-relative glob: {raw!r}")
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if not parts:
        raise ValueError(f"empty glob: {raw!r}")
    for part in parts:
        if part == ".." or not SEGMENT.fullmatch(part):
            raise ValueError(f"unsupported glob segment {part!r} in {raw!r}")
        if "**" in part and part != "**":
            raise ValueError(f"`**` must be a whole segment in {raw!r}")
    return "/".join(parts)


def parse_files(text: str) -> list[str]:
    """Comma-separated globs, normalized, without duplicates."""
    globs = {normalize_glob(item) for item in text.split(",") if item.strip()}
    if not globs:
        raise ValueError("no file globs given")
    return sorted(globs)


def files_line(body: str) -> list[str] | None:
    """Globs from the ticket body's first `Files:` line; None without one."""
    match = FILES_LINE.search(body)
    return parse_files(match.group(1)) if match else None


def literal_prefix(glob: str) -> str:
    cut = min((i for i, c in enumerate(glob) if c in "*?"), default=len(glob))
    return glob[:cut]


def file_clash(mine: list[str], theirs: list[str]) -> bool:
    return any(overlaps(a, b) for a in mine for b in theirs)


def overlaps(a: str, b: str) -> bool:
    """Conservative: the literal prefix of one starts with that of the other."""
    pa, pb = literal_prefix(a), literal_prefix(b)
    return pa.startswith(pb) or pb.startswith(pa)


# --- Process evidence -------------------------------------------------------

GONE = "gone"  # the provider saw ESRCH; nothing else counts as gone
PROBES = (GONE, "alive", "eperm", "reused", "error")


@dataclass(frozen=True)
class ProcessIdentity:
    """A PID or process group ID with the start identity the provider read
    before any signal (for example the kernel start time)."""

    pid: int
    start: str

    @classmethod
    def from_json(cls, data: Any) -> ProcessIdentity:
        if not isinstance(data, dict) or set(data) != {"pid", "start"}:
            raise ValueError("process identity needs exactly pid and start")
        pid, start = data["pid"], data["start"]
        if type(pid) is not int or pid <= 1:
            raise ValueError(f"bad pid {pid!r}")
        if not isinstance(start, str) or not start.strip():
            raise ValueError(f"pid {pid} has no start identity")
        return cls(pid, start)

    def to_json(self) -> Json:
        return {"pid": self.pid, "start": self.start}


@dataclass(frozen=True)
class ProcessEvidence:
    """What a runner records for its session so a takeover can prove it
    stopped: the tick wrapper, every process group it started (registered
    before the group runs anything), and descendants that left those groups."""

    owner: str
    provider: str
    wrapper: ProcessIdentity
    groups: tuple[ProcessIdentity, ...]
    descendants: tuple[ProcessIdentity, ...]
    recorded_at: float

    @classmethod
    def from_json(cls, data: Any) -> ProcessEvidence:
        keys = {"owner", "provider", "wrapper", "groups", "descendants", "recorded_at"}
        if not isinstance(data, dict) or set(data) != keys:
            raise ValueError(f"process evidence needs exactly {sorted(keys)}")
        owner, provider = data["owner"], data["provider"]
        if not isinstance(owner, str) or not ID.fullmatch(owner):
            raise ValueError(f"bad evidence owner {owner!r}")
        if not isinstance(provider, str) or not provider:
            raise ValueError("process evidence names no provider")
        if not isinstance(data["groups"], list) or not isinstance(
            data["descendants"], list
        ):
            raise ValueError("groups and descendants must be lists")
        recorded = data["recorded_at"]
        if not isinstance(recorded, (int, float)) or isinstance(recorded, bool):
            raise ValueError("bad recorded_at")
        return cls(
            owner,
            provider,
            ProcessIdentity.from_json(data["wrapper"]),
            tuple(ProcessIdentity.from_json(g) for g in data["groups"]),
            tuple(ProcessIdentity.from_json(d) for d in data["descendants"]),
            float(recorded),
        )

    def to_json(self) -> Json:
        return {
            "owner": self.owner,
            "provider": self.provider,
            "wrapper": self.wrapper.to_json(),
            "groups": [g.to_json() for g in self.groups],
            "descendants": [d.to_json() for d in self.descendants],
            "recorded_at": self.recorded_at,
        }


@dataclass(frozen=True)
class StopReport:
    """The provider's result after stopping a session. Each probe is one of
    PROBES, keyed by the recorded PID or group ID. `escaped` lists processes of
    the session found outside every recorded group and not registered."""

    wrapper: str
    groups: dict[int, str]
    descendants: dict[int, str]
    escaped: tuple[ProcessIdentity, ...] = ()


class EvidenceProvider(Protocol):
    """Implemented by the process helper
    (https://github.com/phaabe/live.moafunk.de/issues/503).

    `live` is True only for a provider that reads real processes; fixtures and
    fakes set it False and never prove a takeover. `admission_blocked` says
    whether an admission block for the owner is written under the admission
    lock. `stop` checks each start identity before any signal, never signals a
    reused ID, stops the wrapper and the recorded groups and descendants, and
    reports `gone` only for ESRCH."""

    name: str
    live: bool

    def admission_blocked(self, owner: str) -> bool: ...

    def stop(self, evidence: ProcessEvidence) -> StopReport: ...


def stop_blockers(evidence: ProcessEvidence, report: StopReport) -> list[str]:
    """Why the report does not prove the session stopped; empty when it does."""
    problems = []
    if report.wrapper != GONE:
        problems.append(f"wrapper {evidence.wrapper.pid}: {report.wrapper}")
    for kind, recorded, seen in (
        ("group", evidence.groups, report.groups),
        ("descendant", evidence.descendants, report.descendants),
    ):
        for ident in recorded:
            probe = seen.get(ident.pid, "not checked")
            if probe != GONE:
                problems.append(f"{kind} {ident.pid}: {probe}")
    problems.extend(f"escaped process {p.pid}" for p in report.escaped)
    return problems


def load_provider(env: dict[str, str] | None = None) -> EvidenceProvider | None:
    path = (os.environ if env is None else env).get(PROVIDER_ENV)
    if not path:
        return None
    try:
        spec = importlib.util.spec_from_file_location("epic_process_provider", path)
        if spec is None or spec.loader is None:
            raise ImportError(path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        provider: EvidenceProvider = module.provider()
    except Exception as err:  # any failure: no provider, so no takeover
        raise Refused(f"cannot load {PROVIDER_ENV}={path}: {err}") from None
    return provider


# --- Store ------------------------------------------------------------------


def default_root() -> Path:
    return lock_dir().parent / "leases" / "v1"


def default_pointer() -> Path:
    return Path.home() / ".local" / "state" / "epic-loop" / "leases-root"


@dataclass
class Store:
    root: Path = field(default_factory=default_root)
    pointer: Path = field(default_factory=default_pointer)
    lock_seconds: float = LOCK_SECONDS
    clock: Callable[[], float] = time.time

    @property
    def file(self) -> Path:
        return self.root / "leases.json"

    @property
    def lock_file(self) -> Path:
        return self.root / "leases.lock"

    @contextmanager
    def locked(self, shared: bool = False) -> Generator[None]:
        """flock on leases.lock. The OS frees it when the holder dies; a
        timeout is Blocked, never a forced take."""
        mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        deadline = time.monotonic() + self.lock_seconds
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            handle = self.lock_file.open("a")
        except OSError as err:
            raise Blocked(f"cannot open {self.lock_file}: {err}") from None
        with handle:
            while True:
                try:
                    fcntl.flock(handle.fileno(), mode | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise Blocked(
                            f"lease lock busy for {self.lock_seconds:g}s"
                        ) from None
                    time.sleep(0.05)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def pointer_data(self) -> Json | None:
        """{"root": path, "store": floor, "floors": [...]}: the current root,
        the floor of the one store allowed there, and every floor issued."""
        try:
            data = json.loads(self.pointer.read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as err:
            raise Blocked(f"cannot read {self.pointer}: {err}") from None
        floors = data.get("floors") if isinstance(data, dict) else None
        if (
            not isinstance(data.get("root"), str)
            or not isinstance(floors, list)
            or not all(type(f) is int for f in floors)
            or data.get("store") not in floors
        ):
            raise Blocked(f"corrupt root pointer {self.pointer}")
        return data

    def pointed_root(self) -> Path | None:
        data = self.pointer_data()
        return Path(data["root"]) if data else None

    def write_pointer(self, floor: int) -> None:
        old = self.pointer_data()
        floors = sorted(set(old["floors"] if old else []) | {floor})
        try:
            self.pointer.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            tmp = self.pointer.with_name(f".{self.pointer.name}.{os.getpid()}")
            tmp.write_text(
                json.dumps({"root": str(self.root), "store": floor, "floors": floors})
            )
            os.replace(tmp, self.pointer)
        except OSError as err:
            raise Blocked(f"cannot write {self.pointer}: {err}") from None

    def check_root(self) -> None:
        pointed = self.pointed_root()
        if pointed is not None and pointed != self.root:
            raise Blocked(
                f"lease root moved from {pointed} to {self.root}; "
                f"run leases.py handoff --from {pointed}"
            )

    def load(self, retired_ok: bool = False) -> Json:
        """The store; the caller holds the lock. Never an empty default."""
        try:
            raw = self.file.read_text()
        except FileNotFoundError:
            raise Blocked(
                f"no lease store at {self.file}; run leases.py init only if no "
                "work is owned anywhere"
            ) from None
        except OSError as err:
            raise Blocked(f"cannot read {self.file}: {err}") from None
        try:
            data = json.loads(raw)
            validate_store(data)
        except (ValueError, TypeError, KeyError) as err:
            raise Blocked(f"corrupt lease store {self.file}: {err}") from None
        if data.get("handed_off_to") and not retired_ok:
            raise Blocked(f"lease store handed off to {data['handed_off_to']}")
        return data

    def save(self, data: Json) -> None:
        """Temp file, fsync, os.replace: a crash leaves the old or the new
        store, never a partial one."""
        tmp = self.root / f".leases.json.{os.getpid()}.tmp"
        try:
            with tmp.open("w") as handle:
                json.dump(data, handle, indent=1, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.file)
        except OSError as err:
            raise Blocked(f"cannot write {self.file}: {err}") from None

    @contextmanager
    def transaction(self) -> Generator[Json]:
        """Lock, read, yield for changes. The result is validated like a load
        before it is written: a bad change is exit 2 and writes nothing."""
        with self.locked():
            data = self.registered()
            before = copy.deepcopy(data)
            yield data
            if data != before:
                data["version"] += 1
                try:
                    validate_store(data)
                except (ValueError, TypeError, KeyError) as err:
                    raise ValueError(f"refusing an invalid change: {err}") from None
                self.save(data)

    def read(self) -> Json:
        with self.locked(shared=True):
            return self.registered()

    def registered(self) -> Json:
        """The store, only if the pointer names this root and this store's
        floor. The caller holds the lock."""
        self.check_root()
        data = self.load()
        pointer = self.pointer_data()
        if pointer is None or pointer["store"] != data["floor"]:
            raise Blocked(
                f"lease store {self.file} is not registered in {self.pointer}"
            )
        return data


def validate_store(data: Any) -> None:
    """Every field that ownership, slots or reservations rely on, and the
    links between records. Anything acquire could not have written is
    corrupt."""
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise ValueError(f"not a schema {SCHEMA} lease store")
    for name in ("version", "floor"):
        if type(data.get(name)) is not int or data[name] < 0:
            raise ValueError(f"bad {name}")
    floor = data["floor"]
    if floor % (1 << GENERATION_BITS) or floor == 0:
        raise ValueError("bad floor")
    gens, leases = data.get("generations"), data.get("leases")
    if not isinstance(gens, dict) or not isinstance(leases, dict):
        raise ValueError("generations and leases must be objects")
    for key, gen in gens.items():
        if type(gen) is not int or not floor < gen < floor + (1 << GENERATION_BITS):
            raise ValueError(f"{key}: generation {gen!r} outside this store")
    for key, rec in leases.items():
        validate_record(key, rec, gens)
    live = [r for r in leases.values() if r["state"] in HELD]
    for n, rec in enumerate(live):
        for other in live[n + 1 :]:
            if file_clash(rec["files"], other["files"]):
                raise ValueError(f"{rec['key']} and {other['key']} share files")
            if rec["kind"] == other["kind"] == "impl" and rec["pr"] is not None:
                if rec["pr"] == other["pr"]:
                    raise ValueError(f"{rec['key']} and {other['key']} share a PR")


def optional(value: Any, kind: type) -> bool:
    return value is None or (type(value) is kind and (kind is not int or value > 0))


def validate_record(key: str, rec: Any, gens: Json) -> None:
    if not isinstance(rec, dict) or set(rec) != RECORD_KEYS or rec["key"] != key:
        raise ValueError(f"{key}: bad record fields")
    kind, state, owner = rec["kind"], rec["state"], rec["owner"]
    problems = []
    if state not in STATES:
        problems.append("state")
    if not isinstance(owner, str) or not ID.fullmatch(owner):
        problems.append("owner")
    elif rec["agent"] != agent_of(owner):
        problems.append("agent")
    if rec["generation"] != gens.get(key) or type(rec["generation"]) is not int:
        problems.append("generation")
    if not isinstance(rec["repo"], str) or not rec["repo"]:
        problems.append("repo")
    if not (optional(rec["issue"], int) and optional(rec["pr"], int)):
        problems.append("issue/pr")
    if rec["head"] is not None and not (
        isinstance(rec["head"], str) and SHA.fullmatch(rec["head"])
    ):
        problems.append("head")
    files = rec["files"]
    if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
        problems.append("files")
    elif files != sorted(set(files)) or any(safe_glob(f) != f for f in files):
        problems.append("files")
    for name in ("worktree", "branch", "reason"):
        if not optional(rec[name], str):
            problems.append(name)
    if rec["outcome"] not in (None, *OUTCOMES):
        problems.append("outcome")
    if type(rec["tentative"]) is not bool:
        problems.append("tentative")
    if not isinstance(rec["uncertain"], list) or not all(
        isinstance(u, str) for u in rec["uncertain"]
    ):
        problems.append("uncertain")
    if not isinstance(rec["handoffs"], list) or not all(
        isinstance(h, dict) for h in rec["handoffs"]
    ):
        problems.append("handoffs")
    for name in ("acquired_at", "renewed_at"):
        if type(rec[name]) not in (int, float):
            problems.append(name)
    if rec["evidence"] is not None:
        try:
            if ProcessEvidence.from_json(rec["evidence"]).owner != owner:
                problems.append("evidence owner")
        except ValueError:
            problems.append("evidence")
    if kind == "review":
        if (
            rec["action"] != "review"
            or key != f"review:{rec['pr']}:{rec['head']}"
            or rec["pr"] is None
            or rec["head"] is None
            or files != []
            or rec["slot"] is not None
        ):
            problems.append("review key, files or slot")
    elif kind == "impl":
        want = f"impl:{rec['issue']}" if rec["issue"] is not None else None
        if want is None and rec["pr"] is not None:
            want = f"impl:pr:{rec['pr']}"
        slot = None
        if state in HELD:
            slot = "pending" if rec["pr"] is None else "pr"
        if (
            rec["action"] not in IMPL_ACTIONS
            or key != want
            or not files
            or rec["slot"] != slot
            or state == "superseded"
        ):
            problems.append("impl key, files or slot")
    else:
        problems.append("kind")
    if problems:
        raise ValueError(f"{key}: bad {', '.join(problems)}")


def safe_glob(glob: str) -> str | None:
    try:
        return normalize_glob(glob)
    except ValueError:
        return None


def new_floor(used: list[int]) -> int:
    """A random incarnation this pointer never issued: generations of a
    recreated store cannot repeat a lost store's tokens, even if the clock
    went back."""
    while True:
        floor = (secrets.randbits(30) + 1) << GENERATION_BITS
        if floor not in used:
            return floor


def new_store(root: Path, floor: int) -> Json:
    return {
        "schema": SCHEMA,
        "version": 0,
        "floor": floor,
        "root": str(root),
        "generations": {},
        "leases": {},
    }


def init(store: Store, recreate: bool = False) -> Json:
    """Create an empty store. When the pointer shows a store was here before,
    only with `recreate`: a lost store is replaced on purpose, never by a
    runner."""
    with store.locked():
        pointer = store.pointer_data()
        pointed = Path(pointer["root"]) if pointer else None
        if pointed is not None and pointed != store.root:
            raise Refused(
                f"root pointer names {pointed}; use leases.py handoff --from {pointed}"
            )
        if store.file.exists():
            raise Refused(f"lease store already exists at {store.file}")
        if pointed == store.root and not recreate:
            raise Refused(
                f"the store at {store.file} was lost; check for owned work, "
                "then run leases.py init --recreate"
            )
        data = new_store(store.root, new_floor(pointer["floors"] if pointer else []))
        # Register first: a store the pointer does not name is never used.
        # If the save then fails, `init --recreate` retries with a new floor.
        store.write_pointer(data["floor"])
        store.save(data)
        return data


def handoff(store: Store, old_root: Path) -> Json:
    """Move every record and generation from old_root, then retire old_root.

    Three writes: the copy, the retired old store, the pointer. Running it
    again after a failure finishes the job. Until the old store is retired it
    stays the source, so a copy made before a failure is made again."""
    old = Store(old_root, store.pointer, store.lock_seconds, store.clock)
    if old.root == store.root:
        raise ValueError("old and new root are the same")
    with old.locked(), store.locked():
        pointed = store.pointed_root()
        if pointed not in (None, old.root, store.root):
            raise Refused(f"root pointer names {pointed}, not {old.root}")
        source = old.load(retired_ok=True)
        retired = source.get("handed_off_to")
        if retired not in (None, str(store.root)):
            raise Refused(f"{old.root} was handed off to {retired}")
        if store.file.exists():
            current = store.load()
            if current.get("handoff_from") != str(old.root):
                raise Refused(f"lease store already exists at {store.file}")
        if retired:
            data = store.load()
        else:
            data = copy.deepcopy(source)
            data["root"] = str(store.root)
            data["version"] += 1
            data["handoff_from"] = str(old.root)
            store.save(data)
            source["handed_off_to"] = str(store.root)
            source["version"] += 1
            old.save(source)
        store.write_pointer(data["floor"])
        return data


# --- Operations -------------------------------------------------------------


def agent_of(owner: str) -> str:
    if not ID.fullmatch(owner):
        raise ValueError(f"bad instance id {owner!r}")
    return owner.split("-", 1)[0]


def lease_key(action: str, issue: int | None, pr: int | None, head: str | None) -> str:
    for name, value in (("issue", issue), ("pr", pr)):
        if value is not None and (type(value) is not int or value <= 0):
            raise ValueError(f"bad {name} {value!r}")
    if head is not None and not SHA.fullmatch(head):
        raise ValueError(f"bad head {head!r}: need a 40-char SHA")
    if action == "review":
        if pr is None or head is None or not SHA.fullmatch(head):
            raise ValueError("review needs --pr and a 40-char --head")
        return f"review:{pr}:{head}"
    if action not in IMPL_ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    if issue is not None:
        return f"impl:{issue}"
    if pr is not None:
        return f"impl:pr:{pr}"
    raise ValueError(f"{action} needs --issue or --pr")


def held(data: Json) -> list[Json]:
    return [r for r in data["leases"].values() if r["state"] in HELD]


def used_slots(data: Json, agent: str, open_prs: OpenPrs) -> int:
    """Open PRs plus pending claims of one agent kind. `open_prs` maps each
    open PR to its `Issue:` number (or None). A lease's PR counts even when the
    caller misses it, so the caller never frees a slot. A PR already published
    for a pending claim counts once, through the claim."""
    impl = [r for r in held(data) if r["kind"] == "impl" and r["agent"] == agent]
    pending = {r["issue"] for r in impl if r["slot"] == "pending"}
    prs = {n for n, issue in open_prs.items() if issue is None or issue not in pending}
    prs |= {r["pr"] for r in impl if r["pr"] is not None}
    return len(prs) + len(pending)


def pr_owner(data: Json, pr: int, key: str) -> str | None:
    """Another held implementation lease already linked to this PR."""
    for rec in held(data):
        if rec["kind"] == "impl" and rec["pr"] == pr and rec["key"] != key:
            return str(rec["key"])
    return None


def file_conflicts(data: Json, key: str, files: list[str]) -> list[str]:
    found = []
    for rec in held(data):
        if rec["key"] == key:
            continue
        for mine in files:
            for theirs in rec["files"]:
                if overlaps(mine, theirs):
                    found.append(f"{mine} overlaps {theirs} of {rec['key']}")
    return found


def next_generation(data: Json, key: str) -> int:
    gen = data["generations"].get(key, data["floor"]) + 1
    data["generations"][key] = gen
    return gen


def as_json(evidence: ProcessEvidence | None) -> Json | None:
    return evidence.to_json() if evidence else None


def link_pr(data: Json, rec: Json, pr: int) -> None:
    """Turn the pending slot into the PR: one step, counted once."""
    if rec["kind"] != "impl" or rec["pr"] not in (None, pr):
        raise Refused(f"{rec['key']} is linked to PR {rec['pr']}, not {pr}")
    other = pr_owner(data, pr, rec["key"])
    if other:
        raise Refused(f"PR {pr} belongs to {other}")
    rec["pr"], rec["slot"] = pr, "pr"


def supersede_reviews(data: Json, pr: int, head: str, now: float) -> None:
    for rec in held(data):
        if rec["kind"] == "review" and rec["pr"] == pr and rec["head"] != head:
            rec["state"] = "superseded"
            rec["reason"] = f"new head {head}"
            rec["released_at"] = now


def acquire(
    store: Store,
    *,
    action: str,
    owner: str,
    issue: int | None = None,
    pr: int | None = None,
    head: str | None = None,
    files: list[str] | None = None,
    branch: str | None = None,
    worktree: str | None = None,
    open_prs: OpenPrs | None = None,
    evidence: ProcessEvidence | None = None,
    repo: str = REPO,
    expect_version: int | None = None,
) -> Json:
    """Take the key, the slot and the files in one step, or refuse."""
    agent = agent_of(owner)
    key = lease_key(action, issue, pr, head)
    kind = "review" if action == "review" else "impl"
    if kind == "review":
        wanted: list[str] = []
    else:
        wanted = sorted({normalize_glob(f) for f in files}) if files else [ALL_FILES]
    if evidence is not None and evidence.owner != owner:
        raise ValueError(f"evidence belongs to {evidence.owner}, not {owner}")
    with store.transaction() as data:
        if expect_version is not None and data["version"] != expect_version:
            raise Refused(
                f"store changed: version {data['version']}, expected {expect_version}"
            )
        now = store.clock()
        rec = data["leases"].get(key)
        if rec is not None and rec["state"] in HELD:
            if rec["owner"] != owner:
                raise Refused(f"{key} is held by {rec['owner']}")
            if kind == "impl" and pr is not None and rec["pr"] is None:
                link_pr(data, rec, pr)
            # Owned since an earlier tick: never a new tentative reservation.
            # Evidence describes only the session that touched it last.
            rec.update(renewed_at=now, tentative=False, evidence=as_json(evidence))
            return copy.deepcopy(rec)
        # A superseded review head comes back with a higher generation: the
        # caller read it fresh, so it is the current head again.
        if kind == "impl":
            if pr is not None and pr_owner(data, pr, key):
                raise Refused(f"PR {pr} belongs to {pr_owner(data, pr, key)}")
            used = used_slots(data, agent, open_prs or {})
            if pr is None and used >= MAX_OPEN_PRS:
                raise Refused(f"{agent} has no free PR slot (max {MAX_OPEN_PRS})")
            clash = file_conflicts(data, key, wanted)
            if clash:
                raise Refused("; ".join(clash))
        else:
            assert pr is not None and head is not None
            supersede_reviews(data, pr, head, now)
        rec = {
            "key": key,
            "kind": kind,
            "action": action,
            "owner": owner,
            "agent": agent,
            "generation": next_generation(data, key),
            "repo": repo,
            "issue": issue,
            "pr": pr,
            "head": head,
            "files": wanted,
            "worktree": worktree,
            "branch": branch,
            "evidence": as_json(evidence),
            "acquired_at": now,
            "renewed_at": now,
            "state": "active",
            "slot": None
            if kind == "review"
            else ("pr" if pr is not None else "pending"),
            "tentative": True,
            "uncertain": [],
            "outcome": None,
            "released_at": None,
            "reason": None,
            # Recovery records outlive the lease until clear-recovery.
            "handoffs": list(rec["handoffs"]) if rec else [],
        }
        data["leases"][key] = rec
        return copy.deepcopy(rec)


def owned(data: Json, key: str, owner: str, generation: int) -> Json:
    rec = data["leases"].get(key)
    if rec is None:
        raise Lost(f"no lease {key}")
    if rec["owner"] != owner or rec["generation"] != generation:
        raise Lost(
            f"{key} is generation {rec['generation']} of {rec['owner']}, "
            f"not {generation} of {owner}"
        )
    if rec["state"] not in HELD:
        raise Lost(f"{key} is {rec['state']}")
    return rec


def renew(
    store: Store,
    *,
    owner: str,
    key: str | None = None,
    generation: int | None = None,
    pr: int | None = None,
    head: str | None = None,
    branch: str | None = None,
    worktree: str | None = None,
    work: bool = False,
    evidence: ProcessEvidence | None = None,
    uncertain: str | None = None,
    reconciled: str | None = None,
) -> list[Json]:
    """Refresh renewed_at and the process evidence of the running session.
    A renew without evidence drops the old evidence: evidence must describe
    the session that touched the lease last, or takeover refuses. With a key,
    also record its PR (turns the pending slot into that PR), head, branch,
    worktree, uncertain publications, and that work started."""
    agent_of(owner)
    if evidence is not None and evidence.owner != owner:
        raise ValueError(f"evidence belongs to {evidence.owner}, not {owner}")
    with store.transaction() as data:
        now = store.clock()
        if key is None:
            mine = [r for r in held(data) if r["owner"] == owner]
            for rec in mine:
                rec.update(renewed_at=now, evidence=as_json(evidence))
            return copy.deepcopy(mine)
        if generation is None:
            raise ValueError("--key needs --generation")
        rec = owned(data, key, owner, generation)
        if pr is not None:
            link_pr(data, rec, pr)
        if head is not None:
            if not SHA.fullmatch(head):
                raise ValueError(f"bad head {head!r}")
            if rec["kind"] == "review" and head != rec["head"]:
                raise Lost(f"{key} is for head {rec['head']}")
            rec["head"] = head
            if rec["pr"] is not None:
                supersede_reviews(data, rec["pr"], head, now)
        for name, value in (("branch", branch), ("worktree", worktree)):
            if value is not None:
                rec[name] = value
        rec["evidence"] = as_json(evidence)
        if uncertain and uncertain not in rec["uncertain"]:
            rec["uncertain"].append(uncertain)
        if reconciled:
            rec["uncertain"] = [u for u in rec["uncertain"] if u != reconciled]
        if work:
            rec["tentative"] = False
        rec["state"], rec["reason"] = "active", None
        rec["renewed_at"] = now
        return [copy.deepcopy(rec)]


def check(
    store: Store,
    *,
    key: str,
    owner: str,
    generation: int,
    repo: str = REPO,
    issue: int | None = None,
    pr: int | None = None,
    branch: str | None = None,
    head: str | None = None,
) -> Json:
    """The caller may write to this target: same owner and generation, a held
    lease, and the command's real repo, issue, PR, branch and review head."""
    rec = owned(store.read(), key, owner, generation)
    problems = []
    if rec["repo"] != repo:
        problems.append(f"repo {repo}, lease {rec['repo']}")
    for name, value in (("issue", issue), ("pr", pr), ("branch", branch)):
        if value is not None and rec[name] != value:
            problems.append(f"{name} {value}, lease {rec[name]}")
    if rec["kind"] == "review" and head != rec["head"]:
        problems.append(f"head {head}, lease {rec['head']}")
    if problems:
        raise Lost(f"{key}: " + "; ".join(problems))
    return rec


def release(
    store: Store, *, key: str, owner: str, generation: int, outcome: str
) -> Json:
    """Free the key, slot and files. `stale` frees only a tentative lease:
    one acquired this tick with no work attached."""
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}")
    with store.transaction() as data:
        rec = owned(data, key, owner, generation)
        if outcome == "stale" and not rec["tentative"]:
            raise Refused(f"{key} has work attached; a stale recheck keeps it")
        rec.update(
            state="released", outcome=outcome, released_at=store.clock(), slot=None
        )
        return copy.deepcopy(rec)


def extend(
    store: Store, *, key: str, owner: str, generation: int, files: list[str]
) -> Json:
    """Add globs to an implementation lease before editing them. On overlap
    nothing changes and the lease keeps what it had."""
    wanted = sorted({normalize_glob(f) for f in files})
    if not wanted:
        raise ValueError("no files to add")
    with store.transaction() as data:
        rec = owned(data, key, owner, generation)
        if rec["kind"] != "impl":
            raise ValueError("review leases take no file reservations")
        new = [f for f in wanted if f not in rec["files"]]
        clash = file_conflicts(data, key, new)
        if clash:
            raise Refused("; ".join(clash))
        rec["files"] = sorted(set(rec["files"]) | set(new))
        rec["renewed_at"] = store.clock()
        return copy.deepcopy(rec)


def flag(store: Store, *, key: str, reason: str) -> Json:
    """Mark a held lease as waiting for a manual takeover. Ownership, slot and
    files stay; the owner's next renew clears the mark."""
    with store.transaction() as data:
        rec = data["leases"].get(key)
        if rec is None or rec["state"] not in HELD:
            raise Refused(f"no held lease {key}")
        rec["state"], rec["reason"] = "needs-takeover", reason
        return copy.deepcopy(rec)


def clear_recovery(store: Store, *, key: str, index: int) -> Json:
    """Drop one takeover record after its old worktree was cleaned up by hand.
    The only way a recovery record goes away."""
    with store.transaction() as data:
        rec = data["leases"].get(key)
        if rec is None or not 0 <= index < len(rec["handoffs"]):
            raise ValueError(f"{key} has no recovery record {index}")
        del rec["handoffs"][index]
        return copy.deepcopy(rec)


def listing(store: Store, owner: str | None = None) -> Json:
    """Read-only view for the monitor and --status. Takes no lease."""
    data = store.read()
    records = [
        r for r in data["leases"].values() if owner is None or r["owner"] == owner
    ]
    slots = {kind: used_slots(data, kind, {}) for kind in ("claude", "codex")}
    return {
        "version": data["version"],
        "root": data["root"],
        "max_open_prs": MAX_OPEN_PRS,
        "slots": slots,
        "leases": sorted(records, key=lambda r: r["key"]),
    }


# --- Takeover ---------------------------------------------------------------

Git = Callable[[list[str]], str]


def run_git(args: list[str]) -> str:
    return subprocess.run(
        ["git", "--no-optional-locks", *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def recovery_ref(key: str, generation: int) -> str:
    return f"refs/epic-recovery/{key.replace(':', '-')}/g{generation}"


def pin_worktree(rec: Json, git: Git) -> Json:
    """Record the old worktree and pin its HEAD. Reads and one ref write only:
    the files, index and branch stay as they are."""
    path = rec["worktree"]
    if not path:
        if rec["kind"] == "impl" and not rec["tentative"]:
            raise Refused(f"{rec['key']} has work but no recorded worktree")
        return {"worktree": None}
    if not Path(path).is_dir():
        raise Refused(f"old worktree {path} is missing; cannot preserve its work")
    try:
        head = git(["-C", path, "rev-parse", "--verify", "HEAD"]).strip()
        branch = git(["-C", path, "branch", "--show-current"]).strip() or None
        status = git(["-C", path, "status", "--porcelain=v2", "--untracked-files=all"])
        ref = recovery_ref(rec["key"], rec["generation"])
        git(["-C", path, "update-ref", ref, head])
    except (OSError, subprocess.CalledProcessError) as err:
        raise Refused(f"cannot pin old worktree {path}: {err}") from None
    return {
        "worktree": path,
        "head": head,
        "branch": branch,
        "status": status,
        "recovery_ref": ref,
    }


@contextmanager
def target_locks(rec: Json, root: Path) -> Generator[None]:
    """Hold the old owner's target locks without waiting. An extra condition
    only: a free lock never proves the session stopped."""
    numbers = sorted({n for n in (rec["issue"], rec["pr"]) if isinstance(n, int)})
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with ExitStack() as stack:
        for number in numbers:
            handle = stack.enter_context((root / f"{number}.lock").open("a"))
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Refused(f"target {number} is locked by a running tick") from None
        yield


def takeover(
    store: Store,
    *,
    key: str,
    to: str,
    provider: EvidenceProvider | None,
    git: Git = run_git,
    locks: Path | None = None,
) -> Json:
    """Manual takeover. Refuses unless every check is proved; then the
    generation goes up by one and the old worktree stays pinned."""
    snapshot = store.read()["leases"].get(key)
    if snapshot is None or snapshot["state"] not in HELD:
        raise Refused(f"no held lease {key}")
    old = snapshot["owner"]
    if agent_of(to) != snapshot["agent"] or to == old:
        raise ValueError(f"{to} cannot take over {key} from {old}")
    if provider is None:
        raise Refused(f"no process-evidence provider; set {PROVIDER_ENV}")
    if provider.live is not True:
        raise Refused(f"provider {provider.name} is not live; fixtures prove nothing")
    if not snapshot["evidence"]:
        raise Refused(f"{key} has no process evidence (legacy record)")
    try:
        evidence = ProcessEvidence.from_json(snapshot["evidence"])
    except ValueError as err:
        raise Refused(f"{key}: unusable process evidence: {err}") from None
    if evidence.owner != old:
        raise Refused(f"{key}: evidence is for {evidence.owner}, not {old}")
    if snapshot["uncertain"]:
        raise Refused(
            f"{key}: reconcile uncertain publications first: {snapshot['uncertain']}"
        )
    try:
        if not provider.admission_blocked(old):
            raise Refused(f"{old} has no admission block")
        problems = stop_blockers(evidence, provider.stop(evidence))
    except Refused:
        raise
    except Exception as err:  # a provider failure proves nothing
        raise Refused(f"provider {provider.name} failed: {err!r}") from None
    if problems:
        raise Refused(f"{old} not proved stopped: " + "; ".join(problems))
    with target_locks(snapshot, locks or lock_dir()):
        pinned = pin_worktree(snapshot, git)
        with store.transaction() as data:
            rec = data["leases"].get(key)
            if rec != snapshot:
                raise Refused(f"{key} changed during takeover; run it again")
            now = store.clock()
            rec["handoffs"].append(
                {
                    "from": old,
                    "to": to,
                    "generation": rec["generation"],
                    "at": now,
                    **pinned,
                }
            )
            gen = data["generations"][key] + 1
            data["generations"][key] = gen
            rec.update(
                owner=to,
                generation=gen,
                state="active",
                reason=None,
                evidence=None,
                worktree=None,
                tentative=False,
                renewed_at=now,
            )
            return copy.deepcopy(rec)


# --- CLI --------------------------------------------------------------------


def read_evidence(path: Path | None) -> ProcessEvidence | None:
    if path is None:
        return None
    return ProcessEvidence.from_json(json.loads(path.read_text()))


def open_pr(text: str) -> tuple[int, int | None]:
    """`N` or `N:ISSUE`: an open PR of the agent and its `Issue:` number."""
    number, _, issue = text.partition(":")
    return int(number), int(issue) if issue else None


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = top.add_subparsers(dest="command", required=True)
    sub.add_parser("init").add_argument("--recreate", action="store_true")
    sub.add_parser("handoff").add_argument(
        "--from", dest="old", required=True, type=Path
    )

    def lease(p: argparse.ArgumentParser) -> None:
        p.add_argument("--key", required=True)
        p.add_argument("--owner", required=True)
        p.add_argument("--generation", required=True, type=int)

    p = sub.add_parser("acquire")
    p.add_argument("--action", required=True)
    p.add_argument("--owner", required=True)
    p.add_argument("--issue", type=int)
    p.add_argument("--pr", type=int)
    p.add_argument("--head")
    p.add_argument("--files")
    p.add_argument("--branch")
    p.add_argument("--worktree")
    p.add_argument("--open-pr", action="append", type=open_pr, default=[])
    p.add_argument("--evidence-file", type=Path)
    p.add_argument("--repo", default=REPO)
    p.add_argument("--expect-version", type=int)

    p = sub.add_parser("renew")
    p.add_argument("--owner", required=True)
    p.add_argument("--key")
    p.add_argument("--generation", type=int)
    p.add_argument("--pr", type=int)
    p.add_argument("--head")
    p.add_argument("--branch")
    p.add_argument("--worktree")
    p.add_argument("--work", action="store_true")
    p.add_argument("--evidence-file", type=Path)
    p.add_argument("--uncertain")
    p.add_argument("--reconciled")

    p = sub.add_parser("check")
    lease(p)
    p.add_argument("--repo", default=REPO)
    p.add_argument("--issue", type=int)
    p.add_argument("--pr", type=int)
    p.add_argument("--branch")
    p.add_argument("--head")

    p = sub.add_parser("release")
    lease(p)
    p.add_argument("--outcome", required=True, choices=OUTCOMES)

    p = sub.add_parser("extend")
    lease(p)
    p.add_argument("--files", required=True)

    p = sub.add_parser("flag")
    p.add_argument("--key", required=True)
    p.add_argument("--reason", required=True)

    sub.add_parser("list").add_argument("--owner")

    p = sub.add_parser("clear-recovery")
    p.add_argument("--key", required=True)
    p.add_argument("--index", required=True, type=int)

    p = sub.add_parser("takeover")
    p.add_argument("--key", required=True)
    p.add_argument("--to", required=True)
    return top


def run(args: argparse.Namespace, store: Store) -> Any:
    if args.command == "init":
        return init(store, args.recreate)
    if args.command == "handoff":
        return handoff(store, args.old)
    if args.command == "acquire":
        rec = acquire(
            store,
            action=args.action,
            owner=args.owner,
            issue=args.issue,
            pr=args.pr,
            head=args.head,
            files=parse_files(args.files) if args.files else None,
            branch=args.branch,
            worktree=args.worktree,
            open_prs=dict(args.open_pr),
            evidence=read_evidence(args.evidence_file),
            repo=args.repo,
            expect_version=args.expect_version,
        )
        return {
            "lease": {"key": rec["key"], "generation": rec["generation"]},
            "record": rec,
        }
    if args.command == "renew":
        return renew(
            store,
            owner=args.owner,
            key=args.key,
            generation=args.generation,
            pr=args.pr,
            head=args.head,
            branch=args.branch,
            worktree=args.worktree,
            work=args.work,
            evidence=read_evidence(args.evidence_file),
            uncertain=args.uncertain,
            reconciled=args.reconciled,
        )
    if args.command == "check":
        return check(
            store,
            key=args.key,
            owner=args.owner,
            generation=args.generation,
            repo=args.repo,
            issue=args.issue,
            pr=args.pr,
            branch=args.branch,
            head=args.head,
        )
    if args.command == "release":
        return release(
            store,
            key=args.key,
            owner=args.owner,
            generation=args.generation,
            outcome=args.outcome,
        )
    if args.command == "extend":
        return extend(
            store,
            key=args.key,
            owner=args.owner,
            generation=args.generation,
            files=parse_files(args.files),
        )
    if args.command == "flag":
        return flag(store, key=args.key, reason=args.reason)
    if args.command == "list":
        return listing(store, args.owner)
    if args.command == "clear-recovery":
        return clear_recovery(store, key=args.key, index=args.index)
    return takeover(store, key=args.key, to=args.to, provider=load_provider())


def main(argv: list[str] | None = None, store: Store | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = run(args, store or Store())
    except Blocked as err:
        print(f"leases: blocked: {err}", file=sys.stderr)
        return BLOCKED
    except Refused as err:
        print(f"leases: refused: {err}", file=sys.stderr)
        return REFUSED
    except Lost as err:
        print(f"leases: lost: {err}", file=sys.stderr)
        return LOST
    except (ValueError, OSError) as err:
        print(f"leases: {err}", file=sys.stderr)
        return INVALID
    print(json.dumps(result, indent=1, sort_keys=True))
    return OK


if __name__ == "__main__":
    sys.exit(main())
