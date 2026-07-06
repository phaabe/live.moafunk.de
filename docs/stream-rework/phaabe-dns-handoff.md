# For phaabe — DNS handoff for the Hetzner migration

**TL;DR:** Everything for moving the backend + streaming onto one new Hetzner
box is built and tested in parallel. **Production is untouched and stays that
way until we deliberately flip.** The only thing I can't do myself is DNS —
the `moafunk.de` zone lives in **your** Hetzner account. I need you to either
hand me a DNS API token (preferred) or make two small record changes. ~5 minutes.

---

## Background (30 seconds)

- The backend + admin (`admin.live.moafunk.de`) currently run on **AWS Lightsail**
  (`18.157.219.113`); the live audio stream runs on the **old NMS relay**
  (`stream.moafunk.de` → `78.47.222.82`).
- We've stood up a **single new Hetzner box** (`178.104.160.103`, in Anton's
  Hetzner account) that runs both the backend and the new Icecast/Liquidsoap
  streaming stack. It's validated and running in parallel — **no public traffic
  yet, DNS still points everything at the old boxes.**
- To cut over we need to repoint DNS. The zone is on Hetzner's nameservers
  (`helium/hydrogen/oxygen.ns.hetzner`) under your account, so it's your call.

---

## What I need — pick ONE

### ✅ Option A (preferred): give me a Hetzner DNS API token

This lets me make the change *now* (lower a TTL, harmless) and do the actual
cutover flip at the exact right moment, without you having to be online for it.

1. Open **dns.hetzner.com** (it now opens inside the Hetzner Cloud Console).
2. Top-right **account / profile menu → "API tokens"** (may be under
   *Security* / *API tokens* — the menu moved when DNS merged into the console).
3. **Create an access token** (give it a name like `moafunk-migration`).
   It grants read/write to your DNS zones — that's what's needed.
4. **Send it to me securely — not in plain chat/email.** Easiest:
   **Bitwarden Send** (https://send.bitwarden.com) with a 1-view / 1-day expiry,
   or drop it in the shared `live.moafunk.de` Bitwarden Secrets Manager project
   if you have access. You can **delete/revoke the token right after** the
   cutover — I'll tell you when we're done.

That's it. I handle the rest.

### Option B: make the two changes yourself

Both are on **one record** — the `admin.live` **A record** in the `moafunk.de`
zone (currently `admin.live.moafunk.de → 18.157.219.113`).

1. **Now (do this anytime — zero impact):** change that record's **TTL → `60`**.
   Leave the value as `18.157.219.113`. This just makes the later flip propagate
   in ~1 minute instead of hours; nothing moves.
2. **At cutover (I'll ping you, we'll do it together in a quiet window):**
   change the **value** `18.157.219.113` → **`178.104.160.103`**. Keep TTL 60.
   If anything looks wrong we change it straight back — Lightsail stays running
   as the instant rollback.

> **Don't touch** any other record. `live.moafunk.de` (the public site → GitHub
> Pages) and everything else stay exactly as they are.

---

## Heads-up for later (no action needed now)

- **Public stream record:** when we flip the listener stream too, we'll add/point
  one record (e.g. `radio.live.moafunk.de → 178.104.160.103`, or repoint
  `stream.moafunk.de`) at the new box. A DNS token (Option A) covers this
  automatically; with Option B I'll send you the exact record.
- **Lightsail DB:** the one-time cutover copies the live SQLite DB off the
  Lightsail box. Anton has the Lightsail SSH key via Bitwarden — please confirm
  it's still the current key, or be ready to help for ~5 minutes.
- **Decommission:** after a few real shows on the new box, we retire the
  **Lightsail instance** and the **old NMS relay** (`78.47.222.82`). Anton will
  coordinate — nothing to do until then.

## What does NOT change

Domain registration, the GitHub repo, GHCR, Cloudflare R2 storage, the public
site (`live.moafunk.de` → GitHub Pages), and every DNS record except the single
`admin.live` flip above. Low blast radius, fully reversible.

---

*Questions → ping Anton. Nothing here affects the live site or stream until we
deliberately run the cutover together.*
