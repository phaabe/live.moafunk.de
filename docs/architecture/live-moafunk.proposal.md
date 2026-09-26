# live.moafunk.de · architecture proposal

Companion text for `live-moafunk.proposal.html`. The diagram shows the target
state. This file holds the reasoning and the order of work.

`live-moafunk.html` shows the system as it runs today. Both diagrams are
pinned to commit `d879abfa5dd8653a4821152a40fa0ec4828ccf2f` and link to source
lines. Regenerate with:

```sh
node ~/.claude/skills/archify/bin/archify.mjs deliver architecture \
  docs/architecture/live-moafunk.proposal.architecture.json \
  docs/architecture/live-moafunk.proposal.html --quality showcase --repo-root .
```

## What exists today

- One Hetzner host. nginx terminates TLS and proxies the API and `/live.mp3`.
- The API process (Axum) does everything: HTTP, the stream WebSocket, the
  ffmpeg ingest, the recording tee, boot-time recovery, the Telegram bot loop
  and the chat bridge.
- Ingest copies the browser's Opus into the Liquidsoap harbor. Liquidsoap is
  the only lossy encode: MP3 256 kbps to Icecast `/test.mp3` and `/live.mp3`.
- Recording already writes crash-safe 10 s MPEG-TS segments into a `.segs`
  directory. At stop they are concatenated, uploaded with S3 multipart,
  size-verified and recorded. On boot, orphaned segment dirs are recovered.
- Publishing (SoundCloud upload, Instagram post, Telegram notify) runs inline
  in handlers or spawned tasks. Instagram retries a transient error a few
  times with a fixed 5 s delay. There is no job table.
- SQLite pool has 5 connections. No explicit `journal_mode` is set in code.
- Prometheus, blackbox and Alertmanager run on the same host. Alerts go to
  Telegram. Nothing outside the host watches the host.
- The weekly backup workflow still SSHes into `LIGHTSAIL_HOST` and syncs only
  `unheard-artists-prod`. Since the Hetzner move it backs up nothing.
  `moafunk-prod` (finalized shows) was never covered.

## What is proposed

Diagram nodes whose sublabel starts with `PROPOSED` do not exist yet. The
`SRC` badge on a node links to the code that exists today.

### 1 · Backups on Hetzner, both buckets, restore test

- Point `backup.yml` at the Hetzner host. Snapshot SQLite with the backup API
  (consistent copy), not a file copy.
- Sync `unheard-artists-prod` and `moafunk-prod` to versioned off-site
  storage with separate credentials.
- Target: 24 h recovery point for metadata, 30 daily versions.
- Rehearse a restore once a month and note the time it takes.

### 2 · External probe and heartbeat

- A probe outside the host checks public TLS and samples `/live.mp3` during
  scheduled shows.
- The host sends a heartbeat to the same service. A missed heartbeat alerts,
  so a dead host is noticed, not just a dead stream.

### 3 · Recording manifest and worker

- Add a manifest per recording: `capturing → sealed → uploaded → verified →
  indexed → complete`. Mark short or incomplete captures explicitly.
- Delete local segments only after the R2 object is verified and the database
  row is committed. Cleanup must check state and never race recovery.
- Move recover, upload, verify and publish into a worker. First as a task
  group in the API process, later as its own process.

### 4 · Durable jobs (after 3)

- Store jobs in SQLite in the same transaction as the domain change.
- The worker leases jobs, renews leases, retries with backoff and stops after
  a limit. Failed jobs land in an operator retry queue.
- Publishing is at least once. Use provider idempotency where it exists and
  reconcile before retrying an ambiguous result.

### 5 · Process split (last)

- Split ingest, API and worker into separately supervised processes so an API
  deploy does not kill a broadcast.
- Needs short-lived, show-scoped stream tickets validated at connect, and a
  home for the Telegram bot loop and chat bridge. Not before 1 to 4.

## What stays

- One host, one SQLite file, no automatic failover.
- Liquidsoap and Icecast stay host-networked under systemd.
- The public site stays on GitHub Pages.

## Open questions

- Which off-site target for backups: a second R2 account, Hetzner Storage
  Box, or Backblaze B2.
- Which external monitor: a managed uptime service or a tiny probe on a second
  VM.
- Whether `journal_mode=WAL` is already set through `DATABASE_URL` in the
  production env file.
