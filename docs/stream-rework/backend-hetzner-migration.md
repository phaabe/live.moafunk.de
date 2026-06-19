# Move A — migrate the backend Lightsail → Hetzner

A lift-and-shift of the existing backend container to a fresh Hetzner Cloud box,
using [`backend/scripts/deploy_hetzner.sh`](../../backend/scripts/deploy_hetzner.sh)
(a faithful fork of `deploy_lightsail.sh` — same SSH+Docker+nginx+certbot flow,
minus the AWS static-IP lookup). The same `docker-compose.prod.yml` deploys
unchanged; the Icecast stack slots in beside it later.

> This is the tactical step. The full greenfield consolidation (retire NMS,
> everything on Hetzner) is tracked in the umbrella ticket — see
> [`../../README`](#) / the milestone. This doc gets the backend over with its
> data intact; the streaming re-architecture is layered on after.

## Why / context

- The backend is on AWS Lightsail today; the relay (NMS) is already on Hetzner.
- Hetzner is much cheaper for compute and includes ~20 TB/mo egress per server
  (vs. metered AWS) — which is the lever for streaming + recording bandwidth.
- The deploy is provider-agnostic, so the move is low-friction *at the tooling
  level*. The only careful part is the **stateful SQLite cutover** and DNS/TLS.

## Pre-reqs

- A fresh **Hetzner Cloud** instance (Ubuntu 22.04/24.04). Recommended: **CPX21/CX22**
  (3 vCPU / 4 GB) — headroom for the backend's ffmpeg work plus the future
  Icecast/Liquidsoap stack. **Do not reuse the old NMS relay VM.**
- Your SSH public key added to the instance at creation (Hetzner Cloud default
  user is `root`).
- A **Hetzner Cloud Firewall** allowing inbound `22/80/443` (add `1935`/`8010`
  later for streaming). The OS `ufw` is separate.
- The GHCR pull token + the production `.env` (CI generates it from Bitwarden;
  for a manual run, export one with `--env-file`).

## Cutover sequence (minimise downtime)

### 1. Provision + first deploy (no DNS change yet)
```bash
IP=<hetzner-ip> SSH_KEY=~/.ssh/hetzner_ed25519 \
GHCR_USER=phaabe GHCR_TOKEN=<ghcr-pat> \
./backend/scripts/deploy_hetzner.sh \
  --env-file /path/to/production.env \
  --init-db                                  # fresh empty DB; real data copied in step 3
```
This installs Docker/compose, pulls the image, starts `unheard-api` on
`127.0.0.1:8000`, and passes the local health check. No public traffic yet.

### 2. Set up nginx + TLS (still no DNS cutover)
Run with `--setup-nginx --nginx-domain admin.live.moafunk.de --certbot-email phaabe@gmail.com`.
certbot is **skipped automatically** until DNS points at the new box (the script
compares the domain's A record to `--ip`), so this is safe to run early — it
lays down the nginx vhost and waits.

### 3. Migrate the SQLite DB (the stateful bit)
The DB is the only state on the box (media is on R2). Do this in a short window:
```bash
# a. Quiesce writers on the OLD box (stop the container so the DB is consistent):
ssh <lightsail> 'cd /opt/unheard-backend && sudo docker compose -f docker-compose.prod.yml stop'

# b. Copy the live DB off Lightsail (use the SQLite backup API for a clean copy):
ssh <lightsail> 'sqlite3 /opt/unheard-backend/data/unheard.db ".backup /tmp/unheard.db"'
scp -i ~/.ssh/lightsail.pem <lightsail>:/tmp/unheard.db /tmp/unheard.db

# c. Push it to the NEW box and restart the container:
scp -i ~/.ssh/hetzner_ed25519 /tmp/unheard.db root@<hetzner-ip>:/opt/unheard-backend/data/unheard.db
ssh root@<hetzner-ip> 'cd /opt/unheard-backend && sudo docker compose -f docker-compose.prod.yml restart'
```
> There's also `backend/scripts/backup` (rclone → R2) and `scripts/db/init_sqlite.sh`
> on the box; the `.backup` approach above is the simplest consistent point-in-time copy.

### 4. Verify on the new box before DNS
```bash
ssh root@<hetzner-ip> 'curl -fsS http://127.0.0.1:8000/health'
# Hit it through nginx without DNS:
curl -fsS --resolve admin.live.moafunk.de:80:<hetzner-ip> http://admin.live.moafunk.de/health
# Spot-check real data is present (shows, recordings, users) via the admin API.
```

### 5. Flip DNS + issue TLS
- Repoint the `admin.live.moafunk.de` **A record** → `<hetzner-ip>` (lower TTL beforehand).
- Once it resolves to the new box, re-run the deploy with `--setup-nginx` (certbot
  now sees matching DNS and issues the cert), or run certbot directly on the box.
- Confirm `https://admin.live.moafunk.de/health` is green from the public internet.

### 6. Point the RTMP push (only if also moving streaming)
The backend's RTMP target is configurable (`RTMP_URL`/`RTMP_STREAM_KEY` →
`config.rtmp_destination()`). For a pure host move, leave it pointing at the
existing NMS. When the streaming stack lands on the same Hetzner box, set
`RTMP_URL=rtmp://127.0.0.1/live` and the push becomes a localhost hop. (This is
where NMS gets retired — see the umbrella ticket.)

### 7. Decommission Lightsail
After a soak period (a few days of real shows), stop the Lightsail instance,
take a final snapshot, then delete it. Keep the final DB backup on R2.

## Rollback
DNS is the switch. If anything looks wrong after the flip, repoint the A record
back to the Lightsail IP (still running until step 7) and restart its container.
Because the DB was copied (not moved), the old box still has its last-known-good
state — only writes made on the new box during the window are lost, so keep the
window short and ideally outside a live show.

## What does NOT change
- **Cloudflare R2** object storage (recordings, artist media) — host-independent,
  free egress. No migration.
- The **container image** and `docker-compose.prod.yml` — identical on both hosts.
- All app config/secrets — same `.env` (regenerate from Bitwarden in CI).

## CI note
`.github/workflows/backend.yml` auto-deploys to Lightsail on push (Bitwarden
`LIGHTSAIL_IP`/`LIGHTSAIL_SSH_KEY`). A **manual** `deploy-hetzner` job is also
wired: it runs `deploy_hetzner.sh` with the same generated `.env`, reading
everything — including the box's `HETZNER_IP` and `HETZNER_SSH_KEY` — from
**Bitwarden Secrets Manager** (single source of truth, same project as the app
secrets).

To deploy to Hetzner: ensure `HETZNER_IP`/`HETZNER_SSH_KEY` exist in the SM
project, then run the workflow via **Actions → Run workflow** with
`deploy_hetzner=true` (first run: also `setup_nginx=true`, `init_db=true`). It never runs on push, so Lightsail stays
the automatic prod path until the DNS cutover.

### Flipping the producer to Icecast

The deploy is wired so the producer target is a **single dispatch choice**, not
a code change. The `stream_output` input (default `rtmp`) controls the generated
`.env`:

- `stream_output=rtmp` → `STREAM_OUTPUT=rtmp` (unchanged from Lightsail; the
  backend's config default pushes RTMP to NMS).
- `stream_output=icecast` → writes
  `STREAM_OUTPUT=icecast`,
  `ICECAST_URL=icecast://source:<HARBOR_LIVE_PASSWORD>@host.docker.internal:8005/live`,
  `ICECAST_STATUS_URL=http://host.docker.internal:8010/status-json.xsl` — i.e.
  ffmpeg pushes MP3 to the co-located Liquidsoap harbor `live` mount and metrics
  poll the local Icecast. The recording tee stays an independent ffmpeg, so the
  flip never gaps the archive (#185 isolation).

> **Why `host.docker.internal`, not `127.0.0.1`** (verified on the box
> 2026-06-19): Liquidsoap + Icecast run **host-networked** (bind `0.0.0.0:8005/8010`),
> but `unheard-api` is **bridge-networked**, so *its* `127.0.0.1` is the container,
> not the host — a `127.0.0.1:8005` push fails. `docker-compose.prod.yml` maps
> `host.docker.internal` → the host gateway (`extra_hosts: host-gateway`) so the
> producer reaches the harbor. Validated: pushing the backend's exact ffmpeg args
> from inside the `unheard-api` container to the harbor `live` mount made
> Liquidsoap decode + `mksafe`-switch to the live source (≈12 s harbor latency).

**One manual prerequisite before the first `stream_output=icecast` run:** create a
secret named **`HARBOR_LIVE_PASSWORD`** in the Bitwarden SM project
(`37a9cffb-…`, value = the box's `/etc/moafunk/stream.env` `HARBOR_LIVE_PASSWORD`)
and paste its UUID into the `Get stream secrets from Bitwarden (icecast flip only)`
step in `backend.yml`, replacing `REPLACE_WITH_HARBOR_LIVE_PASSWORD_UUID`. That
step only runs on the icecast path, so the default RTMP deploy never needs it.
After the cutover this manual job can become the push default. (Tracked in the
umbrella ticket.)
