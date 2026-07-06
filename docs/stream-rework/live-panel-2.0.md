# Live Panel 2.0 — design handoff

> **Status:** approved design, not yet implemented (design session 2026-07-06, Cowork).
> Interactive prototype: [`live-panel-2.0-prototype.html`](./live-panel-2.0-prototype.html) — open in a browser.
> Builds on the show-dashboard redesign shipped on branch `feat/show-dashboard-redesign`.

## Context

The show dashboard (`/shows/:id`) was redesigned in this session (branch
`feat/show-dashboard-redesign`, 5 commits, un-pushed at time of writing):

1. `refactor(stream): extract LiveSetupTest from FlowLive` — device setup + `/test`
   rehearsal now a shared component, rendered inline on the dashboard.
2. `feat(admin_dashboard): redesigned show dashboard for all show types` — header →
   status strip → phase panel (prep / launchpad / post-production) + announcements.
3. `style(stream): metric-card telemetry panels on the live panel`
4. `feat(admin_dashboard): inline meta editing, clickable announcement slots` —
   schedule & host modal from the header meta line; announcement rows open composers.
5. `feat(stream): redesign the live panel (waiting room + on-air card)` — current
   FlowOnAir restyle (interim state; **this doc describes its successor**).

Live Panel 2.0 replaces the streaming phase of `FlowOnAir.vue` (`/stream/on-air`).
The waiting phase (countdown card) stays as shipped in commit 5.

## Design decisions (locked)

- **Stop streaming is the only prominent stream control.** It lives in the status
  bar. Reconnect and "stop & change settings" are small icon buttons next to it.
  The record toggle becomes the REC chip state — no separate control card.
- **Reading order:** status bar → analyzers → connection & upload → live chat.
- Upload bitrate is user-selectable and can auto-degrade; lowering it only degrades
  the *source* feed — as long as it stays above the Icecast mount's output encoding,
  listeners hear no difference, so Auto may be aggressive.
- The `/test` rehearsal is the pre-air bandwidth check: the same connection metrics
  run during the prep test broadcast, so the dashboard can say "connection good
  enough" *before* air time.

## Layout spec

### 1. Status bar (full-width card)

- Left: pulsing LIVE dot · `LIVE` label · REC chip (`REC 42:13`, red pill, ticks) ·
  listener count (`👥 23`).
- Right: `On air` elapsed (mono) · `Ends in` remaining (mono) · icon button
  *Reconnect stream* · icon button *Stop and change settings* · **danger button
  `⏹ Stop`**.
- Bottom edge: 4 px show-progress bar (elapsed / scheduled duration).
- Remaining-time warning states carry over from the current end-time banner
  (amber < 5 min, auto-stop at 0).

### 2. Analyzers (two cards, side by side; stack < ~600 px)

| | Input — your source | Stream — what listeners hear |
|---|---|---|
| Visual | ~28-band spectrum, teal | Same, purple, labeled "~6 s behind" |
| Meter | live peak dB readout (top right) | — |
| Fader | **Input gain** (0–150 %) | **Monitor volume** (0–100 %) |

### 3. Connection & upload (full-width card)

- Header row: title · **verdict pill** (`Good — 4.1× headroom` green /
  `Tight — 1.3×` amber / `Too slow — lower the quality` red) · (prototype only:
  "Simulate congestion" button).
- Left ⅔: 60 s throughput sparkline (measured upload, solid teal) vs. **"needed
  for target" dashed line** (target bitrate × ~1.15 container overhead).
- Right ⅓: metric tiles — Upload (kbps) · Buffer (s) · RTT (ms) · Late (count).
- Footer: **Upload quality** segmented control `Auto | 320 | 192 | 128 | 96 kbps`
  + note: "Auto steps down when the buffer grows · switch = <1 s encoder restart,
  covered by the relay".

### 4. Live chat (full-width card)

- Header: Telegram icon · "Live chat · Moafunk channel" · `Connected` pill.
- Scrolling message list (username accent-colored; host replies green with
  "(host)" suffix).
- Footer: text input ("Reply as host…") + send button.

## Implementation map

| Element | How | Code pointers |
|---|---|---|
| Input spectrum | Web Audio `AnalyserNode` on the capture stream | `useAudioCapture` (`mediaStream`/`processedStream`), extend `DbMeter.vue` / `useDbMeter` |
| Stream spectrum | `AnalyserNode` on the preview `<audio>` element | `StreamPreviewPlayer.vue`; **requires CORS headers on Icecast mounts** (`docs/stream-rework/prod/icecast.xml`) |
| Input gain | `GainNode` in the capture's processed chain | `useAudioCapture`; a volume slider existed in `FlowStreaming.vue` (unrouted, reference only) |
| Monitor volume | `.volume` on the preview element | `StreamPreviewPlayer.vue` |
| Upload rate (encoded) | sum `MediaRecorder.ondataavailable` chunk bytes/s | recorder lives in `FlowOnAir.vue` / `LiveSetupTest.vue` |
| Upload rate (egress) & buffer | diff `WebSocket.bufferedAmount` per tick; buffer(s) = bufferedAmount / current byte-rate. **Best congestion signal, zero backend changes.** | `useStreamSocket.ts` |
| RTT + late chunks | timestamped ack (or ping/pong) frames on the stream WS | backend `handlers/` stream WS + `useStreamSocket.ts` |
| Bitrate switch | `audioBitsPerSecond` is fixed at `MediaRecorder` construction (currently hard-coded 192000) → stop + recreate recorder with new value; sub-second gap covered by harbor `mksafe` (`docs/stream-rework/prod/moafunk.liq`) | `FlowOnAir.vue`, `LiveSetupTest.vue` |
| Auto bitrate | step down one notch when buffered seconds > threshold for N ticks; step up after stable period | new composable, e.g. `useUploadHealth.ts` |
| Telegram chat | backend bot reads the channel's discussion group (webhook/getUpdates) → fan out over a panel WS; host replies via bot API | backend `telegram.rs`; new WS endpoint |
| Timers / progress | already present (`elapsedText`, `remainingText`) | `FlowOnAir.vue` |
| Listeners / quality | already polled Icecast telemetry | `FlowOnAir.vue` (`metrics`, 10 s poll) |

## Tickets

Milestone **Live Panel 2.0** + 6 issues. The session's GitHub connector had a stale
token, so run this instead (labels follow CLAUDE.md conventions):

```bash
gh api repos/phaabe/live.moafunk.de/milestones -f title="Live Panel 2.0" \
  -f description="Redesigned on-air cockpit (design session 2026-07-06): compact status bar with Stop as the only prominent control, input + stream spectrum analyzers with gain/monitor faders, connection & upload quality card (throughput vs target, WS buffer, RTT, verdict), selectable + auto upload bitrate, Telegram live chat bridge."

gh issue create -t "Live panel 2.0: shell — compact status bar, card layout" \
  -l "type::frontend" -l "project::Stream" -m "Live Panel 2.0" \
  -b "Restructure /stream/on-air into the new cockpit: status bar (LIVE dot, REC chip w/ elapsed, listeners, on-air elapsed, ends-in, progress bar) with Stop streaming as the only prominent control (reconnect + stop-&-change-settings as icon buttons); card slots below: analyzers (2-col), connection & upload (full width), live chat (full width). Record toggle folds into the REC chip. Existing handlers/polling reused. Spec: docs/stream-rework/live-panel-2.0.md"

gh issue create -t "Live panel 2.0: input & stream spectrum analyzers with faders" \
  -l "type::frontend" -l "project::Stream" -m "Live Panel 2.0" \
  -b "Two Web-Audio AnalyserNode spectra: input (audioCapture.mediaStream, extends DbMeter) and stream (StreamPreviewPlayer audio element — needs CORS on Icecast mounts). Input gain fader = GainNode in useAudioCapture's processed chain; monitor fader = preview element volume. Stream analyzer labeled '~6 s behind'. Spec: docs/stream-rework/live-panel-2.0.md"

gh issue create -t "Live panel 2.0: connection & upload quality card (browser-side)" \
  -l "type::frontend" -l "project::Stream" -m "Live Panel 2.0" \
  -b "Measure upload health in the browser: encoded rate from MediaRecorder chunk sizes, real egress via ws.bufferedAmount drain, send buffer in seconds, late-chunk counter. Throughput sparkline vs 'needed for target' line + Good/Tight/Too-slow verdict pill. Also surface the verdict during the /test rehearsal in the prep panel ('connection good enough' before air time). No backend changes required. Spec: docs/stream-rework/live-panel-2.0.md"

gh issue create -t "Live panel 2.0: selectable + auto upload bitrate" \
  -l "type::frontend" -l "project::Stream" -m "Live Panel 2.0" \
  -b "Quality selector (Auto / 320 / 192 / 128 / 96 kbps Opus). audioBitsPerSecond is fixed at MediaRecorder construction (currently hard-coded 192k) → switching restarts the recorder; sub-second gap is covered by the harbor's mksafe. Auto mode: step down when buffered seconds exceed a threshold for N ticks, step back up after a stable period. Spec: docs/stream-rework/live-panel-2.0.md"

gh issue create -t "Live panel 2.0: stream WS ack/RTT + late-chunk telemetry" \
  -l "type::backend" -l "project::Stream" -m "Live Panel 2.0" \
  -b "Add timestamped ack (or ping/pong) frames to the existing stream WebSocket so the panel can display round-trip time and confirm chunk delivery; expose per-connection counters for late/dropped chunks. Spec: docs/stream-rework/live-panel-2.0.md"

gh issue create -t "Live panel 2.0: Telegram live chat bridge" \
  -l "type::backend" -l "project::Telegram" -m "Live Panel 2.0" \
  -b "Bridge the channel's discussion group into the live panel: bot reads new messages (webhook or getUpdates) → fan out over a panel WebSocket; host replies posted via the bot API with a host badge. Panel UI: message list + text input (frontend part in the shell issue). Spec: docs/stream-rework/live-panel-2.0.md"
```

## Suggested order

1. Shell (pure layout, no new data) → 2. analyzers + faders → 3. connection card
(browser-only metrics; reuse during prep test) → 4. bitrate switch + auto →
5. backend WS telemetry → 6. Telegram bridge (biggest new subsystem, independent).

## Environment notes (from this session)

- `frontend/.env.local` (created, gitignored): `VITE_STREAM_ICECAST_TEST_URL=https://radio.live.moafunk.de/test.mp3`
  makes the rehearsal playback use the real test mount in local dev.
- For the rehearsal *push* leg, the local backend needs
  `ICECAST_TEST_URL=icecast://source:<HARBOR_TEST_PASSWORD>@radio.live.moafunk.de:8005/test`
  in `backend/.env.local` — password from the prod `stream.env`; port 8005 may need
  an SSH tunnel (`ssh -L 8005:127.0.0.1:8005 <box>`).
- The whole session's dashboard work sits on `feat/show-dashboard-redesign` —
  push + PR (squash-merge) before starting Live Panel 2.0 on a fresh branch.
