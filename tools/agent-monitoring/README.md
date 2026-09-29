# Claude and Codex progress

Local Grafana for the architecture epic runners. It observes work; it does not
start agent ticks, change GitHub, or deploy anything to production.

## Start on this Mac

Requires Python 3.10+, Docker Compose, and `gh` authenticated with access to
the repository and project board. Use the existing host login. No credential
is copied to containers.

From this worktree's root:

```bash
bash tools/agent-monitoring/service.sh start
```

Open [Grafana](http://127.0.0.1:13000/d/epic-agents). It has three pages:

- [Overview](http://127.0.0.1:13000/d/epic-agents): both agents side by side.
- [Claude](http://127.0.0.1:13000/d/epic-agent-claude) and
  [Codex](http://127.0.0.1:13000/d/epic-agent-codex): one agent in detail,
  with its runner log.

Grafana is a read-only local
viewer with no login or default administrator account. Prometheus is at
<http://127.0.0.1:19090>. Both ports bind only to `127.0.0.1`.

The host collector runs under launchd for the **current login session** and
restarts if it exits. Run `start` again after logging out or rebooting.
The worktree must remain at its current path. No login item is installed.

```bash
bash tools/agent-monitoring/service.sh status
bash tools/agent-monitoring/service.sh stop
```

Stopping preserves the named Docker volumes. They hold up to 30 days of
Prometheus data, capped at 1 GB, 30 days of runner logs, and Grafana settings.

For foreground use or custom runner paths, stop the service first, then:

```bash
bash tools/agent-monitoring/run.sh --state-dir /path/to/epic-loop
# Ctrl-C stops collection; stop the containers separately:
docker compose -f tools/agent-monitoring/compose.yaml down
```

## Run more agents

A runner can run as its own agent. Set `EPIC_AGENT_ID` (for example
`claude-2` or `codex-review`; the id starts with the kind) and schedule the
runner as usual. Optional: `EPIC_AGENT_LABEL` (up to 40 characters) and
`EPIC_AGENT_INTERVAL_SECONDS` (your schedule; default 600 for Claude, 180 for
Codex). The interval decides when an agent counts as late.

The runner registers in `agents/<id>/` of the state dir and keeps its lock,
log and state files there. The collector and Alloy find it within 10 seconds.
Runners without `EPIC_AGENT_ID` keep the old files and show as `claude` and
`codex`. At most 12 agents are shown.

```bash
python3 scripts/epic/agents.py list              # what the collector sees
python3 scripts/epic/agents.py retire --id claude-2
```

A retired agent stays visible for 24 hours. Agents of one kind share the
selector, so two of them can pick the same PR. The dashboard shows this as a
collision; it does not prevent it.

## What the dashboards show

Every task is placed in the epic as a path:
Epic › area › task › subtask › leaf › PR, for example
`B1 · Authorize…` › `B1.1 · Define authorization…` › `B1.1.6 · Stop scheduled…`
› `PR 410`. Each level links to its issue or PR. For an issue, the leaf shown
is the next open one in the epic's batch order. For a PR, it is the PR's
`Leaf IDs:` line. The overview shows the running task, or the last successful
session's task when no tick runs.

| View | Meaning |
| --- | --- |
| Task path | Where the current, last or queued action sits in the epic |
| Runner state and tick elapsed | Lock owner's PID is alive, absent, or over its timeout budget |
| Current action | Action stored by the runner, linked to its issue or PR |
| Last successful session | Model exited successfully and the shared gate recorded it |
| Last observed tick exit | Latest finish marker found in a bounded log tail |
| Next actions and dependency waits | Shared selector's proposals and prerequisite leaves |
| Open PRs | Executor, title, draft status, current-head counterpart verdict and CI |
| Issues and checked leaves | Project statuses and unique leaf checkboxes, per executor |
| Needs Anton | Issues and PRs carrying `needs-anton` |
| Delivery history | Completed issue count and merged-PR window since collection began |
| Freshness and alerts | Missing/stale collector data and runner metadata problems |
| Runner state over time (agent page) | When ticks ran |
| Tick summary and full log (agent page) | The runner log from Loki; search with the box at the top |

The dashboards are generated. Change `scripts/epic/dashboards.py`, then run
`python3 scripts/epic/dashboards.py` and commit the JSON. A test fails when the
JSON is out of date.

Activity is collected every 5 seconds; Prometheus scrapes every 5 seconds and
Grafana refreshes every 5 seconds. Visible latency can reach about 15 seconds.
GitHub is polled 120 seconds after each fetch completes, with a 90-second fetch
timeout. This avoids making GitHub requests at dashboard refresh frequency.

The local panels hide data older than 30 seconds; GitHub panels hide data older
than 300 seconds. A failed GitHub poll retains the old snapshot and timestamp,
shows failure, and never turns unknown work into zero work. Alerts appear in
Grafana and Prometheus; no external notifications are sent.

## Interpretation and limits

- A live tick can be selecting work or waiting on GitHub. It does not prove
  that a model is generating output. An empty action file during selection
  still counts as a live tick.
- Runner identity uses the PID in the existing lock. PID reuse is possible;
  overdue and orphaned locks need inspection. Inactive does not prove that
  a scheduler is installed or healthy.
- Pause means a request to stop starting work. An existing tick may still run.
- A successful model exit is not a completed issue. A merged PR is not proof
  of activation. Checked leaves and project status remain separate measures.
- Queue rows come from the existing selector, including its current merge
  proposals. They are informational and never replace the merge checker.
- Verdicts shown are the latest unedited standalone counterpart comment for
  the current head. This view does not include GitHub review approvals.
- The shared GitHub collector fetches up to 100 open PRs and 300 merged PRs
  per integration branch, and 500 project items. Merged totals are a bounded
  window, not a lifetime counter. It does not backfill historical events.
- Only issues in this repository assigned to Claude or Codex are counted.
  A `needs-anton` issue and its PR can both count. PR attribution uses the
  Executor/Reviewer text, because the agents share a GitHub account.
- The last exit is found in the final 128 KiB of each log. It can refer to an
  earlier tick or be absent. No error rate is inferred from this sample.
  Exit 75 is shown as "blocked": Codex's session ended with a valid blocked
  result and its runner waits before trying the same task again.
- The tick ledger (`scripts/epic/ticks.py`) reads each runner log once in
  full, then only new lines. It names each tick's outcome: ok, blocked,
  timeout, killed, interrupted (no finish line) or error. Exit 75 is blocked
  only if the runner logged a blocked result. A marker in the log cannot
  prove who wrote it, so this data is best effort (`source="log"`).
  Counters (`epic_ticks_total`) count only ticks that finish after the
  collector started reading; older history fills the other tick panels. The
  log has no finish time, so a tick's end is the time the collector read it
  (up to 5 s late), or unknown for history. The checkpoint is
  `runtime/ticks-<agent>.json` (`ticks-agents-<id>.json` for registered
  agents); delete it to rebuild (counters restart at 0).
- Runners also write `<kind>-ticks.jsonl`: one start and one finish event per
  tick, with the outcome and stage the runner knows (a quota stop or a
  blocked result is "blocked", not "error") and the real finish time. Once a
  runner writes events, they replace the log as the counted source
  (`source="events"` for Codex; `events_unverified` for Claude, whose model
  might write that file). Invalid lines are counted in
  `epic_tick_events_rejected_total` and skipped.
- `epic_backoff_info` lists Codex retry delays that are still active (value =
  when the delay ends), and whether the PR head is still current. The reason
  text is never exported.
- `epic_permission_decisions_total` counts the Claude permission gate's allow
  and deny answers. Its log is also a Loki stream (`stream="permissions"`,
  label `decision`); lines older than 167 hours are not sent (Loki rejects
  lines older than a week).
- Handoff (`epic_handoff_wait_seconds`, per kind): a PR waits for review
  when the selector would offer the other kind a `review` for its head. The
  clock starts when the collector first sees that head waiting, restarts on
  a new head, and is kept in `runtime/handoff.json` across restarts. It is
  the observed wait, not the time since the push. `epic_handoff_stalled` is
  1 when both kinds have a PR waiting over 30 min, no pause, no agent of
  either kind running, new or unreadable, and the observation is under
  5 min old: a suspected stalled handoff.
- Delivery (`delivery.prom`, per kind): merged PRs per day, review rounds
  (heads the reviewer gave a verdict on) and time to merge for PRs merged in
  the last 14 days; medians over the last 7. It uses REST calls only, after
  each GitHub poll; merged PRs are cached in `runtime/delivery.json` and their
  comments checked again every 30 min. A PR with incomplete comment history
  sets `epic_delivery_complete 0` and is left out, not guessed.
- `epic_area_leaves` counts checklist leaves per epic area and executor
  kind; a leaf in several issues counts once.
- An agent is late when no tick started for longer than twice its interval
  plus its tick budget. A pause never makes an agent late.
- Log lines get the time Alloy read them. On the first start, older lines all
  get that start time. Lines over 16 KB are cut.
- Token costs, model utilization, per-task percentage and completion ETA are
  omitted because these runners do not provide reliable inputs for them.

## Data and troubleshooting

The collector reads `~/.local/state/epic-loop/` and `~/.epic-pause` by default.
Use `--state-dir` (or `EPIC_STATE_DIR`) and `--pause-file` for other locations.
`run.sh` and `service.sh` give the same state directory to the collector and to
Alloy, so metrics and logs always come from one runner. It reads lock/gate
JSON and bounded log tails; model text and prompts never enter metrics.
Only sanitized `.prom` files are mounted into the textfile exporter.

The runner logs are shown on the agent pages. Alloy reads the state directory
read-only, but only the logs the collector lists in
`runtime/alloy/targets.json`: regular files of known agents, never links.
Without a running collector, no new log lines reach Loki. These logs contain
model transcripts, prompts and command output. They stay on this Mac: Loki and
Alloy have no published ports, and Grafana binds to `127.0.0.1`. No container gets host
credentials or source code.

Runtime files and the collector log are in the ignored `runtime/` directory.
Only one collector may use an output directory. The launchd script controls
one service named `de.moafunk.agent-monitoring` on this Mac. Stop the foreground
collector before starting that service, or the output lock will reject it.

If local data age rises, check the service and `runtime/collector.log`.
If GitHub collection fails, check `gh auth status` and project access on the
host. Logs report error classes without copying subprocess responses. Run
`python3 scripts/epic/next_action.py --status` for the underlying diagnostic.

```bash
python3 -m unittest discover -s scripts/epic
ruff check scripts/epic
ruff format --check scripts/epic
docker compose -f tools/agent-monitoring/compose.yaml config --quiet
docker compose -f tools/agent-monitoring/compose.yaml exec -T prometheus \
  promtool check config /etc/prometheus/prometheus.yml
```

Configuration follows [Grafana provisioning](https://grafana.com/docs/grafana/latest/administration/provisioning/),
[Prometheus configuration](https://prometheus.io/docs/prometheus/latest/configuration/configuration/)
and the [textfile collector](https://github.com/prometheus/node_exporter#textfile-collector).
