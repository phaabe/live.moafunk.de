"""Disposable protected-home fixtures shared by host and native controls."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

FOUNDATION_FILES = (
    ".codex/protected_home.py",
    ".codex/hooks/scripts/epic-guard.sh",
    ".codex/hooks/scripts/epic_guard.py",
)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ProtectedFixture:
    def __init__(self, base: Path, repo: Path, code_root: Path | None = None) -> None:
        self.base = base.resolve()
        self.repo = repo.resolve()
        self.code_root = (code_root or repo).resolve()
        source_root = Path(__file__).resolve().parents[2]
        for relative in FOUNDATION_FILES:
            target = self.code_root / relative
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_root / relative, target)
        self.protected = self.base / "operator"
        self.home = self.protected / "codex-home"
        self.binding = self.protected / "protected-home.json"
        self.gitnexus = self.base / "gitnexus-state"
        self.temporary_parent = self.base / "tick-temporary"
        self.review_parent = self.base / "review-evidence"
        self.worktree_parent = self.base / "worktrees"
        for path in (
            self.home / "rules",
            self.gitnexus,
            self.temporary_parent,
            self.review_parent,
            self.worktree_parent,
        ):
            path.mkdir(parents=True, exist_ok=True)
        self.git_binary = shutil.which("git")
        if self.git_binary is None:
            raise RuntimeError("Git is required for protected-home fixtures")
        metadata = self.git("rev-parse", "--git-common-dir")
        self.metadata = (self.repo / metadata).resolve()
        origins = self.git("remote", "get-url", "--all", "origin").splitlines()
        self.hook = self.protected / "guard.sh"
        self.hook.write_text("#!/bin/bash\nset -euo pipefail\nexit 0\n")
        self.hook.chmod(0o700)
        self.hooks_json: dict[str, Any] = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": ".*",
                        "hooks": [
                            {"type": "command", "command": str(self.hook), "timeout": 5}
                        ],
                    }
                ]
            }
        }
        identity = {
            "event_name": "pre_tool_use",
            "matcher": ".*",
            "hooks": [
                {
                    "type": "command",
                    "command": str(self.hook),
                    "timeout": 5,
                    "async": False,
                }
            ],
        }
        trusted_hash = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        )
        self.hook_trust = {
            f"{self.home}/hooks.json:pre_tool_use:0:0": {"trusted_hash": trusted_hash}
        }
        self.config_extra = ""
        self.data: dict[str, Any] = {
            "schema": 1,
            "runner_root": str(self.repo),
            "code_root": str(self.code_root),
            "codex_home": str(self.home),
            "allowed_origin_urls": origins,
            "writable_roots": {
                "git_metadata": str(self.metadata),
                "gitnexus": str(self.gitnexus),
            },
            "temporary_parent": str(self.temporary_parent),
            "review_parent": str(self.review_parent),
            "worktree_parents": [str(self.worktree_parent)],
            "network_access": False,
            "gitnexus_mcp": {
                "command": "/usr/bin/false",
                "args": ["mcp"],
                "enabled": False,
            },
        }
        self.env = {
            "CODEX_HOME": str(self.home),
            "EPIC_CODEX_PROTECTED_CONFIG": str(self.binding),
            "EPIC_RUNTIME_LEGACY": "1",
        }
        self.render()

    def git(self, *args: str) -> str:
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_")
        }
        result = subprocess.run(
            [str(self.git_binary), "-C", str(self.repo), *args],
            env=env,
            check=True,
            text=True,
            capture_output=True,
        )
        return result.stdout.strip()

    def render(self) -> None:
        quoted = json.dumps
        config = (
            'sandbox_mode = "workspace-write"\napproval_policy = "never"\n'
            "[features]\nhooks = true\n"
            "[sandbox_workspace_write]\n"
            f"writable_roots = {quoted(list(self.data['writable_roots'].values()))}\n"
            f"network_access = {quoted(self.data['network_access'])}\nexclude_tmpdir_env_var = true\nexclude_slash_tmp = true\n"
            f'[projects.{quoted(str(self.repo))}]\ntrust_level = "untrusted"\n'
        )
        for key, state in self.hook_trust.items():
            config += f"[hooks.state.{quoted(key)}]\n"
            for name, value in state.items():
                config += f"{name} = {quoted(value)}\n"
        config += "[mcp_servers.gitnexus]\n"
        for name, value in self.data["gitnexus_mcp"].items():
            config += f"{name} = {quoted(value)}\n"
        config += self.config_extra
        (self.home / "config.toml").write_text(config)
        (self.home / "hooks.json").write_text(json.dumps(self.hooks_json))
        for name in ("codex-feature-git.rules", "codex-cleanup-git.rules"):
            target = self.home / "rules" / name
            if not target.exists():
                target.write_text(
                    'prefix_rule(pattern = ["codex-foundation-denied"], decision = "forbidden")\n'
                )
        self.reseal()

    def reseal(self) -> None:
        self.data["hook_trust"] = self.hook_trust
        self.data["files"] = {
            str(path): sha(path)
            for path in (
                self.home / "config.toml",
                self.home / "hooks.json",
                self.home / "rules/codex-feature-git.rules",
                self.home / "rules/codex-cleanup-git.rules",
                self.hook,
                *(self.code_root / relative for relative in FOUNDATION_FILES),
            )
        }
        self.binding.write_text(json.dumps(self.data))
        self.binding.chmod(0o600)


def prepare(base: Path, repo: Path, code_root: Path | None = None) -> ProtectedFixture:
    return ProtectedFixture(base, repo, code_root)


def enable_source_profile(fixture: ProtectedFixture) -> None:
    """Operator-style migration of a disposable, already sealed home."""
    config = fixture.home / "config.toml"
    lines = []
    legacy_table = False
    for line in config.read_text().splitlines():
        if line.startswith("["):
            legacy_table = line == "[sandbox_workspace_write]"
        if not legacy_table and not line.startswith("sandbox_mode ="):
            lines.append(line)
    profile = "epic-source-edit"
    text = f'default_permissions = "{profile}"\n' + "\n".join(lines) + "\n"
    text += f'[permissions.{profile}]\nextends = ":workspace"\n'
    text += f"[permissions.{profile}.filesystem]\n"
    for path, access in (
        (":slash_tmp", "read"),
        (":tmpdir", "read"),
        (str(fixture.metadata), "write"),
        (str(fixture.gitnexus), "write"),
    ):
        text += f'{json.dumps(path)} = "{access}"\n'
    text += f'[permissions.{profile}.filesystem.":workspace_roots"]\n'
    for path, access in (
        (".codex", "read"),
        (".codex/epic_lock.py", "write"),
        (".codex/tests", "write"),
        (".codex/README.md", "write"),
    ):
        text += f'{json.dumps(path)} = "{access}"\n'
    text += f"[permissions.{profile}.network]\nenabled = {json.dumps(fixture.data['network_access'])}\n"
    config.write_text(text)
    fixture.reseal()
