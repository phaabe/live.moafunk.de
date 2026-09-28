"""Build the agent Grafana dashboards: one overview and one page per agent.

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
AGENTS = {"claude": "Claude", "codex": "Codex"}
REPO = "https://github.com/phaabe/live.moafunk.de"

# Hide panels whose source stopped updating instead of showing stale work.
LOCAL = " and on() (time() - epic_local_snapshot_timestamp_seconds < 30)"
GITHUB = " and on() (time() - epic_github_snapshot_timestamp_seconds < 300)"
CONTEXT_LABELS = (
    "epic, epic_url, area, task, task_url, subtask, subtask_url, leaf, leaves_done, pr"
)
# Level name, label, and the label holding its link.
LEVELS = (
    ("Epic", "epic", "epic_url"),
    ("Area", "area", None),
    ("Task", "task", "task_url"),
    ("Subtask", "subtask", "subtask_url"),
    ("Leaf", "leaf", "target"),
    ("PR", "pr", "target"),
)
STATE_COLORS = {
    "running": "green",
    "inactive": "text",
    "orphaned": "red",
    "overdue": "orange",
    "unknown": "purple",
}


def with_context(metric: str) -> str:
    """Add the task hierarchy to an action metric; keep rows without context."""
    guard = LOCAL if metric.startswith("epic_current") else GITHUB
    if metric.startswith("epic_last_successful"):
        guard = LOCAL
    joined = (
        f"(({metric}{guard}) * on(target) group_left({CONTEXT_LABELS}) "
        f"(epic_task_context_info{GITHUB}))"
    )
    return f"{joined} or on(agent, action, target) ({metric}{guard})"


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


def link(title: str, url: str) -> Json:
    return {"title": title, "url": url, "targetBlank": url.startswith("http")}


class Board:
    def __init__(self, uid: str, title: str, links: list[Json]) -> None:
        self.uid, self.title, self.links = uid, title, links
        self.panels: list[Json] = []
        self.y = 0
        self.row_height = 0

    def add(self, panel: Json, x: int, w: int, h: int) -> None:
        panel["id"] = len(self.panels) + 1
        panel["gridPos"] = {"x": x, "y": self.y, "w": w, "h": h}
        self.row_height = max(self.row_height, h)
        self.panels.append(panel)

    def row(self, title: str | None = None) -> None:
        """Start a new line of panels, optionally under a section header."""
        self.y += self.row_height
        self.row_height = 0
        if title:
            self.panels.append(
                {
                    "id": len(self.panels) + 1,
                    "type": "row",
                    "title": title,
                    "collapsed": False,
                    "gridPos": {"x": 0, "y": self.y, "w": 24, "h": 1},
                    "panels": [],
                }
            )
            self.y += 1

    def render(self) -> Json:
        return {
            "uid": self.uid,
            "title": self.title,
            "tags": ["agents", "epic"],
            "schemaVersion": 39,
            "version": 1,
            "editable": False,
            "refresh": "5s",
            "timezone": "browser",
            "time": {"from": "now-6h", "to": "now"},
            "links": self.links,
            "templating": {"list": []},
            "panels": self.panels,
        }


def text(content: str) -> Json:
    return {
        "type": "text",
        "title": "",
        "transparent": True,
        "options": {"mode": "markdown", "content": content},
    }


def stat(
    title: str,
    expr: str,
    *,
    unit: str = "none",
    legend: str = "",
    text_mode: str = "value",
    mappings: list[Json] | None = None,
    thresholds: list[Json] | None = None,
    overrides: list[Json] | None = None,
    description: str = "",
    no_value: str = "–",
) -> Json:
    return {
        "type": "stat",
        "title": title,
        "description": description,
        "datasource": PROM,
        "targets": [target(expr, legend=legend)],
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "noValue": no_value,
                "mappings": mappings or [],
                "color": {"mode": "thresholds"},
                "thresholds": {
                    "mode": "absolute",
                    "steps": thresholds or [{"color": "text", "value": None}],
                },
            },
            "overrides": overrides or [],
        },
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "values": False, "fields": ""},
            "textMode": text_mode,
            "colorMode": "value",
            "graphMode": "none",
            "justifyMode": "center",
            "orientation": "auto",
        },
    }


def table(
    title: str,
    expr: str,
    columns: dict[str, str],
    *,
    links: dict[str, str] | None = None,
    description: str = "",
    no_value: str = "Nothing here",
    colors: dict[str, dict[str, str]] | None = None,
    sort: str | None = None,
) -> Json:
    """Table of `columns` (label -> header), in that order.

    `links` maps a shown label to the hidden label holding its URL.
    """
    links = links or {}
    overrides = []
    for column, url in links.items():
        overrides.append(
            {
                "matcher": {"id": "byName", "options": columns[column]},
                "properties": [
                    {
                        "id": "links",
                        "value": [
                            link("Open on GitHub", "${__data.fields." + url + "}")
                        ],
                    }
                ],
            }
        )
    for url in sorted(set(links.values()) - set(columns)):
        overrides.append(
            {
                "matcher": {"id": "byName", "options": url},
                "properties": [{"id": "custom.hidden", "value": True}],
            }
        )
    for column, values in (colors or {}).items():
        overrides.append(
            {
                "matcher": {"id": "byName", "options": column},
                "properties": [
                    {
                        "id": "mappings",
                        "value": [
                            {
                                "type": "value",
                                "options": {
                                    value: {"color": color, "index": i}
                                    for i, (value, color) in enumerate(values.items())
                                },
                            }
                        ],
                    },
                    {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                ],
            }
        )
    transformations: list[Json] = [
        {
            "id": "filterFieldsByName",
            "options": {
                "include": {
                    "names": [*columns, *links.values(), *([sort] if sort else [])]
                }
            },
        },
    ]
    if sort:
        transformations.append(
            {"id": "sortBy", "options": {"sort": [{"field": sort, "desc": False}]}}
        )
    transformations.append(
        {
            "id": "organize",
            "options": {
                "excludeByName": {sort: True} if sort and sort not in columns else {},
                "indexByName": {name: i for i, name in enumerate(columns)},
                "renameByName": columns,
            },
        }
    )
    return {
        "type": "table",
        "title": title,
        "description": description,
        "datasource": PROM,
        "targets": [target(expr, table=True)],
        "fieldConfig": {
            "defaults": {
                "noValue": no_value,
                "custom": {"filterable": False, "wrapText": True},
            },
            "overrides": overrides,
        },
        "options": {"showHeader": True, "cellHeight": "sm"},
        "transformations": transformations,
    }


def runner_stats(board: Board, agent: str, x: int, width: int) -> None:
    """State, tick elapsed, last successful session, last exit."""
    w = width // 4
    board.add(
        stat(
            "State",
            f'epic_runner_state{{agent="{agent}"}}{LOCAL}',
            legend="{{state}}",
            text_mode="name",
            overrides=[
                {
                    "matcher": {"id": "byName", "options": state},
                    "properties": [
                        {"id": "color", "value": {"mode": "fixed", "fixedColor": color}}
                    ],
                }
                for state, color in STATE_COLORS.items()
            ],
            description="running: a tick holds the lock. It may be selecting "
            "work, not generating output. inactive: no tick runs now.",
        ),
        x,
        w,
        4,
    )
    board.add(
        stat(
            "Tick running for",
            f'epic_tick_elapsed_seconds{{agent="{agent}"}}{LOCAL}',
            unit="s",
            no_value="idle",
        ),
        x + w,
        w,
        4,
    )
    board.add(
        stat(
            "Last session",
            f'(time() - epic_last_session_success_timestamp_seconds{{agent="{agent}"}}){LOCAL}',
            unit="s",
            description="Time since the last model session that exited "
            "successfully. It does not mean the task is done.",
            thresholds=[
                {"color": "text", "value": None},
                {"color": "orange", "value": 6 * 3600},
            ],
        ),
        x + 2 * w,
        w,
        4,
    )
    board.add(
        stat(
            "Last exit",
            f'epic_last_observed_exit_code{{agent="{agent}"}}{LOCAL}',
            mappings=[
                {
                    "type": "value",
                    "options": {
                        "0": {"text": "ok", "color": "green"},
                        # Codex: the session ended with a valid blocked result.
                        "75": {"text": "blocked", "color": "orange"},
                    },
                },
                {
                    "type": "range",
                    "options": {"from": 1, "to": 255, "result": {"color": "red"}},
                },
            ],
            description="Last finished tick in the log. blocked: the task "
            "could not move; the runner waits before trying it again.",
        ),
        x + 3 * w,
        width - 3 * w,
        4,
    )


def queue_table(agent_filter: str) -> Json:
    return table(
        "Next actions and waits",
        with_context(f"epic_queued_action_info{{{agent_filter}}}"),
        {
            "agent": "Agent",
            "action": "Action",
            "task": "Task",
            "subtask": "Subtask",
            "leaf": "Next leaf",
            "leaves_done": "Leaves done",
            "reason": "Why",
        },
        links={"subtask": "subtask_url", "task": "task_url", "leaf": "target"},
        colors={"Action": {"wait": "text", "review": "blue", "claim": "green"}},
        no_value="–",
        description="What the shared selector would do next, from the "
        "latest GitHub poll. Wait rows name the leaves they wait for.",
    )


def pr_table(agent_filter: str) -> Json:
    return table(
        "Open PRs",
        f"(epic_pr_info{{{agent_filter}}}{GITHUB}) * on(target) "
        f"group_left(task, task_url, subtask, subtask_url, leaf) "
        f"(epic_task_context_info{GITHUB}) or on(target) "
        f"(epic_pr_info{{{agent_filter}}}{GITHUB})",
        {
            "agent": "Agent",
            "title": "PR",
            "subtask": "Subtask",
            "leaf": "Leaf",
            "review": "Review",
            "checks": "CI",
            "draft": "Draft",
        },
        links={"title": "target", "subtask": "subtask_url"},
        colors={
            "Review": {
                "approved": "green",
                "changes requested": "red",
                "waiting": "orange",
            },
            "CI": {
                "green": "green",
                "failed": "red",
                "pending": "orange",
                "unknown": "text",
            },
        },
        no_value="–",
        description="Review is the other agent's latest verdict for the "
        "current head. This page never authorizes a merge.",
    )


def progress(board: Board, agent_filter: str, x: int, w: int) -> None:
    for i, (title, expr, legend) in enumerate(
        (
            (
                "Issues by status",
                # `>` binds tighter than `and`; filter the counts, not the guard.
                f"(epic_issues{{{agent_filter}}}{GITHUB}) > 0",
                "{{agent}} · {{status}}",
            ),
            (
                "Checklist leaves",
                f"epic_checklist_leaves{{{agent_filter}}}{GITHUB}",
                "{{agent}} · {{state}}",
            ),
        )
    ):
        board.add(
            {
                "type": "bargauge",
                "title": title,
                "datasource": PROM,
                "targets": [target(expr, legend=legend)],
                "fieldConfig": {
                    "defaults": {
                        "min": 0,
                        "color": {"mode": "palette-classic"},
                        "noValue": "No data",
                    },
                    "overrides": [],
                },
                "options": {
                    "orientation": "horizontal",
                    "displayMode": "basic",
                    "showUnfilled": True,
                    "text": {"titleSize": 12, "valueSize": 18},
                    "reduceOptions": {"calcs": ["lastNotNull"], "values": False},
                },
            },
            x + i * (w // 2),
            w // 2,
            7,
        )


def overview() -> Json:
    pages = [
        link(f"{name} details", f"/d/epic-agent-{agent}")
        for agent, name in AGENTS.items()
    ]
    board = Board(
        "epic-agents",
        "Agents · Overview",
        [
            *pages,
            link("Epic", f"{REPO}/issues/312"),
            link("Project board", "https://github.com/users/anneoneone/projects/2"),
        ],
    )
    board.add(
        text(
            "Claude and Codex on the "
            f"[architecture epic]({REPO}/issues/312). "
            "Local data every 5 s, GitHub every 2 min. "
            "**Details and logs:** [Claude](/d/epic-agent-claude) · "
            "[Codex](/d/epic-agent-codex)"
        ),
        0,
        24,
        2,
    )
    board.row()
    health = (
        (
            "Local data age",
            "time() - epic_local_snapshot_timestamp_seconds",
            "s",
            [{"color": "green", "value": None}, {"color": "red", "value": 30}],
        ),
        (
            "GitHub data age",
            "time() - epic_github_snapshot_timestamp_seconds",
            "s",
            [{"color": "green", "value": None}, {"color": "red", "value": 300}],
        ),
        (
            "Last GitHub poll",
            "epic_github_collection_success",
            "none",
            [{"color": "red", "value": None}, {"color": "green", "value": 1}],
        ),
        (
            "Paused",
            f"epic_pause_requested{LOCAL}",
            "none",
            [{"color": "green", "value": None}, {"color": "orange", "value": 1}],
        ),
        (
            "Firing alerts",
            'count(ALERTS{alertstate="firing"}) or vector(0)',
            "none",
            [{"color": "green", "value": None}, {"color": "red", "value": 1}],
        ),
        (
            "Needs Anton",
            f"sum(epic_needs_operator{GITHUB})",
            "none",
            [{"color": "green", "value": None}, {"color": "orange", "value": 1}],
        ),
    )
    for i, (title, expr, unit, steps) in enumerate(health):
        mappings = []
        if title == "Last GitHub poll":
            mappings = [
                {
                    "type": "value",
                    "options": {"0": {"text": "failed"}, "1": {"text": "ok"}},
                }
            ]
        if title == "Paused":
            mappings = [
                {
                    "type": "value",
                    "options": {"0": {"text": "no"}, "1": {"text": "yes"}},
                }
            ]
        board.add(
            stat(title, expr, unit=unit, thresholds=steps, mappings=mappings),
            i * 4,
            4,
            3,
        )
    for i, (agent, name) in enumerate(AGENTS.items()):
        x = i * 12
        if i == 0:
            board.row()
        board.add(
            text(f"### [{name} ›](/d/epic-agent-{agent})"),
            x,
            12,
            2,
        )
    board.row()
    for i, agent in enumerate(AGENTS):
        runner_stats(board, agent, i * 12, 12)
    board.row()
    for i, (agent, name) in enumerate(AGENTS.items()):
        board.add(
            path_table(
                f"{name} · current task",
                f'(epic_current_action_info{{agent="{agent}"}}{LOCAL}) or on(agent) '
                f'label_replace(epic_last_successful_action_info{{agent="{agent}"}}{LOCAL}, '
                '"action", "last session: $1", "action", "(.*)")',
                "The running tick's action, placed in the epic. When no tick "
                "runs, the last successful session's action.",
            ),
            i * 12,
            12,
            8,
        )
    board.row()
    board.add(pr_table('agent=~".+"'), 0, 24, 8)
    board.row()
    board.add(queue_table('agent=~".+"'), 0, 24, 12)
    board.row("Delivery")
    progress(board, 'agent=~".+"', 0, 24)
    board.row()
    board.add(
        {
            "type": "timeseries",
            "title": "Done issues and merged PRs",
            "datasource": PROM,
            "targets": [
                {
                    **target(
                        'epic_issues{status="Done"}',
                        legend="{{agent}} · done issues",
                    ),
                    "instant": False,
                    "range": True,
                },
                {
                    **target(
                        "epic_merged_prs_in_window",
                        legend="{{agent}} · merged PRs",
                        ref="B",
                    ),
                    "instant": False,
                    "range": True,
                },
            ],
            "fieldConfig": {
                "defaults": {"custom": {"lineWidth": 2, "fillOpacity": 0}},
                "overrides": [],
            },
            "options": {"legend": {"displayMode": "list", "placement": "right"}},
        },
        0,
        24,
        7,
    )
    board.row()
    board.add(
        table(
            "Monitoring alerts",
            'ALERTS{alertstate="firing"}',
            {"alertname": "Alert", "agent": "Agent", "severity": "Severity"},
            no_value="No alerts",
        ),
        0,
        24,
        4,
    )
    return board.render()


def path_table(title: str, actions: str, description: str) -> Json:
    """Where an action sits in the epic: an indented, linked tree.

    `actions` is a guarded PromQL expression with one action series.
    """
    action = (
        f'label_replace(label_replace(label_replace(({actions}), "level", "Action", "", ""), '
        '"depth", "0", "", ""), "text", "$1", "action", "(.*)")'
    )
    tree = f"(epic_task_level_info{GITHUB}) * on(target) group_left() ({actions})"
    panel = table(
        title,
        f"{action} or {tree}",
        {"level": "Level", "text": "Path"},
        links={"text": "url"},
        description=description,
        no_value="No task",
        sort="depth",
    )
    panel["options"]["showHeader"] = False
    panel["fieldConfig"]["overrides"].append(
        {
            "matcher": {"id": "byName", "options": "Level"},
            "properties": [
                {"id": "custom.width", "value": 80},
                {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                {"id": "color", "value": {"mode": "fixed", "fixedColor": "blue"}},
            ],
        }
    )
    return panel


def agent_page(agent: str, name: str) -> Json:
    other = next(a for a in AGENTS if a != agent)
    board = Board(
        f"epic-agent-{agent}",
        f"Agents · {name}",
        [
            link("Overview", "/d/epic-agents"),
            link(f"{AGENTS[other]} details", f"/d/epic-agent-{other}"),
            link("Epic", f"{REPO}/issues/312"),
        ],
    )
    board.add(
        text(
            f"**{name}** runner · [‹ Overview](/d/epic-agents) · "
            f"[{AGENTS[other]}](/d/epic-agent-{other}) · "
            f"Log file: `~/.local/state/epic-loop/{agent}.log`"
        ),
        0,
        24,
        2,
    )
    board.row()
    runner_stats(board, agent, 0, 16)
    board.add(
        stat(
            "Open PRs",
            f'sum(epic_open_prs{{agent="{agent}"}}){GITHUB}',
        ),
        16,
        4,
        4,
    )
    board.add(
        stat(
            "Needs Anton",
            f'epic_needs_operator{{agent="{agent}"}}{GITHUB}',
            thresholds=[
                {"color": "green", "value": None},
                {"color": "orange", "value": 1},
            ],
        ),
        20,
        4,
        4,
    )
    board.row("Task context")
    board.add(
        path_table(
            "Working on now",
            f'epic_current_action_info{{agent="{agent}"}}{LOCAL}',
            "The running tick's action, placed in the epic. Empty when idle.",
        ),
        0,
        12,
        9,
    )
    board.add(
        path_table(
            "Last successful session",
            f'epic_last_successful_action_info{{agent="{agent}"}}{LOCAL}',
            "The last session that exited successfully. It does not mean "
            "the task is done.",
        ),
        12,
        12,
        9,
    )
    board.row()
    board.add(
        {
            "type": "state-timeline",
            "title": "Runner state over time",
            "datasource": PROM,
            "targets": [
                {
                    **target(
                        f'max by (state) (epic_runner_state{{agent="{agent}"}}) == 1',
                        legend="{{state}}",
                    ),
                    "instant": False,
                    "range": True,
                }
            ],
            "fieldConfig": {
                "defaults": {
                    "color": {"mode": "fixed", "fixedColor": "green"},
                    "custom": {"fillOpacity": 80, "lineWidth": 0},
                },
                "overrides": [
                    {
                        "matcher": {"id": "byName", "options": state},
                        "properties": [
                            {
                                "id": "color",
                                "value": {"mode": "fixed", "fixedColor": color},
                            }
                        ],
                    }
                    for state, color in STATE_COLORS.items()
                ],
            },
            "options": {
                "mergeValues": True,
                "showValue": "never",
                "rowHeight": 0.8,
                "legend": {"showLegend": False},
            },
        },
        0,
        24,
        5,
    )
    board.row("Work queue")
    board.add(pr_table(f'agent="{agent}"'), 0, 24, 7)
    board.row()
    board.add(queue_table(f'agent="{agent}"'), 0, 24, 10)
    board.row()
    progress(board, f'agent="{agent}"', 0, 24)
    board.row("Logs")
    board.add(
        logs(
            "Tick summary",
            f'{{agent="{agent}"}} |~ "^(tick: |\\\\{{\\"action\\")"',
            "Start, selected action, result and exit of each tick.",
        ),
        0,
        24,
        8,
    )
    board.row()
    board.add(
        logs(
            "Full log",
            f'{{agent="{agent}"}} |~ "(?i)$search"',
            "Everything the runner and model wrote. Use the Search box above.",
        ),
        0,
        24,
        18,
    )
    result = board.render()
    result["templating"]["list"] = [
        {
            "name": "search",
            "label": "Search log",
            "type": "textbox",
            "query": "",
            "current": {"text": "", "value": ""},
        }
    ]
    return result


def logs(title: str, expr: str, description: str) -> Json:
    return {
        "type": "logs",
        "title": title,
        "description": description,
        "datasource": LOKI,
        "targets": [{"refId": "A", "expr": expr, "queryType": "range"}],
        "options": {
            "showTime": True,
            "wrapLogMessage": True,
            "sortOrder": "Descending",
            "enableLogDetails": True,
            "dedupStrategy": "none",
        },
    }


def build() -> dict[str, Json]:
    pages = {"overview.json": overview()}
    for agent, name in AGENTS.items():
        pages[f"{agent}.json"] = agent_page(agent, name)
    return pages


def render(page: Json) -> str:
    return json.dumps(page, indent=2, ensure_ascii=False) + "\n"


def main() -> None:
    for name, page in build().items():
        (OUT / name).write_text(render(page))


if __name__ == "__main__":
    main()
