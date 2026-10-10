# Legacy API maintenance window

Work item: https://github.com/phaabe/live.moafunk.de/issues/381, O1.2.2.
Use for the first gate/precheck install and every interim O1.2.5 deployment
until O2 is activated. Source baseline: `06fe21c21256dfcf6bd29e2f0694852ceb1a0925`.
The [shared inventory](../evidence/runtime-inventory.md) records host evidence
and unresolved gates. This procedure is not production qualification.

## Entry conditions and record

The operator reserves a show-free window, including drain, failed-install
recovery and rollback time. Announce that API/admin access and reconnects are
unavailable; legacy MP3 may go silent. No approved fallback or HLS is assumed.
Record the operator, UTC start/deadline, approved target digest, rollback
digest, source commit and chosen path below. If a step fails, keep ingress
closed, record the failure and stop. A deadline is a refusal boundary, never
permission to kill a recording.

Before touching the host:

1. Complete O1.2.1 rollout: the release push must build only. Retain its run
   URL and image digest. Do not use the legacy backend workflow for this
   procedure: it still performs automatic replacement and media setup.
2. List queued/running legacy workflow runs for **all refs**, including manual
   runs. Drain them, or cancel and reconcile their remote processes. A cancelled
   Actions run does not prove its SSH child stopped. Check host processes and
   container/image state; prohibit new legacy/manual/cron deploys throughout
   the window. Record run IDs, outcomes and host observations.
3. Verify the inventory against the running image and live nginx configuration.
   Inspect configuration privately; retain hashes and redacted route/binding
   observations. Never paste environment values, cookies or private keys.
4. Pre-pull the exact approved image and retain the old image locally. Record
   RepoDigests and image IDs using narrowly formatted `docker inspect` output.
   Do not rely on `latest`, a mutable SHA tag or a registry remaining available.
5. Agree the scheduler boundary in the next section **before** admitting a
   maintenance attempt. Freeze external schedule writers and direct producers.

Log every phase transition in UTC and keep evidence under a root-only host
directory. Record booleans/counts, show/version IDs and artifact hashes, not
credentials, presigned URLs or private programme content. Retain complete
private logs where necessary; publish only redacted evidence links.

## One deployment at a time

Every operator and the future O1.2.5 host script must hold the same host lock
from before ingress closes until reopen or completed rollback. In a root
Bash session on the approved host:

```bash
set -euo pipefail
umask 077
exec 9>/run/lock/moafunk-api-deploy.lock
flock -n 9
```

If `flock` fails, exit without changing anything. Keep descriptor 9 open in
this session and its deployment child. Never unlink the lock file to bypass
an owner. Record owner PID and operation ID. A second attempt must fail before
nginx, snapshot or replacement. The lock is advisory: legacy workflows do not
take it, hence the explicit run/host reconciliation above. If the session
dies, ingress remains closed; the next operator reconciles remote processes,
snapshots and image state before continuing. Do not automate reopening in an
EXIT trap.

## Close admission and preserve drain access

The checked-in [map](../../stream-rework/local-test-harness/maintenance/admission-map.conf)
and [server rule](../../stream-rework/local-test-harness/maintenance/admission-server.conf)
are a temporary allowlist for the source baseline, not a replacement vhost.
Install the map once in nginx `http {}` and the rule at the beginning of
**every** backend ingress `server {}` before other rewrite/return directives.
Keep existing proxy, TLS, authentication and public-media locations intact.
Preserve the existing public `/metrics` denial; during the window the broader
rule refuses it too. The future internal precheck stays inaccessible publicly.

| Request | During drain |
| --- | --- |
| `GET /ws/stream` (also `test=true`, `force=true`, reconnects), `GET /ws/stream-test` | 503; no new producer or rehearsal |
| `POST /api/recording/start`, `POST /api/my-show/go-live` | 503; no recording or prerecorded start |
| `POST /api/shows`, `PUT /api/shows/:id`, `POST /api/my-show/confirm`, upload/finalize/delete and host-assignment mutations | 503; schedule and eligibility frozen |
| `GET /ws/recording/finalize`, `POST /api/shows/:id/recordings/reexport` | 503; no new merge/transcode/upload job |
| `POST /api/stream/stop`, `POST /api/recording/stop`, `POST /api/recording/marker` | Proxy normally with existing authentication |
| `GET /health`, `/api/auth/me`, `/api/stream/status`, `/api/stream/metrics`, `/api/recording/status`, `/api/shows`, `/api/shows/:id`, `/api/shows/:id/recordings`, `/api/my-show`, `/api/my-shows` | Proxy normally with existing authentication |
| `POST /api/auth/login`, `POST /api/auth/logout` | Proxy normally; establish operator session before drain |
| `GET /live.mp3`, `/test.mp3`, `/status-json.xsl` | Existing media locations remain usable |
| Every other new request | 503, including admin page/assets; preload the UI or use authenticated operator requests |

Route anchors: `backend/src/main.rs` route registration;
`handlers/stream_ws.rs`, `handlers/recording.rs`, and `handlers/api.rs`.
Recompare them to the **deployed** commit. The repository is not proof of what
the host serves. Add no allowlist exception without reviewing its effects.

Save the previous nginx configuration privately. Run `nginx -t`, then reload.
Probe the public hostname, alternate vhosts, HTTP/TLS and direct IP paths.
Require actual 503 responses for start, rehearsal, recording and schedule
requests, and working authenticated stop/diagnostic requests. A 401 from the
backend is not proof of ingress exclusion. Test without starting real work.
The [nginx map](https://nginx.org/en/docs/http/ngx_http_map_module.html) uses
normalized `$uri`, so query strings cannot opt out of the rule.

Existing sockets and requests survive a
[graceful reload](https://nginx.org/en/docs/control.html). Record the old
worker generation and reconcile every already-admitted mutation and producer
handshake. Either all old workers finish, or prove remaining old connections
are only listeners and cannot submit new API work. Do not kill listener
workers to manufacture this evidence. If that cannot be established, defer
the attempt or announce the additional ingress outage explicitly.

Also exclude loopback clients, other containers, secondary proxies, direct
harbor/RTMP sources and automation. An nginx rule does not block those paths.
Keep established producers alive until drained; no broad firewall drop or
media-service restart is a substitute. Verify the actual host network policy.

## Bound the scheduler, including catch-up

`scheduler::check_prerecorded_show_start` runs every 30 seconds, including an
immediate startup tick. It bypasses nginx and selects confirmed, unstarted
prerecorded shows from `date >= date('now', '-2 days')` with a start time.
It can start an already-due show until its parsed end, or for six hours after
start if end is absent/invalid. Overnight end dates matter. `show_start_utc`
uses Europe/Berlin and rejects ambiguous/nonexistent local times. A future-only
calendar view or a check for the next 30 minutes misses catch-up candidates.

Under the admission freeze, inspect every selected row and any task already
selected by the previous tick. Record IDs, resolved UTC times, confirmation,
started state, uploaded media, host assignment and occurrence state. Use the
exact deployed implementation for time conversion and eligibility. Treat
invalid/unknown data or a query failure as unresolved, never an empty schedule.
The whole interval through rollback and reopen must be covered. Do not edit
the database to fake `prerecorded_started_at` or invent a scheduler pause flag.

Choose and record one path:

- **Bounded running API:** prove no runnable or catch-up start exists and no
  candidate becomes due through the window deadline. Wait for any previously
  selected start to settle and repeat schedule/producer checks. All schedule
  writers remain excluded. If time approaches the boundary, stop the attempt.
- **Announced API outage:** when long-term scheduler exclusion cannot be
  proven, first establish a short safe interval for drain and shutdown. Finish
  existing work, then stop only `unheard-api` before that interval expires.
  Confirm the container and its child producers stopped and no restart source
  can revive it. The scheduler is then absent during snapshot/replacement.
  If even the drain-to-stop interval cannot be proven safe, postpone maintenance;
  stopping a busy API is not this fallback. Before starting either new or old
  image, resolve catch-up candidates again: a restart can immediately start one.

The outage path still needs a safe restart boundary. Starting the new image
behind closed nginx does not stop its scheduler or orphan recovery. If the
candidate cannot start safely with the retained data, leave the API stopped
and escalate instead of guessing. Operator changes to the schedule require
their own recorded decision and a fresh freeze/boundary check.

## Drain producer, capture and finalization

1. List live and test producers, capture sessions, existing finalize sockets,
   admitted upload/re-export requests and startup orphan-recovery jobs. Freeze
   new non-HTTP writers, including bots and scheduled host jobs relevant to
   these artifacts. Ask existing producers to finish normally; use authorized
   stop routes if agreed. Keep recording markers usable until capture ends.
2. Require stream inactive **including rehearsal**, and recording inactive.
   Account for the 30-second disconnect grace task in `stream_ws.rs`; a timed
   sleep alone is not completion evidence.
3. Correlate each captured show/version with completed raw upload, verified
   object size, markers, recording DB row and completed archive work. Check
   errors and local residual artifacts. Account for detached SoundCloud tasks
   and existing merge/re-export jobs. Read object metadata with host tooling;
   keep secrets and signed URLs out of the log.
4. Repeat admission, scheduler and drain checks immediately before snapshot
   and immediately before replacement. A new active task returns to drain.

**Do not accept idle status alone.** `finalize_and_upload` clears the recording
manager before awaiting upload. `/api/stream/stop` may return success after a
logged finalize failure; DB row creation can fail without failing the upload
response. The legacy API has no complete active-finalization registry.
No active ffmpeg process or no `finalizing` row is also insufficient. Retain a
per-operation ledger of terminal results. If a pending operation cannot be
reconciled, do not snapshot/replace; the outage path does not waive drain.
Unresolved recording artifacts stay on disk and in the backup, never deleted
as cleanup. Record any previously failed artifact separately from active work.

## Snapshot, replace and roll back

Hold admission and the lock throughout. Log `DRAINED` only with all evidence
above. Stop the API for a consistent filesystem snapshot even on the bounded
path; this is a brief replacement outage. Confirm no process writes its data
directory. Never use `docker compose down`, restart Docker or restart media.

Retain one operation-specific, root-readable recovery set:

- SQLite consistent backup, `PRAGMA integrity_check = ok`, checksum and schema
  version; include WAL/SHM with the stopped data directory as recovery evidence.
  Never copy only the live `.db` while writers may still run.
- Complete persisted data directory, including `recordings-temp`, segments,
  finalize checkpoints and upload staging. Record size and available disk.
- Effective Compose configuration and original files, nginx configuration,
  image identities and private runtime configuration. Keep secret-bearing files
  on the approved host with original permissions; publish only hashes.
- Old image available locally and a tested restore to a separate scratch
  directory. Open the restored SQLite backup read-only and check integrity.
  Backups are not evidence of a usable rollback until restore succeeds.

Use an operator-reviewed **API-only** invocation pinned as
`image: ghcr.io/phaabe/live.moafunk.de-backend@sha256:<approved digest>`.
The current `deploy_hetzner.sh` is unsuitable: even with nginx setup disabled
it provisions packages, changes Docker configuration, copies scripts and uses
Compose down/up. The current Compose file joins image and tag with a colon;
do not pass a digest through `UNHEARD_TAG`. O1.2.5 must supply the reviewed
API-only path/override before this procedure can authorize its invocation.
Replace only the API service; preserve nginx, Liquidsoap, Icecast and monitoring.

For the first install the precheck endpoint is absent. Record the completed
manual window explicitly; missing/401/500/timeouts are never automatic approval.
Later O1.2.5 prechecks add a refusal check **inside** this exclusion window;
they do not replace admission freeze, scheduler exclusion or drain evidence.

After replacement, keep ingress closed. Verify image digest, health, bindings,
DB migrations, startup recovery, inactive producer/capture and no catch-up
start. Verify only activated outputs for the recorded readiness profile;
neither silence nor a healthy `/health` proves playable programme audio.
If verification fails, keep ingress closed and choose rollback before deadline.

Rollback sequence: drain any new startup/recovery work, stop the candidate API,
preserve its data separately for investigation, restore the recovery set and
old configuration/image, recheck the restart schedule boundary, then start only
the old API. A migrated DB must not be handed to an older binary without proven
compatibility. Never overwrite the only data copy, prune images, initialize a
fresh DB, revert the build-only protection or restart media as a shortcut.
Recheck the same health/drain/output gates after rollback.

Reopen only after the operator records the accepted image, restored/retained
data identity, scheduler state and output result. Restore only the temporary
ingress admission change, run `nginx -t`, reload and verify normal admission
in the approved window. Keep unrelated internal/metrics restrictions. Release
descriptor 9 last. A failed rollout ends with either verified rollback or an
explicit continued outage and named owner, never an unlogged reopen.

## Rehearsal and evidence

Use a disposable host with the exact legacy image, synthetic media and fixture
storage. No production credentials, database or audio. The local nginx test
below tests admission and locking only; it does not simulate backend task
completion or qualify the host.

From the repository root, after confirming `desktop-linux` is the local Docker
Desktop context:

```bash
docker --context desktop-linux run --rm --network none --read-only \
  --user 101:101 --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /tmp:rw,nosuid,nodev,size=16m \
  --mount "type=bind,src=$PWD/docs/stream-rework/local-test-harness/maintenance,dst=/fixture,readonly" \
  --entrypoint /bin/sh \
  nginx:1.28.0-alpine@sha256:30f1c0d78e0ad60901648be663a710bdadf19e4c10ac6782c235200619158284 \
  /fixture/rehearse.sh
```

The fixture publishes no ports, mounts no secrets/socket, uses only container
loopback and removes only its own container. It uses 204 to indicate that a
request passed nginx; this is not application authentication or media evidence.

Rehearse SQLite backup and restore separately on scratch data:

```bash
bash docs/stream-rework/local-test-harness/maintenance/rehearse-snapshot.sh
```

This runs SQLite `.backup`, mutates the candidate schema/data, then `.restore`
into a separate database. It checks integrity, original data/schema and retention
of the failed candidate. The script leaves its private `/tmp` evidence directory.
It does not prove filesystem, image or secret-configuration restoration.

| Required scenario | Evidence and expected result |
| --- | --- |
| Start/rehearsal/schedule edit immediately after closure | 503 at every ingress; stop and diagnostic paths still usable. Local route fixture covers policy; live vhost check remains open. |
| Pending upload after recording status turns idle | Hold fixture storage response, attempt replacement: operator refuses; release upload and reconcile terminal artifact/DB evidence before continuing. Open. |
| Disconnect and finalize race | Drop a synthetic producer; record grace task and finalization, with no replacement during either. Open. |
| Existing requests/old nginx workers | Admit a delayed mutation/upgrade before reload; prove it settles before final snapshot. Open. |
| Two simultaneous deployments | First owner retains host lock; second refuses before any mutation. Local lock primitive tested; actual deploy integration remains O1.2.5. |
| Bounded scheduler path | Empty eligible window through rollback; drain, snapshot, replace and restore old image/data. Open against exact backend image. |
| Outage path | Candidate becomes due after the short drain boundary; API stopped before then; no start while stopped; re-evaluate catch-up before restart/rollback. Open. |
| First install without precheck | Refuse absent manual-window evidence; allow only after all manual gates pass. O1.2.5 integration open. |
| Older workflow still active | Keep first manual run blocked until Actions and host evidence both reconcile. Host check open. |

Record exact commands, fixture image/digest, source/config hashes, timings and
results in the shared inventory and issue. Local policy success is partial
evidence. O1.2.2 remains open until both backend paths and required host gates
are verified or explicitly assigned as activation conditions by the operator.
