# live.moafunk.de · architecture proposal v2

Companion text for `live-moafunk.proposal-v2.html`. This is a rework of
`live-moafunk.proposal.html` (v1) after an architecture review. v1 stays in
the folder for comparison.

Both files are pinned to commit `1934e6b4d006278749590feaeff6464da6c5d38f`
(main, 2026-09-26) and link to source lines. Regenerate with:

```sh
node ~/.claude/skills/archify/bin/archify.mjs deliver architecture \
  docs/architecture/live-moafunk.proposal-v2.architecture.json \
  docs/architecture/live-moafunk.proposal-v2.html --quality showcase --repo-root .
```

## Why v1 needed a rework

v1 was a good engineering plan. It was not the right plan for a volunteer
community radio with one box and a few hours of ops time per month.

1. **Wrong priority.** v1 spends most of its effort on a job worker and a
   jobs table. The show that failed on 2026-09-12 did not fail because of
   publishing. It failed because the host could not go live, an unattended
   upgrade restarted the stream units, and nobody outside the box noticed.
   Broadcast continuity has to come first. Recovery second. Automation last.
2. **No listener in the picture.** v1 had no public site and no listener
   path. For a radio station the listener is the primary user. The old setup
   at least had a dedicated listener host (`stream.moafunk.de`, the NMS
   relay). Today the public player is built with the admin hostname baked in
   (`admin.live.moafunk.de/live.mp3`). That couples the listener endpoint to
   the admin host and to a Pages rebuild.
3. **Deploys are not shown as a risk.** Every push to `main` restarts
   `unheard-api`. That kills the browser WebSocket, the ffmpeg ingest and the
   recording tee. The workflow only carries a comment about it. v1 had no
   deploy node at all.
4. **Dead air is silence.** Liquidsoap wraps the harbor in `mksafe`, so a
   dropped source plays silence. Listeners cannot tell "dead air" from "my
   player is broken". A fallback asset is a one-line change once the file
   exists.
5. **Worker drawn as a separate process.** v1 drew the worker as its own node
   inside the host, while the text said "task group first". With SQLite and
   no WAL, a second writing process is a real risk, not a detail. v2 draws
   the worker where it will live: inside the API process.
6. **Facts moved.** Since v1 was written, the backup job moved to Hetzner
   (PR 307) and the stream units were excluded from needrestart with docker
   live-restore enabled (PR 308). v1's "Exists today" card was already stale.

## What exists today (main, 2026-09-26)

- One Hetzner host (`moafunk`, cpx22, 80 GB disk). nginx terminates TLS for
  `admin.live.moafunk.de` and proxies the API, `/live.mp3`, `/test.mp3` and
  the Icecast status page.
- The API process does everything: HTTP, stream WebSocket, ffmpeg ingest,
  recording tee, boot recovery, Telegram bot loop, chat bridge, publishing.
- The recording spool lives under `./data/recordings-temp`, which is the
  compose bind mount. Segments survive a container restart. Boot recovery
  picks orphaned segment dirs up.
- Liquidsoap: harbor in, one MP3 256 kbps encode, `mksafe` silence on dead
  air, no fallback asset.
- Backups: weekly GitHub Actions job SSHes into the Hetzner box, snapshots
  SQLite with the `.backup` API, syncs **only** `unheard-artists-prod` to
  `unheard-backups`. `moafunk-prod` (finalized shows) is not backed up. The
  backup bucket is in the same Cloudflare account as production. No restore
  has ever been rehearsed.
- Monitoring: Prometheus, blackbox, Alertmanager and Grafana on the same
  host. Alerts go to Telegram only, and that receiver is broken right now
  (issue 300). No node exporter, so no disk, CPU or memory alert. Nothing
  outside the host watches the host.
- SQLite pool: 5 connections, no `journal_mode`, no `busy_timeout` in code.
- Stream units are excluded from needrestart, docker live-restore is on.
  Kernel reboot still pending.
- Public site: GitHub Pages is still on the fallback branch from 12.09
  (issue 304). The public player points at the dead NMS relay.

## What v2 proposes, in order

Diagram nodes whose sublabel starts with `PROPOSED` do not exist. Nodes with
`+ … (proposed)` exist and get an addition. The `SRC` badge links to the
code that exists today.

### 1 · Keep the show on air (days, not weeks)

- **Deploy guard.** `deploy_hetzner.sh` asks `GET /api/stream/status` before
  it recreates `unheard-api`. If a stream is active it exits non-zero unless
  `FORCE_DEPLOY=1`. The push-to-main job then fails loudly instead of cutting
  a show. Add a "deploy later" re-run path.
- **Upgrade and reboot window.** Pin unattended-upgrades and reboots to a
  window that never overlaps the show calendar. Finish the pending kernel
  reboot (issue 298).
- **Fallback audio.** Put one asset on the box (a jingle loop or the last
  finalized show) and switch the live chain from `mksafe(harbor)` to
  `fallback([harbor, single(asset)])`. Listeners hear something, the
  `/api/stream/status` signal still says "not live".
- **Admin go-live override and never-logged-in warning** (issue 301). This is
  the direct fix for 12.09.
- **Show-start watchdog.** The API already knows the schedule and talks to
  Telegram. At show start plus five minutes with no source connected, post to
  the hosts topic. Same at show end with no finalized recording (issues 104
  and 105 cover the reminder side).

### 2 · Own the listener endpoint

- Add an nginx vhost `stream.moafunk.de` on the box that serves `/live.mp3`
  and the status endpoint. Keep the admin vhost for the admin SPA and API.
- Point the Pages build at `stream.moafunk.de`, revert the Pages source
  (issue 304). DNS change needs phaabe.
- Result: the listener URL is stable. A future relay or CDN in front of
  Icecast is a DNS change, not a frontend rebuild. Peak load is fine on one
  box: 300 listeners at 256 kbps is about 77 Mbit/s and 100 GB per three-hour
  show, inside Hetzner's 20 TB.

### 3 · Eyes outside the host

- One managed uptime check (or a tiny VM elsewhere) probes the TLS endpoint
  and the Icecast status page. Do not probe `/live.mp3` itself. The body is
  infinite and the probe times out.
- The box sends a heartbeat to the same service every minute. A missed
  heartbeat pages. This is the only alert that fires when the box is dead.
- Fix the Telegram receiver (issue 300) so on-host alerts work again.

### 4 · Recover what matters

- Add `moafunk-prod` to `backup-r2.sh`. Today the finalized shows are the one
  thing that cannot be re-created and the one thing not backed up.
- Move the backup target to a second account or provider (Hetzner Storage
  Box or Backblaze B2) with its own credentials. Same-account backups do not
  survive an account compromise or a billing lockout.
- Monthly restore rehearsal as a CI job: download the newest DB snapshot,
  run `PRAGMA integrity_check`, count shows, compare with production. Note the
  time it takes.

### 5 · Small hardening before any worker

- Set `journal_mode=WAL`, `synchronous=NORMAL` and `busy_timeout=5000` in
  the pool options, in code, not only in the env file.
- Add node exporter and a disk alert. The spool, docker images and logs share
  80 GB. Disk full during a show means a lost recording.
- Fix the SoundCloud refresh token (issue 302). Publishing reliability is a
  token problem before it is a jobs problem.

### 6 · Worker tasks and jobs, in-process

- Move recover, concat, upload, verify and publish into a task group inside
  the API process. Same binary, same pool, no second writer.
- Add a `jobs` table written in the same transaction as the domain change.
  Lease, retry with backoff, stop after a limit, operator retry queue.
- Recording manifest states: `capturing → sealed → uploaded → verified →
  indexed → complete`. Delete local segments only after `verified` and the
  row is committed.
- **No process split.** v1 planned to split ingest, API and worker last. v2
  drops it. The deploy guard removes the main reason for the split, and a
  split adds a second SQLite writer, stream tickets and a new home for the
  bot loop. Revisit only if the guard proves unworkable.

## What stays

- One host, one SQLite file, no automatic failover.
- Liquidsoap and Icecast-KH host-networked under systemd, API on the docker
  bridge via `host.docker.internal`.
- The public site on GitHub Pages.
- Cloudflare R2 for media and finalized shows.

## Open questions

- Fallback asset: jingle loop, last finalized show, or a "back soon" voice
  clip. Needs a decision from the hosts.
- Off-site backup target: second Cloudflare account, Hetzner Storage Box, or
  Backblaze B2.
- External monitor: managed service or a small VM. A managed free tier is
  enough for one probe and one heartbeat.
- Deploy guard policy: block only, or block and auto-retry after the show.

## Issue links

- https://github.com/phaabe/live.moafunk.de/issues/298 needrestart, live-restore, reboot
- https://github.com/phaabe/live.moafunk.de/issues/300 Alertmanager cannot post to Telegram
- https://github.com/phaabe/live.moafunk.de/issues/301 admin go-live override
- https://github.com/phaabe/live.moafunk.de/issues/302 SoundCloud refresh token
- https://github.com/phaabe/live.moafunk.de/issues/304 Pages source revert
- https://github.com/phaabe/live.moafunk.de/issues/104 bot show reminder
- https://github.com/phaabe/live.moafunk.de/issues/105 bot warns when no final recording
- https://github.com/phaabe/live.moafunk.de/issues/113 rotate backups on R2
- https://github.com/phaabe/live.moafunk.de/pull/307 backups on the Hetzner box
- https://github.com/phaabe/live.moafunk.de/pull/308 stream units out of needrestart
