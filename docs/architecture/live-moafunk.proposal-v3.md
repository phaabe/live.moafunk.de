# Moafunk v3: 200 listeners on a small budget

[Open the diagram](live-moafunk.proposal-v3.html). [Editable source](live-moafunk.proposal-v3.architecture.json).

Keep one Hetzner server, SQLite, Liquidsoap, Icecast and the existing R2 storage. Spend effort on getting shows on air and recovering recordings. Additional servers and services are deferred.

This is a proposal, not a deployment report. Source badges refer to repository commit `1934e6b4d006278749590feaeff6464da6c5d38f`; they establish code at that revision, not current production state. Earlier diagrams are retained for comparison. NEW, CHANGE and LATER identify proposed work. Other nodes show the retained design. Some arrows describe the proposed behaviour.

## Capacity and cost

Assume no more than 200 simultaneous listeners, one programme stream and MP3 at 256 kbps. Each listener receives the already encoded stream; listener count does not require a separate encoder per listener.

| Scenario | Audio traffic before protocol overhead |
| --- | ---: |
| 200 listeners at once | 51.2 Mbit/s |
| Three hours with all 200 connected | 69.12 GB |
| Twenty such shows per month | 1.3824 TB |
| All 200 connected continuously for 30 days | 16.5888 TB |

These are decimal units and estimates, not a server benchmark. Include backups, other downloads and overhead when monitoring actual traffic. Hetzner lists 20 TB included monthly for EU CPX servers: [traffic allowances](https://docs.hetzner.com/robot/general/traffic/). Check the actual account allowance before relying on it.

Keep 256 kbps initially. A lower bitrate is an optional listening-quality decision, not a necessary cost cut for scheduled shows. No CDN, second relay, paid queue, managed database or monitoring VM is required by this audience estimate.

Most first steps use existing infrastructure. External monitoring may use a suitable free allowance; confirm audio sampling support and current limits before choosing a provider. Backup storage grows with retained data. No new paid subscription is assumed.

## 1. Get the host and listeners ready

- Before each show, verify host login, assignment, microphone permission and preview audio on the actual device.
- Provide an authorised admin takeover with an audit record. Never let takeover silently replace an active broadcaster.
- Verify that the deployed public page reaches the intended Icecast stream. Repository configuration alone does not prove the published page uses it.
- Keep the current listener hostname. A separate stream hostname is optional and does not create redundancy on the same machine.

Acceptance: a host completes a rehearsal, an admin can recover from a host login problem, and an independent listener hears the public stream.

## 2. Deploy safely without splitting services

Keep image builds automatic. Initially deploy manually between shows, after checking capture and finalisation have finished. Document one maintenance procedure for application deploys and host reboots.

An automated replacement must acquire a maintenance gate before inspecting stream state. The application must reject new live starts and scheduled playout while that gate is active. Deploy only after existing capture and finalisation are safe. Checking status once leaves a race with a new show starting.

Unknown or unavailable status blocks the deploy. Use an explicit, recorded emergency override. Keep the gate active through replacement and health checks, and define recovery if the deploy fails. Reopening starts must be deliberate and observable.

Keep existing stream-unit restart protections and schedule required upgrades outside shows. They do not protect against application replacement, host crashes or power loss.

Acceptance: a deploy attempted during a rehearsal cannot cut it off, and a show cannot start between the safety check and replacement.

## 3. Protect audio from background work

Bound heavy media work to one job at a time initially. Defer optional exports, video generation and bulk backups during shows. Live ingest, the recording tee and essential archival work must remain independent of optional publishing delays.

Use the existing process and code paths first. Do not introduce a general job platform merely to limit concurrency. Expose failures for manual retry. Add durable publishing jobs only when recurring lost work justifies their maintenance cost.

Monitor free disk, spool growth, CPU and memory through the existing monitoring stack. Set the free-space alert to reserve enough room for the longest show plus processing copies, using measured spool growth. Never automatically delete unverified recordings to free space.

Acceptance: a planned test with 200 listener connections, live recording and one permitted background job has no audio dropouts or recording-tee drops. Run this off-air with bounded duration and traffic. Check connection limits, memory, CPU and completed recording integrity.

## 4. Detect failure without another server

Repair and test the existing alert delivery before adding more alerts. An external availability check is the minimum addition; an optional heartbeat can report that the host's scheduled checks ran.

During scheduled shows, take a short, bounded GET sample of the public audio endpoint. Successful TLS or a status response alone does not prove playable audio. Decode the sample and distinguish transport failure from sustained silence. Allow for intentional quiet passages and avoid alerting outside scheduled airtime. The external checker needs its own expected-show schedule or cached expectation so API failure cannot disable the check.

A continuous stream can be sampled by stopping after a byte or time limit. A timeout after useful audio was received is not automatically a failure. Start with a manual external listening check if available free monitoring cannot sample audio; label that coverage honestly.

Acceptance: deliberately interrupt the source during rehearsal and verify that a real operator receives the alert. Separately test host-unreachable detection.

## 5. Back up the recordings without multiplying storage

At the pinned revision, backups run on Hetzner but the media script names only the artists bucket. Verify current coverage before changing it. Cover both artists and show recordings with separate destination prefixes.

- Take small dated SQLite snapshots using the backup API. Proposed target: at most 24 hours of metadata loss, plus a snapshot after important show changes where practical.
- Copy verified recordings incrementally after archival. Retain older versions or deleted objects for a defined window; a plain mirror propagates accidental deletion.
- Avoid a full new media copy for every daily snapshot. Use append-only recording versions where possible and bounded retention for replaced or deleted objects.
- Keep backup credentials and deletion rights separate where practical. A second account reduces shared account risk; use an existing independent target if available. Same-account copies remain useful but do not cover account loss.
- Restore a database into a temporary environment and retrieve and play a recording. Repeat after backup changes and periodically; record the elapsed time and result.

For illustration, R2 Standard is listed at $0.015/GB-month. One additional 100 GB archive is about $1.50/month before allowances and operations; four full copies are about $6/month. This is an example, not a Moafunk bill estimate. Measure current data volume first. [R2 pricing](https://developers.cloudflare.com/r2/pricing/).

Local segments survive container replacement only while the mounted disk survives. Define the interval from capture to verified remote copy: recordings not yet uploaded remain vulnerable to host-disk loss. Do not promise zero recording loss.

## 6. Keep SQLite and defer optional features

Inspect effective `journal_mode`, `synchronous` and `busy_timeout` using the application's connections before proposing changes. No explicit setting in pool initialization does not establish the runtime value. SQLx documents a default five-second busy timeout: [connection options](https://docs.rs/sqlx/latest/sqlx/sqlite/struct.SqliteConnectOptions.html). Confirm the installed version's behaviour.

SQLite is appropriate for this design; listeners are primarily served by Icecast. WAL can help reader/writer concurrency but still permits only one writer at a time. Keep transactions short. Do not lower durability simply for a presumed performance gain: WAL with `synchronous=NORMAL` can lose recent committed transactions after power loss. [SQLite synchronous settings](https://www.sqlite.org/pragma.html#pragma_synchronous).

Defer a separate worker service, jobs table, database migration and automatic failover. Revisit only after measured contention, repeated unrecoverable background work or an agreed uptime requirement.

## Optional fallback must include the player

Liquidsoap fallback audio alone is insufficient: the current player clears its source when the show becomes inactive. Treat `live`, `interrupted` and `off-air` separately across source status, API and player.

During a short interruption, keep playback connected to a station-owned “back soon” recording and allow the broadcaster to reconnect. At the scheduled end, stop deliberately. Do not label fallback playback as a live host, and do not let fallback hide a missing source from monitoring. Test the actual iPhone and desktop player behaviour before release.

This is a later improvement, not a prerequisite for backups or deployment safety. It cannot keep broadcasting through failure of the only server.

## Diagram reading and remaining limits

The spool-to-R2 edge means the API reads local segments and uploads them; the spool is passive storage. The SQLite-to-backups edge represents the backup script taking a consistent snapshot. Existing local monitoring, the Telegram bot and chat remain within the host/API scope rather than becoming separate new services.

One host remains one failure domain. The budget decision accepts downtime during a host failure and invests in tested recovery. Add capacity only after a representative 200-listener test fails, measured traffic approaches the plan allowance, or the audience requirement changes. Reconsider availability separately if the station can no longer accept recovery downtime.

## Rebuild

Run from this worktree root:

```sh
node ~/.claude/skills/archify/bin/archify.mjs deliver architecture \
  docs/architecture/live-moafunk.proposal-v3.architecture.json \
  docs/architecture/live-moafunk.proposal-v3.html \
  --quality showcase --repo-root . --json
```

The spec and HTML retain the source revision used for the review. Recheck source claims before repinning to a newer commit.
