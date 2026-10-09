"""Claude account usage wait (https://github.com/phaabe/live.moafunk.de/issues/677).

When the Claude CLI reports the account's session limit, every runner on the
same Claude account waits for the reset before it starts another model.

Account: EPIC_CLAUDE_ACCOUNT_KEY, a non-secret alias ([a-zA-Z0-9_-]{1,64},
default `default`). Runners that share an account use the same key and the
same state root; separate accounts set different keys. The key does not find
or log in to an account.

Store: <EPIC_QUOTA_DIR>/claude-usage/<key>/, or under
~/.local/state/epic-loop when EPIC_QUOTA_DIR is unset (claude-tick.sh always
sets it to the shared state dir). Folders are 0700 and created when missing;
an existing unsafe folder, a symlink or a bad file is refused, never repaired.
  state.json            the record (0600, atomic replace)
  account.lock          short lock around every read and write of the record
  probe.lock            held by the wrapper during the one recovery probe
  admissions/<id>.lock  held by the wrapper while its admission runs

Evidence: only the wrapper's terminal CLI result (`--output-format json`):
type `result`, is_error exactly true, terminal_reason `api_error`, the
session ID of this admission and the whole text matching the observed
session-limit message. A 529, another API error, zero cost or a quoted
message alone is no usage evidence.

Wait: a known reset ("resets 3pm (Europe/Berlin)", anchored to the wrapper's
receipt date in that zone) gives retry = reset + 60 s. An unknown zone, an
ambiguous or missing local time, a reset at or before the receipt, or no
receipt use the fallback: 15, then 30, then 60 minutes from the receipt.
Only a recovery probe that confirms the limit again moves to the next step.
An unexpired wait is only extended, never shortened.

Admission (`admit`, under the account lock): no model while the wait runs,
while an earlier admission is unresolved and its lock is free (its holder is
gone), or while the recovery probe runs. After the wait the first admission
is the only recovery probe. A non-error model completion of the same
generation clears the wait. A probe that ends with another API error or no
result keeps recovery mode and moves the next probe 180 s on.

Unresolved records: a crash leaves the admission unresolved, so new models
wait. `repair --id <id>` (operator only) resolves it once its lock is free,
optionally from that session's leftover result file.

Commands (exit 0 open or done, 3 wait, 1 unusable store or failed write,
2 bad input):
  check                          read-only check before selection
  path                           create the folders, print the account folder
  admit --id ID --fd N --probe-fd M
  finish --id ID (--result-file F --receipt T | --inconclusive | --withdrawn)
  status
  repair --id ID [--result-file F [--receipt T]]
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import stat
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from datetime import time as clock
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

KEY_ENV = "EPIC_CLAUDE_ACCOUNT_KEY"
ROOT_ENV = "EPIC_QUOTA_DIR"
DEFAULT_KEY = "default"
KEY = re.compile(r"[a-zA-Z0-9_-]{1,64}")
# The session UUID the wrapper passes with --session-id.
ADMISSION_ID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
FOLDER = "claude-usage"
STATE = "state.json"
ACCOUNT_LOCK = "account.lock"
PROBE_LOCK = "probe.lock"
ADMISSIONS = "admissions"
VERSION = 1
REASON = "session_limit"
MARGIN = timedelta(seconds=60)
FALLBACK = (timedelta(minutes=15), timedelta(minutes=30), timedelta(minutes=60))
PROBE_RETRY = timedelta(seconds=180)
LOCK_WAIT = 30.0
MAX_STATE = 64 * 1024
DETAILS = (
    "missing_authoritative_receipt",
    "unknown_zone",
    "ambiguous_local_time",
    "nonexistent_local_time",
    "past_reset",
)
SESSION_LIMIT = re.compile(
    "You've hit your session limit · resets "
    r"(?P<hour>1[0-2]|[1-9])(?::(?P<minute>[0-5][0-9]))?(?P<half>am|pm) "
    r"\((?P<zone>[A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z0-9_+-]+)*)\)"
)

OPEN = 0
FAILED = 1
BAD = 2
WAIT = 3


class UsageError(Exception):
    """The store cannot be used safely, or a write failed. No model starts."""


class BadInput(Exception):
    """Invalid key, ID or arguments."""


# --- time -------------------------------------------------------------------


def stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_stamp(value: Any) -> datetime:
    if not isinstance(value, str) or not STAMP.fullmatch(value):
        raise ValueError(f"not a UTC timestamp: {value!r}")
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


# --- classification ---------------------------------------------------------


@dataclass(frozen=True)
class Terminal:
    """What the terminal CLI result says.

    kind: usage_quota, transient (another API error), ok (a non-error
    completion), inconclusive (no usable result), withdrawn (no model ran).
    """

    kind: str
    reset: datetime | None = None
    detail: str | None = None  # why the reset is unknown
    void: bool = False  # refused before any model work


def reset_time(
    match: re.Match[str], receipt: datetime
) -> tuple[datetime | None, str | None]:
    """The reset in UTC, or None and why: the clock time on the receipt's date
    in the named zone. Never the next day."""
    try:
        zone = ZoneInfo(match["zone"])
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None, "unknown_zone"
    hour = int(match["hour"]) % 12 + (12 if match["half"] == "pm" else 0)
    local = datetime.combine(
        receipt.astimezone(zone).date(), clock(hour, int(match["minute"] or 0))
    )
    early = local.replace(tzinfo=zone, fold=0).astimezone(timezone.utc)
    late = local.replace(tzinfo=zone, fold=1).astimezone(timezone.utc)
    if early != late:
        back = early.astimezone(zone).replace(tzinfo=None)
        return (
            None,
            "ambiguous_local_time" if back == local else "nonexistent_local_time",
        )
    if early <= receipt:
        return None, "past_reset"
    return early, None


def no_work(envelope: Mapping[str, Any]) -> bool:
    """The session did no model work: zero tokens, cost and API time, and no
    denied tool call."""
    usage = envelope.get("usage")
    if not isinstance(usage, dict):
        return False
    for name in ("input_tokens", "output_tokens"):
        if type(usage.get(name)) is not int or usage[name] != 0:
            return False
    for name in ("cache_read_input_tokens", "cache_creation_input_tokens"):
        if name in usage and (type(usage[name]) is not int or usage[name] != 0):
            return False
    cost = envelope.get("total_cost_usd")
    return (
        type(cost) in (int, float)
        and cost == 0
        and type(envelope.get("duration_api_ms")) is int
        and envelope["duration_api_ms"] == 0
        and envelope.get("permission_denials") == []
    )


def classify(envelope: Any, admission: str, receipt: datetime | None) -> Terminal:
    """The terminal result of this admission's session, from its fields only."""
    if not isinstance(envelope, dict) or envelope.get("type") != "result":
        return Terminal("inconclusive")
    if envelope.get("session_id") != admission:
        return Terminal("inconclusive")
    error = envelope.get("is_error")
    if error is True and envelope.get("terminal_reason") == "api_error":
        text = envelope.get("result")
        match = SESSION_LIMIT.fullmatch(text) if isinstance(text, str) else None
        if match is None:
            return Terminal("transient")
        if receipt is None:
            reset, detail = None, "missing_authoritative_receipt"
        else:
            reset, detail = reset_time(match, receipt)
        return Terminal("usage_quota", reset, detail, no_work(envelope))
    if error is False:
        return Terminal("ok")
    return Terminal("inconclusive")


def read_terminal(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


# --- store ------------------------------------------------------------------


def empty() -> dict[str, Any]:
    return {
        "version": VERSION,
        "generation": 0,
        "wait": None,
        "probe": None,
        "probe_retry_at": None,
        "admissions": {},
    }


def check_record(data: Any) -> dict[str, Any]:
    """The record if it is a valid version 1 record; ValueError says why."""
    if not isinstance(data, dict) or set(data) != set(empty()):
        raise ValueError("unexpected fields")
    if type(data["version"]) is not int or data["version"] != VERSION:
        raise ValueError("unknown version")
    if type(data["generation"]) is not int or data["generation"] < 0:
        raise ValueError("bad generation")
    wait = data["wait"]
    if wait is not None:
        fields = {"reason", "observed_at", "reset_at", "retry_at", "source"}
        fields |= {"fallback_count", "detail"}
        if not isinstance(wait, dict) or set(wait) != fields:
            raise ValueError("bad wait")
        if wait["reason"] != REASON or wait["source"] not in ("cli-clock", "fallback"):
            raise ValueError("bad wait reason or source")
        parse_stamp(wait["observed_at"])
        parse_stamp(wait["retry_at"])
        if (wait["reset_at"] is None) != (wait["source"] == "fallback"):
            raise ValueError("reset and source disagree")
        if wait["reset_at"] is not None:
            parse_stamp(wait["reset_at"])
        if type(wait["fallback_count"]) is not int or wait["fallback_count"] < 0:
            raise ValueError("bad fallback count")
        if wait["detail"] is not None and wait["detail"] not in DETAILS:
            raise ValueError("bad detail")
    if data["probe_retry_at"] is not None:
        parse_stamp(data["probe_retry_at"])
    admissions = data["admissions"]
    if not isinstance(admissions, dict):
        raise ValueError("bad admissions")
    for key, record in admissions.items():
        if not ADMISSION_ID.fullmatch(key):
            raise ValueError("bad admission ID")
        if not isinstance(record, dict) or set(record) != {"at", "generation", "probe"}:
            raise ValueError("bad admission")
        parse_stamp(record["at"])
        if type(record["generation"]) is not int or type(record["probe"]) is not bool:
            raise ValueError("bad admission")
    probe = data["probe"]
    if probe is not None:
        if not isinstance(probe, dict) or set(probe) != {"id", "generation"}:
            raise ValueError("bad probe")
        if probe["id"] not in admissions or type(probe["generation"]) is not int:
            raise ValueError("probe without its admission")
    return data


def safe_parents(path: Path) -> None:
    for parent in path.parents:
        try:
            mode = os.stat(parent).st_mode
        except OSError as error:
            raise UsageError(f"cannot check {parent}: {error.strerror}") from error
        if mode & 0o022 and not mode & stat.S_ISVTX:
            raise UsageError(f"{parent} is group or world writable")


def private_folder(path: Path, create: bool) -> bool:
    """True when the folder exists (creating it first when asked). Refuses a
    link, another owner or a mode other than 0700."""
    if create:
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
        except OSError as error:
            raise UsageError(f"cannot create {path}: {error.strerror}") from error
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as error:
        raise UsageError(f"cannot check {path}: {error.strerror}") from error
    if not stat.S_ISDIR(info.st_mode):
        raise UsageError(f"{path} must be a folder, not a link or a file")
    if info.st_uid != os.getuid():
        raise UsageError(f"{path} must belong to this user")
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise UsageError(f"{path} must have mode 0700")
    return True


@dataclass(frozen=True)
class Store:
    key: str
    folder: Path  # <real root>/claude-usage/<key>

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Store:
        key = env.get(KEY_ENV, DEFAULT_KEY)
        if not KEY.fullmatch(key):
            raise BadInput(f"{KEY_ENV} must match [a-zA-Z0-9_-]{{1,64}}")
        raw = env.get(ROOT_ENV) or str(Path.home() / ".local/state/epic-loop")
        if not os.path.isabs(raw):
            raise BadInput(f"{ROOT_ENV} must be an absolute path")
        try:
            root = Path(raw).resolve(strict=True)
        except FileNotFoundError:
            # No state root yet: nothing stored. `path` creates nothing above it.
            return cls(key, Path(raw) / FOLDER / key)
        except (OSError, RuntimeError) as error:
            raise UsageError(f"cannot use {raw}: {error}") from error
        info = os.stat(root)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise UsageError(f"{root} must be a folder of this user")
        if info.st_mode & 0o022:
            raise UsageError(f"{root} is group or world writable")
        safe_parents(root)
        return cls(key, root / FOLDER / key)

    @property
    def state(self) -> Path:
        return self.folder / STATE

    @property
    def admissions(self) -> Path:
        return self.folder / ADMISSIONS

    def lock_file(self, admission: str) -> Path:
        return self.admissions / f"{admission}.lock"

    def exists(self) -> bool:
        """True when the account folder exists; checks every part that does."""
        return private_folder(self.folder.parent, False) and private_folder(
            self.folder, False
        )

    def create(self) -> None:
        if not self.folder.parent.parent.is_dir():
            raise UsageError(f"state root {self.folder.parent.parent} does not exist")
        for path in (self.folder.parent, self.folder, self.admissions):
            private_folder(path, True)

    def load(self) -> dict[str, Any]:
        """The record; a missing file is an empty record. Never deletes or
        rewrites a bad file."""
        if not self.exists():
            return empty()
        private_folder(self.admissions, False)
        try:
            fd = os.open(self.state, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return empty()
        except OSError as error:
            raise UsageError(f"cannot read {self.state}: {error.strerror}") from error
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise UsageError(
                    f"{self.state} must be a private file (0600, one link)"
                )
            raw = handle.read(MAX_STATE + 1)
        if len(raw) > MAX_STATE:
            raise UsageError(f"{self.state} is too large")
        try:
            return check_record(json.loads(raw))
        except (ValueError, UnicodeDecodeError) as error:
            raise UsageError(
                f"{self.state} is not a valid version 1 record ({error}); "
                "repair it by hand"
            ) from error

    def save(self, data: dict[str, Any]) -> None:
        """Atomic replace. An OSError means the record stays as it was."""
        check_record(data)
        text = json.dumps(data, indent=1, sort_keys=True) + "\n"
        fd, name = tempfile.mkstemp(dir=self.folder, prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as out:
                out.write(text)
                out.flush()
                os.fsync(out.fileno())
            os.replace(name, self.state)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(name)
            raise
        folder = os.open(self.folder, os.O_RDONLY)
        try:
            os.fsync(folder)
        finally:
            os.close(folder)

    @contextlib.contextmanager
    def locked(self, wait: float = LOCK_WAIT) -> Iterator[None]:
        path = self.folder / ACCOUNT_LOCK
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError as error:
            raise UsageError(f"cannot open {path}: {error.strerror}") from error
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise UsageError(f"{path} must be a regular file without aliases")
            deadline = time.monotonic() + wait
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        raise UsageError(
                            f"{path} stayed locked for {wait:.0f} s"
                        ) from None
                    time.sleep(0.05)
            yield
        finally:
            os.close(fd)

    def alive(self, admission: str) -> bool:
        """True while some process holds this admission's lock."""
        path = self.lock_file(admission)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return False
        except OSError as error:
            raise UsageError(f"cannot check {path}: {error.strerror}") from error
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise UsageError(f"{path} must be a regular file")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            return False
        finally:
            os.close(fd)  # drops the lock taken just to test it


def held_file(fd: int, path: Path) -> None:
    """The wrapper's descriptor `fd` is the regular file at `path`."""
    try:
        held = os.fstat(fd)
        named = os.lstat(path)
    except OSError as error:
        raise UsageError(f"descriptor {fd} or {path}: {error.strerror}") from error
    if (
        (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino)
        or not stat.S_ISREG(held.st_mode)
        or held.st_nlink != 1
        or held.st_uid != os.getuid()
        or held.st_mode & 0o022
    ):
        raise UsageError(f"descriptor {fd} is not the private file {path}")


def take(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


# --- decisions --------------------------------------------------------------


def blocker(store: Store, data: dict[str, Any], now: datetime) -> str | None:
    """Why no model may start now, or None."""
    for other, record in sorted(data["admissions"].items()):
        if not store.alive(other):
            return (
                f"unresolved admission {other} since {record['at']}; "
                "see claude_usage.py status"
            )
    wait = data["wait"]
    if wait is None:
        return None
    if now < parse_stamp(wait["retry_at"]):
        return f"{REASON} until {wait['retry_at']}"
    if data["probe"] is not None:
        return f"{REASON} recovery probe {data['probe']['id']} is running"
    if data["probe_retry_at"] is not None and now < parse_stamp(data["probe_retry_at"]):
        return f"{REASON} recovery, next probe after {data['probe_retry_at']}"
    return None


def retry_at(store: Store, data: dict[str, Any], now: datetime) -> str | None:
    """When the wait `blocker` names ends, or None: no wait, or no known time
    (an unresolved admission or a running probe)."""
    if any(not store.alive(other) for other in data["admissions"]):
        return None
    wait = data["wait"]
    if wait is None:
        return None
    if now < parse_stamp(wait["retry_at"]):
        return str(wait["retry_at"])
    probe = data["probe_retry_at"]
    if data["probe"] is None and probe is not None and now < parse_stamp(probe):
        return str(probe)
    return None


def check(store: Store, now: datetime) -> tuple[int, str | None]:
    """Read only: (WAIT, why) or (OPEN, None)."""
    reason = blocker(store, store.load(), now)
    return (WAIT, reason) if reason is not None else (OPEN, None)


def admit(
    store: Store, admission: str, fd: int, probe_fd: int, now: datetime
) -> tuple[int, str]:
    """Record this admission before the model starts: (OPEN, "probe" or
    "normal") or (WAIT, why). The wrapper keeps `fd` (and for the probe
    `probe_fd`) open, and so locked, until `finish`."""
    own = store.lock_file(admission)
    held_file(fd, own)
    held_file(probe_fd, store.folder / PROBE_LOCK)
    recorded = False
    try:
        with store.locked():
            data = store.load()
            if admission in data["admissions"]:
                raise BadInput(f"admission {admission} is already recorded")
            reason = blocker(store, data, now)
            if reason is not None:
                return WAIT, reason
            # blocker() passed with a wait: it has expired, this is the probe.
            probe = data["wait"] is not None
            if probe and not take(probe_fd):
                return WAIT, f"{REASON} recovery probe lock is held"
            if not take(fd):
                raise UsageError(f"{own} is locked by another process")
            data["admissions"][admission] = {
                "at": stamp(now),
                "generation": data["generation"],
                "probe": probe,
            }
            if probe:
                data["probe"] = {"id": admission, "generation": data["generation"]}
                data["probe_retry_at"] = None
            try:
                store.save(data)
            except OSError as error:
                raise UsageError(
                    f"cannot record admission {admission}: {error.strerror}"
                ) from error
            recorded = True
            return OPEN, "probe" if probe else "normal"
    finally:
        if not recorded:
            with contextlib.suppress(OSError):
                os.unlink(own)


def record_quota(
    data: dict[str, Any],
    terminal: Terminal,
    receipt: datetime,
    now: datetime,
    probe: bool,
) -> None:
    wait = data["wait"]
    count = wait["fallback_count"] if wait is not None else 0
    if terminal.reset is not None:
        retry, source, steps = terminal.reset + MARGIN, "cli-clock", count
    elif wait is None or probe:
        # A new wait, or a recovery probe that hit the limit again.
        retry = receipt + FALLBACK[min(count, len(FALLBACK) - 1)]
        source, steps = "fallback", count + 1
    else:
        # More evidence of the same wait (a model admitted before it).
        retry = receipt + FALLBACK[min(max(count - 1, 0), len(FALLBACK) - 1)]
        source, steps = "fallback", count
    if wait is not None:
        current = parse_stamp(wait["retry_at"])
        if current > now and current >= retry:
            return  # never shorten an unexpired wait
    data["wait"] = {
        "reason": REASON,
        "observed_at": stamp(receipt),
        "reset_at": stamp(terminal.reset) if terminal.reset is not None else None,
        "retry_at": stamp(retry),
        "source": source,
        "fallback_count": steps,
        "detail": terminal.detail,
    }
    data["probe_retry_at"] = None
    data["generation"] += 1


def resolve(
    data: dict[str, Any],
    admission: str,
    terminal: Terminal,
    receipt: datetime | None,
    now: datetime,
) -> None:
    record = data["admissions"].pop(admission)
    probe = data["probe"] is not None and data["probe"]["id"] == admission
    if probe:
        data["probe"] = None
    if terminal.kind == "usage_quota":
        record_quota(data, terminal, receipt or now, now, probe)
    elif terminal.kind == "ok":
        wait = data["wait"]
        if (
            wait is not None
            and record["generation"] == data["generation"]
            and parse_stamp(wait["retry_at"]) <= now
        ):
            data["wait"] = None
            data["probe_retry_at"] = None
            data["generation"] += 1
    elif terminal.kind in ("transient", "inconclusive") and probe:
        data["probe_retry_at"] = stamp(now + PROBE_RETRY)


def finish(
    store: Store,
    admission: str,
    terminal_of: Any,
    receipt: datetime | None,
    now: datetime,
) -> Terminal | None:
    """Resolve this admission. `terminal_of(admission)` gives its Terminal.
    None when it was already resolved (no change)."""
    with store.locked():
        data = store.load()
        if admission not in data["admissions"]:
            return None
        terminal = terminal_of(admission)
        resolve(data, admission, terminal, receipt, now)
        try:
            store.save(data)
        except OSError as error:
            raise UsageError(
                f"cannot store the result of {admission}: {error.strerror}; "
                "it stays unresolved"
            ) from error
    with contextlib.suppress(OSError):
        os.unlink(store.lock_file(admission))
    return terminal


def repair(
    store: Store,
    admission: str,
    terminal_of: Any,
    receipt: datetime | None,
    now: datetime,
) -> tuple[int, str]:
    """Operator repair of an unresolved admission whose holder is gone."""
    with store.locked():
        data = store.load()
        if admission not in data["admissions"]:
            return OPEN, f"admission {admission} is not unresolved; no change"
        if store.alive(admission):
            return WAIT, f"admission {admission} still runs; no change"
        terminal = terminal_of(admission)
        resolve(data, admission, terminal, receipt, now)
        try:
            store.save(data)
        except OSError as error:
            raise UsageError(f"cannot store the repair: {error.strerror}") from error
    with contextlib.suppress(OSError):
        os.unlink(store.lock_file(admission))
    return OPEN, f"admission {admission} resolved as {terminal.kind}"


def status_lines(store: Store, now: datetime) -> list[str]:
    data = store.load()
    head = f"claude usage ({store.key}):"
    wait = data["wait"]
    lines = []
    if wait is None:
        lines.append(f"{head} no wait")
    elif now < parse_stamp(wait["retry_at"]):
        lines.append(f"{head} {REASON} until {wait['retry_at']} ({wait['source']})")
    elif data["probe"] is not None:
        lines.append(f"{head} {REASON} recovery, probe {data['probe']['id']} running")
    elif data["probe_retry_at"] is not None and now < parse_stamp(
        data["probe_retry_at"]
    ):
        lines.append(
            f"{head} {REASON} recovery, next probe after {data['probe_retry_at']}"
        )
    else:
        lines.append(f"{head} {REASON} recovery, the next model is the probe")
    for admission, record in sorted(data["admissions"].items()):
        if store.alive(admission):
            lines.append(f"  admission {admission} running since {record['at']}")
        else:
            lines.append(
                f"  unresolved admission {admission} since {record['at']}: repair with "
                f"python3 scripts/epic/claude_usage.py repair --id {admission}"
            )
    return lines


# --- command line -----------------------------------------------------------


def receipt_of(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return parse_stamp(value)
    except ValueError:
        return None


def terminal_from(path: Path | None, receipt: datetime | None, how: str) -> Any:
    if how == "withdrawn":
        return lambda _admission: Terminal("withdrawn")
    if how == "inconclusive" or path is None:
        return lambda _admission: Terminal("inconclusive")
    return lambda admission: classify(read_terminal(path), admission, receipt)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "command", choices=["check", "path", "admit", "finish", "status", "repair"]
    )
    parser.add_argument("--id")
    parser.add_argument("--fd", type=int)
    parser.add_argument("--probe-fd", type=int)
    parser.add_argument("--result-file", type=Path)
    parser.add_argument("--receipt")
    how = parser.add_mutually_exclusive_group()
    how.add_argument("--inconclusive", action="store_true")
    how.add_argument("--withdrawn", action="store_true")
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    try:
        store = Store.from_env(os.environ)
        if args.command in ("admit", "finish", "repair"):
            if args.id is None or not ADMISSION_ID.fullmatch(args.id):
                raise BadInput("--id must be a lowercase session UUID")
        if args.command == "check":
            code, reason = check(store, now)
            if reason is not None:
                print(f"claude usage ({store.key}): {reason}; no model")
            return code
        if args.command == "path":
            store.create()
            print(store.folder)
            return OPEN
        if args.command == "status":
            print("\n".join(status_lines(store, now)))
            return OPEN
        if args.command == "admit":
            if args.fd is None or args.probe_fd is None:
                raise BadInput("admit needs --fd and --probe-fd")
            code, word = admit(store, args.id, args.fd, args.probe_fd, now)
            if code == WAIT:
                print(f"claude usage ({store.key}): {word}; no model")
            else:
                kind = "the recovery probe" if word == "probe" else "a normal model"
                print(f"claude usage ({store.key}): admitted {args.id} as {kind}")
            return code
        receipt = receipt_of(args.receipt)
        mode = (
            "withdrawn"
            if args.withdrawn
            else "inconclusive"
            if args.inconclusive
            else ""
        )
        if args.command == "repair":
            code, line = repair(
                store,
                args.id,
                terminal_from(args.result_file, receipt, mode),
                receipt,
                now,
            )
            print(f"claude usage ({store.key}): {line}")
            return code
        if not mode and args.result_file is None:
            raise BadInput("finish needs --result-file, --inconclusive or --withdrawn")
        terminal = finish(
            store, args.id, terminal_from(args.result_file, receipt, mode), receipt, now
        )
        if terminal is None:
            print(
                f"claude usage ({store.key}): {args.id} already resolved",
                file=sys.stderr,
            )
            print("resolved")
            return OPEN
        if terminal.kind == "usage_quota":
            wait = store.load()["wait"]
            until = wait["retry_at"] if wait is not None else "?"
            print(
                f"claude usage ({store.key}): {REASON}; no model until {until}",
                file=sys.stderr,
            )
        print(terminal.kind + (" void" if terminal.void else ""))
        return OPEN
    except BadInput as error:
        print(f"claude usage: {error}", file=sys.stderr)
        return BAD
    except UsageError as error:
        print(f"claude usage: {error}; no model", file=sys.stderr)
        return FAILED
    except OSError as error:
        print(f"claude usage: {error.strerror or error}; no model", file=sys.stderr)
        return FAILED


if __name__ == "__main__":
    sys.exit(main())
