# Streaming design — decision record

> Why each decision in [streaming-design.md](streaming-design.md) was made.
> Codex and Claude reviewed the design in turns on 2026-09-26. This record
> merges both review logs. Codex approved it and Claude confirmed.
> The full logs and earlier drafts are in the commits of https://github.com/phaabe/live.moafunk.de/pull/309.
> This file does not change the design. If the two differ,
> streaming-design.md wins.

## 1. Outcome

Codex and Claude reviewed the design in turns until both accepted the same
draft (v3, now streaming-design.md). The agreed design:

- **Continuous station stream** (Anton's choice). Between shows the public
  mounts play approved station content. The player stops only when the
  listener stops it.
- **Two formats.** Native HLS is the planned default for tested Apple
  clients. The direct MP3 stream stays for everyone else and as the fallback.
- **One public programme model** feeds the page, the lock screen (Media
  Session) and the stream title that ICY clients see.
- **A stable public stream host**, separate from the admin host.
- **Known limits:**
  - One origin server. If it goes down, the stream stops.
  - While the phone is locked and our page JS is suspended, the lock screen
    shows the last metadata we set. There is no freshness guarantee.

## 2. How the review ran

| Round | Who | What |
|---|---|---|
| Start | Claude | First plan (`mobile-playback-plan.md`) |
| Start | Codex | Own research, then review of Claude's plan and design v1 |
| C1 | Claude | CHANGES REQUESTED v1: HLS as the planned iOS default, stable host, generation counter, presenter, artwork, concrete values, broadcast auth |
| X2 | Codex | Internal challenge: Apple HLS rules, background limits, programme end, failover, delivered identity |
| C2 | Claude | Reply to X2: 4 s segments, short window, continuous vs finite as Anton's choice, Liquidsoap as output truth |
| X3–X4 | Codex | Accepts most points; finds the artwork race; RFC 8216 retention; narrower auth policy |
| C3 | Claude | Accepts; new item: HLS must not rely on `canPlayType` alone |
| X5 | Codex | Submits design v2 |
| C4 | Claude | CHANGES REQUESTED v2: HLS selection rule, restart behind a cached master |
| X6 | Codex | Anton chooses continuous; stable media playlist URL; epoch handshake |
| C5 | Claude | Accepts; fallback content becomes a launch prerequisite |
| X7 / C6 | both | **Accept design v3** (now streaming-design.md) |

(C = Claude round, X = Codex round, in the review logs linked above. "v3 §N" means section N of streaming-design.md.)

## 3. Decisions by topic

Each row gives the starting positions, how the topic was settled, and where
the result lives in streaming-design.md.

### 3.1 Delivery: HLS or MP3

- **Codex (v1):** direct MP3 as the baseline, plus a required native-HLS
  trial. Prefer HLS on mobile only after device results.
- **Claude (C1):** go further. Native HLS is the planned default for
  Safari/iOS, with a go/rollback gate. Reason: on the MP3 path, recovery
  needs our page JS, which iOS may not run while locked. With native HLS,
  playlist reload and segment retry run in the OS media stack. C1 also
  claimed that native HLS keeps AirPlay working, where hls.js with
  ManagedMediaSource does not.
- **Correction (Codex X3, accepted by Claude in C3):** ManagedMediaSource
  does not rule out AirPlay; it requires an alternative AirPlay source
  ([WebKit](https://webkit.org/blog/14735/webkit-features-in-safari-17-1/)).
  Native HLS avoids that requirement. AirPlay and CarPlay each stay a
  separate device gate.
- **Settled:** native HLS is the planned Apple default, released only after
  the real-device gates pass. MP3 is permanent. No hls.js in the first
  release. Latency is measured on both paths as its own gate. (v3 "Decision
  and limits", §2, §8)

### 3.2 HLS settings

- **Codex (X2):** Apple's authoring spec: 6 s segments, ≥ 6 segments,
  `PROGRAM-DATE-TIME` in every playlist, discontinuity sequence, about 15
  minutes of playlist history.
- **Claude (C2):** keep the Apple MUSTs, but start with 4 s segments
  (latency matters for live chat) and a short window (a 15-minute window
  enlarges the seekable range and invites scrub UI, and we offer no DVR).
- **Codex (X4):** accepted as a documented deviation from Apple's SHOULDs,
  with 6 s as the comparison. Added RFC 8216 §6.2.2: after leaving the
  playlist, a segment stays on the origin for at least its duration plus the
  longest playlist that contained it.
- **Settled:** AAC-LC 128 kbps, MPEG-TS first. 4 s segments (6 s as the
  fallback, 2 s only if latency needs it). 10 segments in the playlist.
  Removed segments kept ≥ 120 s. Playlist `max-age=1`, segments
  `max-age=60`. (v3 §2)

### 3.3 Which clients get HLS

- **Claude (C3):** `canPlayType` alone is not enough. Android Chrome returns
  `"maybe"`, so it would get HLS without any device test.
- **Codex (X5):** agreed. The AirPlay-API check is a capability heuristic,
  not proof of device or engine version.
- **Settled:** HLS only when rollout config enables it, `canPlayType` is
  non-empty, **and** the client is in the device-qualified set (first
  release: tested Apple WebKit environments). Everyone else gets MP3. After
  3 failed recoveries, switch once to MP3 and never switch back in that
  session. (v3 §2)

### 3.4 Encoder restarts

- **Claude (C4):** after a Liquidsoap restart, a playing client keeps
  loading the old media playlist and stalls quietly. Proposed: close the old
  playlist or return 404.
- **Codex (X6):** a stronger fix. Keep both the master and media playlist
  URLs stable, and put only the segments under per-epoch paths. Closing or
  deleting a playlist does not prove that native players reload the master.
- **Settled:** stable playlist URLs, continuous media sequence and
  discontinuity sequence across restarts, a locked-iPhone restart test, and
  a stated limit for full disk loss. (v3 §1, §2)

### 3.5 Between shows

- **Codex (X3):** prefers continuous station playback. A silence mount never
  signals "programme ended", and a JS drain timer does not run reliably
  while locked.
- **Claude (C2):** a product decision for Anton. Listed both options. For
  finite shows: close HLS with `ENDLIST`; initially keep MP3 silence.
- **Codex (X4):** `ENDLIST` does not prove that the lock screen clears.
  Finite MP3 needs a mount that really closes (accepted in C3).
- **Anton:** chose continuous station playback.
- **Claude (C5):** so approved fallback content is a launch prerequisite.
- **Settled:** source priority is qualified producer → approved fallback
  playlist → emergency station ident. `mksafe` silence is only an alerted
  last resort under the fallback, never the programme. No JS drain timer.
  Data use is documented (~115 MB/h MP3, ~58 MB/h AAC). (v3 §6)

### 3.6 Player lifecycle and status

- **Codex (review of Claude's plan):** a failed status request is treated as
  off-air and destroys healthy audio. Reconnects need cancellation, dedup
  and a generation token.
- **Claude (C1):** one progress-based watchdog; `error`, `stalled`,
  `online` and visibility events only schedule an early check. Concrete
  backoff values.
- **Codex (X3, X2):** the first failed poll already marks data stale.
  There is no freshness bound while locked.
- **Settled:** one scheduler, 8 s without progress, backoff 1–30 s with
  ±20 % jitter, reset after 30 s of healthy playback. An unexpected OS pause
  becomes "interrupted"; we don't fight calls or other apps.
  `NotAllowedError` becomes tap-to-resume. Poll every 10 s while visible;
  an unknown status never stops audio. Deploy reloads are blocked while the
  user wants to listen. (v3 §3)

### 3.7 Broadcast identity and output authority

- **Codex (v1):** `StreamState` owns the identity; all start and stop paths
  use one contract.
- **Claude (C1, C2):** add a numeric generation. Liquidsoap reports which
  source is audible through callbacks to the backend.
- **Codex (X3, X4, X6):** a `u64` resets on restart, so also use a public
  UUID. Harbor connect is not proof of audible programme. Epoch UUIDs can't
  be ordered, so they need an explicit activation handshake.
- **Settled:** broadcast UUID plus runtime generation. Producer state,
  selected output and delivery health are separate facts. Liquidsoap sends
  ordered events and 5 s snapshots to an internal route on the existing
  `127.0.0.1:8000` port. After 15 s without an update, output is
  `unknown`. Modes: live, prerecorded, fallback, off_air, unknown.
  Rehearsal is never public. (v3 §4)

### 3.8 Who may broadcast a show

- **Claude (C1 addendum):** verified that the WS upgrade only checks login,
  and `force=true` lets anyone take over.
- **Codex (X4):** verified the artist-assignment path. The existing
  `require_show_editor` omits artists, and `resolve_user_shows` reads only
  one linked artist profile. Proposed a narrower policy.
- **Settled:** admin/superadmin, the direct host, or a user linked to an
  assigned artist (EXISTS over all linked profiles). Creator alone does not
  grant broadcast rights. One shared helper for browser and prerecorded
  go-live. `force` is limited to the current owner or an admin. (v3 §4)

### 3.9 Presenter name

- **Settled:** a new nullable `shows.public_presenter`. UNHEARD defaults to
  the assigned artists' public names; other shows fall back to the station
  name. Never a login name. (v3 §5)

### 3.10 Artwork

- **Claude (C1):** a lazy resize endpoint keyed by `cover_generated_at`,
  with a 302 from an old revision to the current one.
- **Codex (X3):** race: an upload replaces the bytes before the revision
  changes, so an old URL can cache new bytes for a year. The 302 also loses
  the exact old cover.
- **Claude (C3):** accepted; proposed a keep-last-5 retention.
  **Codex (X5):** public URLs can live longer than that; keep all versions.
- **Settled:** each cover is published as an immutable private object named
  by content hash, and the revision is committed after it. Derivatives
  (512/256/96 JPEG) are stored immutably. There are no redirects, errors
  are never cached, and all versions are kept for now. (v3 §5)

### 3.11 Public interface and interoperability

- **Claude (C1):** the public stream sits on the admin host today. Proposed
  a stable public host with `.mp3`, `.m3u8`, `.m3u`/`.pls`,
  `now-playing.json`, artwork, exposed ICY headers and a directory listing.
- **Codex (X3, X5):** accepted. Old URLs stay as aliases. The hostname needs
  the domain owner and a check of what the retired NMS host still serves.
  NTS already has `PROGRAM-DATE-TIME`, so this is conformance, not a lead.
- **Settled:** v3 §1. ICY titles are set through Liquidsoap, not through
  Icecast admin updates. This avoids competing Liquidsoap and Icecast-admin
  metadata updates (v3 §6).

### 3.12 Origin outage

- **Both:** the first release does not survive loss of the single server,
  and says so. There is an off-box audio probe.
- **Settled:** a future failover contract (independent fallback audio,
  stable URLs, 3 failed probes → fallback, 60 s healthy before failback,
  5 min hold-down). Trigger: one unrecovered origin outage during an
  announced show starts a standby review. (v3 §7)

## 4. Problems found in today's code

Verified during the review against commit `1934e6b`:

| Problem | Where | Found by |
|---|---|---|
| One failed status poll stops working audio | `frontend/src/streamDetector.ts` `checkBackendLive`, `frontend/src/main.ts` | Codex, confirmed by Claude |
| Any logged-in user can stream as any `show_id`; `force=true` lets anyone take over | `backend/src/handlers/stream_ws.rs:70-104` | Claude |
| `require_show_editor` omits assigned artists; `resolve_user_shows` reads one artist profile only | `backend/src/handlers/api.rs:5710`, `:4890` | Codex |
| The public status endpoint returns the host login and, while recording, a server file path | `backend/src/stream_bridge.rs` `get_status` | Claude |
| The live harbor input is wrapped directly in `mksafe`, which blocks a real fallback programme | `docs/stream-rework/prod/moafunk.liq` | Codex (v3 §6), Claude (C6) |
| The play button works only while "live"; this must change for continuous mode | `frontend/src/player.ts` `play` | Claude (C6) |
| The HLS config still defaults to the retired NMS URL | `frontend/src/config.ts` | Codex |

## 5. Corrections each side made to its own claims

- **Claude:**
  - The research claimed that NTS shows only show-level metadata and has no
    custom recovery. Codex found track metadata and an error replay path in
    NTS's bundle.
  - Claude listed `PROGRAM-DATE-TIME` as something NTS lacks, but NTS has
    it.
  - Claude said ManagedMediaSource rules out AirPlay; it only needs an
    alternative AirPlay source.
  - Claude's first artwork design had a cache race.
  - The keep-last-5 retention contradicted stable public URLs.
- **Codex:**
  - v1 left HLS too optional for the stated goal.
  - It first proposed a 15-minute HLS window.
  - It corrected its own Apple spec section reference (discontinuities are
    §8.17).

## 6. Before launch

- Approved fallback playlist and emergency station ident (supplied by
  Anton or the operator).
- Stream hostname and DNS, agreed with the domain owner.
- A Liquidsoap 2.4.4 harness run for HLS options, persistence and metadata
  insertion.
- Real-iPhone gates, as listed in v3 §8.

## 7. Approval of this record

- Codex: approved (Codex log, Round 9).
- Claude: confirmed Codex's approval (Claude log, Round 9).
