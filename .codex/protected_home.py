"""Read-only bootstrap checks for the dedicated Codex runner home.

Only the standard library loads until root and protected file bindings pass.
The operator supplies EPIC_CODEX_PROTECTED_CONFIG; this module never installs
settings, reads auth, or makes an API request.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

BLOCKED = 78
SETTINGS = {
    "codex_runner_config": ".codex/runtime/config.toml",
    "codex_hooks": ".codex/runtime/hooks.json",
    "codex_feature_git_rules": ".codex/runtime/rules/codex-feature-git.rules",
    "codex_cleanup_git_rules": ".codex/runtime/rules/codex-cleanup-git.rules",
    "codex_result_schema": ".codex/tick-result.schema.json",
}
HOME_FILES = (
    "config.toml",
    "hooks.json",
    "rules/codex-feature-git.rules",
    "rules/codex-cleanup-git.rules",
)
FOUNDATION_FILES = (
    ".codex/protected_home.py",
    ".codex/hooks/scripts/epic-guard.sh",
    ".codex/hooks/scripts/epic_guard.py",
)


SOURCE_PROFILE = "epic-source-edit"
SOURCE_PATHS = (".codex/epic_lock.py", ".codex/tests", ".codex/README.md")


def source_permissions(writable: list[Path], network: bool) -> dict[str, Any]:
    """Exact opt-in policy; active code and the dedicated home stay outside roots."""
    return {
        SOURCE_PROFILE: {
            "extends": ":workspace",
            "filesystem": {
                ":slash_tmp": "read",
                ":tmpdir": "read",
                **{str(path): "write" for path in writable[:2]},
                ":workspace_roots": {
                    ".codex": "read",
                    **{path: "write" for path in SOURCE_PATHS},
                },
            },
            "network": {"enabled": network},
        }
    }


class ProtectedHomeError(ValueError):
    """The operator binding does not describe a protected runner."""


def canonical(value: object, label: str, *, exists: bool = True) -> Path:
    if not isinstance(value, str) or not value or not os.path.isabs(value):
        raise ProtectedHomeError(f"{label} must be an absolute path")
    path = Path(value)
    resolved = path.resolve(strict=exists)
    if str(resolved) != value:
        raise ProtectedHomeError(f"{label} must be canonical, without aliases: {value}")
    return resolved


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def object_file(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ProtectedHomeError(f"{path} must contain an object")
    return value


def hook_hash(group: dict[str, Any], hook: dict[str, Any]) -> str:
    """Native PreToolUse command identity (Codex hooks/list currentHash).

    Codex hashes normalized TOML as sorted, compact JSON. Keep this narrow:
    other hook events and async handlers are not runner foundation hooks.
    The native control compares this value with the real hooks/list result.
    """
    allowed = {
        "type",
        "command",
        "timeout",
        "async",
        "statusMessage",
        "additionalContextLimit",
    }
    if (
        set(group) - {"matcher", "hooks"}
        or set(hook) - allowed
        or hook.get("async", False) is not False
    ):
        raise ProtectedHomeError("unsupported protected hook fields or async handler")
    timeout = hook.get("timeout", 600)
    if type(timeout) is not int:
        raise ProtectedHomeError("hook timeout must be an integer")
    normalized = {
        "type": hook["type"],
        "command": hook["command"],
        "async": False,
        "timeout": max(1, timeout),
    }
    if "statusMessage" in hook:
        normalized["statusMessage"] = hook["statusMessage"]
    if "additionalContextLimit" in hook and hook["additionalContextLimit"] != 2500:
        normalized["additionalContextLimit"] = hook["additionalContextLimit"]
    identity = {
        "event_name": "pre_tool_use",
        "matcher": group["matcher"],
        "hooks": [normalized],
    }
    serialized = json.dumps(
        identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return "sha256:" + hashlib.sha256(serialized).hexdigest()


def within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def protected(path: Path, writable: list[Path]) -> None:
    # A writable ancestor can replace the whole protected directory. A
    # writable descendant is fine (Git metadata below the code checkout).
    if any(within(path, root) or within(path.parent, root) for root in writable):
        raise ProtectedHomeError(
            f"protected path or replacement parent is writable: {path}"
        )


def owner_only(path: Path) -> None:
    for item in (path, *path.parents):
        mode = item.stat().st_mode
        if mode & 0o022 and not (mode & stat.S_ISVTX):
            raise ProtectedHomeError(
                f"protected path has a group/world writable ancestor: {item}"
            )
    if path.stat().st_uid != os.getuid():
        raise ProtectedHomeError(
            f"protected path is not owned by this operator: {path}"
        )


def git(repo: Path, *args: str, executable: str = "git") -> str:
    # Avoid inherited -c / alternate repository selectors. The effective
    # origin URLs still include Git's configured insteadOf/pushInsteadOf.
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    result = subprocess.run(
        [executable, "-C", str(repo), *args],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise ProtectedHomeError(f"Git root check failed: {result.stderr.strip()}")
    return result.stdout.rstrip("\n")


def runner_configs(repo: Path, git_binary: str = "git") -> None:
    # Include ignored files. Git's untracked listing alone misses precisely
    # the ignored project layers this preflight has to refuse.
    tracked = set(git(repo, "ls-files", "-z", executable=git_binary).split("\0"))

    def unreadable(error: OSError) -> None:
        raise error

    for base, directories, files in os.walk(
        repo, followlinks=False, onerror=unreadable
    ):
        directories[:] = [name for name in directories if name != ".git"]
        here = Path(base)
        if ".codex" in directories and (here / ".codex").is_symlink():
            raise ProtectedHomeError(
                f"aliased runner Codex directory: {here / '.codex'}"
            )
        if here.name != ".codex":
            continue
        config = here / "config.toml"
        if config.exists() or config.is_symlink():
            relative = config.relative_to(repo).as_posix()
            if relative not in tracked:
                raise ProtectedHomeError(
                    f"untracked or ignored runner Codex config: {relative}"
                )
            if config.is_symlink():
                raise ProtectedHomeError(f"aliased runner Codex config: {relative}")
        # A symlink to a directory could hide an ignored config layer.
        for name in directories:
            if (here / name).is_symlink():
                raise ProtectedHomeError(
                    f"aliased runner Codex directory: {here / name}"
                )


def checked_manifest(code: Path, values: Mapping[str, str]) -> dict[str, Any]:
    path = canonical(values.get("EPIC_RUNTIME_MANIFEST"), "EPIC_RUNTIME_MANIFEST")
    if path != code / "runtime-manifest.json":
        raise ProtectedHomeError("manifest is not in the bound runtime")
    runtime_home = canonical(values.get("EPIC_RUNTIME_HOME"), "EPIC_RUNTIME_HOME")
    pin_path = canonical(str(runtime_home / "pin.json"), "runtime pin")
    owner_only(pin_path)
    pin = object_file(pin_path)
    revision = values.get("EPIC_RUNTIME_REVISION", "")
    if (
        not re.fullmatch(r"[0-9a-f]{40}", revision)
        or pin.get("revision") != revision
        or pin.get("manifest_sha256") != digest(path)
        or code != runtime_home / "revisions" / revision
    ):
        raise ProtectedHomeError("runtime manifest does not match the protected pin")
    manifest = object_file(path)
    if manifest.get("schema") != 1 or manifest.get("contract") != 1:
        raise ProtectedHomeError("unsupported runtime manifest")
    files = manifest.get("files", {})
    settings = manifest.get("settings", {})
    if not isinstance(files, dict) or not isinstance(settings, dict):
        raise ProtectedHomeError("runtime file and setting hashes are missing")
    seen: set[str] = set()

    def unreadable(error: OSError) -> None:
        raise error

    for base, directories, filenames in os.walk(code, onerror=unreadable):
        for name in (".", *directories, *filenames):
            item = Path(base) / name
            item_stat = item.lstat()
            if item_stat.st_mode & 0o222 or not (
                stat.S_ISREG(item_stat.st_mode) or stat.S_ISDIR(item_stat.st_mode)
            ):
                raise ProtectedHomeError(f"runtime is not sealed: {item}")
            if item.is_file() and item != path:
                relative = item.relative_to(code).as_posix()
                seen.add(relative)
                if files.get(relative) != digest(item):
                    raise ProtectedHomeError(
                        f"missing or changed runtime file: {relative}"
                    )
    if seen != set(files):
        raise ProtectedHomeError("runtime manifest file inventory differs")
    executables = manifest.get("executables", {})
    if not isinstance(executables, dict) or not {
        "python3",
        "git",
        "gtimeout",
        "gitnexus",
    }.issubset(executables):
        raise ProtectedHomeError("foundation executable identities are missing")
    for name, entry in executables.items():
        executable = entry.get("path") if isinstance(entry, dict) else None
        executable_path = canonical(executable, f"executable {name}")
        if digest(executable_path) != entry.get("sha256"):
            raise ProtectedHomeError(f"missing or changed executable: {name}")
    for name, relative in SETTINGS.items():
        actual = digest(code / relative)
        if settings.get(name) != actual or files.get(relative) != actual:
            raise ProtectedHomeError(f"missing or changed foundation setting: {name}")
    for relative in (
        ".codex/protected_home.py",
        ".codex/codex-tick.sh",
        ".codex/hooks/scripts/epic_guard.py",
        ".codex/hooks/scripts/epic-guard.sh",
        "scripts/epic/runtime.py",
        "scripts/epic/write_checks.py",
    ):
        if files.get(relative) != digest(code / relative):
            raise ProtectedHomeError(f"missing or changed foundation code: {relative}")
    if manifest.get("revision") != values.get("EPIC_RUNTIME_REVISION"):
        raise ProtectedHomeError("runtime revision does not match its manifest")
    return manifest


def validate(
    mode: str,
    repo_root: Path,
    code_root: Path,
    env: Mapping[str, str] | None = None,
    *,
    model_root: Path | None = None,
    temp_dir: Path | None = None,
    extra_write_dirs: tuple[Path, ...] = (),
) -> dict[str, Any]:
    values = os.environ if env is None else env
    if mode not in ("legacy", "pinned"):
        raise ProtectedHomeError("unknown runtime mode")
    if mode == "legacy":
        if values.get("EPIC_RUNTIME_LEGACY") != "1" or values.get("EPIC_RUNTIME_ROOT"):
            raise ProtectedHomeError("legacy mode needs explicit EPIC_RUNTIME_LEGACY=1")
        runtime_home = Path(
            values.get(
                "EPIC_RUNTIME_HOME",
                str(Path(values.get("HOME", "")) / ".local/share/epic-runtime"),
            )
        )
        for name in ("configured.json", "pin.json"):
            try:
                (runtime_home / name).lstat()
            except FileNotFoundError:
                continue
            raise ProtectedHomeError(
                "legacy mode is disabled after runtime configuration"
            )
    elif values.get("EPIC_RUNTIME_LEGACY") == "1":
        raise ProtectedHomeError("pinned mode cannot also select legacy")
    binding = canonical(
        values.get("EPIC_CODEX_PROTECTED_CONFIG"), "EPIC_CODEX_PROTECTED_CONFIG"
    )
    owner_only(binding)
    data = object_file(binding)
    if data.get("schema") != 1:
        raise ProtectedHomeError("unsupported protected-home schema")
    repo = canonical(str(repo_root), "runner root")
    code = canonical(str(code_root), "code root")
    if mode == "legacy" and code != repo:
        raise ProtectedHomeError("legacy code must use the approved runner checkout")
    if mode == "pinned" and code != canonical(
        values.get("EPIC_RUNTIME_ROOT"), "EPIC_RUNTIME_ROOT"
    ):
        raise ProtectedHomeError("pinned code differs from EPIC_RUNTIME_ROOT")
    home = canonical(values.get("CODEX_HOME"), "CODEX_HOME")
    if repo != canonical(data.get("runner_root"), "bound runner root"):
        raise ProtectedHomeError("runner root differs from protected binding")
    if code != canonical(data.get("code_root"), "bound code root"):
        raise ProtectedHomeError("code root differs from protected binding")
    if home != canonical(data.get("codex_home"), "bound CODEX_HOME"):
        raise ProtectedHomeError("CODEX_HOME differs from protected binding")
    if home == Path(values.get("HOME", "")) / ".codex":
        raise ProtectedHomeError("personal CODEX_HOME is not a dedicated runner home")
    roots = data.get("writable_roots")
    if not isinstance(roots, dict) or set(roots) != {"git_metadata", "gitnexus"}:
        raise ProtectedHomeError(
            "writable_roots must name exact git_metadata and gitnexus roots"
        )
    writable = [canonical(roots[key], key) for key in ("git_metadata", "gitnexus")]
    for path in (binding, home, code, repo):
        protected(path, writable)
    allocation_parents = [
        canonical(data.get("temporary_parent"), "temporary_parent"),
        canonical(data.get("review_parent"), "review_parent"),
        *(
            canonical(value, "worktree parent")
            for value in data.get("worktree_parents", [])
        ),
    ]
    for root in writable:
        if any(
            within(parent, root) or within(root, parent)
            for parent in allocation_parents
        ):
            raise ProtectedHomeError(
                f"static writable root overlaps allocation parent: {root}"
            )
    if mode == "pinned":
        protected(
            canonical(values.get("EPIC_RUNTIME_HOME"), "EPIC_RUNTIME_HOME")
            / "pin.json",
            writable,
        )
    manifest = checked_manifest(code, values) if mode == "pinned" else {}
    for entry in manifest.get("executables", {}).values():
        protected(Path(entry["path"]).resolve(), writable)
    git_binary = manifest.get("executables", {}).get("git", {}).get("path", "git")
    if (
        canonical(
            git(repo, "rev-parse", "--show-toplevel", executable=git_binary), "Git root"
        )
        != repo
    ):
        raise ProtectedHomeError("runner root is not a Git checkout root")
    origins = data.get("allowed_origin_urls")
    if (
        not isinstance(origins, list)
        or not origins
        or not all(isinstance(x, str) and x for x in origins)
    ):
        raise ProtectedHomeError("allowed_origin_urls must be nonempty")
    for args in (
        ("remote", "get-url", "--all", "origin"),
        ("remote", "get-url", "--push", "--all", "origin"),
    ):
        urls = git(repo, *args, executable=git_binary).splitlines()
        if len(urls) != 1 or any(url not in origins for url in urls):
            raise ProtectedHomeError("effective Git origin URL is not approved")
    runner_configs(repo, git_binary)
    metadata = Path(git(repo, "rev-parse", "--git-common-dir", executable=git_binary))
    if not metadata.is_absolute():
        metadata = repo / metadata
    if writable[0] != metadata.resolve():
        raise ProtectedHomeError(
            "Git writable root is not the checkout's exact metadata directory"
        )
    temporary_parent = canonical(data.get("temporary_parent"), "temporary_parent")
    review_parent = canonical(data.get("review_parent"), "review_parent")
    if temp_dir is not None:
        temp = canonical(str(temp_dir), "tick temporary directory")
        if temp.parent != temporary_parent or not re.fullmatch(
            r"codex-tick-[A-Za-z0-9._-]+", temp.name
        ):
            raise ProtectedHomeError(
                "temporary directory is not an exact allocated tick root"
            )
        writable.append(temp)
    for extra in extra_write_dirs:
        extra = canonical(str(extra), "extra writable directory")
        relative = extra.relative_to(review_parent)
        if (
            len(relative.parts) != 2
            or not relative.parts[0].isdigit()
            or not re.fullmatch(r"[0-9a-f]{40}", relative.parts[1])
        ):
            raise ProtectedHomeError(
                "review evidence root must be <review_parent>/<PR>/<SHA>"
            )
        writable.append(extra)
    if model_root is not None:
        model = canonical(str(model_root), "model working directory")
        if model != temp_dir:
            parents = data.get("worktree_parents", [])
            if not parents or not any(
                within(model, canonical(item, "worktree parent")) for item in parents
            ):
                raise ProtectedHomeError(
                    "model working directory is not an approved worktree"
                )
            if (
                canonical(
                    git(model, "rev-parse", "--show-toplevel", executable=git_binary),
                    "model Git root",
                )
                != model
            ):
                raise ProtectedHomeError(
                    "model working directory must be a worktree root"
                )
            common = Path(
                git(model, "rev-parse", "--git-common-dir", executable=git_binary)
            )
            if not common.is_absolute():
                common = model / common
            if common.resolve() != writable[0]:
                raise ProtectedHomeError("model worktree belongs to another repository")
        writable.append(model)
    for root in writable:
        if root in (
            Path("/"),
            Path("/tmp").resolve(),
            Path(values.get("HOME", "/")),
            home,
        ):
            raise ProtectedHomeError(f"blanket writable root: {root}")
        if within(root, home) or (
            root != writable[0] and (within(root, code) or within(root, repo))
        ):
            raise ProtectedHomeError(
                f"writable root overlaps protected code or home: {root}"
            )
    for path in (binding, home, code, repo):
        protected(path, writable)
    owner_only(home)
    files = data.get("files")
    if not isinstance(files, dict) or not files:
        raise ProtectedHomeError("protected file hashes are missing")
    for relative in HOME_FILES:
        if str(home / relative) not in files:
            raise ProtectedHomeError(f"protected file binding is missing: {relative}")
    for relative in FOUNDATION_FILES:
        if str(code / relative) not in files:
            raise ProtectedHomeError(f"foundation code binding is missing: {relative}")
    for filename, expected in files.items():
        path = canonical(filename, "protected file")
        protected(path, writable)
        owner_only(path)
        if (
            not path.is_file()
            or not isinstance(expected, str)
            or digest(path) != expected
        ):
            raise ProtectedHomeError(f"protected file changed: {path}")
    # Unexpected user-layer files can introduce rules, plugins or extra hooks.
    expected_rules = {"codex-feature-git.rules", "codex-cleanup-git.rules"}
    if {p.name for p in (home / "rules").iterdir()} != expected_rules:
        raise ProtectedHomeError("unexpected protected-home rule file")
    for name in ("AGENTS.md", "AGENTS.override.md", "plugins", "managed_config.toml"):
        if (home / name).exists() or (home / name).is_symlink():
            raise ProtectedHomeError(f"unexpected protected-home settings: {name}")
    config = tomllib.loads((home / "config.toml").read_text())
    if set(config) & {"profile", "profiles", "sandbox_permissions"}:
        raise ProtectedHomeError(
            "alternate permission or profile settings are not allowed"
        )
    if config.get("approval_policy") != "never":
        raise ProtectedHomeError("protected config must use approval never")
    network_access = data.get("network_access")
    if type(network_access) is not bool:
        raise ProtectedHomeError("protected network_access policy is missing")
    permission_profile = config.get("default_permissions")
    if "permissions" in config or "default_permissions" in config:
        if (
            permission_profile != SOURCE_PROFILE
            or config.get("permissions") != source_permissions(writable, network_access)
            or set(config) & {"sandbox_mode", "sandbox_workspace_write"}
        ):
            raise ProtectedHomeError(
                "alternate permission profile differs from protected allowlist"
            )
        # Native permissions resolve paths at startup. Never let a source
        # alias turn an exact source grant into a write to protected state.
        for root in writable[2:]:
            for relative in SOURCE_PATHS:
                source = root / relative
                if source.resolve() != source:
                    raise ProtectedHomeError("source permission path is aliased")
                entries = (source, *source.rglob("*")) if source.is_dir() else (source,)
                for path in entries:
                    if path.is_symlink() or (
                        path.is_file() and path.stat().st_nlink != 1
                    ):
                        raise ProtectedHomeError(
                            "source permission path contains an alias"
                        )
    else:
        if config.get("sandbox_mode") != "workspace-write":
            raise ProtectedHomeError(
                "protected config must use workspace-write and approval never"
            )
        sandbox = config.get("sandbox_workspace_write", {})
        if sandbox.get("writable_roots") != [str(p) for p in writable[:2]]:
            raise ProtectedHomeError(
                "config writable roots differ from protected allowlist"
            )
        if any(
            sandbox.get(key) is not expected
            for key, expected in (
                ("network_access", network_access),
                ("exclude_tmpdir_env_var", True),
                ("exclude_slash_tmp", True),
            )
        ):
            raise ProtectedHomeError(
                "protected config widens network or temporary access"
            )
    projects = config.get("projects", {})
    if projects.get(str(repo), {}).get("trust_level") != "untrusted" or any(
        value.get("trust_level") != "untrusted" for value in projects.values()
    ):
        raise ProtectedHomeError("runner and project layers must remain untrusted")
    if config.get("features", {}).get("hooks") is not True:
        raise ProtectedHomeError("protected hooks are disabled")
    trust = data.get("hook_trust")
    if not isinstance(trust, dict) or not trust:
        raise ProtectedHomeError("stored hook trust hashes are missing")
    if config.get("hooks", {}).get("state") != trust or any(
        not isinstance(entry, dict)
        or entry.get("enabled", True) is not True
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(entry.get("trusted_hash", "")))
        for entry in trust.values()
    ):
        raise ProtectedHomeError(
            "protected hooks are not enabled with stored trust hashes"
        )
    hooks = object_file(home / "hooks.json").get("hooks", {})
    groups = hooks.get("PreToolUse", [])
    if set(hooks) != {"PreToolUse"} or not groups:
        raise ProtectedHomeError("protected PreToolUse hooks are missing")
    expected_trust: set[str] = set()
    for group_index, group in enumerate(groups):
        if group.get("matcher") != ".*" or not group.get("hooks"):
            raise ProtectedHomeError("protected hooks must cover every tool")
        for hook_index, hook in enumerate(group["hooks"]):
            words = shlex.split(hook.get("command", ""))
            if hook.get("type") != "command" or not words:
                raise ProtectedHomeError("protected hook command is missing")
            script = (
                words[1]
                if words[0] in ("/bin/bash", "/bin/sh") and len(words) == 2
                else words[0]
            )
            if (
                len(words) != 1
                and not (words[0] in ("/bin/bash", "/bin/sh") and len(words) == 2)
            ) or script not in files:
                raise ProtectedHomeError(
                    "protected hook must run one bound absolute script"
                )
            canonical(script, "protected hook script")
            key = f"{home}/hooks.json:pre_tool_use:{group_index}:{hook_index}"
            expected_trust.add(key)
            if key in trust and trust[key].get("trusted_hash") != hook_hash(
                group, hook
            ):
                raise ProtectedHomeError(
                    "stored trust hash does not match the native hook identity"
                )
    if set(trust) != expected_trust:
        raise ProtectedHomeError("stored trust keys do not match protected hooks")
    mcp = config.get("mcp_servers", {})
    if set(mcp) != {"gitnexus"} or mcp["gitnexus"] != data.get("gitnexus_mcp"):
        raise ProtectedHomeError(
            "MCP configuration differs from protected GitNexus binding"
        )
    if mode == "pinned":
        protected(
            canonical(values.get("EPIC_RUNTIME_HOME"), "EPIC_RUNTIME_HOME")
            / "pin.json",
            writable,
        )
        for entry in manifest["executables"].values():
            protected(Path(entry["path"]).resolve(), writable)
        for name, relative in SETTINGS.items():
            if name != "codex_result_schema":
                home_relative = relative.removeprefix(".codex/runtime/")
                if digest(home / home_relative) != manifest["settings"][name]:
                    raise ProtectedHomeError(
                        f"protected home differs from pinned setting: {name}"
                    )
        # This is the first operational import, after protected root checks.
        spec = importlib.util.spec_from_file_location(
            "codex_checked_runtime", code / "scripts/epic/runtime.py"
        )
        if spec is None or spec.loader is None:
            raise ProtectedHomeError("cannot load bound runtime validator")
        runtime = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runtime)
        status, failures = runtime.validate(code / "runtime-manifest.json")
        if status:
            raise ProtectedHomeError(f"runtime manifest validation failed: {failures}")
    return {
        "code_root": str(code),
        "repo_root": str(repo),
        "codex_home": str(home),
        "writable_roots": [str(path) for path in writable],
        "permission_profile": permission_profile,
        "temporary_parent": str(temporary_parent),
        "review_parent": str(review_parent),
        "python3": manifest.get("executables", {})
        .get("python3", {})
        .get("path", sys.executable),
        "executables": manifest.get("executables", {}),
    }


def validate_hook(code_root: Path) -> dict[str, Any]:
    marker = (
        Path(
            os.environ.get("EPIC_LOCK_DIR")
            or str(Path.home() / ".local/state/epic-loop/target-locks")
        )
        / "runtime-promotion.json"
    )
    try:
        if not stat.S_ISREG(marker.lstat().st_mode):
            raise ProtectedHomeError("promotion marker is not a regular file")
        marker.read_bytes()
    except FileNotFoundError:
        pass
    if canonical(os.environ.get("EPIC_RUNTIME_ROOT"), "EPIC_RUNTIME_ROOT") != code_root:
        raise ProtectedHomeError("hook source differs from the pinned runtime root")
    repo = canonical(os.environ.get("EPIC_TRUSTED_ROOT"), "EPIC_TRUSTED_ROOT")
    return validate("pinned", repo, code_root)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "hook-python"))
    parser.add_argument("--mode", choices=("legacy", "pinned"), default="pinned")
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--code-root", type=Path, required=True)
    parser.add_argument("--model-root", type=Path)
    parser.add_argument("--temp-dir", type=Path)
    parser.add_argument("--extra-write-dir", type=Path, action="append", default=[])
    args = parser.parse_args()
    try:
        if args.command == "hook-python":
            print(validate_hook(args.code_root)["python3"])
        else:
            if args.repo is None:
                raise ProtectedHomeError("--repo is required")
            print(
                json.dumps(
                    validate(
                        args.mode,
                        args.repo,
                        args.code_root,
                        model_root=args.model_root,
                        temp_dir=args.temp_dir,
                        extra_write_dirs=tuple(args.extra_write_dir),
                    )
                )
            )
        return 0
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        print(f"Codex protected home blocked: {error}", file=sys.stderr)
        return BLOCKED


if __name__ == "__main__":
    sys.exit(main())
