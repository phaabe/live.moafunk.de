"""Route an epic item without an owner to Claude, Codex or Anton.

Pure functions, no GitHub calls. Rules (issue 487, "Routing"):
  1. An existing Executor (project field or PR line) always wins.
  2. Otherwise each file's owner comes from `file_rules` in .github/epic-lanes.yml
     (first matching pattern, as in scripts/epic_guard/check.py).
  3. One agent owns every file -> that agent. Otherwise -> needs-anton.
  4. No files known yet (an issue before refinement) -> Claude, for `refine` only.
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LANES_FILE = Path(__file__).resolve().parents[2] / ".github" / "epic-lanes.yml"
NEEDS_ANTON = "needs-anton"
REFINER = "Claude"


@dataclass(frozen=True)
class Route:
    """`agent` is Claude, Codex, or None when nobody may act (see `reason`)."""

    agent: str | None
    reason: str
    lane: str | None = None


def load_rules(path: Path = LANES_FILE) -> list[dict[str, Any]]:
    """The file rules of the lane map. The .yml file is plain JSON."""
    return json.loads(path.read_text())["file_rules"]


def rule_for(rules: list[dict[str, Any]], path: str) -> dict[str, Any] | None:
    return next(
        (r for r in rules if fnmatch.fnmatchcase(path, r["pattern"])),
        None,
    )


def route(
    executor: str | None,
    files: list[str] | None,
    rules: list[dict[str, Any]],
    action: str,
) -> Route:
    """Who may do `action` on an item with this Executor and these files."""
    if executor:
        return Route(executor, f"Executor is {executor}")
    if not files:
        if action == "refine":
            return Route(REFINER, "no files known yet; Claude refines")
        return Route(None, f"{NEEDS_ANTON}: no files known yet")
    owners: set[str] | None = None
    lanes: list[str] | None = None
    for path in sorted(files):
        rule = rule_for(rules, path)
        if rule is None:
            return Route(None, f"{NEEDS_ANTON}: {path} has no owner in epic-lanes.yml")
        owners = set(rule["owners"]) if owners is None else owners & set(rule["owners"])
        lanes = (
            list(rule["lanes"])
            if lanes is None
            else [lane for lane in lanes if lane in rule["lanes"]]
        )
    if not owners or len(owners) > 1:
        return Route(None, f"{NEEDS_ANTON}: files have different owners")
    (agent,) = owners
    if not lanes:
        return Route(None, f"{NEEDS_ANTON}: files are in different lanes")
    return Route(agent, f"all files owned by {agent}", lanes[0])
