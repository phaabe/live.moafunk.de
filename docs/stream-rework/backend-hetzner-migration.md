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
`.github/workflows/backend.yml` deploys via the Lightsail path + Bitwarden
secrets (`LIGHTSAIL_IP`, `LIGHTSAIL_SSH_KEY`). After the cutover, add Hetzner
equivalents (`HETZNER_IP`, `HETZNER_SSH_KEY`) and switch the deploy step to
`deploy_hetzner.sh`. Until then, deploy to Hetzner manually with the command in
step 1. (Tracked in the umbrella ticket.)
