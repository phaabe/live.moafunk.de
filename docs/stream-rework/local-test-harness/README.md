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

> **Ordering matters.** Liquidsoap's RTMP puller gives up if the source isn't there
> when it first connects, then sits on `mksafe` silence. So if `/test.mp3` is **playing
> but silent**, the tone simply isn't reaching Liquidsoap: start `./push-test-tone.sh`,
> then re-pull with `docker compose restart liquidsoap`. Verify with
> `docker compose logs liquidsoap | tail` (want a successful connect to
> `rtmp://nms:1935/live/stream-io-test`, not repeated failures).

## Validate (this mirrors the prod gates)

> **Don't use `curl -I`/`-sI` against an Icecast mount** — Icecast answers `HEAD` with
> `400 Bad Request` (it only serves `GET`/`SOURCE`). A 400 there does **not** mean the
> mount is down. Use the GET-based checks below.

| Check | How |
|---|---|
| NMS got the push | `curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/live/stream-io-test.flv` → `200` |
| Icecast serving MP3 | `curl -s -o /dev/null -w '%{http_code} %{content_type}\n' --max-time 3 http://localhost:8010/test.mp3` → `200 audio/mpeg` |
| **Audio is non-silent** | `curl -s --max-time 5 http://localhost:8010/test.mp3 -o /tmp/t.mp3; ffmpeg -hide_banner -i /tmp/t.mp3 -af volumedetect -f null - 2>&1 \| grep mean_volume` — silence ≈ `-91 dB`, a 440 Hz tone ≈ `-20 dB` or louder |
| Icecast stats | open `http://localhost:8010/admin/stats.xml` (user `admin` / pass `localadmin`) — one source, listener count |
| Desktop playback | open `http://localhost:8010/test.mp3` in a browser (ignore the player's duration readout — it's a meaningless estimate for a live stream) |
| **iOS gate** | on the **iPhone's Safari address bar** (not a shell!), enter `http://<your-laptop-LAN-ip>:8010/test.mp3` — **must play** |

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
