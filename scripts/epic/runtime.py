"""Pinned runner runtime: roots, manifest, pin, promotion marker, admission.

Contract of https://github.com/phaabe/live.moafunk.de/issues/584. Full text:
docs/implementation/runner-runtime.md.

Two roots. The runtime root (EPIC_RUNTIME_ROOT) holds the pinned code that a
tick runs; it changes only on promotion. The repo root (EPIC_TRUSTED_ROOT) is
the runner's git checkout, used only for git. Worktree, lock and state paths
do not move.

Install home: EPIC_RUNTIME_HOME, default ~/.local/share/epic-runtime.
  revisions/<sha>/                 one read-only install per revision
  revisions/<sha>/runtime-manifest.json
  pin.json                         the revision ticks run
  configured.json                  written once at bootstrap; pinned mode is on

Lock dir: EPIC_LOCK_DIR (target_lock.lock_dir()), shared by all runners.
  runtime.lock                     admission lock: ticks LOCK_SH, promotion LOCK_EX
  runtime-promotion.json           promotion marker: blocks every admission
  admitted/<tick_id>.json          one record per admitted tick

Usage:
  runtime.py validate --manifest F     0 ok, 1 mismatch, 2 unreadable or unknown schema
  runtime.py resolve                   the pinned install as JSON; 78 when blocked
  runtime.py mode                      pinned, legacy, or 78 when neither is allowed
  runtime.py admit --fd N --tick-id ID --agent A [--pid P]
                                       0 admitted, 75 busy or promotion running
  runtime.py release --tick-id ID      removes the admission record
  runtime.py hold --fd N               command prefix: LOCK_SH, then the barrier
  runtime.py barrier                   0 allowed, 1 refused by the promotion marker
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import target_lock

SCHEMA = 1
CONTRACT = 1
OK, MISMATCH, UNREADABLE = 0, 1, 2
BUSY = 75
BLOCKED = 78
MANIFEST = "runtime-manifest.json"
PIN = "pin.json"
CONFIGURED = "configured.json"
LOCK = "runtime.lock"
MARKER = "runtime-promotion.json"
ADMITTED = "admitted"
PHASES = ("prepared", "switched", "smoked")
REVISION = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
TICK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
AGENTS = ("claude", "codex")
# Tick entry per agent, relative to the install.
ENTRIES = {"claude": "scripts/epic/claude-tick.sh", "codex": ".codex/codex-tick.sh"}
# Settings files have fixed install-relative paths; the manifest holds their hashes.
SETTINGS = {
    "claude_runner_settings": "scripts/epic/claude-runner-settings.json",
    "claude_mcp_config": "scripts/epic/claude-mcp-config.json",
    "codex_runner_config": ".codex/runtime/config.toml",
    "codex_hooks": ".codex/runtime/hooks.json",
    "codex_feature_git_rules": ".codex/runtime/rules/codex-feature-git.rules",
    "codex_cleanup_git_rules": ".codex/runtime/rules/codex-cleanup-git.rules",
    "codex_result_schema": ".codex/tick-result.schema.json",
}
EXECUTABLES = ("claude", "codex", "codex_launcher", "gitnexus", "python3", "gtimeout")


class RuntimeBlocked(Exception):
    """A runtime file is missing, unreadable or does not match."""


# --- paths ------------------------------------------------------------------


def home(env: Mapping[str, str] | None = None) -> Path:
    values = os.environ if env is None else env
    value = values.get("EPIC_RUNTIME_HOME")
    return Path(value) if value else Path.home() / ".local/share/epic-runtime"


def lock_dir() -> Path:
    return target_lock.lock_dir()


def marker_path() -> Path:
    return lock_dir() / MARKER


def admitted_dir() -> Path:
    return lock_dir() / ADMITTED


def configured(where: Path) -> bool:
    """Pinned mode is on: bootstrap wrote configured.json, or a pin exists."""
    return (where / CONFIGURED).exists() or (where / PIN).exists()


def code_root(fallback: Path, env: Mapping[str, str] | None = None) -> Path:
    """Where runner code loads from: EPIC_RUNTIME_ROOT in a pinned tick.

    Without it, pinned mode refuses instead of loading code from `fallback`
    (the checkout or the caller's own file). Legacy mode and machines without
    pinned mode keep `fallback`, today's behaviour.
    """
    values = os.environ if env is None else env
    root = values.get("EPIC_RUNTIME_ROOT") or ""
    if root:
        if not os.path.isabs(root):
            raise RuntimeBlocked(f"EPIC_RUNTIME_ROOT {root} is not an absolute path")
        return Path(root)
    if configured(home(values)):
        raise RuntimeBlocked(
            "pinned mode is configured but EPIC_RUNTIME_ROOT is not set; "
            "start ticks through the epic-tick launcher"
        )
    return fallback


# --- small file helpers -----------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def write_json(path: Path, data: Any) -> None:
    """Atomic: a temp file in the same directory, then os.replace."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(data, indent=1) + "\n")
    os.replace(temp, path)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- manifest ---------------------------------------------------------------


def install_files(install: Path) -> tuple[dict[str, Path], list[str]]:
    """Every file of the install except the manifest, and paths that are not
    plain files or directories (symlinks, sockets): those fail validation."""
    files: dict[str, Path] = {}
    odd: list[str] = []
    for dirpath, dirnames, filenames in os.walk(install):
        here = Path(dirpath)
        for name in [*dirnames, *filenames]:
            path = here / name
            rel = path.relative_to(install).as_posix()
            mode = path.lstat().st_mode
            if stat.S_ISDIR(mode):
                continue
            if not stat.S_ISREG(mode):
                odd.append(rel)
            elif rel != MANIFEST:
                files[rel] = path
    return files, odd


def helper_entry(
    path: Path, config: Path | None, keys: tuple[str, ...]
) -> dict[str, Any]:
    """Manifest entry of an installed Git helper and its config file."""
    entry: dict[str, Any] = {"sha256": sha256_file(path), "config": None}
    if config is not None:
        data = read_json(config)
        entry["config"] = {
            "path": str(config),
            "sha256": sha256_file(config),
            "values": {key: data.get(key) for key in keys},
        }
    return entry


def build_manifest(
    install: Path,
    revision: str,
    executables: Mapping[str, Mapping[str, str]],
    helpers: Mapping[str, Any] | None = None,
    settings: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Hash the install and write its manifest (last step before read-only).

    `executables`: name -> {path, version}; the SHA-256 is computed here.
    `helpers`: helper path -> helper_entry(). `settings`: names from SETTINGS.
    """
    if not REVISION.fullmatch(revision):
        raise ValueError(f"revision {revision!r} is not a 40-hex commit")
    files, odd = install_files(install)
    if odd:
        raise ValueError(f"install has non-regular files: {', '.join(sorted(odd))}")
    manifest = {
        "schema": SCHEMA,
        "revision": revision,
        "contract": CONTRACT,
        "created_at": now_iso(),
        "files": {rel: sha256_file(path) for rel, path in sorted(files.items())},
        "executables": {
            name: {
                "path": str(spec["path"]),
                "version": str(spec["version"]),
                "sha256": sha256_file(Path(spec["path"])),
            }
            for name, spec in executables.items()
        },
        "helpers": dict(helpers or {}),
        "settings": {name: sha256_file(install / SETTINGS[name]) for name in settings},
    }
    (install / MANIFEST).write_text(json.dumps(manifest, indent=1) + "\n")
    return manifest


def structure(manifest: Any) -> str | None:
    """Why the manifest's shape is unusable, or None."""
    if not isinstance(manifest, dict):
        return "manifest is not an object"
    if type(manifest.get("schema")) is not int or manifest["schema"] != SCHEMA:
        return f"unknown manifest schema {manifest.get('schema')!r}"
    if not isinstance(manifest.get("revision"), str) or not REVISION.fullmatch(
        manifest["revision"]
    ):
        return "revision is not a 40-hex commit"
    if type(manifest.get("contract")) is not int:
        return "contract is not an integer"
    for key in ("files", "executables", "helpers", "settings"):
        if not isinstance(manifest.get(key), dict):
            return f"{key} is not an object"
    for rel, digest in manifest["files"].items():
        if not isinstance(digest, str) or not SHA256.fullmatch(digest):
            return f"files[{rel}] is not a SHA-256"
    for name, spec in manifest["executables"].items():
        if name not in EXECUTABLES:
            return f"unknown executable {name}"
        if not isinstance(spec, dict) or not all(
            isinstance(spec.get(k), str) for k in ("path", "version", "sha256")
        ):
            return f"executables[{name}] needs path, version and sha256"
        if not os.path.isabs(spec["path"]):
            return f"executables[{name}] path is not absolute"
    for path, spec in manifest["helpers"].items():
        if not os.path.isabs(path) or not isinstance(spec, dict):
            return f"helpers[{path}] is not an absolute path with an entry"
        config = spec.get("config")
        if config is not None and not (
            isinstance(config, dict)
            and isinstance(config.get("path"), str)
            and isinstance(config.get("sha256"), str)
            and isinstance(config.get("values"), dict)
        ):
            return f"helpers[{path}] config needs path, sha256 and values"
    for name in manifest["settings"]:
        if name not in SETTINGS:
            return f"unknown settings entry {name}"
    return None


def check_hash(
    item: str, path: Path, expected: str, failures: list[dict[str, Any]]
) -> None:
    try:
        actual = sha256_file(path)
    except OSError as error:
        actual = f"unreadable: {error.strerror or error}"
    if actual != expected:
        failures.append({"item": item, "expected": expected, "actual": actual})


def validate(path: Path, contract: int = CONTRACT) -> tuple[int, list[dict[str, Any]]]:
    """(exit code, failures) for one install's manifest."""
    try:
        manifest = read_json(path)
    except (OSError, ValueError) as error:
        return UNREADABLE, [
            {"item": "manifest", "expected": "readable JSON", "actual": str(error)}
        ]
    reason = structure(manifest)
    if reason:
        return UNREADABLE, [
            {"item": "manifest", "expected": "schema 1", "actual": reason}
        ]
    failures: list[dict[str, Any]] = []
    if manifest["contract"] != contract:
        failures.append(
            {"item": "contract", "expected": contract, "actual": manifest["contract"]}
        )
    install = path.parent
    files, odd = install_files(install)
    for rel in sorted(odd):
        failures.append(
            {
                "item": f"files[{rel}]",
                "expected": "regular file",
                "actual": "not a regular file",
            }
        )
    for rel in sorted(set(files) - set(manifest["files"])):
        failures.append(
            {"item": f"files[{rel}]", "expected": "absent", "actual": "unexpected file"}
        )
    for rel, digest in sorted(manifest["files"].items()):
        check_hash(f"files[{rel}]", install / rel, digest, failures)
    for rel, entry in sorted(install_modes(install).items()):
        failures.append(
            {"item": f"mode[{rel}]", "expected": "read-only", "actual": entry}
        )
    for name, spec in sorted(manifest["executables"].items()):
        check_hash(f"executables[{name}]", Path(spec["path"]), spec["sha256"], failures)
    for helper, spec in sorted(manifest["helpers"].items()):
        check_hash(
            f"helpers[{helper}]", Path(helper), str(spec.get("sha256")), failures
        )
        config = spec.get("config")
        if config is None:
            continue
        check_hash(
            f"helpers[{helper}].config",
            Path(config["path"]),
            config["sha256"],
            failures,
        )
        try:
            data = read_json(Path(config["path"]))
        except (OSError, ValueError):
            continue  # the hash check above already failed
        for key, value in config["values"].items():
            if not isinstance(data, dict) or data.get(key) != value:
                failures.append(
                    {
                        "item": f"helpers[{helper}].config.{key}",
                        "expected": value,
                        "actual": data.get(key) if isinstance(data, dict) else None,
                    }
                )
    for name, digest in sorted(manifest["settings"].items()):
        check_hash(f"settings[{name}]", install / SETTINGS[name], digest, failures)
    return (MISMATCH if failures else OK), failures


def install_modes(install: Path) -> dict[str, str]:
    """Install paths that can still be written (by anyone)."""
    found: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(install):
        for name in [".", *dirnames, *filenames]:
            path = Path(dirpath) / name if name != "." else Path(dirpath)
            mode = path.lstat().st_mode
            if mode & 0o222:
                rel = path.relative_to(install).as_posix()
                found.setdefault(rel, oct(mode & 0o777))
    return found


# --- pin and entry mode -----------------------------------------------------


def write_configured(where: Path) -> None:
    """Bootstrap only. No runner command removes this file."""
    write_json(where / CONFIGURED, {"schema": SCHEMA, "configured_at": now_iso()})


def write_pin(
    where: Path, revision: str, manifest_sha256: str, promotion_id: str,
    previous: Mapping[str, str] | None,
) -> None:  # fmt: skip
    """Callers hold the promoter lock."""
    write_json(
        where / PIN,
        {
            "schema": SCHEMA,
            "revision": revision,
            "manifest_sha256": manifest_sha256,
            "previous": dict(previous) if previous else None,
            "promotion_id": promotion_id,
            "promoted_at": now_iso(),
        },
    )


def read_pin(where: Path) -> dict[str, Any]:
    try:
        pin = read_json(where / PIN)
    except FileNotFoundError:
        raise RuntimeBlocked(f"no pin at {where / PIN}") from None
    except (OSError, ValueError) as error:
        raise RuntimeBlocked(f"pin unreadable: {error}") from None
    if (
        not isinstance(pin, dict)
        or pin.get("schema") != SCHEMA
        or not REVISION.fullmatch(str(pin.get("revision")))
        or not SHA256.fullmatch(str(pin.get("manifest_sha256")))
        or not isinstance(pin.get("promotion_id"), str)
    ):
        raise RuntimeBlocked("pin is invalid")
    return pin


def resolve(where: Path) -> dict[str, Any]:
    """The pinned install: pin valid, manifest hash matches, manifest valid."""
    pin = read_pin(where)
    install = where / "revisions" / pin["revision"]
    manifest = install / MANIFEST
    try:
        digest = sha256_file(manifest)
    except OSError as error:
        raise RuntimeBlocked(f"pinned manifest unreadable: {error}") from None
    if digest != pin["manifest_sha256"]:
        raise RuntimeBlocked("pinned manifest does not match the pin's hash")
    code, failures = validate(manifest)
    if code != OK:
        items = ", ".join(str(f["item"]) for f in failures[:5])
        raise RuntimeBlocked(f"pinned install fails validation: {items}")
    if read_json(manifest)["revision"] != pin["revision"]:
        raise RuntimeBlocked("pinned manifest names another revision")
    return {
        "install": str(install),
        "revision": pin["revision"],
        "manifest": str(manifest),
    }


def mode(env: Mapping[str, str] | None = None) -> str:
    """How a tick entry may run: "pinned" (started by the launcher) or
    "legacy" (explicit, only while pinned mode was never configured)."""
    values = os.environ if env is None else env
    where = home(values)
    if values.get("EPIC_RUNTIME_ROOT"):
        if values.get("EPIC_RUNTIME_LEGACY"):
            raise RuntimeBlocked(
                "EPIC_RUNTIME_LEGACY and EPIC_RUNTIME_ROOT are both set"
            )
        return "pinned"
    if values.get("EPIC_RUNTIME_LEGACY") == "1":
        if configured(where):
            raise RuntimeBlocked(
                f"legacy mode is refused: pinned mode is configured in {where}"
            )
        return "legacy"
    raise RuntimeBlocked(
        "no runtime: start through the epic-tick launcher, or set EPIC_RUNTIME_LEGACY=1"
    )


# --- promotion marker -------------------------------------------------------


def read_marker() -> dict[str, Any] | None:
    """The marker, None when there is none. Unreadable: raises (fail closed)."""
    path = marker_path()
    try:
        data = read_json(path)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise RuntimeBlocked(f"promotion marker unreadable: {error}") from None
    if not isinstance(data, dict) or not isinstance(data.get("admitted"), list):
        raise RuntimeBlocked("promotion marker is invalid")
    return data


def begin_promotion(candidate: str, previous: str | None) -> dict[str, Any]:
    """Write the marker (phase prepared), then snapshot the admitted ticks.

    The marker exists before the snapshot is read. A tick writes its record
    before it checks the marker, so every tick that passed the check is in
    the snapshot. Refuses when a marker exists.
    """
    path = marker_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    marker = {
        "schema": SCHEMA,
        "promotion_id": secrets.token_hex(8),
        "candidate": candidate,
        "previous": previous,
        "phase": "prepared",
        "started_at": now_iso(),
        "admitted": [],
    }
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise RuntimeBlocked(
            "a promotion marker exists; run recover <promotion_id>"
        ) from None
    with os.fdopen(fd, "w") as stream:
        stream.write(json.dumps(marker, indent=1) + "\n")
    marker["admitted"] = admission_records()
    write_json(path, marker)
    return marker


def set_phase(promotion_id: str, phase: str) -> dict[str, Any]:
    if phase not in PHASES:
        raise ValueError(f"unknown phase {phase}")
    marker = owned_marker(promotion_id)
    marker["phase"] = phase
    write_json(marker_path(), marker)
    return marker


def clear_marker(promotion_id: str) -> None:
    """Callers run this only after both smoke checks passed."""
    owned_marker(promotion_id)
    marker_path().unlink()


def owned_marker(promotion_id: str) -> dict[str, Any]:
    marker = read_marker()
    if marker is None:
        raise RuntimeBlocked("no promotion marker")
    if marker.get("promotion_id") != promotion_id:
        raise RuntimeBlocked(
            f"the marker belongs to promotion {marker.get('promotion_id')}"
        )
    return marker


# --- processes --------------------------------------------------------------


def process_table() -> dict[int, tuple[int, str]]:
    """pid -> (ppid, normalised start time). `ps` pads lstart with spaces."""
    out = subprocess.run(
        ["/bin/ps", "-A", "-o", "pid=,ppid=,lstart="],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    ).stdout
    table: dict[int, tuple[int, str]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = (int(parts[1]), " ".join(parts[2:]))
    return table


def ancestors(pid: int, table: Mapping[int, tuple[int, str]]) -> list[tuple[int, str]]:
    """(pid, start) of `pid` and every ancestor that is still alive."""
    chain: list[tuple[int, str]] = []
    seen: set[int] = set()
    while pid in table and pid not in seen and pid > 0:
        seen.add(pid)
        parent, start = table[pid]
        chain.append((pid, start))
        pid = parent
    return chain


def process_entry(pid: int, table: Mapping[int, tuple[int, str]]) -> dict[str, Any]:
    if pid not in table:
        raise RuntimeBlocked(f"process {pid} is not running")
    return {"pid": pid, "start": table[pid][1]}


# --- admission --------------------------------------------------------------


def admission_records() -> list[dict[str, Any]]:
    found = []
    folder = admitted_dir()
    if not folder.is_dir():
        return found
    for path in sorted(folder.glob("*.json")):
        try:
            record = read_json(path)
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and isinstance(record.get("processes"), list):
            found.append(record)
    return found


def prune(table: Mapping[int, tuple[int, str]]) -> None:
    """Remove records of ticks that died without cleanup (SIGKILL, reboot).

    Housekeeping only: a dead tick's pid and start time match no live
    ancestor, so its record never admits anything.
    """
    folder = admitted_dir()
    if not folder.is_dir():
        return
    for path in folder.glob("*.json"):
        try:
            record = read_json(path)
            processes = record["processes"]
            alive = any(
                table.get(p["pid"], (0, ""))[1] == " ".join(str(p["start"]).split())
                for p in processes
            )
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if not alive:
            path.unlink(missing_ok=True)


def admitted_caller(
    marker: Mapping[str, Any], pid: int, table: Mapping[int, tuple[int, str]]
) -> bool:
    """One of the caller's ancestors is an admitted tick of the snapshot,
    by pid and start time. The environment never counts."""
    admitted = {
        (p.get("pid"), " ".join(str(p.get("start", "")).split()))
        for record in marker.get("admitted", [])
        if isinstance(record, dict)
        for p in record.get("processes", [])
        if isinstance(p, dict)
    }
    return any(
        (p, " ".join(start.split())) in admitted for p, start in ancestors(pid, table)
    )


def write_barrier(pid: int | None = None) -> str | None:
    """None to allow a write, else why it is refused.

    No marker: allowed. Marker: only callers inside a tick that was admitted
    before the promotion started. Unreadable marker or process table: refused.
    """
    try:
        marker = read_marker()
    except RuntimeBlocked as error:
        return f"runtime promotion: {error}"
    if marker is None:
        return None
    try:
        table = process_table()
    except (OSError, subprocess.SubprocessError) as error:
        return f"runtime promotion in progress and the process table is unreadable: {error}"
    if admitted_caller(marker, os.getpid() if pid is None else pid, table):
        return None
    return (
        f"runtime promotion {marker.get('promotion_id')} in progress: "
        "writes outside an admitted tick are refused"
    )


def lock_shared(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def admit(fd: int, tick_id: str, agent: str, pid: int) -> str | None:
    """None when admitted, else why not. The caller opened `fd` on runtime.lock.

    Order: LOCK_SH, admission record, then the marker check. On refusal the
    record is removed; the caller closes `fd`, which frees the lock.
    """
    if not TICK_ID.fullmatch(tick_id):
        raise ValueError(f"bad tick id {tick_id!r}")
    if not lock_shared(fd):
        return "runtime promotion holds the admission lock"
    table = process_table()
    prune(table)
    record = {
        "tick_id": tick_id,
        "agent": agent,
        "revision": os.environ.get("EPIC_RUNTIME_REVISION") or None,
        "processes": [process_entry(pid, table)],
    }
    path = admitted_dir() / f"{tick_id}.json"
    write_json(path, record)
    try:
        marker = read_marker()
    except RuntimeBlocked as error:
        path.unlink(missing_ok=True)
        return str(error)
    if marker is not None:
        path.unlink(missing_ok=True)
        return f"runtime promotion {marker.get('promotion_id')} in progress"
    return None


def release(tick_id: str) -> None:
    if not TICK_ID.fullmatch(tick_id):
        raise ValueError(f"bad tick id {tick_id!r}")
    (admitted_dir() / f"{tick_id}.json").unlink(missing_ok=True)


def hold(fd: int) -> str | None:
    """Command prefix step: LOCK_SH on `fd` (inherited by the command), then
    the write barrier. None to run the command."""
    if not lock_shared(fd):
        return "runtime promotion holds the admission lock"
    return write_barrier(os.getppid())


# --- command line -----------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate").add_argument("--manifest", required=True, type=Path)
    sub.add_parser("resolve")
    sub.add_parser("mode")
    p = sub.add_parser("admit")
    p.add_argument("--fd", required=True, type=int)
    p.add_argument("--tick-id", required=True)
    p.add_argument("--agent", required=True, choices=AGENTS)
    p.add_argument("--pid", type=int)
    sub.add_parser("release").add_argument("--tick-id", required=True)
    sub.add_parser("hold").add_argument("--fd", required=True, type=int)
    sub.add_parser("barrier")
    try:
        args = parser.parse_args(argv)
    except SystemExit as error:
        return UNREADABLE if error.code else OK
    try:
        return run(args)
    except RuntimeBlocked as error:
        print(f"runtime: {error}", file=sys.stderr)
        return BLOCKED
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"runtime: {error}", file=sys.stderr)
        return UNREADABLE


def run(args: argparse.Namespace) -> int:
    if args.command == "validate":
        code, failures = validate(args.manifest)
        print(json.dumps({"ok": code == OK, "failures": failures}))
        return code
    if args.command == "resolve":
        print(json.dumps(resolve(home())))
        return OK
    if args.command == "mode":
        print(mode())
        return OK
    if args.command == "admit":
        refused = admit(args.fd, args.tick_id, args.agent, args.pid or os.getppid())
        if refused:
            print(f"runtime: {refused}", file=sys.stderr)
            return BUSY
        return OK
    if args.command == "release":
        release(args.tick_id)
        return OK
    if args.command == "hold":
        refused = hold(args.fd)
        if refused:
            print(f"runtime: {refused}", file=sys.stderr)
            return UNREADABLE
        return OK
    refused = write_barrier(os.getppid())
    if refused:
        print(f"runtime: {refused}", file=sys.stderr)
        return MISMATCH
    return OK


if __name__ == "__main__":
    sys.exit(main())
