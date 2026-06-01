# Local Icecast test harness

Rehearse the **entire** Phase-2 chain — NodeMediaServer 2.4.9 → Liquidsoap → Icecast —
on your laptop, fed by a synthetic tone. Validates the mount + **iOS playback** before
you touch the fragile prod relay box, and without waiting for a live show.

See the full prod procedure in [`../phase-2-icecast-runbook.md`](../phase-2-icecast-runbook.md).
This harness is the "Tier 1 — local rehearsal" step.

## Prerequisites

- Docker + `docker compose`
- `ffmpeg` on the host (for `push-test-tone.sh`) — or use the in-container fallback in that script
- An iPhone on the **same Wi-Fi** as your laptop (for the iOS gate)

## Run it

```bash
cd docs/stream-rework/local-test-harness
docker compose up --build            # NMS + Icecast + Liquidsoap
```

In a second terminal, feed audio in:

```bash
./push-test-tone.sh                  # 440 Hz tone
# ./push-test-tone.sh ~/music.mp3    # …or loop a real file to hear music
```

Liquidsoap serves **silence** to `/test.mp3` the moment it's up (thanks to `mksafe`),
then switches to your audio within a few seconds of starting the push.

## Validate (this mirrors the prod gates)

| Check | How |
|---|---|
| NMS got the push | `curl -sI http://localhost:8000/live/stream-io-test.flv` → `200` (and `…/index.m3u8` once HLS warms up) |
| Icecast serving MP3 | `curl -sI http://localhost:8010/test.mp3` → `200`, `Content-Type: audio/mpeg` |
| Icecast stats | open `http://localhost:8010/admin/stats.xml` (user `admin` / pass `localadmin`) — one source, listener count |
| Desktop playback | open `http://localhost:8010/test.mp3` in a browser |
| **iOS gate** | on the iPhone, Safari → `http://<your-laptop-LAN-ip>:8010/test.mp3` — **must play** |

Find your LAN IP: `ipconfig getifaddr en0` (macOS Wi-Fi).

> **iOS note:** test over plain `http://` *directly* in Safari. iOS blocks `http` audio
> embedded in an `https` page (mixed content) — that's expected and is solved in prod by
> putting the mount behind TLS (runbook Step 4), not a codec problem.

If the iPhone plays `/test.mp3`, the **codec + transport decision is validated** — MP3
over Icecast reaches iOS, exactly as the migration needs.

## Tear down

```bash
docker compose down -v
```

Pure local containers — nothing here connects to prod, the relay box, or R2.

## Notes / knobs

- **Passwords** (`localsource` / `localadmin`) live in `icecast/icecast.xml` and
  `liquidsoap/moafunk.liq` — local-only; they must match each other.
- **Codec:** the mount is `%mp3` on purpose (iOS-safe). Do **not** switch the public
  mount to `%opus`/Ogg — iOS Safari can't decode it. Add Opus only as an *extra* mount.
- **Liquidsoap version:** pinned to `savonet/liquidsoap:v2.2.5`; bump the tag in
  `docker-compose.yml` if you prefer a newer 2.x. (`input.ffmpeg`/`mksafe` are stable across 2.x.)
- **Icecast:** stock `icecast2` here for convenience; it's protocol-identical to
  Icecast-KH for this test. Switch to KH before the real cutover (KH adds the relay/
  listener-limit features Phase 4 needs).
- Only the throwaway `stream-io-test` key is used, so this never resembles the real
  `stream-io` mount even if you point it at a real NMS by mistake.
