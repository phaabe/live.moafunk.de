"""Build the agent Grafana dashboards: Cockpit, Agent detail, Delivery, Tickets.

Layout, colors and thresholds follow design v2 (plan appendix). No page
lists agent ids: rows and the agent picker come from `epic_agent_info`, so
a new agent appears on its own. The old per-agent pages only link on.

Run after changing this file, then commit the JSON:
    python3 scripts/epic/dashboards.py
test_dashboards.py fails when the committed JSON is out of date.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

Json = dict[str, Any]
OUT = Path(__file__).resolve().parents[2] / "tools/agent-monitoring/grafana/dashboards"
PROM = {"type": "prometheus", "uid": "agent-prometheus"}
LOKI = {"type": "loki", "uid": "agent-loki"}
REPO = "https://github.com/phaabe/live.moafunk.de"
# Only the redirect pages from the time of two fixed agents name an agent.
LEGACY_PAGES = {"claude": "Claude", "codex": "Codex"}

# Hide data whose source stopped updating instead of showing stale work.
LOCAL = " and on() (time() - epic_local_snapshot_timestamp_seconds < 30)"
GITHUB = " and on() (time() - epic_github_snapshot_timestamp_seconds < 300)"
DELIVERY = " and on() (time() - epic_delivery_snapshot_timestamp_seconds < 600)"
# Ticket data: a fresh snapshot, and the last board read worked. A failed
# read keeps the old tickets.prom, so its age alone is not enough.
BOARD_HEALTH = 'epic_ticket_source_ok{source="board"}'
TICKETS = (
    " and on() (time() - epic_ticket_snapshot_timestamp_seconds < 300)"
    f" and on() ({BOARD_HEALTH} == 1)"
)
# For ticket sparklines: the whole series only while ticket data is good now.
# Take the latest value first, then compare: a filter inside last_over_time
# would keep an older good sample for up to a minute.
TICKETS_AT_END = (
    " and on() (last_over_time((time() - epic_ticket_snapshot_timestamp_seconds)"
    "[1m:] @ end()) < 300)"
    f" and on() (({BOARD_HEALTH} @ end()) == 1)"
)
# Waits grow from cached GitHub data: only while the handoff was seen lately.
HANDOFF = " and on() (time() - epic_handoff_observed_timestamp_seconds < 300)"
# Failure counts only while the tick history is read and exported, as in
# the AgentFailingRepeatedly alert.
TRUSTED = "".join(
    f" and on(agent) epic_{name} == 1"
    for name in (
        "runner_read_success",
        "tick_ledger_read_success",
        "tick_events_read_success",
        "tick_ledger_export_success",
    )
)
FAILURES = f"(epic_tick_consecutive_failures{TRUSTED})"
# Late comes from tick timing too: cached timing after a failed read is not.
LATE = f"(epic_agent_presence == 4{TRUSTED})"
# For sparklines: the whole series only while delivery data is fresh now, so
# the "last value" reducer never shows an expired number.
FRESH_AT_END = (
    " and on() last_over_time((time() - epic_delivery_snapshot_timestamp_seconds"
    " < 600)[1m:] @ end())"
)

# Design v2 colors.
KIND = {"claude": "#E0875A", "codex": "#35C2C2"}
OUTCOME = {
    "ok": "#5AB45F",
    "blocked": "#5B8DEF",
    "timeout": "#E8B530",
    "killed": "#A57BE0",
    "interrupted": "#E5484D",
    "error": "#E5484D",
}
# Severity values from ticks.SEVERITY.
SEVERITY = {
    "ok": 1,
    "blocked": 2,
    "timeout": 3,
    "killed": 4,
    "interrupted": 5,
    "error": 6,
}
PRESENCE = {
    "running": "#5AB45F",
    "idle": "#2C2F36",
    "new": "transparent",
    "late": "#C8342E",
    "unknown": "#9A9BA6",
    "retired": "#6E717A",
}
ACT_NOW, LOOK_SOON, STALE = "#C8342E", "#E0A526", "#9A9BA6"
GREY, MUTED, EMPTY_SLOT = "#6E717A", "#9A9BA6", "#22252B"
NEUTRAL = "text"


def target(expr: str, *, table: bool = False, legend: str = "", ref: str = "A") -> Json:
    result: Json = {
        "refId": ref,
        "expr": expr,
        "instant": True,
        "range": False,
        "legendFormat": legend,
    }
    if table:
        result["format"] = "table"
    return result


def ranged(expr: str, *, legend: str = "", ref: str = "A") -> Json:
    return {**target(expr, legend=legend, ref=ref), "instant": False, "range": True}


def link(title: str, url: str) -> Json:
    return {"title": title, "url": url, "targetBlank": url.startswith("http")}


# The design's tab row, the same on every page.
PAGE_LINKS = [
    link("Cockpit", "/d/epic-agents"),
    link("Agent detail", "/d/epic-agent"),
    link("Delivery", "/d/epic-delivery"),
    link("Tickets", "/d/epic-tickets"),
    link("Epic #312 on GitHub ↗", f"{REPO}/issues/312"),
]


def steps(*pairs: tuple[float | None, str]) -> Json:
    return {
        "mode": "absolute",
        "steps": [{"value": value, "color": color} for value, color in pairs],
    }


def value_map(values: dict[str, tuple[str, str]]) -> Json:
    """Value → (text, color). Empty text keeps the value."""
    return {
        "type": "value",
        "options": {
            value: {"index": i, "color": color, **({"text": text} if text else {})}
            for i, (value, (text, color)) in enumerate(values.items())
        },
    }


def regex_map(pattern: str, color: str, index: int) -> Json:
    return {
        "type": "regex",
        "options": {"pattern": pattern, "result": {"index": index, "color": color}},
    }


def override(matcher: str, name: str, *properties: tuple[str, Any]) -> Json:
    return {
        "matcher": {"id": matcher, "options": name},
        "properties": [{"id": key, "value": value} for key, value in properties],
    }


def by_name(name: str, *properties: tuple[str, Any]) -> Json:
    return override("byName", name, *properties)


def cell(kind: str, **extra: Any) -> tuple[str, Json]:
    return ("custom.cellOptions", {"type": kind, **extra})


SEVERITY_COLORS = value_map(
    {str(v): (k, OUTCOME[k]) for k, v in SEVERITY.items() if k != "interrupted"}
    | {str(SEVERITY["interrupted"]): ("interrupted", OUTCOME["interrupted"])}
)


class Board:
    def __init__(
        self,
        uid: str,
        title: str,
        links: list[Json],
        time_from: str = "now-24h",
        refresh: str = "5s",
    ) -> None:
        self.uid, self.title, self.links, self.time_from = uid, title, links, time_from
        self.refresh = refresh
        self.panels: list[Json] = []
        self.variables: list[Json] = []
        self.ids = 0

    def place(self, panel: Json, x: int, y: int, w: int, h: int) -> Json:
        self.ids += 1
        panel["id"] = self.ids
        panel["gridPos"] = {"x": x, "y": y, "w": w, "h": h}
        return panel

    def add(self, panel: Json, x: int, y: int, w: int, h: int) -> None:
        self.panels.append(self.place(panel, x, y, w, h))

    def add_row(
        self, title: str, y: int, children: list[tuple[Json, int, int, int, int]]
    ) -> None:
        """A collapsed row; its panels open with it."""
        row = {"type": "row", "title": title, "collapsed": True}
        row = self.place(row, 0, y, 24, 1)
        row["panels"] = [self.place(child, *pos) for child, *pos in children]
        self.panels.append(row)

    def render(self) -> Json:
        return {
            "uid": self.uid,
            "title": self.title,
            "tags": ["agents", "epic"],
            "schemaVersion": 39,
            "version": 1,
            "editable": False,
            "graphTooltip": 1,
            "refresh": self.refresh,
            "timezone": "browser",
            "time": {"from": self.time_from, "to": "now"},
            "links": self.links,
            "templating": {"list": self.variables},
            "panels": self.panels,
        }


def text(content: str, title: str = "") -> Json:
    return {
        "type": "text",
        "title": title,
        "transparent": not title,
        "options": {"mode": "markdown", "content": content},
    }


def stat(
    title: str,
    targets: list[Json],
    *,
    unit: str = "none",
    mappings: list[Json] | None = None,
    thresholds: Json | None = None,
    overrides: list[Json] | None = None,
    description: str = "",
    no_value: str = "—",
    color_mode: str = "value",
    graph: str = "none",
    decimals: int | None = None,
    time_from: str | None = None,
) -> Json:
    defaults: Json = {
        "unit": unit,
        "noValue": no_value,
        "mappings": mappings or [],
        "color": {"mode": "thresholds"},
        "thresholds": thresholds or steps((None, NEUTRAL)),
    }
    if decimals is not None:
        defaults["decimals"] = decimals
    # A sparkline over a fixed window, so the last step always has data.
    window = {"timeFrom": time_from, "interval": "5m"} if time_from else {}
    return {
        **window,
        "type": "stat",
        "title": title,
        "description": description,
        "datasource": PROM,
        "targets": targets,
        "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "values": False, "fields": ""},
            "textMode": "value_and_name" if len(targets) > 1 else "value",
            "colorMode": color_mode,
            "graphMode": graph,
            "justifyMode": "center",
            "orientation": "horizontal",
            "wideLayout": True,
        },
    }


def table_panel(
    title: str,
    targets: list[Json],
    transformations: list[Json],
    overrides: list[Json],
    *,
    description: str = "",
    no_value: str = "–",
    header: bool = True,
) -> Json:
    return {
        "type": "table",
        "title": title,
        "description": description,
        "datasource": PROM,
        "targets": targets,
        "transformations": transformations,
        "fieldConfig": {
            "defaults": {
                "noValue": no_value,
                "custom": {
                    "filterable": False,
                    "align": "auto",
                    "inspect": False,
                },
                "color": {"mode": "thresholds"},
                "thresholds": steps((None, NEUTRAL)),
            },
            # Grafana adds "filter for value" buttons to each cell for
            # Prometheus data; only an override on every field removes them.
            "overrides": [
                *overrides,
                override("byRegexp", ".*", ("filterable", False)),
            ],
        },
        "options": {
            "showHeader": header,
            "cellHeight": "sm",
            "footer": {"show": False},
        },
    }


def keep_fields(
    names: list[str], rename: dict[str, str], hide: list[str]
) -> list[Json]:
    """Keep `names` in order, rename them, and exclude `hide` (sort keys)."""
    return [
        {"id": "filterFieldsByName", "options": {"include": {"names": names}}},
        {
            "id": "organize",
            "options": {
                "excludeByName": dict.fromkeys(hide, True),
                "indexByName": {name: i for i, name in enumerate(names)},
                "renameByName": rename,
            },
        },
    ]


def matrix(ref: str, row: str, column: str, *, single: bool = False) -> list[Json]:
    """One query's rows turned into columns, keyed by `row` for a join.

    A panel with one query names its value field "Value", else "Value #ref".
    """
    return [
        {
            "id": "groupingToMatrix",
            "filter": {"id": "byRefId", "options": ref},
            "options": {
                "columnField": column,
                "rowField": row,
                "valueField": "Value" if single else f"Value #{ref}",
            },
        },
        # The matrix names its key column "row\\column".
        {
            "id": "renameByRegex",
            "options": {"regex": f"^{row}\\\\{column}$", "renamePattern": row},
        },
    ]


def join(field: str) -> Json:
    return {"id": "joinByField", "options": {"byField": field, "mode": "outer"}}


def github_link(url_field: str) -> tuple[str, list[Json]]:
    return ("links", [link("Open on GitHub", "${__data.fields." + url_field + "}")])


# "https://github.com/.../pull/412" shows as "PR 412".
SHORT_LINKS = [
    {
        "type": "regex",
        "options": {
            "pattern": ".*/pull/(\\d+)$",
            "result": {"index": 0, "text": "PR $1"},
        },
    },
    {
        "type": "regex",
        "options": {
            "pattern": ".*/issues/(\\d+)$",
            "result": {"index": 1, "text": "issue $1"},
        },
    },
]


def hidden(name: str) -> Json:
    return by_name(name, ("custom.hidden", True))


def median_expr(name: str) -> str:
    """A median sparkline, empty unless the median exists now.

    The collector stops publishing a median when its 7-day window is empty,
    while delivery data stays fresh: the last value must not linger.
    """
    return f"{name}{DELIVERY}{FRESH_AT_END} and on() ({name} @ end())"


# ---------------------------------------------------------------- Cockpit


def needs_anton() -> str:
    """Reasons to look now; -1 when the pause is the only one."""
    reasons = " + ".join(
        f"(({expr}) or vector(0))"
        for expr in (
            f"sum(epic_needs_operator{GITHUB})",
            f"sum(epic_handoff_stalled{HANDOFF})",
            f"count({FAILURES} >= 3)",
            f"count({LATE})",
            "count(count by (target) (epic_agent_collision) > 1)",
            'count((sum by (agent) (increase(epic_permission_decisions_total{decision="deny"}[1h])) '
            "and on(agent) epic_agent_info) >= 5)",
            "count(count by (agent) (epic_backoff_info) >= 3)",
            f"count((epic_handoff_wait_seconds{HANDOFF}) > 3600)",
        )
    )
    return (
        f"((({reasons}) > 0) or (-1 * sum(epic_pause_requested == 1)) or vector(0))"
        f"{LOCAL}"
    )


def canvas_text(
    name: str,
    *,
    top: int,
    size: int,
    field: str = "",
    fixed: str = "",
    left: int = 12,
    width: int = 240,
    align: str = "left",
    color: str = "",
) -> Json:
    """One text on a canvas tile: a field's shown value, or fixed text.

    Without `color`, a field takes its threshold or mapping color.
    """
    return {
        "type": "metric-value" if field else "text",
        "name": name,
        "config": {
            "text": (
                {"mode": "field", "field": field, "fixed": ""}
                if field
                else {"mode": "fixed", "fixed": fixed}
            ),
            "color": {"fixed": color} if color else {"field": field, "fixed": "text"},
            "size": size,
            "align": align,
            "valign": "middle",
        },
        "background": {"color": {"fixed": "transparent"}},
        "border": {"color": {"fixed": "transparent"}},
        "constraint": {"horizontal": "left", "vertical": "top"},
        "placement": {"top": top, "left": left, "width": width, "height": size + 8},
    }


VALUE_SIZE, CAPTION_SIZE, CAPTION_TOP = 30, 12, 40
# The tile draws its own title: a panel title is cut short at 1280 px.
TITLE_SIZE, TITLE_ROOM = 13, 28


def tile(
    title: str,
    targets: list[Json],
    elements: list[Json],
    overrides: list[Json],
    *,
    description: str = "",
) -> Json:
    """A design tile: title, the value on the left, a short caption under it.

    A stat panel only centers its value, so this is a canvas.
    """
    for element in elements:
        element["placement"]["top"] += TITLE_ROOM
    heading = canvas_text("title", fixed=title, top=0, size=TITLE_SIZE, color="text")
    heading["config"]["weight"] = "medium"
    return {
        "type": "canvas",
        # No panel title, so Grafana draws no header; the canvas has it.
        "title": "",
        "description": description,
        "datasource": PROM,
        "targets": targets,
        "fieldConfig": {
            "defaults": {
                "noValue": "—",
                "color": {"mode": "thresholds"},
                "thresholds": steps((None, NEUTRAL)),
                "mappings": [],
            },
            "overrides": overrides,
        },
        "options": {
            "inlineEditing": False,
            "showAdvancedTypes": True,
            "panZoom": False,
            "infinitePan": False,
            "root": {
                "type": "frame",
                "name": "root",
                "background": {"color": {"fixed": "transparent"}},
                "border": {"color": {"fixed": "transparent"}},
                "constraint": {"horizontal": "left", "vertical": "top"},
                "placement": {},
                "elements": [heading, *elements],
            },
        },
    }


def caption_map(zero: str, more: str, **values: str) -> list[Json]:
    """Caption text for 0, for ≥ 1, and for other exact values."""
    return [
        value_map({"0": (zero, MUTED)} | {k: (v, MUTED) for k, v in values.items()}),
        {
            "type": "range",
            "options": {"from": 1, "to": 1e12, "result": {"index": 9, "text": more}},
        },
    ]


def value_caption(fields: list[str]) -> list[Json]:
    return [
        canvas_text("value", field=fields[0], top=0, size=VALUE_SIZE),
        canvas_text(
            "caption",
            field=fields[1],
            top=CAPTION_TOP,
            size=CAPTION_SIZE,
            color=MUTED,
        ),
    ]


def number_tile(
    title: str,
    expr: str,
    captions: list[Json],
    *,
    thresholds: Json | None = None,
    mappings: list[Json] | None = None,
    description: str = "",
) -> Json:
    """One number, with a caption from value mappings on the same query."""
    return tile(
        title,
        [target(expr, legend="value"), target(expr, legend="caption", ref="B")],
        value_caption(["value", "caption"]),
        [
            by_name(
                "value",
                ("thresholds", thresholds or steps((None, NEUTRAL))),
                ("mappings", mappings or []),
            ),
            by_name("caption", ("mappings", captions)),
        ],
        description=description,
    )


def attention_tiles(board: Board) -> None:
    red = steps((None, NEUTRAL), (1, ACT_NOW))
    tiles = [
        number_tile(
            "Needs Anton",
            needs_anton(),
            caption_map("nothing waiting", "look now", **{"-1": "only the pause"}),
            thresholds=red,
            mappings=[value_map({"-1": ("paused", LOOK_SOON)})],
        ),
        number_tile(
            "Paused",
            f"max(epic_pause_requested){LOCAL}",
            caption_map("loop running", "runners skip ticks"),
            mappings=[value_map({"0": ("No", NEUTRAL), "1": ("Yes", LOOK_SOON)})],
        ),
        tile(
            "Agents",
            [
                target(
                    f'sum(epic_agents_registered{{presence="running"}}){LOCAL}',
                    legend="running",
                ),
                target(
                    f'sum(epic_agents_registered{{presence!="retired"}}){LOCAL}',
                    legend="registered",
                    ref="B",
                ),
            ],
            [
                # "4 / 8": running ends where "/ 8" starts.
                canvas_text(
                    "running",
                    field="running",
                    top=0,
                    size=VALUE_SIZE,
                    width=40,
                    align="right",
                ),
                canvas_text(
                    "registered", field="registered", top=0, size=VALUE_SIZE, left=58
                ),
                canvas_text(
                    "caption",
                    fixed="running / registered",
                    top=CAPTION_TOP,
                    size=CAPTION_SIZE,
                    color=MUTED,
                ),
            ],
            [by_name("registered", ("unit", "prefix:/ "))],
        ),
    ]
    for title, expr, zero, more in (
        (
            "Agents failing",
            f"count({FAILURES} >= 3)",
            "none failing 3 in a row",
            "last 3+ ticks failed",
        ),
        (
            "Agents late",
            f"count({LATE})",
            "all on schedule",
            "missed the schedule",
        ),
        (
            "Collisions",
            "count(count by (target) (epic_agent_collision) > 1)",
            "no shared targets",
            "agents share a target",
        ),
    ):
        tiles.append(
            number_tile(
                title,
                f"(({expr}) or vector(0)){LOCAL}",
                caption_map(zero, more),
                thresholds=red,
            )
        )
    waits = f"(epic_handoff_wait_seconds{HANDOFF})"
    tiles.append(
        tile(
            "PRs waiting > 30 min",
            [
                target(
                    f"((count({waits} > 1800)) or vector(0)){LOCAL}", legend="value"
                ),
                target(
                    f"((max({waits})) or vector(0)){LOCAL}", legend="oldest", ref="B"
                ),
            ],
            [
                *value_caption(["value", "oldest"])[:1],
                canvas_text(
                    "label",
                    fixed="oldest",
                    top=CAPTION_TOP,
                    size=CAPTION_SIZE,
                    color=MUTED,
                ),
                canvas_text(
                    "oldest",
                    field="oldest",
                    top=CAPTION_TOP,
                    size=CAPTION_SIZE,
                    left=52,
                    color=MUTED,
                ),
            ],
            [
                by_name(
                    "value",
                    (
                        "thresholds",
                        steps((None, NEUTRAL), (1, LOOK_SOON), (2, ACT_NOW)),
                    ),
                ),
                by_name(
                    "oldest",
                    ("unit", "s"),
                    ("decimals", 0),
                    ("mappings", [value_map({"0": ("–", MUTED)})]),
                ),
            ],
        )
    )
    tiles.append(
        tile(
            "Data freshness",
            [
                target(
                    "time() - max(epic_local_snapshot_timestamp_seconds)",
                    legend="local",
                ),
                target(
                    "time() - max(epic_github_snapshot_timestamp_seconds)",
                    legend="GitHub",
                    ref="B",
                ),
            ],
            [
                canvas_text("local", field="local", top=0, size=VALUE_SIZE),
                canvas_text(
                    "label",
                    fixed="GitHub sync",
                    top=CAPTION_TOP,
                    size=CAPTION_SIZE,
                    color=MUTED,
                ),
                canvas_text(
                    "GitHub",
                    field="GitHub",
                    top=CAPTION_TOP,
                    size=CAPTION_SIZE,
                    left=92,
                ),
            ],
            [
                by_name(
                    "local",
                    ("unit", "s"),
                    ("decimals", 0),
                    (
                        "thresholds",
                        steps((None, NEUTRAL), (30, LOOK_SOON), (120, ACT_NOW)),
                    ),
                ),
                by_name(
                    "GitHub",
                    ("unit", "s"),
                    ("decimals", 0),
                    ("thresholds", steps((None, MUTED), (300, LOOK_SOON))),
                ),
            ],
        )
    )
    for i, panel in enumerate(tiles):
        board.add(panel, 3 * i, 0, 3, 3)


def stale(label: str) -> str:
    """All agents with `label`="data stale", for when local data is old."""
    # Only the agent label, so the join keeps one field per name.
    return f'label_replace(max by (agent) (epic_agent_info), "{label}", "data stale", "", "")'


def agent_table() -> Json:
    """One row per agent; columns from the plan appendix."""
    queries = {
        "A": "epic_agent_info",
        # Stale local data: every agent still gets a row that says so.
        "B": f"(epic_agent_presence_info{LOCAL}) or on(agent) {stale('presence')}",
        "C": "epic_agent_order",
        # Task of the current or last action, from the GitHub context.
        "D": f"(epic_agent_row_info{LOCAL}) * on(target) group_left(task, task_url) "
        f"(epic_task_context_info{GITHUB}) or on(agent) (epic_agent_row_info{LOCAL}) "
        f"or on(agent) {stale('outcome_text')}",
        # Only while running: an idle agent keeps its last elapsed value.
        "E": "(epic_tick_elapsed_seconds / on(agent) epic_tick_budget_seconds)"
        f" and on(agent) (epic_agent_presence == 1){LOCAL}",
        "F": f"(time() - max by (agent) (epic_tick_last_info)){LOCAL}",
        # -1: a count exists but its tick history cannot be read: unknown.
        "G": f"({FAILURES} or on(agent) (-1 * group by (agent) "
        f"(epic_tick_consecutive_failures))){LOCAL}",
        "I": f"epic_agent_next_tick_seconds{LOCAL}",
        "J": f"(count by (agent) (epic_backoff_info)){LOCAL}",
        # A 1 h window still holds agents that are gone: keep current ones.
        "K": 'round(sum by (agent) (increase(epic_permission_decisions_total{decision="deny"}[1h])))'
        + " and on(agent) epic_agent_info"
        + LOCAL,
    }
    names = [
        "kind",
        "agent",
        "label",
        "presence",
        "action_text",
        "task",
        "Value #E",
        "outcome_text",
        "Value #F",
        "Value #G",
        "recent_strip",
        "Value #I",
        "Value #J",
        "Value #K",
        # Link and sort helpers, hidden.
        "target",
        "task_url",
        "Value #C",
    ]
    rename = {
        "kind": " ",
        "agent": "Agent",
        "label": "Label",
        "presence": "Presence",
        "action_text": "Current / last action",
        "task": "Task",
        "Value #E": "Tick elapsed",
        "outcome_text": "Last outcome",
        "Value #F": "Ago",
        "Value #G": "Failed",
        "recent_strip": "Last 20",
        "Value #I": "Next tick",
        "Value #J": "Backoff",
        "Value #K": "Denied 1 h",
    }
    transformations = [
        join("agent"),
        {"id": "sortBy", "options": {"sort": [{"field": "Value #C", "desc": False}]}},
        *keep_fields(names, rename, ["Value #C"]),
    ]
    overrides = [
        by_name(
            " ",
            ("custom.width", 50),
            cell("color-text"),
            ("mappings", [value_map({k: ("●", c) for k, c in KIND.items()})]),
        ),
        by_name(
            "Agent",
            ("custom.width", 100),
            ("links", [link("Agent page", "/d/epic-agent?var-agent=${__value.raw}")]),
        ),
        by_name(
            "Label",
            ("custom.minWidth", 80),
            cell("color-text"),
            ("color", {"mode": "fixed", "fixedColor": MUTED}),
        ),
        by_name(
            "Presence",
            ("custom.width", 76),
            cell("color-background", mode="basic"),
            ("noValue", "data stale"),
            (
                "mappings",
                [
                    value_map(
                        {k: ("", c) for k, c in PRESENCE.items()}
                        | {"data stale": ("", STALE)}
                    )
                ],
            ),
        ),
        by_name(
            "Current / last action",
            ("custom.minWidth", 120),
            cell("color-background", mode="basic"),
            ("color", {"mode": "fixed", "fixedColor": "transparent"}),
            ("mappings", [regex_map("^collision.*", ACT_NOW, 0)]),
            github_link("target"),
        ),
        by_name("Task", ("custom.minWidth", 60), github_link("task_url")),
        by_name(
            "Tick elapsed",
            ("custom.width", 100),
            ("unit", "percentunit"),
            ("decimals", 0),
            ("min", 0),
            ("max", 1),
            cell("gauge", mode="basic"),
            (
                "thresholds",
                steps((None, "#5AB45F"), (0.7, "#E8B530"), (0.9, "#E5484D")),
            ),
            ("noValue", " "),
        ),
        by_name(
            "Last outcome",
            ("custom.width", 124),
            cell("color-background", mode="basic"),
            ("color", {"mode": "fixed", "fixedColor": "transparent"}),
            (
                "mappings",
                [
                    regex_map("^ok.*", OUTCOME["ok"], 0),
                    regex_map("^blocked.*", OUTCOME["blocked"], 1),
                    regex_map("^(error|interrupted).*", OUTCOME["error"], 2),
                    regex_map("^timeout.*", OUTCOME["timeout"], 3),
                    regex_map("^killed.*", OUTCOME["killed"], 4),
                    regex_map("^data stale$", STALE, 5),
                ],
            ),
        ),
        by_name(
            "Ago",
            ("custom.width", 60),
            ("unit", "s"),
            ("decimals", 0),
            cell("color-text"),
            ("color", {"mode": "fixed", "fixedColor": MUTED}),
        ),
        by_name(
            "Failed",
            ("custom.width", 50),
            cell("color-background", mode="basic"),
            ("thresholds", steps((None, "transparent"), (2, LOOK_SOON), (3, ACT_NOW))),
            ("mappings", [value_map({"-1": ("?", "transparent")})]),
        ),
        by_name(
            "Last 20",
            ("custom.width", 116),
            # The collector sends one colored bar per tick as HTML.
            cell("markdown"),
        ),
        by_name(
            "Next tick",
            ("custom.width", 70),
            ("unit", "s"),
            ("decimals", 0),
            cell("color-background", mode="basic"),
            # Overdue (negative) is red; an empty cell stays plain.
            (
                "mappings",
                [
                    {
                        "type": "range",
                        "options": {
                            "from": -1e12,
                            "to": -1e-9,
                            "result": {"index": 0, "color": ACT_NOW},
                        },
                    }
                ],
            ),
            ("color", {"mode": "fixed", "fixedColor": "transparent"}),
        ),
        by_name(
            "Backoff",
            ("custom.width", 60),
            cell("color-text"),
            ("thresholds", steps((None, GREY), (1, "#F0B94A"))),
        ),
        by_name(
            "Denied 1 h",
            ("custom.width", 80),
            cell("color-text"),
            ("thresholds", steps((None, GREY), (1, "#F0B94A"), (5, "#FF7A7F"))),
        ),
        hidden("target"),
        hidden("task_url"),
    ]
    panel = table_panel(
        "Agents",
        [target(q, table=True, ref=r) for r, q in queries.items()],
        transformations,
        overrides,
        description="One row per registered agent, running first. Last 20: "
        "one bar per tick, newest right. Backoff: "
        "Codex retry delays. Denied: Claude permission gate.",
    )
    return panel


def outcome_timeline() -> Json:
    return {
        "type": "state-timeline",
        "title": "Tick outcomes · last 24 h · held until the next tick",
        "datasource": PROM,
        "timeFrom": "24h",
        "hideTimeOverride": True,
        "targets": [
            ranged(
                # A gap while the collector is down; agents retired or gone
                # by the end of the range are hidden.
                "epic_tick_last_outcome and on(agent) (epic_agent_presence > 0)"
                " and on(agent) (epic_agent_presence @ end() > 0)" + LOCAL,
                legend="{{agent}}",
            )
        ],
        "fieldConfig": {
            "defaults": {
                # Fixed, so the legend lists the outcome mappings.
                "color": {"mode": "fixed", "fixedColor": "#2C2F36"},
                "mappings": [SEVERITY_COLORS],
                "custom": {"fillOpacity": 90, "lineWidth": 0},
                "noValue": "No ticks",
            },
            "overrides": [],
        },
        "options": {
            "mergeValues": True,
            "showValue": "never",
            "rowHeight": 0.9,
            "alignValue": "left",
            "legend": {
                "showLegend": True,
                "displayMode": "list",
                "placement": "bottom",
            },
        },
    }


# "https://github.com/.../pull/412" shows as "#412", as on GitHub.
PR_NUMBER = [
    {
        "type": "regex",
        "options": {
            "pattern": ".*/pull/(\\d+)$",
            "result": {"index": 0, "text": "#$1"},
        },
    }
]


def handoff_table() -> Json:
    """One row per direction: its oldest waiting PR, and who could review it."""
    reviewers = " or ".join(
        f'label_replace(epic_agents_registered_kind{{kind="{reviewer}"}}{LOCAL}, '
        f'"waiter_kind", "{waiter}", "", "")'
        for waiter, reviewer in (("claude", "codex"), ("codex", "claude"))
    )
    waits = f"(epic_handoff_wait_seconds{HANDOFF})"
    queries = {
        # All rows vanish while GitHub is not seen: no "0 waiting" guess.
        "A": f"({reviewers}){HANDOFF}",
        "B": f"count by (waiter_kind) ({waits}){LOCAL}",
        "C": f"topk by (waiter_kind) (1, {waits}){LOCAL}",
    }
    names = ["waiter_kind", "target", "Value #C", "Value #B", "Value #A"]
    rename = {
        "waiter_kind": "PRs → review",
        "target": "Oldest",
        "Value #C": "Waits",
        "Value #B": "PRs",
        "Value #A": "Reviewers",
    }
    return table_panel(
        "Handoff · per kind",
        [target(q, table=True, ref=r) for r, q in queries.items()],
        [join("waiter_kind"), *keep_fields(names, rename, [])],
        [
            by_name(
                "PRs → review",
                ("custom.minWidth", 110),
                cell("color-text"),
                (
                    "mappings",
                    [
                        value_map(
                            {
                                "claude": ("Claude → Codex", KIND["claude"]),
                                "codex": ("Codex → Claude", KIND["codex"]),
                            }
                        )
                    ],
                ),
            ),
            by_name(
                "Oldest",
                ("custom.width", 60),
                ("mappings", PR_NUMBER),
                ("noValue", "none"),
                ("links", [link("Open on GitHub", "${__value.raw}")]),
            ),
            by_name(
                "Waits",
                ("custom.width", 72),
                ("unit", "s"),
                ("decimals", 0),
                cell("color-background", mode="basic"),
                (
                    "thresholds",
                    steps((None, "transparent"), (900, LOOK_SOON), (1800, ACT_NOW)),
                ),
            ),
            by_name("PRs", ("noValue", "0"), ("custom.width", 44)),
            by_name(
                "Reviewers",
                ("custom.width", 80),
                cell("color-background", mode="basic"),
                ("thresholds", steps((None, LOOK_SOON), (1, "transparent"))),
            ),
        ],
        description="Ready PRs of each kind waiting for a review by the other "
        "kind: the oldest one and how long it waits. Amber: over 15 min, or no "
        "agent of the reviewer kind. Red: over 30 min. Empty when GitHub data "
        "is stale.",
        no_value="No current data",
    )


def epic_progress() -> Json:
    return {
        "type": "barchart",
        "title": "Epic progress",
        "description": "Checklist leaves per area: done by Claude, done by "
        "Codex, still open.",
        "datasource": PROM,
        "targets": [
            target(
                f'sum by (area, kind) (epic_area_leaves{{state="done"}}){GITHUB}',
                table=True,
            ),
            target(
                f'sum by (area) (epic_area_leaves{{state="open"}}){GITHUB}',
                table=True,
                ref="B",
            ),
        ],
        "transformations": [
            *matrix("A", "area", "kind"),
            join("area"),
            *keep_fields(
                ["area", "claude", "codex", "unknown", "Value #B"],
                {
                    "claude": "Claude",
                    "codex": "Codex",
                    "unknown": "Other",
                    "Value #B": "Open",
                },
                [],
            ),
        ],
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "fixed", "fixedColor": GREY},
                "min": 0,
                # No value axis: the bars and their counts say enough.
                "custom": {
                    "axisPlacement": "hidden",
                    "fillOpacity": 100,
                    "lineWidth": 0,
                },
            },
            "overrides": [
                by_name("area", ("custom.axisPlacement", "left")),
                by_name(
                    "Claude", ("color", {"mode": "fixed", "fixedColor": KIND["claude"]})
                ),
                by_name(
                    "Codex", ("color", {"mode": "fixed", "fixedColor": KIND["codex"]})
                ),
                by_name("Open", ("color", {"mode": "fixed", "fixedColor": "#2C2F36"})),
            ],
        },
        "options": {
            "orientation": "horizontal",
            "stacking": "normal",
            "showValue": "auto",
            "barWidth": 0.7,
            "xField": "area",
            "legend": {
                "showLegend": True,
                "displayMode": "list",
                "placement": "right",
            },
        },
    }


def open_prs() -> Json:
    """Columns from the design: PR, executor, task, review, CI, rounds, age."""
    queries = {
        "A": f"epic_pr_info{GITHUB}",
        "B": f"max by (target, task, task_url) (epic_task_context_info{GITHUB})",
        "C": f"epic_pr_review_rounds{DELIVERY}",
        "D": f"(time() - epic_pr_opened_timestamp_seconds){DELIVERY}",
    }
    names = [
        "target",
        "agent",
        "title",
        "task",
        "review",
        "checks",
        "Value #C",
        "Value #D",
        "task_url",
    ]
    rename = {
        "target": "PR",
        "agent": "Executor",
        "title": "Title",
        "task": "Task",
        "review": "Review",
        "checks": "CI",
        "Value #C": "Rounds",
        "Value #D": "Age",
    }
    return table_panel(
        "Open PRs",
        [target(q, table=True, ref=r) for r, q in queries.items()],
        [
            join("target"),
            # Only rows of open PRs; the task context also has issues.
            {
                "id": "filterByValue",
                "options": {
                    "filters": [{"fieldName": "title", "config": {"id": "isNotNull"}}],
                    "type": "include",
                    "match": "all",
                },
            },
            {
                "id": "sortBy",
                "options": {"sort": [{"field": "Value #D", "desc": False}]},
            },
            *keep_fields(names, rename, []),
        ],
        [
            by_name(
                "PR",
                ("custom.width", 60),
                ("mappings", PR_NUMBER),
                ("links", [link("Open on GitHub", "${__value.raw}")]),
            ),
            by_name(
                "Executor",
                ("custom.width", 80),
                cell("color-text"),
                (
                    "mappings",
                    [value_map({k: (k.capitalize(), c) for k, c in KIND.items()})],
                ),
            ),
            by_name(
                "Task",
                ("custom.minWidth", 100),
                ("noValue", "–"),
                github_link("task_url"),
            ),
            by_name(
                "Review",
                ("custom.width", 130),
                cell("color-text"),
                (
                    "mappings",
                    [
                        value_map(
                            {
                                "approved": ("", "#5AB45F"),
                                "changes requested": ("", LOOK_SOON),
                                "waiting": ("awaiting review", MUTED),
                            }
                        )
                    ],
                ),
            ),
            by_name(
                "CI",
                ("custom.width", 60),
                cell("color-text"),
                (
                    "mappings",
                    [
                        value_map(
                            {
                                "green": ("pass", "#5AB45F"),
                                "failed": ("fail", "#E5484D"),
                                "pending": ("running", MUTED),
                                "unknown": ("", MUTED),
                            }
                        )
                    ],
                ),
            ),
            by_name("Rounds", ("custom.width", 60), ("noValue", "–")),
            by_name(
                "Age",
                ("custom.width", 70),
                ("unit", "s"),
                ("decimals", 0),
                ("noValue", "–"),
            ),
            hidden("task_url"),
        ],
        description="Review is the other kind's latest verdict for the current "
        "head. Rounds and age come from the delivery data. This page never "
        "authorizes a merge.",
        no_value="No open PRs",
    )


def cockpit() -> Json:
    board = Board(
        "epic-agents",
        "Cockpit",
        PAGE_LINKS,
    )
    attention_tiles(board)
    board.add(agent_table(), 0, 3, 24, 10)
    board.add(outcome_timeline(), 0, 13, 16, 6)
    board.add(handoff_table(), 16, 13, 8, 6)
    board.add(epic_progress(), 0, 19, 9, 5)
    board.add(open_prs(), 9, 19, 15, 5)
    return board.render()


# ----------------------------------------------------------- Agent detail


def not_reported(kind: str) -> str:
    return f"Not reported by this runner ({kind} only)"


def agent_detail() -> Json:
    board = Board(
        "epic-agent",
        "Agent detail",
        PAGE_LINKS,
    )
    board.variables = [
        {
            "name": "agent",
            "label": "Agent",
            "type": "query",
            "datasource": PROM,
            # Registered agents and those retired in the last 24 h.
            "query": {
                "query": "label_values(epic_agent_info, agent)",
                "refId": "agent",
            },
            "definition": "label_values(epic_agent_info, agent)",
            "refresh": 2,
            "sort": 1,
            "includeAll": False,
            "multi": False,
        },
        {
            "name": "search",
            "label": "Search log",
            "type": "textbox",
            "query": "",
            "current": {"text": "", "value": ""},
        },
    ]
    a = 'agent="$agent"'
    header = table_panel(
        "",
        [
            target(f"epic_agent_info{{{a}}}", table=True),
            target(f"epic_agent_presence_info{{{a}}}{LOCAL}", table=True, ref="B"),
            target(f"epic_agent_interval_seconds{{{a}}}", table=True, ref="C"),
            target(f"epic_agent_budget_seconds{{{a}}}", table=True, ref="D"),
        ],
        [
            join("agent"),
            *keep_fields(
                ["kind", "agent", "label", "presence", "Value #C", "Value #D"],
                {
                    "kind": " ",
                    "agent": "Agent",
                    "label": "Label",
                    "presence": "Presence",
                    "Value #C": "Interval",
                    "Value #D": "Tick budget",
                },
                [],
            ),
        ],
        [
            by_name(
                " ",
                ("custom.width", 50),
                cell("color-text"),
                ("mappings", [value_map({k: ("●", c) for k, c in KIND.items()})]),
            ),
            by_name("Agent", ("custom.width", 100)),
            by_name(
                "Presence",
                cell("color-background", mode="basic"),
                ("noValue", "data stale"),
                ("mappings", [value_map({k: ("", c) for k, c in PRESENCE.items()})]),
            ),
            by_name(
                "Label",
                cell("color-text"),
                ("color", {"mode": "fixed", "fixedColor": MUTED}),
            ),
            by_name("Presence", ("custom.width", 80)),
            by_name("Interval", ("unit", "s"), ("custom.width", 70)),
            by_name("Tick budget", ("unit", "s"), ("custom.width", 90)),
        ],
    )
    board.add(header, 0, 0, 9, 3)
    tiles = (
        ("Ticks today", f"sum(epic_ticks_today{{{a}}}){LOCAL}", "none", None, "—"),
        (
            "Success rate today",
            f'(sum(epic_ticks_today{{{a},outcome="ok"}}) / sum(epic_ticks_today{{{a}}})){LOCAL}',
            "percentunit",
            steps((None, ACT_NOW), (0.5, LOOK_SOON), (0.7, NEUTRAL)),
            "—",
        ),
        (
            "Median tick duration",
            f'max(epic_tick_duration_today_seconds{{{a},quantile="0.5"}}){LOCAL}',
            "s",
            None,
            "—",
        ),
        (
            "Sessions blocked",
            f'sum(epic_ticks_today{{{a},outcome="blocked"}}){LOCAL}',
            "none",
            steps((None, NEUTRAL), (11, LOOK_SOON)),
            "—",
        ),
        (
            # Codex's one count, or Claude's counters and how many model
            # ticks lack full usage: never added into one total.
            "Tokens today",
            [
                target(f"sum(epic_tokens_today{{{a}}}){LOCAL}", legend="tokens"),
                target(
                    f"sum by (counter) (epic_usage_tokens_today{{{a}}}){LOCAL}",
                    legend="{{counter}}",
                    ref="B",
                ),
                target(
                    f'sum(epic_usage_ticks_today{{{a},coverage!="complete"}}){LOCAL}',
                    legend="ticks without full usage",
                    ref="C",
                ),
            ],
            "short",
            None,
            "Not reported yet",
        ),
    )
    for i, (title, expr, unit, thresholds, no_value) in enumerate(tiles):
        board.add(
            stat(
                title,
                expr if isinstance(expr, list) else [target(expr)],
                unit=unit,
                thresholds=thresholds,
                no_value=no_value,
            ),
            9 + 3 * i,
            0,
            3,
            3,
        )
    board.add(tick_history(a), 0, 3, 15, 9)
    board.add(hour_matrix(a), 15, 3, 9, 4)
    board.add(backoff_table(a), 15, 7, 9, 5)
    board.add(permission_bars(a), 0, 12, 7, 4)
    board.add(
        logs(
            "Last 10 denials",
            f'{{{a}, stream="permissions", decision="deny"}}',
            "Commands the Claude permission gate denied.",
            limit=10,
        ),
        0,
        16,
        7,
        6,
    )
    board.add(
        logs(
            "Errors only",
            f'{{{a}, stream!="permissions"}} |~ "(?i)(error|failed|exit=[1-9]|timed out)"',
            "Runner log lines that report a failure.",
        ),
        7,
        12,
        17,
        4,
    )
    board.add(
        logs(
            "Runner log",
            f'{{{a}, stream!="permissions"}} |~ "(?i)$search"',
            "Everything the runner and model wrote. Use the Search box above.",
        ),
        7,
        16,
        17,
        6,
    )
    return board.render()


def tick_history(a: str) -> Json:
    names = [
        "tick",
        "outcome",
        "exit",
        "phase",
        "action",
        "target",
        "Value",
        "tokens",
        "usage",
        "source",
    ]
    return table_panel(
        "Tick history",
        [target(f"epic_tick_info{{{a}}}{LOCAL}", table=True)],
        [
            {"id": "sortBy", "options": {"sort": [{"field": "tick", "desc": True}]}},
            *keep_fields(
                names,
                {
                    "tick": "Started",
                    "outcome": "Outcome",
                    "exit": "Exit",
                    "phase": "Stage",
                    "action": "Action",
                    "target": "Target",
                    "Value": "Duration",
                    "tokens": "Tokens",
                    "usage": "Usage",
                    "source": "Source",
                },
                [],
            ),
        ],
        [
            by_name(
                "Outcome",
                ("custom.width", 110),
                cell("color-background", mode="basic"),
                ("mappings", [value_map({k: ("", c) for k, c in OUTCOME.items()})]),
            ),
            by_name(
                "Duration",
                ("unit", "s"),
                ("noValue", "running"),
                # -1: the tick has no finish time (old log import, interrupted).
                ("mappings", [value_map({"-1": ("unknown", MUTED)})]),
            ),
            by_name("Tokens", ("noValue", "–")),
            by_name("Usage", ("noValue", "–")),
            by_name("Started", ("custom.width", 170)),
            by_name("Exit", ("custom.width", 50)),
            by_name(
                "Target",
                ("custom.width", 90),
                ("mappings", SHORT_LINKS),
                ("links", [link("Open on GitHub", "${__value.raw}")]),
            ),
        ],
        description="Newest first. Tokens: Codex's `tokens used` count. Usage: "
        "Claude's input, output, cache read and cache write tokens, each on its "
        "own, or why they are missing. Source: events (written by the runner) "
        "or log (best effort).",
        no_value="No ticks yet",
    )


def hour_matrix(a: str) -> Json:
    return table_panel(
        "Outcome per hour · 7 days",
        [target(f"epic_tick_hour_worst{{{a}}}{LOCAL}", table=True)],
        [
            *matrix("A", "day", "hour", single=True),
            {"id": "sortBy", "options": {"sort": [{"field": "day", "desc": True}]}},
        ],
        [
            by_name("day", ("custom.width", 90), ("displayName", "Day")),
            override(
                "byRegexp",
                "^\\d\\db?$",
                cell("color-background", mode="basic"),
                ("color", {"mode": "fixed", "fixedColor": "#2C2F36"}),
                ("noValue", " "),
                (
                    "mappings",
                    [
                        value_map(
                            {str(v): (" ", OUTCOME[k]) for k, v in SEVERITY.items()}
                        )
                    ],
                ),
            ),
        ],
        description="Worst outcome of each local hour. Empty: no tick.",
        no_value="No ticks in 7 days",
    )


def backoff_table(a: str) -> Json:
    return table_panel(
        "Backoff",
        [target(f"epic_backoff_info{{{a}}} * 1000{LOCAL}", table=True)],
        keep_fields(
            ["target", "Value", "current_head", "estimated"],
            {
                "target": "Target",
                "Value": "Retry after",
                "current_head": "Head still current",
                "estimated": "Estimated",
            },
            [],
        ),
        [
            by_name(
                "Target",
                ("mappings", SHORT_LINKS),
                ("links", [link("Open on GitHub", "${__value.raw}")]),
            ),
            by_name(
                "Retry after",
                ("unit", "dateTimeFromNow"),
                cell("color-text"),
                ("thresholds", steps((None, NEUTRAL))),
            ),
        ],
        description="Codex retry delays still active. A retry within 10 min is "
        "about to happen.",
        no_value=not_reported("Codex") + " or no delay",
    )


def permission_bars(a: str) -> Json:
    return {
        "type": "barchart",
        "title": "Permission gate · per hour",
        "datasource": PROM,
        "interval": "1h",
        "targets": [
            ranged(
                f"sum by (decision) (increase(epic_permission_decisions_total{{{a}}}[1h]))",
                legend="{{decision}}",
            )
        ],
        "fieldConfig": {
            "defaults": {"noValue": not_reported("Claude"), "decimals": 0, "min": 0},
            "overrides": [
                by_name("allow", ("color", {"mode": "fixed", "fixedColor": GREY})),
                by_name("deny", ("color", {"mode": "fixed", "fixedColor": ACT_NOW})),
            ],
        },
        "options": {
            "stacking": "normal",
            "showValue": "never",
            "xTickLabelSpacing": 100,
            "legend": {
                "showLegend": True,
                "displayMode": "list",
                "placement": "bottom",
            },
        },
    }


def logs(title: str, expr: str, description: str, limit: int | None = None) -> Json:
    query: Json = {"refId": "A", "expr": expr, "queryType": "range"}
    if limit is not None:
        query["maxLines"] = limit
    return {
        "type": "logs",
        "title": title,
        "description": description,
        "datasource": LOKI,
        "targets": [query],
        "options": {
            "showTime": True,
            "wrapLogMessage": True,
            "sortOrder": "Descending",
            "enableLogDetails": True,
            "dedupStrategy": "none",
        },
    }


# --------------------------------------------------------------- Delivery


def delivery_page() -> Json:
    board = Board(
        "epic-delivery",
        "Delivery",
        PAGE_LINKS,
        time_from="now-14d",
    )
    board.add(
        {
            "type": "timeseries",
            "title": "Burn-up",
            "description": "Checklist leaves done per area; the dashed line is all leaves.",
            "datasource": PROM,
            "targets": [
                ranged(
                    f'sum by (area) (epic_area_leaves{{state="done"}}){GITHUB}',
                    legend="{{area}}",
                ),
                ranged(f"sum(epic_area_leaves){GITHUB}", legend="all leaves", ref="B"),
            ],
            "fieldConfig": {
                "defaults": {
                    "color": {"mode": "shades", "fixedColor": MUTED},
                    "custom": {
                        "fillOpacity": 60,
                        "lineWidth": 1,
                        "stacking": {"mode": "normal", "group": "A"},
                        "lineInterpolation": "stepAfter",
                    },
                    "min": 0,
                },
                "overrides": [
                    by_name(
                        "all leaves",
                        ("custom.stacking", {"mode": "none"}),
                        ("custom.fillOpacity", 0),
                        ("custom.lineStyle", {"fill": "dash", "dash": [6, 4]}),
                        ("color", {"mode": "fixed", "fixedColor": "#D8D9DA"}),
                    )
                ],
            },
            "options": {"legend": {"displayMode": "list", "placement": "bottom"}},
        },
        0,
        0,
        16,
        11,
    )
    board.add(
        stat(
            "Median review rounds",
            [ranged(median_expr("epic_review_rounds_median"))],
            thresholds=steps((None, NEUTRAL), (2.5, LOOK_SOON)),
            graph="area",
            time_from="7d",
            decimals=1,
            description="Heads the reviewer gave a verdict on, PRs merged in the last 7 days.",
        ),
        16,
        0,
        4,
        5,
    )
    board.add(
        stat(
            "Median time to merge",
            [ranged(median_expr("epic_time_to_merge_median_seconds"))],
            unit="s",
            thresholds=steps((None, NEUTRAL), (6 * 3600, LOOK_SOON)),
            graph="area",
            time_from="7d",
            description="From PR opened to merged, last 7 days.",
        ),
        20,
        0,
        4,
        5,
    )
    board.add(
        {
            "type": "barchart",
            "title": "Issues by project status · per kind",
            "datasource": PROM,
            "targets": [
                target(f"sum by (agent, status) (epic_issues){GITHUB}", table=True)
            ],
            "transformations": matrix("A", "agent", "status", single=True),
            "fieldConfig": {
                "defaults": {
                    "color": {"mode": "shades", "fixedColor": MUTED},
                    "min": 0,
                },
                "overrides": [],
            },
            "options": {
                "orientation": "horizontal",
                "stacking": "normal",
                "showValue": "never",
                "xField": "agent",
                "legend": {
                    "showLegend": True,
                    "displayMode": "list",
                    "placement": "bottom",
                },
            },
        },
        16,
        5,
        8,
        6,
    )
    board.add(
        {
            "type": "barchart",
            "title": "Merged PRs per day · by executor kind",
            "datasource": PROM,
            "targets": [target(f"epic_merged_prs_day{DELIVERY}", table=True)],
            "transformations": [
                *matrix("A", "day", "kind", single=True),
                {
                    "id": "sortBy",
                    "options": {"sort": [{"field": "day", "desc": False}]},
                },
            ],
            "fieldConfig": {
                "defaults": {
                    "min": 0,
                    "decimals": 0,
                    "color": {"mode": "fixed", "fixedColor": GREY},
                },
                "overrides": [
                    by_name(kind, ("color", {"mode": "fixed", "fixedColor": color}))
                    for kind, color in KIND.items()
                ],
            },
            "options": {
                "stacking": "normal",
                "showValue": "never",
                "xField": "day",
                "xTickLabelRotation": -45,
                "legend": {
                    "showLegend": True,
                    "displayMode": "list",
                    "placement": "bottom",
                },
            },
        },
        0,
        11,
        9,
        11,
    )
    board.add(pr_flow(), 9, 11, 15, 11)
    return board.render()


def pr_flow() -> Json:
    names = [
        "target",
        "executor",
        "reviewer",
        "opened",
        "state",
        "Value #A",
        "Value #B",
    ]
    return table_panel(
        "PR flow",
        [
            target(f"epic_pr_flow_info{DELIVERY}", table=True),
            target(
                f"max by (target) (epic_handoff_wait_seconds{HANDOFF}){LOCAL}",
                table=True,
                ref="B",
            ),
        ],
        [
            {"id": "filterByRefId", "options": {"include": "A|B"}},
            join("target"),
            {
                "id": "filterByValue",
                "options": {
                    "filters": [
                        {"fieldName": "executor", "config": {"id": "isNotNull"}}
                    ],
                    "type": "include",
                    "match": "all",
                },
            },
            {"id": "sortBy", "options": {"sort": [{"field": "opened", "desc": True}]}},
            *keep_fields(
                names,
                {
                    "target": "PR",
                    "executor": "Executor",
                    "reviewer": "Reviewer",
                    "opened": "Opened",
                    "state": "State",
                    "Value #A": "Review rounds",
                    "Value #B": "Waiting",
                },
                [],
            ),
        ],
        [
            by_name(
                "PR",
                ("custom.width", 80),
                ("mappings", SHORT_LINKS),
                ("links", [link("Open on GitHub", "${__value.raw}")]),
            ),
            *[
                by_name(
                    column,
                    cell("color-text"),
                    ("mappings", [value_map({k: ("", c) for k, c in KIND.items()})]),
                )
                for column in ("Executor", "Reviewer")
            ],
            by_name("State", ("custom.width", 80)),
            by_name("Review rounds", ("custom.width", 110)),
            by_name("Opened", ("custom.width", 180)),
            by_name(
                "Waiting",
                ("custom.width", 100),
                ("unit", "s"),
                cell("color-background", mode="basic"),
                (
                    "thresholds",
                    steps((None, "transparent"), (1800, LOOK_SOON), (3600, ACT_NOW)),
                ),
                ("noValue", "–"),
            ),
        ],
        description="The last 14 PRs, open and merged. Waiting: observed wait "
        "for a review by the other kind.",
        no_value="No PRs in 14 days",
    )


# ---------------------------------------------------------------- Tickets

STATUS = {
    "Backlog": "#A57BE0",
    "Refinement": "#5B8DEF",
    "Ready": "#E8B530",
    "In progress": "#D96BB0",
    "In review": "#4FC3E8",
    "Done": "#5AB45F",
}
EXECUTOR = {
    "Claude": KIND["claude"],
    "Codex": KIND["codex"],
    "Anton": MUTED,
    "Unassigned": MUTED,
    "Unknown": STALE,
}
# (id, title, color when not empty, rule) in the collector's order.
CHECKS = (
    (
        "ready_undeclared",
        # Short titles: the panel title is cut at 1280 px; the rule is in
        # the description.
        "Ready · dep. not declared",
        ACT_NOW,
        "Ready, and an open blocked-by issue is not named in a Start after line "
        "of a readiness comment. The runner could claim it too early.",
    ),
    (
        "ready_claimable",
        "Ready · may be claimed",
        NEUTRAL,
        "Ready, and the runner selector would offer it as a claim now.",
    ),
    (
        "in_progress_long",
        "In progress > 1d",
        LOOK_SOON,
        "In progress for more than 24 h (since the collector saw it enter; at "
        "least that long for tickets it found there).",
    ),
    (
        "in_review_long",
        "In review > 1d",
        ACT_NOW,
        "In review for more than 24 h (since the collector saw it enter; at "
        "least that long for tickets it found there).",
    ),
    (
        "refinement_unreviewed",
        "Refinement · not reviewed",
        LOOK_SOON,
        "Refinement, and no approved body review matches the current body.",
    ),
    (
        "done_open",
        "Done · issue open",
        LOOK_SOON,
        "Board Status is Done but the issue is still open.",
    ),
    (
        "label_out_of_sync",
        "Label out of sync",
        LOOK_SOON,
        "The status:: label does not match the board Status: no label, two or "
        "more, another status, or status::sync left by an unfinished sync. "
        "Fix with set_status.py repair. A change shows here for one refresh "
        "until the helper's label write follows its board write.",
    ),
)
# Tile widths in the CHECKS order; the short titles fit 3 columns at 1280 px.
CHECK_WIDTHS = (4, 4, 3, 3, 4, 3, 3)
TICKETS_URL = "/d/epic-tickets"


def check_selection() -> str:
    """A vector with label check="$check"; selects one check or "all"."""
    return 'label_replace(vector(1), "check", "$check", "", "")'


def selected_tickets() -> str:
    """Info rows of the tickets in the chosen check, or of all tickets."""
    chosen = check_selection()
    members = f"(epic_ticket_check_member and on(check) {chosen})"
    everything = (
        f'({chosen} and on(check) label_replace(vector(1), "check", "all", "", ""))'
    )
    rows = (
        f"((epic_ticket_info and on(issue) {members})"
        f" or (epic_ticket_info and on() {everything})){TICKETS}"
    )
    # pr_url only for tickets with a PR, so an empty PR cell gets no link.
    return f'label_replace({rows}, "pr_url", "{REPO}/pull/$1", "pr", "(.+)")'


# epic_ticket_check_severity → tile color: the worst ticket in the check.
CHECK_SEVERITY = {3: ACT_NOW, 2: LOOK_SOON, 1: NEUTRAL, 0: NEUTRAL}


def severity_color(check: str) -> str:
    """One row with label color for the check's severity; none when unknown."""
    series = f'epic_ticket_check_severity{{check="{check}"}}'
    return " or ".join(
        f'(label_replace(({series}{TICKETS}) == {level}, "color", "{color}", "", ""))'
        for level, color in CHECK_SEVERITY.items()
    )


def check_tile(check: str, title: str, color: str, rule: str) -> Json:
    """Count of one check. -1 when its source is unknown: grey, never 0.

    Query B sets the color from the check's severity. Without B (unknown or
    an older collector) the thresholds apply; the "unknown" mapping wins.
    """
    panel = stat(
        title,
        [
            target(
                f'((epic_ticket_check_count{{check="{check}"}}{TICKETS})'
                " or on() vector(-1))"
            ),
            target(severity_color(check), ref="B"),
        ],
        mappings=[value_map({"-1": ("unknown", STALE)})],
        thresholds=steps((None, NEUTRAL), (1, color)),
        description=rule + " Color: amber or red when a ticket waits too long. "
        "Click to show these tickets in the table.",
    )
    # B is config, not a second value.
    panel["options"]["textMode"] = "value"
    panel["transformations"] = [
        {"id": "labelsToFields", "options": {"mode": "columns"}},
        {
            "id": "configFromData",
            "options": {
                "configRefId": "B",
                "applyTo": {"id": "byFrameRefID", "options": "A"},
                "mappings": [
                    {"fieldName": "color", "handlerKey": "color"},
                    *(
                        {"fieldName": name, "handlerKey": "__ignore"}
                        for name in ("epic_ticket_check_severity", "Time", "check")
                    ),
                ],
            },
        },
    ]
    panel["fieldConfig"]["defaults"]["links"] = [
        link(
            "Show these tickets",
            f"{TICKETS_URL}?var-check={check}&${{__url_time_range}}",
        )
    ]
    return panel


def status_stat(status: str) -> Json:
    """Count now, with a 7-day sparkline. Nothing when the count is missing
    now: the range reducer must not show an older value."""
    series = f'epic_tickets_by_status{{status="{status}"}}'
    panel = stat(
        status,
        [ranged(f"{series}{TICKETS}{TICKETS_AT_END} and on() ({series} @ end())")],
        # Grey when there is no number; every count is >= 0.
        thresholds=steps((None, STALE), (0, STATUS[status])),
        graph="area",
        time_from="7d",
        decimals=0,
        no_value="unknown",
        description=f"Tickets in {status} now; the change is against 7 days ago. "
        "Unknown while the board was not read in the last 5 min.",
    )
    panel["options"]["showPercentChange"] = True
    # More tickets in a status is neither good nor bad: no red or green.
    panel["options"]["percentChangeColorMode"] = "same_as_value"
    # The fixed 7-day window is in the description; the badge cuts the title.
    panel["hideTimeOverride"] = True
    # 5-min steps over 7 days: the last step is at most 5 min old, so a fresh
    # collector shows a number at once (Grafana's default step is ~30 min).
    panel["maxDataPoints"] = 7 * 24 * 12
    return panel


def cumulative_flow() -> Json:
    return {
        "type": "timeseries",
        "title": "Tickets per status",
        "description": "Task and Subtask tickets per board status. Gaps: the "
        "collector did not see the board then.",
        "datasource": PROM,
        "targets": [
            ranged(
                f"sum by (status) (epic_tickets_by_status){TICKETS}",
                legend="{{status}}",
            )
        ],
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "fixed", "fixedColor": STALE},
                "custom": {
                    "fillOpacity": 70,
                    "lineWidth": 1,
                    "stacking": {"mode": "normal", "group": "A"},
                    "lineInterpolation": "stepAfter",
                    "spanNulls": False,
                },
                "min": 0,
                "decimals": 0,
            },
            "overrides": [
                by_name(status, ("color", {"mode": "fixed", "fixedColor": color}))
                for status, color in STATUS.items()
            ],
        },
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}},
    }


def ticket_columns(names: list[str], rename: dict[str, str]) -> list[Json]:
    """Issue numbers sort as numbers; Done sorts by done_sort.

    ready_entered is a Unix time in a label: a time field, empty when unknown.
    """
    return [
        {
            "id": "convertFieldType",
            "options": {
                "conversions": [
                    {"targetField": "issue", "destinationType": "number"},
                    {"targetField": "done_sort", "destinationType": "number"},
                    {
                        "targetField": "ready_entered",
                        "destinationType": "time",
                        "dateFormat": "X",
                    },
                ],
                "fields": {},
            },
        },
        *keep_fields(names, rename, []),
    ]


def ticket_overrides(shown: list[str]) -> list[Json]:
    """Column settings, only for the columns a table shows (or hides)."""
    overrides = [
        by_name(
            "Ticket",
            ("custom.width", 80),
            ("links", [link("Open on GitHub", "${__data.fields.url}")]),
        ),
        hidden("url"),
        hidden("done_sort"),
        by_name(
            "Status",
            ("custom.width", 110),
            cell("color-text"),
            ("mappings", [value_map({k: ("", c) for k, c in STATUS.items()})]),
        ),
        by_name(
            "Executor",
            ("custom.width", 100),
            cell("color-text"),
            ("mappings", [value_map({k: ("", c) for k, c in EXECUTOR.items()})]),
        ),
        by_name(
            "PR",
            ("custom.width", 80),
            ("mappings", SHORT_LINKS),
            ("links", [link("Open on GitHub", "${__value.raw}")]),
        ),
        by_name(
            "Last agent",
            ("custom.width", 150),
            cell("color-text"),
            (
                "mappings",
                [
                    regex_map(f"^{name}( .*)?$", EXECUTOR[name], i)
                    for i, name in enumerate(("Claude", "Codex"))
                ],
            ),
        ),
        by_name(
            "Entered",
            # A fixed short format: the browser's local format ("10/06/2026,
            # 10:18:48 PM") lost its first digit even at 170 px.
            ("custom.width", 140),
            ("unit", ENTERED_FORMAT),
        ),
        by_name(
            "≥/?",
            # Grafana's minimum column width; a smaller one overflows the panel.
            ("custom.width", 50),
            ("custom.align", "right"),
            cell("color-text"),
            ("noValue", " "),
            ("mappings", [EXACT_MARK]),
        ),
        by_name("In status", ("custom.width", 90), ("unit", "s"), ("decimals", 0)),
        by_name("Since Ready", ("custom.width", 100), ("unit", "dateTimeFromNow")),
    ]
    return [o for o in overrides if o["matcher"]["options"] in shown]


ENTERED_FORMAT = "time:YYYY-MM-DD HH:mm"


# epic_ticket_status_entered_seconds{exact}: "≥" a lower bound, "?" the time
# crosses a coverage gap of the collector.
EXACT_MARK = {
    "type": "value",
    "options": {
        "1": {"index": 0, "text": " "},
        "0": {"index": 1, "text": "≥", "color": MUTED},
        "gap": {"index": 2, "text": "?", "color": LOOK_SOON},
    },
}
ENTERED = "epic_ticket_status_entered_seconds"


def ticket_table() -> Json:
    selected = selected_tickets()
    queries = {
        "A": selected,
        # Keeps the exact label for the "≥" / "?" mark.
        "B": f"(time() - {ENTERED}{TICKETS}) and on(issue) {selected}",
        "C": f"(max by (issue) ({ENTERED}) * 1000{TICKETS}) and on(issue) {selected}",
    }
    names = [
        "issue",
        "title",
        "status",
        "executor",
        "last_agent",
        "Value #C",
        "exact",
        "Value #B",
        "ready_entered",
        "pr_url",
        "note",
        "url",
    ]
    rename = {
        "issue": "Ticket",
        "title": "Title",
        "status": "Status",
        "executor": "Executor",
        "last_agent": "Last agent",
        "Value #C": "Entered",
        "exact": "≥/?",
        "Value #B": "In status",
        "ready_entered": "Since Ready",
        "pr_url": "PR",
        "note": "Note",
    }
    return table_panel(
        "Tickets · $check",
        [target(q, table=True, ref=r) for r, q in queries.items()],
        [
            join("issue"),
            # Only the tickets of query A: B and C never add a row.
            {
                "id": "filterByValue",
                "options": {
                    "filters": [{"fieldName": "title", "config": {"id": "isNotNull"}}],
                    "type": "include",
                    "match": "all",
                },
            },
            *ticket_columns(names, rename),
            {"id": "sortBy", "options": {"sort": [{"field": "Ticket"}]}},
        ],
        ticket_overrides([*rename.values(), "url"]),
        description="Choose a check with a tile or the Check picker; All shows "
        "every ticket: not Done, plus Done in the last 7 days or still open. "
        "In status: since the collector saw the ticket enter its status; ≥ is a "
        "lower bound (found there, or the entry is unsure), ? crosses a gap in "
        "the collector's data. Since Ready: the last entry into Ready. "
        "(unverified): the agent comes from a run event, not from GitHub.",
    )


# Hidden variable for the history: the issues of the chosen check, or of the
# whole table for All. Grafana joins them into a Loki regex (305|307).
ISSUES = {
    "name": "issues",
    "type": "query",
    "datasource": PROM,
    "query": {"query": f"query_result({selected_tickets()})", "refId": "issues"},
    "regex": '/issue="(\\d+)"/',
    "refresh": 2,
    "sort": 3,
    "multi": True,
    "includeAll": True,
    "current": {"text": "All", "value": "$__all"},
    "options": [],
    "hide": 2,
}

RETIRED = "retired"  # ticket_history.RETIRED: a segment a later gap replaced
# One series per segment: the newest rev wins. Retired is kept in the text
# and dropped in Grafana: a filter in LogQL would bring back the segment's
# older revisions.
SEGMENTS = (
    'topk by (segment_id) (1, max_over_time({stream="ticket_segments"} | json'
    ' | issue=~"$issues" | keep segment_id, issue, status, agent, start, rev'
    ' | label_format text="{{.status}}{{if .agent}} · {{.agent}}{{end}}",'
    ' start_ms="{{.start}}000" | unwrap rev [7d]) by (segment_id, issue, text,'
    " start_ms))"
)


def to_type(field: str, kind: str) -> Json:
    return {
        "id": "convertFieldType",
        "options": {
            "conversions": [{"targetField": field, "destinationType": kind}],
            "fields": {},
        },
    }


def status_history() -> Json:
    """One row per ticket, colored by status over 7 days.

    A row draws each segment from its start to the next one, so only the
    start time is needed. A gap is its own segment: transparent, no text.
    """
    mappings = [
        regex_map(f"^{status}( · .*)?$", color, i)
        for i, (status, color) in enumerate(STATUS.items())
    ]
    mappings.append(
        {
            "type": "value",
            "options": {
                "gap": {"index": len(STATUS), "text": " ", "color": "transparent"}
            },
        }
    )
    return {
        "type": "state-timeline",
        "title": "Status history · $check",
        "description": "One row per ticket over 7 days, in the order the rows "
        "start. In progress and In review show the agent. Empty parts: the "
        "collector did not see the board then. A check filter keeps the full "
        "history of its tickets.",
        "datasource": LOKI,
        "timeFrom": "7d",
        "hideTimeOverride": True,
        "targets": [{"refId": "A", "expr": SEGMENTS, "queryType": "instant"}],
        "transformations": [
            {"id": "labelsToFields", "options": {"mode": "columns"}},
            {"id": "merge", "options": {}},
            to_type("start_ms", "number"),
            to_type("start_ms", "time"),
            {
                "id": "filterByValue",
                "options": {
                    "filters": [
                        {
                            "fieldName": "text",
                            "config": {
                                "id": "regex",
                                "options": {"value": f"^{RETIRED}( · .*)?$"},
                            },
                        }
                    ],
                    "type": "exclude",
                    "match": "any",
                },
            },
            {
                "id": "filterFieldsByName",
                "options": {"include": {"names": ["issue", "text", "start_ms"]}},
            },
            {"id": "sortBy", "options": {"sort": [{"field": "start_ms"}]}},
            {
                "id": "partitionByValues",
                "options": {
                    "fields": ["issue"],
                    "keepFields": False,
                    "naming": {"asLabels": True},
                },
            },
        ],
        "fieldConfig": {
            "defaults": {
                "displayName": "#${__field.labels.issue}",
                "color": {"mode": "fixed", "fixedColor": GREY},
                "mappings": mappings,
                "custom": {"fillOpacity": 80, "lineWidth": 0},
                "noValue": "No history",
            },
            "overrides": [],
        },
        "options": {
            "mergeValues": True,
            "showValue": "auto",
            "rowHeight": 0.9,
            "alignValue": "left",
            "legend": {"showLegend": False},
        },
    }


def board_column(status: str) -> Json:
    names = ["issue", "title", "executor", "url", "done_sort"]
    rename = {"issue": "Ticket", "title": "Title", "executor": "Executor"}
    order = (
        [{"field": "done_sort", "desc": True}]
        if status == "Done"
        else [{"field": "Ticket"}]
    )
    return table_panel(
        status,
        [target(f'epic_ticket_info{{status="{status}"}}{TICKETS}', table=True)],
        [
            *ticket_columns(names, rename),
            {"id": "sortBy", "options": {"sort": order}},
        ],
        ticket_overrides([*rename.values(), "url", "done_sort"]),
        description="Done: still-open issues first, then the newest closed."
        if status == "Done"
        else "",
    )


# ticket_history.TIMED, in flow order.
TIMED = ("Refinement", "Ready", "In progress", "In review")


def aging() -> Json:
    """The 20 open tickets that sit longest in their status."""
    # Only the copied labels: an info series whose other labels changed
    # (last_agent, note) must not make the match many-to-many.
    info = 'max by (issue, title, status, url) (epic_ticket_info{status!="Done"})'
    expr = (
        f"topk(20, (time() - {ENTERED}) * on(issue)"
        f" group_left(title, status, url) {info}{TICKETS})"
    )
    names = ["issue", "title", "status", "exact", "Value", "url"]
    rename = {
        "issue": "Ticket",
        "title": "Title",
        "status": "Status",
        "exact": "≥/?",
        "Value": "In status",
    }
    return table_panel(
        "Aging · 20 longest in status",
        [target(expr, table=True)],
        [
            *ticket_columns(names, rename),
            {
                "id": "sortBy",
                "options": {"sort": [{"field": "In status", "desc": True}]},
            },
        ],
        [
            # Its own In status: the table's fixed width cuts long ages here.
            *ticket_overrides(["Ticket", "Title", "Status", "≥/?", "url"]),
            by_name("In status", ("unit", "s"), ("decimals", 0)),
        ],
        description="Open tickets (not Done) by time in their current status. "
        "≥ is a lower bound, ? crosses a gap in the collector's data.",
    )


def done_per_day() -> Json:
    return {
        "type": "barchart",
        "title": "Done per day · by executor",
        "description": "Tickets that entered Done per day (Berlin time), last "
        "14 days, by the Executor at that time.",
        "datasource": PROM,
        "targets": [target(f"epic_tickets_done_day{TICKETS}", table=True)],
        "transformations": [
            *matrix("A", "day", "executor", single=True),
            {"id": "sortBy", "options": {"sort": [{"field": "day", "desc": False}]}},
        ],
        "fieldConfig": {
            "defaults": {
                "min": 0,
                "decimals": 0,
                "noValue": "–",
                "color": {"mode": "fixed", "fixedColor": GREY},
            },
            "overrides": [
                by_name(name, ("color", {"mode": "fixed", "fixedColor": color}))
                for name, color in EXECUTOR.items()
            ],
        },
        "options": {
            "stacking": "normal",
            "showValue": "never",
            "xField": "day",
            "xTickLabelRotation": -45,
            "legend": {
                "showLegend": True,
                "displayMode": "list",
                "placement": "bottom",
            },
        },
    }


def flow_stat(title: str, queries: dict[str, str], description: str) -> Json:
    """Seconds; "–" when the collector has no samples, never 0."""
    panel = stat(
        title,
        [
            target(f"{expr}{TICKETS}", legend=legend, ref=chr(ord("A") + i))
            for i, (legend, expr) in enumerate(queries.items())
        ],
        unit="s",
        no_value="–",
        decimals=1,
        description=description,
    )
    # Named values in one text size: one value alone would draw larger.
    panel["options"]["textMode"] = "value_and_name"
    panel["options"]["text"] = {"titleSize": 14, "valueSize": 20}
    return panel


def ordered(expr: str, label: str, values: tuple[str, ...]) -> str:
    """Adds label "order" with each value's position, for a sort."""
    for i, value in enumerate(values):
        expr = f'label_replace({expr}, "order", "{i}", "{label}", "{value}")'
    return expr


def time_in_status() -> Json:
    series = "epic_ticket_time_in_status_seconds"
    queries = {
        "A": ordered(f'{series}{{quantile="0.5"}}', "status", TIMED),
        "B": f'{series}{{quantile="0.85"}}',
    }
    names = ["status", "Value #A", "Value #B"]
    return {
        "type": "barchart",
        "title": "Time in status · p50 and p85",
        "description": "Summed time in each status per done ticket, last 14 "
        "days. A status without clean samples has no bar.",
        "datasource": PROM,
        "targets": [
            target(f"{q}{TICKETS}", table=True, ref=r) for r, q in queries.items()
        ],
        "transformations": [
            join("status"),
            to_type("order", "number"),
            {"id": "sortBy", "options": {"sort": [{"field": "order"}]}},
            *keep_fields(names, {"Value #A": "p50", "Value #B": "p85"}, []),
        ],
        "fieldConfig": {
            "defaults": {"unit": "s", "min": 0, "noValue": "–"},
            "overrides": [
                by_name("p50", ("color", {"mode": "fixed", "fixedColor": MUTED})),
                by_name("p85", ("color", {"mode": "fixed", "fixedColor": GREY})),
            ],
        },
        "options": {
            "orientation": "horizontal",
            "showValue": "never",
            "xField": "status",
            "legend": {
                "showLegend": True,
                "displayMode": "list",
                "placement": "bottom",
            },
        },
    }


def episode_key(series: str) -> str:
    """One row per done episode: key "issue/episode"."""
    return f'label_join({series}, "key", "/", "issue", "episode")'


def done_tickets() -> Json:
    queries = {
        "A": f"{episode_key('epic_ticket_done_seconds * 1000')}{TICKETS}",
        "B": f"sum by (key) ({episode_key('epic_ticket_cycle_seconds')}){TICKETS}",
        "C": f"sum by (key) ({episode_key('epic_ticket_lead_seconds')}){TICKETS}",
    }
    names = ["issue", "executor", "Value #A", "Value #B", "Value #C"]
    rename = {
        "issue": "Ticket",
        "executor": "Executor",
        "Value #A": "Done",
        "Value #B": "Cycle",
        "Value #C": "Lead",
    }
    return table_panel(
        "Done tickets · cycle and lead time",
        [target(q, table=True, ref=r) for r, q in queries.items()],
        [
            join("key"),
            *ticket_columns(names, rename),
            {"id": "sortBy", "options": {"sort": [{"field": "Done", "desc": True}]}},
        ],
        [
            by_name(
                "Ticket",
                ("custom.width", 80),
                ("links", [link("Open on GitHub", f"{REPO}/issues/${{__value.raw}}")]),
            ),
            *ticket_overrides(["Executor"]),
            # Fits 10 of 24 columns at 1280 px.
            by_name("Done", ("custom.width", 110), ("unit", "dateTimeFromNow")),
            by_name("Cycle", ("custom.width", 90), ("unit", "s"), ("decimals", 1)),
            by_name("Lead", ("custom.width", 90), ("unit", "s"), ("decimals", 1)),
        ],
        description="Tickets that entered Done in the last 14 days, newest "
        "first. Cycle: first In progress to Done. Lead: first Ready to Done. "
        "–: not measured (the ticket skipped the status, or the time crosses "
        "a gap in the collector's data that no status label change explains). "
        "Label changes from set_status.py give exact times also while the "
        "collector was off; a status moved by hand on the board in such a gap "
        "stays invisible.",
    )


def flow_times(y: int) -> list[tuple[Json, int, int, int, int]]:
    cycle = "epic_ticket_cycle_quantile_seconds"
    return [
        (aging(), 0, y, 12, 12),
        (done_per_day(), 12, y, 12, 12),
        (
            flow_stat(
                "Cycle time",
                {
                    "p50": f'{cycle}{{quantile="0.5"}}',
                    "p85": f'{cycle}{{quantile="0.85"}}',
                },
                "First In progress to Done, tickets done in the last 14 days.",
            ),
            0,
            y + 12,
            6,
            6,
        ),
        (
            flow_stat(
                "Lead time · median",
                {"median": "epic_ticket_lead_median_seconds"},
                "First Ready to Done, tickets done in the last 14 days.",
            ),
            0,
            y + 18,
            6,
            6,
        ),
        (time_in_status(), 6, y + 12, 8, 12),
        (done_tickets(), 14, y + 12, 10, 12),
    ]


def tickets_page() -> Json:
    board = Board(
        "epic-tickets",
        "Tickets",
        PAGE_LINKS,
        time_from="now-14d",
        refresh="1m",
    )
    board.variables = [
        {
            "name": "check",
            "label": "Check",
            "type": "custom",
            "query": ", ".join(
                ["All : all"] + [f"{title} : {check}" for check, title, *_ in CHECKS]
            ),
            "current": {"text": "All", "value": "all"},
            "options": [],
            "includeAll": False,
            "multi": False,
        },
        ISSUES,
    ]
    x = 0
    for check, width in zip(CHECKS, CHECK_WIDTHS, strict=True):
        board.add(check_tile(*check), x, 0, width, 3)
        x += width
    for i, status in enumerate(STATUS):
        board.add(status_stat(status), 4 * i, 3, 4, 4)
    board.add(cumulative_flow(), 0, 7, 24, 8)
    board.add(ticket_table(), 0, 15, 24, 9)
    # 14 units (about 450 px): a check holds few tickets, so each row is
    # tall enough for its text; All (about 150 rows) shows colors only.
    board.add(status_history(), 0, 24, 24, 14)
    board.add_row(
        "Board",
        38,
        [(board_column(status), 4 * i, 39, 4, 15) for i, status in enumerate(STATUS)],
    )
    board.add_row("Flow times", 54, flow_times(55))
    return board.render()


# ------------------------------------------------------ Redirect pages


def redirect(agent: str, name: str) -> Json:
    board = Board(f"epic-agent-{agent}", f"Agents · {name}", [])
    board.add(
        text(
            f"This page moved. **[Open {name} on the agent page ›]"
            f"(/d/epic-agent?var-agent={agent})** · [Cockpit](/d/epic-agents)"
        ),
        0,
        0,
        24,
        3,
    )
    return board.render()


def build() -> dict[str, Json]:
    pages = {
        "overview.json": cockpit(),
        "agent.json": agent_detail(),
        "delivery.json": delivery_page(),
        "tickets.json": tickets_page(),
    }
    for agent, name in LEGACY_PAGES.items():
        pages[f"{agent}.json"] = redirect(agent, name)
    return pages


def render(page: Json) -> str:
    return json.dumps(page, indent=2, ensure_ascii=False) + "\n"


def main() -> None:
    for name, page in build().items():
        (OUT / name).write_text(render(page))


if __name__ == "__main__":
    main()
