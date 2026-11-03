# Staging

Staging runs `dev/flow` images at https://staging.live.moafunk.de before a release reaches `main` and production.

**Stopgap:** staging runs on the production box, as a separate and capped compose project. It moves to its own small server later (a `cx23` costs about €5.49 per month). Until then, follow the rules below exactly.

## Rules

- Staging never reaches production data or real accounts. `staging.sh check-env` enforces this:
  - own SQLite DB (a scrubbed copy of production, see `seed-db`)
  - own R2 buckets (`unheard-artists-staging`, `moafunk-staging`) with a token that can only reach them
  - own Liquidsoap and Icecast (harbor 8015, Icecast 8020) with own passwords
  - `SOUNDCLOUD_AUTO_UPLOAD=false`, `TELEGRAM_ENABLED=false`
  - no `GITHUB_DISPATCH_TOKEN` (it would start the production backup workflow), no SoundCloud, Telegram, Instagram, Facebook, OpenAI or `RECOVERY_*` settings
- Staging runs only while someone tests, and never during a show. `staging.sh up` refuses when production is on air, when a show starts within 2 hours, or when the box has less than 10 GB disk or 1.5 GB memory free.
- Hard caps: API 0.75 CPU / 640 MB, Liquidsoap 0.5 CPU / 320 MB, Icecast 0.25 CPU / 96 MB. Nothing restarts on its own, not even after a reboot.
- Never start or stop staging containers by hand; always use `staging.sh`.

## One-time setup

Done by someone with access to the box, DNS, Cloudflare and Bitwarden.

1. DNS: an A record `staging.live.moafunk.de` → `178.104.160.103` (Hetzner DNS zone `moafunk.de`).
2. Cloudflare R2: create the buckets `unheard-artists-staging` and `moafunk-staging`, and an API token that can only read and write those two buckets.
3. Bitwarden: store the `STAGING_*` values from `staging.env.example` and `stream.env.example`. Generate new passwords; never reuse production ones.
4. Copy the files to the box:

   ```bash
   ssh root@178.104.160.103 'mkdir -p /opt/unheard-staging'
   scp backend/staging/{docker-compose.staging.yml,icecast.staging.xml,nginx-staging.conf,staging.sh} docs/stream-rework/prod/moafunk.liq root@178.104.160.103:/opt/unheard-staging/
   ```

5. On the box, create `/opt/unheard-staging/.env` and `/opt/unheard-staging/stream.env` from the two examples, with the Bitwarden values.
6. On the box: `CERTBOT_EMAIL=<email> /opt/unheard-staging/staging.sh setup`. It checks both env files, writes `icecast.xml`, installs the nginx vhost (after `nginx -t`) and gets the TLS certificate once DNS points at the box.
7. Optional: `/opt/unheard-staging/staging.sh seed-db` for a copy of production data. The copy has no sessions, no stored tokens and no usable passwords. Shows from production can show up as missed in staging logs; that is expected.
8. Log in as the staging superadmin and create staging test accounts (a host and an admin).

## Daily use

Pushes to `dev/flow` build an image `sha-<7 chars>` (`backend.yml`). Then, on the box:

```bash
/opt/unheard-staging/staging.sh up sha-1234567   # checks, pulls, starts, waits for healthy
/opt/unheard-staging/staging.sh status
/opt/unheard-staging/staging.sh down             # always stop when done
```

## Tests

`bash backend/staging/staging.test.sh` checks that every unsafe setting stops staging. It needs no Docker and no box access.

## Removing staging

`staging.sh down`, then delete `/opt/unheard-staging`, the nginx vhost `staging.live.moafunk.de` (and its certificate), the two R2 buckets and the DNS record.
