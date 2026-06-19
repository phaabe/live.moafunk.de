# Stream observability stack (issue #178, Phase 4 of #164)

Prometheus + Alertmanager + Blackbox + `icecast_exporter` (+ Grafana), supervised
by systemd via docker compose. Monitors the Icecast-KH/Liquidsoap stack and pages
to the project Telegram bot. **All UIs/metrics ports bind to `127.0.0.1`** — never
exposed publicly.

```
blackbox ── GET /status-json.xsl (synthetic "is Icecast reachable?") ────┐
icecast_exporter ── Icecast /status-json.xsl (per-mount listeners) ──────┤─▶ Prometheus ─▶ Alertmanager ─▶ Telegram
                                                                          │        └─▶ Grafana (127.0.0.1:3000)
```

> Blackbox probes the **status endpoint**, not the audio mount: a live MP3 mount
> is an infinite stream and blackbox reads the body to EOF, so probing `/live.mp3`
> always times out (verified). "Is the live source actually feeding?" is covered
> by `IcecastNoLiveSource` (the per-mount `icecast_listeners` series disappears).

## What it alerts on (and the #178 landmine)

| Alert | Expr (severity) | Meaning |
|-------|-----------------|---------|
| `StreamProbeDown` | `probe_success{job="blackbox_stream"} == 0` for 5m (**critical**) | Icecast status endpoint unreachable — canonical "server down" |
| `IcecastExporterDown` | `up{job="icecast"} == 0` for 5m (**critical**) | Icecast/exporter scrape failing |
| `IcecastNoLiveSource` | `absent(icecast_listeners{listenurl=~".*/live.mp3"})` for 10m (**critical**) | Liquidsoap stopped feeding the live mount |
| `StreamZeroListenersProlonged` | `sum(icecast_listeners) == 0` for 1h (**info**) | informational only (normal between shows) |
| `StreamProbeAbsent` | `absent(probe_success{...})` for 10m (**warning**) | monitoring itself is broken |

**Never** write `... or on() vector(0)` in an alert expr — `vector(0)` is always
present and `< 1`, so the rule fires forever. "stream-down" and "zero-listeners"
are deliberately **separate** alerts; absence uses `absent()` / `up == 0`. (That
`or vector(0)` trick is only for Grafana panels.)

## Deploy

> Runs upstream images (`prom/*`, `markuslindenberg/icecast_exporter`, `grafana/grafana`).

### Via CI (preferred — no manual SSH)

Run the **backend** workflow with `deploy_monitoring=true` (manual dispatch). It pulls
`TELEGRAM_*` + the box IP/SSH key from Bitwarden and runs `backend/scripts/deploy_monitoring.sh`,
which ships this dir to `/etc/moafunk/monitoring`, writes `monitoring.env`, and starts the
systemd unit. Additive + reversible — never touches the backend, stream stack, or DNS.

```bash
gh workflow run backend.yml -f deploy_monitoring=true
```

Grafana's admin password defaults to `admin` (UI is `127.0.0.1`-only, SSH-tunnelled). To
harden, set `GRAFANA_ADMIN_PASSWORD` in `/etc/moafunk/monitoring/monitoring.env` and
`systemctl restart monitoring`.

### Manually (on the box)

> Place this dir at `/etc/moafunk/monitoring`.

```bash
cd /etc/moafunk/monitoring
cp monitoring.env.example monitoring.env      # fill TELEGRAM_*, GRAFANA_* from Bitwarden
chmod +x mon-compose.sh                       # unit calls it to resolve docker compose vs docker-compose
cp monitoring.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now monitoring   # renders alertmanager.yml + compose up
```

> The box may have only the standalone `docker-compose` binary (no `docker compose`
> v2 plugin). `monitoring.service` calls `mon-compose.sh`, which resolves whichever
> is present — so don't hardcode `docker compose` in the unit.

## Verify

Metric names were reconciled against the live exporter on 2026-06-19 (the
`icecast-exporter`/`blackbox` ports are NOT host-published — query via Prometheus,
not `localhost:9146`/`:9115`):

```bash
# All targets healthy (prometheus, icecast, backend, blackbox_stream → up==1):
curl -s 'localhost:9090/api/v1/query?query=up' | jq -r '.data.result[]|"\(.metric.job)=\(.value[1])"'
# Blackbox status probe succeeds (was 0 while probing the audio mount):
curl -s 'localhost:9090/api/v1/query?query=probe_success' | jq '.data.result[].value[1]'
# Live source present (per-mount series; this is what IcecastNoLiveSource watches):
curl -s 'localhost:9090/api/v1/query?query=icecast_listeners' | jq -r '.data.result[].metric.listenurl'
# All rules healthy:
curl -s 'localhost:9090/api/v1/rules' | jq '[..|.health?//empty]|unique'
```

The exporter emits per-mount `icecast_listeners{listenurl=…}` + `icecast_up`, but
**no `icecast_sources`** — hence `IcecastNoLiveSource` keys off the per-mount
series existing. The Blackbox probe targets the **status endpoint** (finite),
never the audio mount (infinite stream → body-read timeout).

Test the page path: `systemctl stop liquidsoap` (or block Icecast) → after 5m
`StreamProbeDown`/`IcecastNoLiveSource` fire to Telegram; restart → they resolve.

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
