"""Claude part of the runtime smoke check (smoke.py --agent claude).

`check(install, manifest)` reads and hashes only: no model, hook or MCP server
starts. smoke.py calls it only after the manifest validated. Each failure is
one line; an empty list passes.

The kept settings come from https://github.com/phaabe/live.moafunk.de/issues/585
and Anton's decisions in https://github.com/phaabe/live.moafunk.de/issues/584.
The expected rules are built here, not read from the runner settings, so a rule
missing there fails: the runner's own ask rules, the project's ask and deny
rules from the install's .claude/settings.json, and the user-only rules.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

EXECUTABLES = ("claude", "python3", "gtimeout", "gitnexus")
SETTINGS = {
    "claude_runner_settings": "scripts/epic/claude-runner-settings.json",
    "claude_mcp_config": "scripts/epic/claude-mcp-config.json",
}
PREFIX = "scripts/epic/lockhold"
PROJECT_SETTINGS = ".claude/settings.json"
HOOK_ROOT = "$EPIC_RUNTIME_ROOT/"
RUNNER_ASK = ["Bash(git push:*)", "Bash(git rebase:*)", "Bash(git -*)"]
USER_ASK = [
    "Bash(brew install:*)",
    "Bash(brew uninstall:*)",
    "Bash(docker volume rm:*)",
    "Bash(kubectl delete:*)",
    "Bash(npm publish:*)",
    "Bash(pip install:*)",
    "Bash(pnpm publish:*)",
    "Bash(terraform apply:*)",
    "Bash(terraform destroy:*)",
    "Bash(yarn publish:*)",
]
USER_DENY = [
    "Bash(dd if=/dev/zero of=/dev/sda*)",
    "Bash(git push --force origin master)",
    "Bash(git push -f origin master)",
    "Bash(rm -rf /Users/*)",
    "Edit(~/.aws/credentials)",
    "Edit(~/.gnupg/**)",
    "Edit(~/.ssh/**)",
    "Edit(/etc/**)",
    "Read(~/.aws/config)",
    "Read(~/.aws/credentials)",
    "Read(~/.docker/config.json)",
    "Read(~/.gnupg/**)",
    "Read(~/.netrc)",
    "Read(~/.secrets)",
    "Read(~/.ssh/**)",
    "Read(/etc/shadow)",
    "Read(/etc/sudoers)",
]
# (event, matcher, script under .claude/hooks/scripts/)
HOOKS = (
    ("PreToolUse", "Bash", "block-dangerous.sh"),
    ("PreToolUse", "Bash", "branch-guard.sh"),
    ("PreToolUse", "Bash", "merge-guard.sh"),
    ("PreToolUse", "Bash", "epic-guard.sh"),
    ("PreToolUse", "Bash", "gh-watch-guard.py"),
    ("PreToolUse", "Bash", "nonascii-bash-guard.sh"),
    ("PreToolUse", "mcp__github__.*", "epic-guard.sh"),
    ("PreToolUse", "Write|Edit|MultiEdit", "nonascii-guard.sh"),
    ("PostToolUse", "Write|Edit|MultiEdit", "auto-format.sh"),
    ("PostToolUse", "Write|Edit|MultiEdit", "auto-lint.sh"),
)
PROJECT_ENV = ("BASH_DEFAULT_TIMEOUT_MS", "BASH_MAX_TIMEOUT_MS", "MCP_TIMEOUT")


def check(install: Path, manifest: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    executables = manifest.get("executables") or {}
    for name in EXECUTABLES:
        if name not in executables:
            failures.append(f"the manifest names no {name} executable")
    claude = Path((executables.get("claude") or {}).get("path") or "")
    if "claude" in executables and not claude.is_file():
        failures.append(f"the claude executable {claude} is missing")
    for name, rel in SETTINGS.items():
        if name not in (manifest.get("settings") or {}):
            failures.append(f"the manifest does not bind {name} ({rel})")
    files = manifest.get("files") or {}
    if PREFIX not in files:
        failures.append(f"the prefix {PREFIX} is not in the manifest")
    elif not os.access(install / PREFIX, os.X_OK):
        failures.append(f"the prefix {PREFIX} is not executable")
    try:
        settings = json.loads(
            (install / SETTINGS["claude_runner_settings"]).read_text()
        )
        project = json.loads((install / PROJECT_SETTINGS).read_text())
        mcp = json.loads((install / SETTINGS["claude_mcp_config"]).read_text())
    except (OSError, ValueError) as error:
        return [*failures, f"settings unreadable: {error}"]
    failures += hook_failures(settings, files)
    failures += rule_failures(settings, project)
    env = settings.get("env") or {}
    for name in PROJECT_ENV:
        if env.get(name) != (project.get("env") or {}).get(name):
            failures.append(f"env {name} differs from the project settings")
    if env.get("DISABLE_TELEMETRY") != "1":
        failures.append("env DISABLE_TELEMETRY is not 1")
    if settings.get("includeCoAuthoredBy") is not False:
        failures.append("includeCoAuthoredBy is not false")
    servers = mcp.get("mcpServers") or {}
    gitnexus = (executables.get("gitnexus") or {}).get("path")
    if set(servers) != {"gitnexus"}:
        failures.append(
            f"the MCP config must name only gitnexus, not {sorted(servers)}"
        )
    elif servers["gitnexus"].get("command") != gitnexus:
        failures.append("the MCP config does not run the manifest's gitnexus")
    return failures


def hook_failures(settings: dict[str, Any], files: dict[str, str]) -> list[str]:
    failures: list[str] = []
    found: set[tuple[str, str, str]] = set()
    for event, entries in (settings.get("hooks") or {}).items():
        for entry in entries:
            for hook in entry.get("hooks") or []:
                command = str(hook.get("command") or "")
                rel = command.strip('"').removeprefix(HOOK_ROOT)
                if not command.startswith(f'"{HOOK_ROOT}') or rel not in files:
                    failures.append(
                        f"hook {command} is not a manifest file under the runtime root"
                    )
                    continue
                found.add((event, entry.get("matcher"), Path(rel).name))
    for event, matcher, script in HOOKS:
        if (event, matcher, script) not in found:
            failures.append(f"missing hook {event} {matcher} {script}")
    return failures


def rule_failures(settings: dict[str, Any], project: dict[str, Any]) -> list[str]:
    rules = settings.get("permissions") or {}
    wanted = {
        "ask": RUNNER_ASK
        + (project.get("permissions") or {}).get("ask", [])
        + USER_ASK,
        "deny": (project.get("permissions") or {}).get("deny", []) + USER_DENY,
    }
    return [
        f"missing {kind} rule {rule}"
        for kind, expected in wanted.items()
        for rule in expected
        if rule not in rules.get(kind, [])
    ]
