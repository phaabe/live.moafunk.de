# Stream observability stack (issue #178, Phase 4 of #164)

Prometheus + Alertmanager + Blackbox + `icecast_exporter` (+ Grafana), supervised
by systemd via docker compose. Monitors the Icecast-KH/Liquidsoap stack and pages
to the project Telegram bot. **All UIs/metrics ports bind to `127.0.0.1`** — never
exposed publicly.

```
blackbox ── GET /live.mp3 (synthetic "can listeners reach the stream?") ─┐
icecast_exporter ── Icecast /status-json.xsl (listeners, sources) ───────┤─▶ Prometheus ─▶ Alertmanager ─▶ Telegram
                                                                          │        └─▶ Grafana (127.0.0.1:3000)
```

## What it alerts on (and the #178 landmine)

| Alert | Expr (severity) | Meaning |
|-------|-----------------|---------|
| `StreamProbeDown` | `probe_success{job="blackbox_stream"} == 0` for 5m (**critical**) | public mount unreachable — the canonical "stream down" |
| `IcecastExporterDown` | `up{job="icecast"} == 0` for 5m (**critical**) | Icecast/exporter scrape failing |
| `IcecastNoSource` | `icecast_sources == 0` for 10m (**critical**) | Liquidsoap stopped feeding Icecast |
| `StreamZeroListenersProlonged` | `sum(icecast_listeners) == 0` for 1h (**info**) | informational only (normal between shows) |
| `StreamProbeAbsent` | `absent(probe_success{...})` for 10m (**warning**) | monitoring itself is broken |

**Never** write `... or on() vector(0)` in an alert expr — `vector(0)` is always
present and `< 1`, so the rule fires forever. "stream-down" and "zero-listeners"
are deliberately **separate** alerts; absence uses `absent()` / `up == 0`. (That
`or vector(0)` trick is only for Grafana panels.)

## Deploy (on the box)

> Runs upstream images (`prom/*`, `markuslindenberg/icecast_exporter`, `grafana/grafana`).
> Place this dir at `/etc/moafunk/monitoring`.

```bash
cd /etc/moafunk/monitoring
cp monitoring.env.example monitoring.env      # fill TELEGRAM_*, GRAFANA_* from Bitwarden
cp monitoring.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now monitoring   # renders alertmanager.yml + compose up
```

## Verify (FIRST DEPLOY — reconcile metric names)

`icecast_exporter` metric names can differ by version. After it's up:

```bash
curl -s localhost:9146/metrics | grep ^icecast_      # confirm icecast_sources / icecast_listeners
curl -s 'localhost:9090/api/v1/rules' | jq '.. .health? // empty' | sort -u   # all "ok"
curl -s localhost:9090/api/v1/query?query=probe_success | jq '.data.result'   # blackbox probing the mount
```

If the exporter's names differ from `icecast_sources` / `icecast_listeners`,
update `prometheus/rules/stream-alerts.yml` (the `StreamProbeDown` alert needs no
change — it's blackbox-based). Then `systemctl reload`/restart `monitoring`.

Test the page path: `systemctl stop liquidsoap` (or block the mount) → after 5m
`StreamProbeDown` should fire exactly once to Telegram; restart → it resolves.

UIs (localhost only) — reach via SSH tunnel, e.g.
`ssh -L 9090:localhost:9090 -L 3000:localhost:3000 root@<box>`.

## Already done elsewhere (don't duplicate)

- **systemd `Restart=always` + memory cap** for Liquidsoap and Icecast-KH: in
  `../liquidsoap.service` / `../icecast.service` (docker `--memory`).

## Backend recording-durability metrics (`backend` scrape job)

The backend exposes a Prometheus `/metrics` endpoint (port 8000, localhost-only by
deployment) with `moafunk_*` counters/gauges the Icecast/Blackbox probes can't see —
a stuck recorder leaves the live mount healthy while the **archive** breaks:

| Metric | Type | Alert |
|--------|------|-------|
| `moafunk_recording_tee_dropped_total` | counter | `RecordingTeeDropping` (chunks dropped now → archive corrupting) |
| `moafunk_recording_incomplete_total` | counter | `RecordingFinalizedIncomplete` |
| `moafunk_recording_r2_upload_failures_total` | counter | `RecordingR2UploadFailing` |
| `moafunk_recording_{writer_stopped,segment_concat_failures,size_mismatch}_total` | counter | (detail for the above) |
| `moafunk_stream_active` / `moafunk_recording_active` | gauge | live state |
| `moafunk_icecast_*` | gauge | mirrors the cached `/api/stream/metrics` poll |

Prometheus scrapes `unheard-api:8000` **directly over the backend's docker network**
(joined as the external `backend` network — set `BACKEND_NETWORK` if its name isn't
`unheard-backend_default`). The backend publishes only on the host's `127.0.0.1:8000`,
and `/metrics` is `404`'d on the public nginx vhost, so it's never reachable from the
internet. All recording alerts are `increase()`-based, so they stay silent until the
backend is on this box; `BackendMetricsDown` (`up == 0`) is expected to fire pre-cutover.

## TODO / follow-ups (out of scope here)

- **Dead-air / silence (EBU-R128) probe**: byte/listener metrics look nominal
  during silence; add a loudness tap for true dead-air alerting.
- **SPOF / CDN**: put a CDN or Icecast-KH master→relay in front of the public
  mount (~300 listeners ≈ 38 Mbps off one box). Higher-leverage, bigger change.
- **"show-scheduled" signal** to gate `StreamZeroListenersProlonged` so it only
  matters when a show is on the calendar.
