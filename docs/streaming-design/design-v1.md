# Streaming design v1

Author: Codex. Submitted for Claude review on 2026-09-26. Not jointly accepted.

## Goal and assumptions

Deliver reliable listening and accurate public broadcast information across iPhone Safari, Android browsers, desktop browsers, and direct-stream players. Use standard protocols and preserve a usable direct URL. NTS demonstrates HLS delivery plus explicit application metadata; improve failure handling, evidence, and metadata consistency where possible.

Assumptions to challenge:

- Website is the first client; a native app is not required to establish this foundation. Home-screen mode, AirPlay, and CarPlay are separate validation targets, not automatic promises.
- Show/presenter/artwork is the initial metadata scope. Optional track data must come from an explicit editorial or playout source; audio transport cannot invent it.
- Existing Hetzner, Liquidsoap, Icecast and Rust backend remain the first deployment platform. A new vendor is not required for the first release, but avoiding vendors is not an absolute product constraint from Anton.
- Robust means preserving playback during metadata outages, recovering eligible media failures, detecting silence/delivery failure, and stating origin-outage limits. It does not mean promising uninterrupted audio during an arbitrary network or hardware outage.
- No current listener scale, cost ceiling, minimum iOS version, or availability SLA has been confirmed. Establish these from evidence before infrastructure sizing and public guarantees.

## 1. Delivery

Keep the existing direct HTTPS MP3 mount as a permanent compatibility endpoint for browsers, apps, directories, and external players. Add audio-only AAC-LC HLS in parallel from the same decoded upstream source, not by transcoding public MP3.

Required trial: native HLS on iOS/Safari and direct MP3 against the same content. Start with AAC-LC 128 kbps, approximately four-second segments, and a short sliding playlist; tune from measurements. These are starting parameters, not guaranteed latency or quality. One rendition is not adaptive bitrate streaming. A second lower-data rendition is a later measured decision.

Player selection uses capability detection and explicit configured URLs. Prefer native HLS only after it passes device gates. Other clients initially use MP3. hls.js is unnecessary unless extending HLS to browsers without native support demonstrates a benefit. Fail from a persistently broken HLS session to MP3 in a bounded, user-intent-aware way; prevent repeated transport oscillation. Transport fallback covers protocol/path failures, not loss of their shared origin.

HLS publication must be atomic, with correct MIME types, CORS where needed, short playlist caching, unique segment URLs across restarts, retention for lagging clients, and proper discontinuity handling. Verify Liquidsoap's installed version and actual output.file.hls signatures in an isolated harness. The configuration in the previous plan is a proposal, not yet a validated recipe.

Resume long-paused live listening at the current live edge. Do not expose DVR controls in the initial product.

## 2. Player lifecycle

Use one persistent HTML audio element for the listener page, without routing through Web Audio. Track explicit listening intent separately from actual media state: idle, starting, playing, buffering, recovering, user-paused, interrupted, and failed.

Start playback directly from the user's play action and handle its promise. Drive visible state from media events. A status/metadata request failure means unknown, never confirmed off-air. Serialize polling or reject older responses using a generation/revision; retain last known identity with an explicit staleness policy. Preserve healthy audio during API failures.

Only one recovery attempt may run at a time. Use capped exponential backoff with jitter and an attempt generation; cancel timers and obsolete play results on user pause/stop, source replacement, or destruction. Do not reload on every waiting event or simply because online fires. Confirm lack of media progress beyond a measured threshold. Treat NotAllowedError as requiring a user action, not a reason for endless automatic retries.

Explicit page/system pause actions cancel listening intent. An unexpected pause may be an OS interruption; do not blindly fight another audio app, a phone call, or an unplugged headset. Define conservative handling in a device-tested transition table. Recovery while locked is a validation target; JS timers are not a background execution guarantee.

Block deployment reload while intent is active or playback is recovering. Keep metadata changes independent of audio source replacement. Public cross-page navigation continuity is out of the first implementation unless a persistent shell is added deliberately; disclose this limitation and prefer opening secondary content separately while listening.

## 3. Broadcast identity and metadata

StreamState holds a broadcast/session ID, optional authorized show ID, actual start time, source mode, and lifecycle generation. Manual browser live, manual prerecorded live, and scheduled prerecorded live all initialize the same contract. Stop, replacement, natural completion, and errors clear only the matching generation; late events from an old producer cannot clear a newer broadcast.

Represent rehearsal separately from public live. Do not derive on-air identity from recording state or timetable alone. Recording failure must not erase an otherwise live show's metadata. Legacy unscheduled broadcasts may be live without a show ID.

GET /api/now-playing returns public station identity, broadcast ID, live state, optional show identity, editorial presenter label, artwork descriptor, effective time, update time, and metadata revision. Support live with no show and station fallback. Keep login names, recording paths, and internal operational fields out of this public contract.

Presenter label: assigned public artist names where appropriate; otherwise an explicit public show/presenter field, with station fallback until supplied. Decide storage location during implementation against current show editing flows; no nonexistent user display-name field is assumed.

A versioned public artwork URL identifies the exact show/content revision/size. Generate fixed-size square JPEG derivatives with a separately versioned station fallback. Prefer a backend endpoint/cache over opening the existing private bucket. Bound the cache and image processing work, coalesce duplicate renders, and perform decoding off async request executors. Use a content revision that cannot point to overwritten bytes. Pre-generated immutable derivatives are also acceptable if simpler across all cover creation/upload paths. Publication must update metadata only after the image is available.

Set Media Session title, presenter, station and artwork from the same metadata shown on the page. Detect capabilities and handle unsupported actions individually. Register play/pause/stop through the shared lifecycle. Do not claim null seek handlers dictate every OS layout. Refresh when any displayed field/revision changes and when the page returns to foreground; a failed refresh must not stop audio.

Retain a minimal public legacy status response during migration. Provide authenticated admin status for existing user/private fields and migrate StreamPage/Dashboard before removal. This avoids breaking cached public bundles.

## 4. Metadata timing and off-air

Separate producer state, what is currently delivered, and what the listener hears. HLS introduces buffering; do not immediately stop listeners at producer disconnect and cut the tail of a show. Maintain broadcast-ended timing and a bounded end/drain policy measured per transport. Unexpected producer loss must be distinguishable from planned end; silence fallback is availability of a mount, not proof of healthy programme audio.

Show-level metadata is allowed a documented transition tolerance tied to delivery delay. For accurate track-level metadata later, add a timestamped event history and synchronize to the playback timeline using suitable HLS metadata or program timestamps. Do not equate a polling interval with synchronization accuracy.

Publish title/presenter to ICY for direct-stream clients as an additional output from the same metadata source. Clear/reset it at show end. ICY does not replace website Media Session integration. HLS metadata is an interoperability option to validate, not an assumed automatic lock-screen integration.

## 5. Operations and availability

Measure actual audio through an external probe: HTTP success and backend active are insufficient. Detect decode failures, prolonged silence when a show should be audible, stale HLS playlists, missing segments, and disagreement between producer and delivery state. Apply grace windows for intentional silence and planned transitions. Record startup time, stalls, recovery attempts/outcomes, transport changes and listener minutes; diagnostic events may arrive only after a disconnected browser returns.

One Hetzner host remains a single point of failure. The initial release improves client/stream reliability but must not claim origin redundancy. For origin-outage availability, add an independently hosted distribution/fallback service and a tested failover path; a relay alone cannot continue a lost live source indefinitely. Define fallback programme audio or producer reconnection to a standby ingest. Choose self-hosted versus managed distribution using confirmed scale, budget, and recovery objectives. This is a separate availability gate, not hidden inside the client fallback claim.

Prefer reusing stable public delivery URLs. Do not introduce signing expirations on public streams or artwork. Use HTTPS, test redirects, document direct MP3 and HLS URLs, and validate external players as well as browsers.

## 6. Validation and rollout

1. Record current deployed configuration and real-iPhone baseline. Confirm supported OS versions and public metadata scope.
2. Ship player/status recovery fixes and meaningful regression tests without changing delivery.
3. Ship authoritative broadcast identity, immutable artwork, public/admin response separation, and Media Session. Test unscheduled broadcasts, rehearsals, stale events, cover replacement, and missing data.
4. Run isolated HLS and ICY trials. Verify segment correctness, restart recovery, latency, source transitions and consistent metadata.
5. Prefer HLS for supported mobile clients only when measured gates pass. Keep MP3 fallback and a rebuild/deploy rollback with explicit empty HLS defaults and workflow wiring.
6. Add independent availability infrastructure when the agreed recovery objective requires it. Do not claim that objective is met before a failover exercise passes.

Real-device gates: locked playback across show changes; Wi-Fi/cellular transitions; short network loss while locked; long pause/resume; calls/Siri; headphone removal; AirPlay; user pause during recovery; API failure while audio stays reachable; deployment during buffering. Test Safari and home-screen mode independently. Android and desktop need equivalent applicable cases; VLC/direct clients verify ICY and stable URLs.

Automated tests cover observable state transitions and boundaries, not just handler registration. Include delayed/out-of-order responses, concurrent error events, abandoned play promises, recorded-show completion, old-session cleanup, and artwork fetched after a show transition.

Agree numerical startup/recovery targets after baseline measurement. A 30-minute locked test is an initial gate, not proof of long-term reliability. A background metadata limitation must be stated; if strict continuous updates cannot pass on supported iOS versions, use a native client for that requirement rather than claiming the website guarantees it.

## Questions for Claude

1. Should native HLS be a required trial with conditional mobile preference, or should MP3-only be our final recommendation? Why?
2. Is the broadcast identity/lifecycle contract sufficient across all current source paths?
3. Which immutable artwork publication approach is simplest with the current cover update paths?
4. Can the first release reasonably defer independent origin availability while stating its limitation, or does Anton's robustness goal require specifying a concrete standby deployment now?
5. What unsupported assumptions or missing failure cases remain?
