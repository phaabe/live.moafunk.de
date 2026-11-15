# Staging

Staging runs `dev/flow` images at https://staging.live.moafunk.de before a release reaches `main` and production. It has its own Hetzner server (`cx23`, Ubuntu 24.04, `nbg1`) and shares nothing with the production box.

## Rules

- Staging never reaches production data or real accounts. `staging.sh` checks this before `setup` and before every `up`:
  - own SQLite DB (empty, or a scrubbed copy of production, see `seed-db`)
  - own R2 buckets (`unheard-artists-staging`, `moafunk-staging`) with a token that can only reach them. `check-r2` proves it: the token must list the staging buckets and get 403 from `unheard-artists-prod`, `unheard-artists-dev` and `moafunk-prod`.
  - own Liquidsoap and Icecast on this box (harbor 8005, Icecast 8010, as in production) with own passwords
  - `SOUNDCLOUD_AUTO_UPLOAD=false`, `TELEGRAM_ENABLED=false`
  - no `GITHUB_DISPATCH_TOKEN` (it would start the production backup workflow), and no other `SOUNDCLOUD_*`, `TELEGRAM_*`, `INSTAGRAM_*`, `FACEBOOK_*`, `OPENAI_*` or `RECOVERY_*` setting
- Secrets live in Bitwarden (`Moafunk STAGING_*` items). `push-secrets.sh` moves them to the box; nobody types or pastes them.
- The production box has no key for the staging box, and the staging box has no key for production.
- Start and stop staging only with `staging.sh`, never with `docker` by hand.

## Files

| File | Runs where | Purpose |
|------|------------|---------|
| `staging.sh` | staging box (`export-db`: production box) | setup, checks, deploy, DB copy |
| `push-secrets.sh` | your Mac | Bitwarden → `staging.sh install-env` over ssh |
| `docker-compose.staging.yml` | staging box | API, Liquidsoap, Icecast |
| `staging.env.example`, `stream.env.example` | staging box | templates `install-env` fills |
| `icecast.staging.xml`, `nginx-staging.conf` | staging box | Icecast config, nginx vhost |
| `staging.test.sh` | anywhere (CI) | proves every unsafe setting is refused and the DB copy is scrubbed |

## One-time setup

Add `-i <key>` to the `ssh`/`scp` commands below if your ssh config does not pick the key.

1. **Server** (hcloud project `live.moafunk.de`): `cx23` in `nbg1` with `ubuntu-24.04`, the SSH key `moafunk-key`, and a firewall `moafunk-staging` that allows only TCP 22, 80 and 443.
2. **DNS** (zone `moafunk.de`, Hetzner DNS): A record `staging.live` → the server's IPv4, TTL 300.
3. **Cloudflare R2** (account `4acacbddb37198e8eed490e4b7c752ee`):
   - create the buckets `unheard-artists-staging` and `moafunk-staging`
   - create an R2 API token with **Object Read & Write** for exactly these two buckets. Put its Access Key ID and Secret Access Key in Bitwarden right away, as `Moafunk STAGING_R2_ACCESS_KEY_ID` and `Moafunk STAGING_R2_SECRET_ACCESS_KEY` (password field). Cloudflare shows them only once.
   - CORS for the admin's audio player: `BUCKETS="unheard-artists-staging moafunk-staging" ORIGINS="https://staging.live.moafunk.de" ./backend/scripts/configure_r2_cors.sh`
4. **Copy the files** to the box (from the repo root):

   ```bash
   ssh root@<staging-ip> 'mkdir -p /opt/unheard-staging/icecast-web'
   scp backend/staging/{staging.sh,docker-compose.staging.yml,icecast.staging.xml,nginx-staging.conf,staging.env.example,stream.env.example} \
     docs/stream-rework/prod/{moafunk.liq,icecast-kh.Dockerfile} root@<staging-ip>:/opt/unheard-staging/
   scp docs/stream-rework/prod/icecast-web/{status-json.xsl,xml2json.xslt} root@<staging-ip>:/opt/unheard-staging/icecast-web/
   ```

5. **Packages and Icecast image:** `ssh root@<staging-ip> /opt/unheard-staging/staging.sh setup`. The first run installs Docker, Compose, nginx, certbot, argon2 and sqlite3, builds the Icecast-KH image, then stops because the env files are missing.
6. **Secrets**, on your Mac. This creates the missing generated items in Bitwarden, then writes `.env` and `stream.env` on the box (mode 600; the superadmin password is stored only as an argon2 hash):

   ```bash
   export BW_SESSION="$(bw unlock --raw)"
   BW_FOLDER="<your folder>" backend/staging/push-secrets.sh root@<staging-ip>
   ```

7. **Finish setup:** `ssh root@<staging-ip> /opt/unheard-staging/staging.sh setup` again. It checks both env files and the R2 token, writes `icecast.xml`, installs the nginx vhost and gets the TLS certificate once DNS points at the box. It is safe to run again at any time.
8. **Start:** `staging.sh up sha-<commit>` (see below), then log in as `staging-admin` (password in `Moafunk STAGING_SUPERADMIN_PASSWORD`) or `staging-host` (password in `Moafunk STAGING_HOST_PASSWORD`).

## Daily use

Every push to `dev/flow` builds an image `sha-<7 chars>` (`backend.yml`). On the box:

```bash
/opt/unheard-staging/staging.sh up sha-1234567   # checks env and R2, pulls, starts, waits for health and both mounts
/opt/unheard-staging/staging.sh status
/opt/unheard-staging/staging.sh down             # stop (data stays)
```

Staging keeps running (also after a reboot) until `down`.

`up` and `seed-db` create or reset the two logins `staging-admin` (superadmin) and `staging-host` (host) to the Bitwarden passwords. `staging.sh users` does only that. To change a password, change it in Bitwarden, run `push-secrets.sh`, then `staging.sh users`.

## A copy of production data

Production stays read-only. Run this on your Mac; the copy is scrubbed on the production box before it leaves, and scrubbed again on staging:

```bash
ssh root@178.104.160.103 'bash -s -- export-db' < backend/staging/staging.sh \
  | ssh root@<staging-ip> '/opt/unheard-staging/staging.sh seed-db -'
```

The copy has no sessions, no stored tokens (SoundCloud), no reset links or recovery emails, no pending uploads and no usable passwords. The scrub refuses the copy if a column that looks like a credential holds data it does not know about. `seed-db` adds `staging-admin` and `staging-host`. Other users you create by hand are gone after a seed. To test as a production user, reset that user's password in the staging admin. Files in the production buckets (artist images, recordings) are not copied, so older entries show no media on staging.

## Tests

`bash backend/staging/staging.test.sh` checks the env rules, `install-env` and the DB scrub. It needs bash 3.2+ and python3, no Docker and no server. CI runs it on every change under `backend/staging/`.

## Removing staging

`staging.sh down`, then delete the server and the `moafunk-staging` firewall (hcloud), the DNS record, the two R2 buckets and their token, and the `Moafunk STAGING_*` Bitwarden items.
