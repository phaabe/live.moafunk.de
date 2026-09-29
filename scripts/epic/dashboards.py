"""Build the agent Grafana dashboards: Cockpit, Agent detail and Delivery.

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
        self, uid: str, title: str, links: list[Json], time_from: str = "now-24h"
    ) -> None:
        self.uid, self.title, self.links, self.time_from = uid, title, links, time_from
        self.panels: list[Json] = []
        self.variables: list[Json] = []

    def add(self, panel: Json, x: int, y: int, w: int, h: int) -> None:
        panel["id"] = len(self.panels) + 1
        panel["gridPos"] = {"x": x, "y": y, "w": w, "h": h}
        self.panels.append(panel)

    def render(self) -> Json:
        return {
            "uid": self.uid,
            "title": self.title,
            "tags": ["agents", "epic"],
            "schemaVersion": 39,
            "version": 1,
            "editable": False,
            "graphTooltip": 1,
            "refresh": "5s",
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
                "custom": {"filterable": False, "align": "auto", "inspect": False},
                "color": {"mode": "thresholds"},
                "thresholds": steps((None, NEUTRAL)),
            },
            "overrides": overrides,
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


# ---------------------------------------------------------------- Cockpit


def needs_anton() -> str:
    """Reasons to look now; -1 when the pause is the only one."""
    reasons = " + ".join(
        f"(({expr}) or vector(0))"
        for expr in (
            f"sum(epic_needs_operator{GITHUB})",
            "sum(epic_handoff_stalled)",
            "count(epic_tick_consecutive_failures >= 3)",
            "count(epic_agent_presence == 4)",
            "count(count by (target) (epic_agent_collision) > 1)",
            'count((sum by (agent) (increase(epic_permission_decisions_total{decision="deny"}[1h])) '
            "and on(agent) epic_agent_info) >= 5)",
            "count(count by (agent) (epic_backoff_info) >= 3)",
            "count(epic_handoff_wait_seconds > 3600)",
        )
    )
    return (
        f"((({reasons}) > 0) or (-1 * sum(epic_pause_requested == 1)) or vector(0))"
        f"{LOCAL}"
    )


def attention_tiles(board: Board) -> None:
    red = steps((None, NEUTRAL), (1, ACT_NOW))
    board.add(
        stat(
            "Needs Anton",
            [target(needs_anton())],
            mappings=[value_map({"-1": ("paused", LOOK_SOON), "0": ("0", NEUTRAL)})],
            thresholds=red,
            description="needs-anton labels, a stalled handoff, an agent failing 3 "
            "times in a row or late, a collision, ≥ 5 denials in 1 h or ≥ 3 "
            "backoffs on one agent, a PR waiting > 60 min. Amber when the pause "
            "is the only reason. — when local data is stale.",
        ),
        0,
        0,
        3,
        3,
    )
    board.add(
        stat(
            "Paused",
            [target(f"max(epic_pause_requested){LOCAL}")],
            mappings=[value_map({"0": ("No", NEUTRAL), "1": ("Yes", LOOK_SOON)})],
        ),
        3,
        0,
        3,
        3,
    )
    board.add(
        stat(
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
        ),
        6,
        0,
        3,
        3,
    )
    for i, (title, expr, description) in enumerate(
        (
            (
                "Agents failing",
                "count(epic_tick_consecutive_failures >= 3)",
                "Agents whose last 3 or more ticks failed.",
            ),
            (
                "Agents late",
                "count(epic_agent_presence == 4)",
                "No tick started for twice the interval plus the tick budget.",
            ),
            (
                "Collisions",
                "count(count by (target) (epic_agent_collision) > 1)",
                "Targets that two running agents work on at once.",
            ),
        )
    ):
        board.add(
            stat(
                title,
                [target(f"(({expr}) or vector(0)){LOCAL}")],
                thresholds=red,
                description="",
            ),
            9 + 3 * i,
            0,
            3,
            3,
        )
    board.add(
        stat(
            "PRs waiting > 30 min",
            [
                target(
                    f"((count(epic_handoff_wait_seconds > 1800)) or vector(0)){LOCAL}"
                )
            ],
            thresholds=steps((None, NEUTRAL), (1, LOOK_SOON), (2, ACT_NOW)),
        ),
        18,
        0,
        3,
        3,
    )
    board.add(
        stat(
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
            unit="s",
            decimals=0,
            overrides=[
                by_name(
                    "local",
                    (
                        "thresholds",
                        steps((None, NEUTRAL), (30, LOOK_SOON), (120, ACT_NOW)),
                    ),
                ),
                by_name(
                    "GitHub", ("thresholds", steps((None, NEUTRAL), (300, LOOK_SOON)))
                ),
            ],
        ),
        21,
        0,
        3,
        3,
    )


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
        "E": f"(epic_tick_elapsed_seconds / on(agent) epic_tick_budget_seconds){LOCAL}",
        "F": f"(time() - max by (agent) (epic_tick_last_info)){LOCAL}",
        "G": f"epic_tick_consecutive_failures{LOCAL}",
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
        "recent_text",
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
        "recent_text": "Last 20",
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
            ("custom.width", 90),
            ("links", [link("Agent page", "/d/epic-agent?var-agent=${__value.raw}")]),
        ),
        by_name(
            "Label",
            ("custom.minWidth", 60),
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
            ("custom.minWidth", 125),
            cell("color-background", mode="basic"),
            ("color", {"mode": "fixed", "fixedColor": "transparent"}),
            ("mappings", [regex_map("^collision.*", ACT_NOW, 0)]),
            github_link("target"),
        ),
        by_name("Task", ("custom.minWidth", 80), github_link("task_url")),
        by_name(
            "Tick elapsed",
            ("custom.width", 90),
            ("unit", "percentunit"),
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
            ("custom.width", 120),
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
            ("custom.width", 64),
            ("unit", "s"),
            ("decimals", 0),
            cell("color-text"),
            ("color", {"mode": "fixed", "fixedColor": MUTED}),
        ),
        by_name(
            "Failed",
            ("custom.width", 44),
            cell("color-background", mode="basic"),
            ("thresholds", steps((None, "transparent"), (2, LOOK_SOON), (3, ACT_NOW))),
        ),
        by_name(
            "Last 20",
            ("custom.width", 140),
            cell("color-background", mode="basic"),
            ("color", {"mode": "fixed", "fixedColor": "transparent"}),
            # The worst outcome in the last 20 ticks sets the color.
            (
                "mappings",
                [
                    regex_map(".*\\b(err|int)\\b.*", OUTCOME["error"], 0),
                    regex_map(".*\\bkil\\b.*", OUTCOME["killed"], 1),
                    regex_map(".*\\btmo\\b.*", OUTCOME["timeout"], 2),
                    regex_map(".*\\bblk\\b.*", OUTCOME["blocked"], 3),
                    regex_map("^\\d+ ok$", OUTCOME["ok"], 4),
                ],
            ),
        ),
        by_name(
            "Next tick",
            ("custom.width", 80),
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
            ("custom.width", 64),
            cell("color-text"),
            ("thresholds", steps((None, GREY), (1, "#F0B94A"))),
        ),
        by_name(
            "Denied 1 h",
            ("custom.width", 72),
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
        "outcome counts of the last 20 ticks, colored by the worst. Backoff: "
        "Codex retry delays. Denied: Claude permission gate.",
    )
    panel["options"]["cellHeight"] = "md"
    return panel


def outcome_timeline() -> Json:
    return {
        "type": "state-timeline",
        "title": "Tick outcomes · 24 h",
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
                "color": {"mode": "thresholds"},
                "thresholds": steps((None, "#2C2F36")),
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
            "legend": {"showLegend": False},
        },
    }


def handoff_table() -> Json:
    """Two rows: what each kind waits for, and who could review it."""
    reviewers = " or ".join(
        f'label_replace(epic_agents_registered_kind{{kind="{reviewer}"}}{LOCAL}, '
        f'"waiter_kind", "{waiter}", "", "")'
        for waiter, reviewer in (("claude", "codex"), ("codex", "claude"))
    )
    queries = {
        "A": reviewers,
        "B": f"count by (waiter_kind) (epic_handoff_wait_seconds){LOCAL}",
        "C": f"max by (waiter_kind) (epic_handoff_wait_seconds){LOCAL}",
    }
    names = ["waiter_kind", "Value #B", "Value #C", "Value #A"]
    rename = {
        "waiter_kind": "PRs by",
        "Value #B": "Waiting",
        "Value #C": "Longest wait",
        "Value #A": "Reviewers",
    }
    return table_panel(
        "Handoff · per kind",
        [target(q, table=True, ref=r) for r, q in queries.items()],
        [join("waiter_kind"), *keep_fields(names, rename, [])],
        [
            by_name(
                "PRs by",
                ("custom.width", 70),
                cell("color-text"),
                ("mappings", [value_map({k: ("", c) for k, c in KIND.items()})]),
            ),
            by_name("Waiting", ("noValue", "0"), ("custom.width", 70)),
            by_name(
                "Longest wait",
                ("custom.width", 100),
                ("unit", "s"),
                cell("color-background", mode="basic"),
                (
                    "thresholds",
                    steps((None, "transparent"), (900, LOOK_SOON), (1800, ACT_NOW)),
                ),
            ),
            by_name(
                "Reviewers",
                cell("color-background", mode="basic"),
                ("thresholds", steps((None, LOOK_SOON), (1, "transparent"))),
            ),
        ],
        description="Ready PRs of each kind waiting for a review by the other "
        "kind. Amber: a wait over 15 min, or no agent of the reviewer kind. "
        "Red: over 30 min.",
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
            "defaults": {"color": {"mode": "fixed", "fixedColor": GREY}, "min": 0},
            "overrides": [
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
            "showValue": "never",
            "xField": "area",
            "legend": {
                "showLegend": True,
                "displayMode": "list",
                "placement": "bottom",
            },
        },
    }


def open_prs() -> Json:
    names = ["title", "agent", "review", "checks", "draft", "target"]
    return table_panel(
        "Open PRs",
        [target(f"epic_pr_info{GITHUB}", table=True)],
        keep_fields(
            names,
            {
                "title": "PR",
                "agent": "By",
                "review": "Review",
                "checks": "CI",
                "draft": "Draft",
            },
            [],
        ),
        [
            by_name("PR", github_link("target")),
            by_name(
                "By",
                ("custom.width", 70),
                cell("color-text"),
                ("mappings", [value_map({k: ("", c) for k, c in KIND.items()})]),
            ),
            by_name(
                "Review",
                ("custom.width", 150),
                cell("color-text"),
                (
                    "mappings",
                    [
                        value_map(
                            {
                                "approved": ("", "#5AB45F"),
                                "changes requested": ("", LOOK_SOON),
                                "waiting": ("pending", MUTED),
                            }
                        )
                    ],
                ),
            ),
            by_name(
                "CI",
                ("custom.width", 80),
                cell("color-text"),
                (
                    "mappings",
                    [
                        value_map(
                            {
                                "green": ("pass", "#5AB45F"),
                                "failed": ("fail", "#E5484D"),
                                "pending": ("", MUTED),
                                "unknown": ("", MUTED),
                            }
                        )
                    ],
                ),
            ),
            by_name("Draft", ("custom.width", 60)),
            hidden("target"),
        ],
        description="Review is the other kind's latest verdict for the current "
        "head. This page never authorizes a merge.",
        no_value="No open PRs",
    )


def cockpit() -> Json:
    board = Board(
        "epic-agents",
        "Agents · Cockpit",
        [
            link("Agent detail", "/d/epic-agent"),
            link("Delivery", "/d/epic-delivery"),
            link("Epic", f"{REPO}/issues/312"),
        ],
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
        "Agents · Agent detail",
        [link("Cockpit", "/d/epic-agents"), link("Delivery", "/d/epic-delivery")],
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
            "Tokens today",
            f"sum(epic_tokens_today{{{a}}}){LOCAL}",
            "short",
            None,
            not_reported("Codex"),
        ),
    )
    for i, (title, expr, unit, thresholds, no_value) in enumerate(tiles):
        board.add(
            stat(
                title,
                [target(expr)],
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
            by_name("Duration", ("unit", "s"), ("noValue", "running")),
            by_name("Tokens", ("noValue", "–")),
            by_name("Started", ("custom.width", 170)),
            by_name("Exit", ("custom.width", 50)),
            by_name(
                "Target",
                ("custom.width", 90),
                ("mappings", SHORT_LINKS),
                ("links", [link("Open on GitHub", "${__value.raw}")]),
            ),
        ],
        description="Newest first. Tokens only for Codex. Source: events "
        "(written by the runner) or log (best effort).",
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
        "Agents · Delivery",
        [link("Cockpit", "/d/epic-agents"), link("Agent detail", "/d/epic-agent")],
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
            [ranged(f"epic_review_rounds_median{DELIVERY}{FRESH_AT_END}")],
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
            [ranged(f"epic_time_to_merge_median_seconds{DELIVERY}{FRESH_AT_END}")],
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
                f"max by (target) (epic_handoff_wait_seconds){LOCAL}",
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
