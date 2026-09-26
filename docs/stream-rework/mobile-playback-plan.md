# Mobile playback plan — reliable audio + lock-screen info (iOS first)

> **Status:** draft for review (2026-09-26). Nothing here is built yet.
> **Scope:** the public player on `live.moafunk.de` and the backend/infra it needs.
> **Reviewer:** please read the "Review asks" section at the end first.

## 1. Problem

On mobile, and on iOS in particular, the public player has two gaps:

1. **Audio drops and does not come back.** The player is one progressive
   Icecast MP3 stream in a plain `<audio>`. When the phone switches Wi-Fi ↔ LTE,
   or the stream stalls while the screen is locked, the connection dies.
   Nothing reconnects it. The listener must open the page and tap play again.
2. **No show info on the lock screen, Control Center, CarPlay or AirPlay.** iOS
   only shows the page title ("Moafunk Radio") and the domain. There is no show
   name, no host, and no artwork.

## 2. Goals and non-goals

**Goals**

- A listener on iPhone can lock the screen, change networks, and keep
  listening. Short drops recover by themselves.
- The lock screen, Control Center and CarPlay show the show title, the host
  and the artwork.
- The work stays small, with no new third-party service. It runs on the
  existing Hetzner box.

**Non-goals**

- A native iOS/Android app.
- Per-track metadata (track lists). We show show-level info only, like NTS.
- Time-shift / rewind (DVR).

## 3. Current state (verified 2026-09-26)

| Area | Today | Where |
|---|---|---|
| Public stream | Icecast-KH `/live.mp3`, MP3 256 kbps 44.1 kHz, behind nginx at `https://admin.live.moafunk.de/live.mp3`. `mksafe`, so the mount is always up and serves silence between shows. | `docs/stream-rework/prod/moafunk.liq` |
| Response headers | `audio/mpeg`, chunked, `Access-Control-Allow-Origin: *`, `icy-metaint: 16000`, `icy-name: Moafunk`. ICY `StreamTitle` is **empty**. | live `curl` |
| Player | One hidden `<audio id="player">`. `src` is set when the show goes live. There are no `error`/`stalled` recovery handlers. The `play()` promise is not handled; the button switches to "pause" even if play fails. Legacy flv.js / NMS HLS branches are still in the code. | `frontend/src/player.ts`, `frontend/src/index.html` |
| Live signal | Polls `GET /api/stream/status` every 8 s. On off→on it calls `restartPlayer()`; on on→off it calls `destroyPlayer()`. | `frontend/src/main.ts`, `frontend/src/streamDetector.ts` |
| Status payload | `{active, user, recording, recording_path?, recording_failed?}`. It has no show title, host or artwork. **It is public and returns the host username and, during a recording, a server file path.** | `backend/src/stream_bridge.rs` (`StreamStatus`, `get_status`) |
| Media Session | Not used. | — |
| Show artwork | `shows/{id}/cover.png` in the private R2 bucket. It is only reachable through presigned URLs (1 h). The size and aspect ratio of the cover are **not confirmed**. | `backend/src/handlers/api.rs` (cover_url) |
| On-air show id | There is no single source of truth. `RecordingManager::current_show_id()` is only set while recording. `StreamState` holds the user, not the show. | `backend/src/recording.rs`, `backend/src/stream_bridge.rs` |

## 4. What others do (research, 2026-09-26)

Checked live with Playwright (desktop Chromium, iPhone user agent) and `curl`.
No real iPhone was used.

- **NTS** (https://www.nts.live/)
  - Web player: **HLS** from a hosted CDN (Radiomast):
    `https://streams.radiomast.io/nts1/hls.m3u8`. It redirects to a geo edge.
    Segments are packed MP3 at 256 kbps, about 12 s each, with about 60 s in
    the live window. The show name is also in the `#EXTINF` title.
  - Player: hls.js on `<audio preload="none">`, no Web Audio, no custom
    reconnect code.
  - Legacy Icecast still exists (`https://stream-relay-geo.ntslive.net/stream`).
    It has the show name in ICY `StreamTitle`, most likely for apps and radio
    directories.
  - Now-playing info: they poll `https://www.nts.live/api/v2/live` about every
    50 s. The response is CDN-cached.
  - Media Session: title "NTS Radio", artist = show name, album = channel,
    artwork at 400px and 800px. Only `play` and `pause` handlers are
    registered, so iOS shows a live-style player with no seek bar.
- **BBC 6 Music:** HLS only, AAC 96k, about 6 s TS segments. Info comes from a
  separate API.
- **Rinse FM, Worldwide FM, Dublab, Radio Paradise, SomaFM:** Icecast
  (AAC 128k or MP3 192–320k). Info comes from separate JSON APIs.
- **Pattern:** the big broadcasters use HLS for the web; small stations stay
  on Icecast. **Everyone** drives the lock-screen info from a JSON API plus
  Media Session. Nobody parses ICY in the browser.

**iOS Safari facts that drive this plan**

| Fact | Confidence | Source |
|---|---|---|
| Media Session is on by default since iOS/Safari 15 | high | https://webkit.org/blog/11989/new-webkit-features-in-safari-15/ |
| Without Media Session metadata, iOS shows the document title and domain | high | observed behaviour |
| Safari does not put ICY or HLS ID3 data on the lock screen by itself | high | — |
| Artwork: iOS often uses only the **first** entry. It was broken in 16.1–16.3 and fixed in 16.4. | medium | https://developer.apple.com/forums/thread/721179 |
| Routing `<audio>` through Web Audio (`createMediaElementSource`) can stop playback in the background or on lock | medium–high | https://bugs.webkit.org/show_bug.cgi?id=231105 |
| A progressive Icecast stream dies on a network change and stays stuck until `src` is reset. HLS recovers, because each segment is a new short request. | high | — |
| hls.js ≥ 1.5 prefers ManagedMediaSource on iOS 17.1+. That needs `disableRemotePlayback` or an AirPlay alternative source, so AirPlay can be lost. Native HLS (`audio.src = m3u8`) keeps AirPlay and CarPlay. | medium | https://webkit.org/blog/14735/webkit-features-in-safari-17-1/, https://github.com/video-dev/hls.js/issues/6482 |
| `play()` must be called synchronously inside the tap handler | high | — |
| Home Screen web apps: background audio and Media Session have had bugs in standalone mode | low–medium | https://bugs.webkit.org/show_bug.cgi?id=261858 |

## 5. Plan

The steps are ordered by value per effort. Steps 1–3 need no infra change and
should fix most user-visible problems. Step 4 is a separate decision (see §7).

### Step 1 — Backend: public now-playing endpoint

- New `GET /api/now-playing` (public, no auth):
  ```json
  {
    "live": true,
    "show": { "id": 39, "title": "…", "host": "…", "started_at": "2026-…Z" },
    "artwork": [
      { "src": "https://admin.live.moafunk.de/api/now-playing/artwork/512.jpg?v=<cover_generated_at>", "sizes": "512x512", "type": "image/jpeg" },
      { "src": "…/256.jpg?v=…", "sizes": "256x256", "type": "image/jpeg" },
      { "src": "…/96.jpg?v=…",  "sizes": "96x96",   "type": "image/jpeg" }
    ]
  }
  ```
- `live` has the same meaning as today's `active` (a `/test` rehearsal is not
  live).
- **On-air show id:** add `current_show_id: Option<i64>` to `StreamState`. Set
  it at go-live (in-browser stream and the pre-recorded auto start), and clear
  it at stop. Don't infer it from the recording manager, because recording is
  optional.
- **Host:** the display name of `host_user_id`. Never the login name.
- **Artwork endpoint** `GET /api/now-playing/artwork/{512|256|96}.jpg`:
  - Load the show cover from R2, centre-crop it to a square, resize it, and
    encode it as JPEG.
  - Cache the result in memory, keyed by `(show_id, cover_generated_at, size)`.
  - If there is no cover, or no show is on air, return the station logo.
  - Headers: `Cache-Control: public, max-age=86400` (the `?v=` changes when
    the cover changes), plus `Access-Control-Allow-Origin: *`.
  - Why not presigned R2 URLs: they expire after 1 h (a long show would lose
    its artwork), the image is a large PNG, and we can't be sure it is square.
- Headers on `/api/now-playing`: `Cache-Control: public, max-age=5`, plus CORS
  for `https://live.moafunk.de`.
- **Hardening in the same step:** stop returning `user` and `recording_path`
  from the public `/api/stream/status`, or move the public player to the new
  endpoint and put the old one behind auth. Check first which admin screens
  read those fields.

Acceptance:
- Unit tests for the JSON shape: off air, live with a cover, live without a
  cover, and a `/test` rehearsal (must give `live: false`).
- The artwork handler returns a square JPEG of the requested size.

### Step 2 — Frontend: Media Session

A new module, `frontend/src/mediaSession.ts`:

- `setNowPlaying(np)`:
  - `navigator.mediaSession.metadata = new MediaMetadata({ title: show.title, artist: show.host, album: 'Moafunk Radio · live', artwork })`.
  - Guard it with `'mediaSession' in navigator`.
  - Also set `document.title = "<show> · Moafunk Radio"` as a fallback.
- Handlers: register only `play`, `pause` and `stop`. Set seek and track
  handlers to `null`, so iOS shows the live-style UI.
- Keep `navigator.mediaSession.playbackState` in sync with the `playing` and
  `pause` events of the `<audio>` element.
- Set the metadata inside the play tap (iOS picks it up best at that point),
  and again whenever the show id or title changes in the poll.
- The poll moves from `/api/stream/status` to `/api/now-playing`, still every
  8 s. The go-live transition logic in `main.ts` stays the same.

Acceptance:
- Vitest with a stubbed `navigator.mediaSession`: metadata is set on play and
  updated on a show change; the handlers are registered; seek handlers stay
  unset.
- Manual check on an iPhone: the lock screen shows title, host and artwork.

### Step 3 — Frontend: playback that recovers on its own

In `frontend/src/player.ts`:

- **Play button:**
  - Call `audio.play()` synchronously in the click handler and handle the
    promise.
  - Show the "pause" state only on the `playing` event.
  - On rejection, show "tap to retry".
- **Reconnect watchdog:** only while the user wants playback (`wantPlaying`
  is true):
  - It fires on `error`, on `stalled` or `waiting` that lasts more than 8 s,
    on `window` `online`, and on `visibilitychange` → visible when the element
    is paused or stuck.
  - Action: `audio.src = base + '?t=' + Date.now(); audio.load(); audio.play()`.
  - Backoff: 1 s, 2 s, 4 s … up to 30 s. Reset the backoff on `playing`.
  - It stops when the user pauses, and when the live poll says the station is
    off air.
- **Resume after a long pause:** always reload `src` on play, so the listener
  joins at the live edge and does not hear old buffer.
- **Cleanup:**
  - Remove the flv.js / NMS HLS branches and the `flv.js` dependency. NMS is
    retired, so Icecast is the only path.
  - Keep one `<audio>` element and never route it through Web Audio.
- Metrics (optional): send a Plausible event on each reconnect, so we can
  measure how often it happens.

Acceptance:
- Vitest with a fake `HTMLMediaElement` and fake timers:
  - `error` leads to a reconnect with a new `?t=`.
  - The backoff grows and is capped.
  - A user pause stops the watchdog.
  - The off-air signal stops the watchdog.
- Manual checks on an iPhone:
  - Wi-Fi → LTE handover while locked: audio comes back within about 10 s.
  - 30 min of locked playback with no drop.

### Step 4 — Infra: HLS next to Icecast (decide after steps 1–3, see §7)

- Liquidsoap: add `output.file.hls` on the same `live_src`:
  - AAC-LC 128 kbps via `%ffmpeg(format="mpegts", %audio(codec="aac", b="128k"))`.
  - `segment_duration=4.0`, `segments=6`, `segments_overhead=5`.
  - Written to a tmpfs dir, e.g. `/run/moafunk/hls`.
- nginx: serve `/hls/`.
  - `.m3u8` gets `Cache-Control: max-age=2`; segments get `max-age=300`.
  - Add `Access-Control-Allow-Origin: *` and the correct MIME types.
- Player: if `audio.canPlayType('application/vnd.apple.mpegurl')` is true
  (Safari, iOS), play HLS natively. Otherwise keep Icecast. No hls.js is
  needed at first.
- Icecast `/live.mp3` stays for desktop, apps, aggregators and as the
  fallback.
- **Cost:**
  - One more AAC encode (small CPU cost).
  - About 12–25 s more delay than Icecast (about 3 s). That affects the live
    chat and host-listener interaction.
  - Liveness still comes from the backend, so mksafe silence is not a
    problem.

### Step 5 — Optional: show title in ICY metadata

- On go-live and on show change, set the mount title, e.g. via Icecast
  `/admin/metadata?mount=/live.mp3&mode=updinfo&song=<Show – Host>` over
  loopback, or via Liquidsoap `insert_metadata`.
- Effect: VLC, radio apps and directories (radio-browser, TuneIn) show the
  show name. The web page does not need it.

### Step 6 — Device test matrix (before closing each step)

| Case | iPhone Safari | iPhone Home Screen app | Android Chrome | Desktop |
|---|---|---|---|---|
| Lock screen: title, host, artwork | ✓ | ✓ | notification | OS media keys |
| Control Center / CarPlay play–pause | ✓ | ✓ | — | — |
| AirPlay to a speaker | ✓ | ✓ | — | — |
| Wi-Fi ↔ LTE while locked | ✓ | ✓ | ✓ | — |
| 30 min locked, no drop | ✓ | ✓ | ✓ | — |
| Show ends → player stops, lock screen clears | ✓ | ✓ | ✓ | ✓ |

## 6. Rollout

- One PR per step: step 1 (backend), then steps 2+3 (frontend; 3 can go
  first), then step 4 (infra + player switch) if we choose it.
- Steps 1–3 are safe to ship while a show is off air. The frontend change is
  backwards compatible if it falls back to `/api/stream/status` while
  `/api/now-playing` is not deployed yet.
- Step 4 is behind a build variable (`VITE_STREAM_HLS_URL`, which already
  exists). If it is empty, Icecast is used everywhere, so rollback is a
  variable change.

## 7. Open decisions

1. **HLS now, or only if steps 1–3 are not enough?** Proposal: only if needed.
   Measure the reconnect rate (Plausible event) for 2–3 shows first.
2. **Codec for HLS:** AAC-LC 128k (less mobile data, the native iOS codec) or
   MP3 256k (the same encoder as Icecast, like NTS)? Proposal: AAC 128k.
3. **Artwork source:** the show cover (`shows/{id}/cover.png`), or always the
   station logo? Proposal: the cover, with the logo as the fallback. The cover
   size and aspect ratio must be confirmed.
4. **Mobile data:** keep Icecast at 256k (about 115 MB/h) or add a 128k mount
   for mobile? It is only relevant if we don't do HLS.

## 8. Risks

- **Media Session artwork on iOS is unreliable.** Mitigation: first entry
  512×512 JPEG, exact `sizes` and `type`, same-origin HTTPS.
- **Aggressive reconnects can hammer the server.** Mitigation: backoff up to
  30 s, and reconnect only while `wantPlaying` is true and the station is
  live.
- **`current_show_id` in `StreamState` can get out of sync** with the
  pre-recorded scheduler path. Mitigation: set and clear it in the same
  functions that set `active`, and add tests for both go-live paths.
- **Hardening `/api/stream/status` can break admin screens** that read `user`
  or `recording_path`. Mitigation: run impact analysis before the change.

## 9. Review asks

Please check the plan against the code and challenge it:

1. Is `StreamState` the right place for the on-air show id? Is there already
   a path (pre-recorded auto start in `backend/src/scheduler.rs`, go-live in
   `backend/src/handlers/api.rs`) where the show id is known and could be
   stored instead?
2. Is anything missing from the reconnect design in step 3? For example
   `pause` events that iOS fires on its own (interruptions, a phone call)
   against a user pause, or double reconnects from `stalled` and `error` at
   the same time.
3. Is a backend resize endpoint the right call for artwork, or is a public R2
   object (pre-rendered 512/256/96 JPEGs written when the cover is generated)
   simpler and cheaper?
4. Which admin callers use `user` / `recording_path` from
   `/api/stream/status`?
5. Anything in §4 (iOS facts) that you know to be wrong or outdated?
