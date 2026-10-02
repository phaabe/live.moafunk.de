"""Smoke check of one runner's installed runtime. No model, no writes.

  smoke.py --agent claude|codex --manifest <install>/runtime-manifest.json

Exit 0 pass, 1 fail, 2 usage error or unreadable manifest. Prints one JSON
line: {"ok": bool, "agent": A, "failures": [...]}.

Shared part: the manifest validates (runtime.validate) and names the agent's
tick entry. Agent part: `check(install, manifest) -> list[str]` in the
install's AGENT_PARTS module, loaded only after the manifest validates.
Each adapter provides its own part; a missing part fails, it is never
skipped. The part only reads, parses and hashes: it starts no model, app
server, hook or MCP server.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import runtime

PASS, FAIL, USAGE = 0, 1, 2
AGENT_PARTS = {
    "claude": "scripts/epic/smoke_claude.py",
    "codex": ".codex/smoke_codex.py",
}


def agent_part(install: Path, agent: str, manifest: dict[str, Any]) -> list[str]:
    path = install / AGENT_PARTS[agent]
    if not path.is_file():
        return [f"{agent} smoke part {AGENT_PARTS[agent]} is missing"]
    spec = importlib.util.spec_from_file_location(f"smoke_{agent}_part", path)
    if spec is None or spec.loader is None:
        return [f"cannot load {path}"]
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        found = module.check(install, manifest)
    except (Exception, SystemExit) as error:  # a broken part fails, never passes
        return [f"{agent} smoke part failed: {error!r}"]
    if not isinstance(found, list) or not all(isinstance(f, str) for f in found):
        return [
            f"{agent} smoke part returned {type(found).__name__}, not a list of str"
        ]
    return found


def smoke(agent: str, manifest_path: Path) -> tuple[int, list[str]]:
    code, failures = runtime.validate(manifest_path)
    if code == runtime.UNREADABLE:
        return USAGE, [str(f["actual"]) for f in failures]
    if code != runtime.OK:
        # Never load an agent part from an install that failed validation.
        return FAIL, [
            f"{f['item']}: expected {f['expected']}, got {f['actual']}"
            for f in failures
        ]
    found: list[str] = []
    install = manifest_path.parent
    manifest = runtime.read_json(manifest_path)
    entry = runtime.ENTRIES[agent]
    if entry not in manifest["files"]:
        found.append(f"tick entry {entry} is not in the manifest")
    found += agent_part(install, agent, manifest)
    return (FAIL if found else PASS), found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--agent", required=True, choices=runtime.AGENTS)
    parser.add_argument("--manifest", required=True, type=Path)
    try:
        args = parser.parse_args(argv)
    except SystemExit as error:
        return USAGE if error.code else PASS
    code, failures = smoke(args.agent, args.manifest)
    print(json.dumps({"ok": code == PASS, "agent": args.agent, "failures": failures}))
    return code


if __name__ == "__main__":
    sys.exit(main())
