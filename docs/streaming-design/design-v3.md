# Streaming design v3 — continuous station, native HLS and direct MP3

Submitted by Codex on 2026-09-26 after joint review of v1 and v2. Awaiting explicit acceptance from both reviewers. Earlier drafts remain unchanged. Changes from v2: Anton selected continuous station playback; stable HLS media-playlist routing across restarts; explicit process-epoch activation; device-qualified HLS eligibility; immutable artwork retention; domain-owner coordination; Liquidsoap metadata ownership; concrete fallback-source priority.

## Decision and limits

Plan native audio-only HLS as the default for iPhone/Safari, permanent direct MP3 for other clients and fallback, and one authoritative public programme model feeding the website, Media Session and ICY. Use a stable public streaming host independent of the admin host. Keep the existing backend, Liquidsoap and Icecast for the first deployment.

Native HLS delegates playlist loading and segment recovery to the media stack. This reduces dependence on page JavaScript during locked playback; it is not a guarantee of uninterrupted playback. Release the planned default only after real-device gates pass. No hls.js or native app is needed in the first release.

Initial metadata is show title, public presenter and artwork. Individual tracks require a later explicit editorial/playout source. Locked-screen metadata is last successfully applied information when JavaScript is suspended: no finite background freshness guarantee is made. Safari, home-screen mode, AirPlay and CarPlay require separate evidence.

One origin remains a single point of failure. This release improves playback and protocol reliability, not origin-outage availability. The latter has a separate defined operating contract below.

## 1. Stable public interface

Proposed public host: `https://stream.moafunk.de`. The final name and DNS changes require domain-owner coordination and retired-NMS inventory using the existing DNS handoff process. Design agreement is not a DNS deployment. Keep existing public URLs working as aliases. The proposed route contract is:

| Path | Meaning |
| --- | --- |
| `/live.mp3` | Permanent direct MP3 stream |
| `/live.m3u8` | HLS master with an audio-only AAC rendition |
| `/hls/live.m3u8` | Stable media-playlist URL retained across process restarts |
| `/hls/segments/<epoch>/...` | Uniquely named immutable media segments |
| `/live.m3u`, `/live.pls` | External player playlists pointing to permanent MP3 |
| `/now-playing.json` | Public JSON, proxy to the backend |
| `/artwork/shows/<id>/<revision>/<transform>/<size>.jpg` | Exact immutable public show artwork |
| `/artwork/station/<revision>/<size>.jpg` | Versioned station artwork |

The master references the stable media-playlist URL and declares measured BANDWIDTH plus AAC-LC CODECS="mp4a.40.2". Existing clients must not need to reload the master to discover an encoder restart. Serve old working MP3 URLs as proxy aliases throughout migration; do not break bookmarks or cached app bundles. Do not reuse retired NMS paths as implicit defaults. HLS media paths must resolve directly, without redirect chains. All endpoints use HTTPS and public CORS without credentials. Expose relevant ICY response headers for clients that request them. Restrict public artwork access to published artwork records; never accept arbitrary private object keys.

Publish a short contract with examples. Register directory listings only as a later authorized operational task, not during this planning exchange.

## 2. Delivery settings and conformance

| Setting | Initial value / rule |
| --- | --- |
| MP3 | Existing 256 kbps stereo, 44.1 kHz; retain proxy buffering disabled |
| HLS | AAC-LC 128 kbps stereo, 48 kHz; MPEG-TS first harness candidate |
| Segment target | 4 seconds initially; compare 6 seconds if stability fails; try 2 seconds only if measured latency requires it |
| Playlist | 10 segments initially, always at least 6 once published as ready |
| Removed segment retention | At least 120 seconds after removal, and never less than segment duration plus the longest playlist containing that segment |
| Playlist caching | `max-age=1`; correct MIME; atomic updates |
| Segment caching | `max-age=60`; unique URLs across all restarts; no overwrite |
| Playlist timeline | PROGRAM-DATE-TIME in every live media playlist, preferably every segment; stable media sequence and correct discontinuity sequence |
| Storage | Bounded filesystem storage and explicit cleanup; persist publication epoch/state across process restart; tmpfs alone must not be assumed reboot-safe |

Four-second segments and a short history deliberately depart from Apple's six-second and 15-minute SHOULD recommendations. Reasons: station listening near live and reduced seekable history. These choices do not guarantee that every OS hides seek controls. Verify both Apple MUST requirements and RFC 8216. A single rendition is not adaptive bitrate streaming. ADTS is an alternative only if the exact Liquidsoap version and clients validate it better than TS.

Segment retention is an origin requirement, not something satisfied by cache TTL. Test a client requesting an older published segment after the playlist advances. With a ~40-second window, 120-second removal retention meets the nominal RFC minimum; enforce using actual durations if settings change. Preserve recently referenced files and stable media-playlist URLs during process restart. Persist sequence/publication state and continue monotonically increasing media sequence with correct discontinuity sequence; put only new segment files under a new epoch. Do not silently freeze the old playlist, reset sequence numbers, or rely on ENDLIST/404 making native clients rediscover the master. Test both graceful restart and process crash while a real iPhone is locked. If installed Liquidsoap cannot satisfy continuity with its persistence support, the publication adapter must supply it before HLS passes the release gate. After full disk loss, test recovery as an explicitly nonseamless case; changing segment epoch alone does not guarantee it.

Encode AAC from the upstream decoded source alongside MP3, not from the public MP3 output. No additional lossy intermediate encode. Measure the pipeline's resampling and clipping behavior. Validate actual Liquidsoap 2.4.4 output.file.hls options in the harness; nearby-version documentation is not execution evidence.

The player uses native HLS only when enabled by rollout configuration, canPlayType for application/vnd.apple.mpegurl is non-empty, and the client falls within the device-qualified eligibility rule. First release: tested Apple WebKit environments using native HLS; an AirPlay capability check such as webkitShowPlaybackTargetPicker on HTMLMediaElement.prototype is a conservative feature filter, not proof of device, browser engine version or reliability. Treat unrecognized, ungated and nonqualifying clients as MP3 clients. Do not assume all iOS browsers use the same engine. Add Android or other desktop native-HLS paths only after their own gates; canPlayType alone does not opt them in. After three failed recovery attempts in a listening session, switch once to MP3; never oscillate back automatically during that session. Handle rejection of the new play request with tap-to-resume. This fallback depends on JS executing and covers media-path problems, not shared-origin loss.

## 3. Player lifecycle and recovery

Maintain one HTML audio element on the listener page. Avoid a Web Audio processing graph. Keep user intent separate from observed state: idle, starting, playing, buffering, recovering, user-paused, interrupted and failed.

- Invoke initial play from the user's action and handle the promise. UI reflects media events, not a CSS class used as state.
- One scheduler checks media progress. Initial threshold: eight seconds without currentTime progress while listening is intended and the element is not in a known user/OS pause. Media error/stalled/waiting/online events request an early check rather than directly reloading the source.
- On native HLS, let native loading recover first; do not repeatedly reset a playlist merely because one segment waits. Confirm progress/ready state before intervention. Observe currentTime behavior under AirPlay and disable any watchdog assumption disproved by that route's tests.
- Backoff: 1, 2, 4, 8, 16, 30 seconds with ±20% jitter. Reset only after 30 seconds of healthy progress. One pending timer and one active recovery operation; generation tokens invalidate stale events and play promises.
- User page/system pause or stop cancels intent and recovery. An unexpected OS pause enters interrupted state; do not auto-play just because online or visibility changes, or fight calls, another audio app, or headphone removal. Resume from an explicit page/system play action unless a device-tested eligible interruption path permits it.
- NotAllowedError stops automatic retries. Other failures have bounded retry frequency; persistent failure offers a clear retry control. Online is a hint, not proof the stream is reachable.
- A long user pause resumes at the live edge with a fresh source as needed. Never reload merely to refresh metadata.
- Block deployment reload while listening intent is active, including buffering/recovery. Cross-page persistence is a separate UI feature; until implemented, keep the listener page open and label links that interrupt it.

Status/metadata: poll every ten seconds while visible and immediately on foreground return or a user media action. Use one request at a time, a five-second timeout, and generation/revision ordering. If background timers run during active playback, they may refresh opportunistically; correctness and recovery must not require them. The first failure makes freshness unknown while retaining the last valid values. Three consecutive failures may trigger a warning. Unknown never stops working audio and never means confirmed off-air.

## 4. Broadcast and output authority

StreamState owns a restart-safe broadcast UUID, monotonic runtime generation, optional authorized show ID, source type and actual producer start time. All live/prerecorded entry and exit paths use this contract. Async completion/failure clears only its own generation. Rehearsal is never public live. Recording failure does not end an otherwise healthy broadcast. Unscheduled live broadcasts with no show ID are valid and use station fallback identity.

Authorization: authenticated admin/superadmin, directly assigned host, or a user linked to an artist assigned to the show. Query all linked artist profiles using EXISTS. Creator/edit permission alone does not grant new broadcast permission. Show ID must exist and pass this rule before public streaming metadata uses it. Share a dedicated broadcast authorization helper between browser and prerecorded paths. Force takeover is a separate check limited to the current stream owner or an administrator. Add regression tests for legitimate UNHEARD artists and unrelated accounts.

Producer active, selected output and delivery health are separate facts. Liquidsoap's final source selector reports ordered events and snapshots to an authenticated internal endpoint through the backend's existing published 127.0.0.1:8000 port (verify deployment wiring). No new public callback port is needed; deny the internal route at public reverse proxies as well as requiring service authentication. Include a Liquidsoap process epoch, event sequence, delivery generation, selected programme mode, associated broadcast UUID when known, and effective UTC time. Harbor connect/disconnect is connectivity evidence, not proof of audible programme. The backend reconciles periodic snapshots every five seconds; after fifteen seconds without an authoritative update, output knowledge is unknown. Exact callback/RPC integration must pass the installed-version harness.

Programme modes: live, prerecorded, fallback, off_air, unknown. Keep recent boundaries for at least ten minutes initially, longer than the initial HLS playlist plus removed-segment retention. Random UUID epochs are identities, not sortable counters. Backend activation binds the current supervised Liquidsoap process epoch using a fresh startup/reconciliation handshake verified against the running service, and retires the previous epoch. Ordinary callbacks from unknown or retired epochs cannot activate themselves. Within the active epoch, accept only increasing event sequence. After backend restart, output mode stays unknown until current-service handshake/snapshot re-establishes authority. Reject responses from superseded reconciliation attempts and stale producer exits. Do not display a producer's show as current when output has selected fallback.

An example public response shape:

```json
{
  "schema_version": 1,
  "station": {"id": "moafunk", "name": "Moafunk Radio"},
  "broadcast_id": "uuid-or-null",
  "producer_live": true,
  "delivery": {"epoch": "uuid", "generation": 4, "mode": "live"},
  "show": {"id": 39, "title": "Show title", "presenter": "Public name"},
  "artwork": [{"src": "https://stream.moafunk.de/artwork/shows/39/hash/v1/512.jpg", "sizes": "512x512", "type": "image/jpeg"}],
  "effective_at": "2026-09-26T00:00:00Z",
  "updated_at": "2026-09-26T00:00:01Z",
  "revision": "opaque-change-token"
}
```

`broadcast_id`, `show` and unknown delivery fields are nullable, not literal placeholder strings. `producer_live` preserves the meaning of active public producer; UI labels derive from delivery mode and freshness, not that boolean alone. No account login, recording path or operational credential enters the public response. Metadata revisions change for any displayed-field change, including same-show presenter/artwork edits. Public JSON uses `max-age=5`, ETag and ACAO *.

Keep legacy public `/api/stream/status` with at least `{active}` during migration. First move StreamPage/Dashboard to authenticated admin detail, then remove public user/private fields. Cached listener bundles must retain a working live-state endpoint.

## 5. Presenter and immutable artwork

Add nullable `shows.public_presenter`, editable in the show dashboard. Use it when explicitly supplied. Otherwise derive UNHEARD presenter from assigned public artist names; other shows use station name until supplied. Artist assignment changes update the public metadata revision. Do not equate account usernames with public identities.

One publication helper handles uploaded, copied, generated and Telegram-replaced covers. Validate and decode safely within existing upload limits. Write an immutable private source object addressed by content hash, then commit `artwork_revision`/object reference. An existing mutable `cover.png` may remain for legacy consumers, but new public routes never read it to satisfy an old revision. Failed uploads or DB writes must be reported; an unreferenced immutable object can be garbage-collected later.

Lazy derivatives read only the immutable source revision. Generate 512/256/96 square JPEGs, quality about 85, on a blocking worker with single-flight per key and bounded concurrency. Include transform version in the route; persist a generated derivative under an immutable private key before long-cache publication so cache eviction/deployment cannot change bytes for an old URL. Use a bounded in-memory cache, initially 64 entries. Expose only these approved public derivatives through the public host; the bucket remains private.

Return `Cache-Control: public, max-age=31536000, immutable` only for a found exact revision. Never redirect an old show revision to a new cover. For the initial deployment, retain published immutable source versions and persisted derivatives indefinitely; only the in-memory cache is evicted. A future deletion policy must define public URL lifetime and respect current/referenced artifacts. If an exact version is unavailable, return a non-cacheable error and let the client use the separate station artwork. Never cache an error or substitute station bytes for a year under a show-artwork URL. Metadata advertises a new revision only after its immutable source is stored and committed. A newly published revision's first render failure must preserve prior valid metadata or station fallback until a successful render is available; enforce an initial derivative readiness check at publication.

## 6. System metadata and programme transitions

Page and Media Session share one displayed programme snapshot. Map title to show title, artist to public presenter, album to station/programme label, artwork to the immutable URLs. Detect Media Session and each supported action; play/pause/stop use the same lifecycle. Clear obsolete position state; do not publish fictional duration. OS seek/control layout remains a device-test result.

Metadata API describes selected output at a timestamp, not exact currentTime on every listener. For the initial show-level feature, document transition tolerance bounded by measured delivery lag while JS runs. Preserve recent boundaries and program timestamps for a later playback-synchronized adapter. If exact locked-screen track freshness becomes mandatory and cannot be validated on supported iOS, add a native client; do not promise that polling, SSE or timed tags alone solves it.

Publish title/presenter into Liquidsoap's selected-source metadata, so its output owns ICY updates consistently across programme changes; verify the installed-version insertion/control interface in the harness. Do not race direct Icecast admin updates against Liquidsoap's later metadata emissions. Reset metadata for fallback/off-air. Test with a direct client that requests ICY metadata. Optional HLS timed metadata must use supported Liquidsoap/container encoding and be verified in clients; it does not replace explicit website Media Session publication.

### Continuous station playback — confirmed by Anton

Anton explicitly chose continuous station playback. Between shows the public mounts carry an operator-approved station loop or archive programme. Source selection and matching programme metadata happen on the server. Listeners stay in the station session until they pause or stop; producer disconnect and failed status polling do not tear down the player. No finite-show endpoint, ENDLIST at every show end, or off-air JS drain timer is part of this release.

Source priority is explicit: qualified public producer (live or prerecorded) → locally available approved fallback playlist → locally available emergency station ident. Rehearsal can never enter this chain. Keep the public input fallible until this selection occurs; wrapping the primary input in mksafe before fallback would make silence appear permanently available and prevent proper fallback. A final safety silence source may prevent a broken mount only as an alerted failure state, never as the claimed fallback programme.

Keep fallback assets on local storage so a backend/R2 metadata outage does not stop between-show audio. Validate assets before activation and prepare their public title/artwork. Start with a three-second producer-loss grace for short handovers and require two seconds of valid source availability before return; tune in the harness. Signal presence alone is not proof of programme sound: silence monitoring is a separate health signal. Preserve encoded tails through the shared output pipeline; do not cut buffered listeners when source mode changes. The final selector publishes the effective programme boundary and delivery generation.

Approved fallback content and an emergency station ident are release prerequisites, not a reason to invent or select third-party audio. Fallback may repeat while there is no live show; its identity must say fallback/rerun and must not retain the former live show's title. Website lock-screen changes remain subject to the stated background-JS limit.

Document ongoing data use for listeners who leave the station playing: approximately 115 MB/hour for 256 kbps MP3 and 58 MB/hour for 128 kbps AAC payload, before transport overhead. Planned pauses and long-resume behavior remain explicit user controls. Finite-show playback is outside this design unless requested separately.

## 7. Operations and future independent availability

Use off-box probes for MP3 and HLS that fetch media, decode, and compare expected programme mode with actual signal. Detect stale playlists, missing segments, decode failure and sustained unexpected silence, with grace for intentional silence and handover. A connected producer or HTTP 200 is insufficient. Alert through existing monitoring integrations during implementation. Record startup latency, stalls, reconnect outcomes, chosen transport and listener duration; disconnected-browser telemetry may arrive late or never.

First-release boundary: complete origin loss interrupts service. Stable public URLs and two formats on one machine do not change that.

Future failover contract: an independent distribution service must retain approved fallback audio locally, select it after sustained origin failure, publish matching fallback metadata, and use stable listener URLs. Producers need a standby ingest and reconnection path for live continuity. Use server-side health selection, not locked-browser JS as the sole failover mechanism. Initial candidate policy: three failed five-second probes trigger fallback, require sixty seconds of healthy origin before failback, and use a five-minute hold-down to avoid flapping. Test and tune before claiming availability. Epoch/discontinuity changes and metadata follow the actual selected output.

One unrecovered origin outage during an announced show triggers a standby review. If surviving origin loss is a required launch criterion, independent deployment and a failover exercise become prerequisites. Hosting purchase, topology sizing and availability SLA require confirmed budget, concurrent listeners and recovery objectives; no current numbers are invented.

## 8. Implementation order and release gates

1. Baseline deployed paths and real devices. Record OS/browser versions, tap-to-audio latency, producer-to-ear latency, interruptions and current display.
2. Fix status uncertainty, playback state, recovery deduplication/cancellation and deployment reload. Add regressions before delivery changes.
3. Implement broadcast authorization/identity, output reconciliation, presenter/artwork publication, public/admin contract migration and Media Session.
4. Add stable public host aliases and HLS/ICY harness. Validate restart continuity, retention, source transitions, discontinuities and exact codec/container.
5. Test continuous fallback transitions and native HLS on real devices. Switch the planned default only if gates pass; otherwise keep MP3 and report the failed gate. Keep transport setting changes explicit in Vite configuration and CI; rollback requires rebuild/deploy. Preserve old URLs.
6. Measure at least two full shows plus fault injection before wider rollout. Record actual outcomes, not just a checklist.

Required tests: metadata API failure with working audio; out-of-order responses; stalled media; concurrent error events; pause during recovery; rejected/late play promises; backend/Liquidsoap restart including a locked native-HLS client; natural prerecorded completion into fallback; wrong-show authorization; missing/changed cover; artwork fetched after a show transition; rehearsal-to-live handover; old producer cleanup after replacement; source active but output failed.

Real iPhone tests: thirty-minute locked playback, show/cover change while locked, Wi-Fi/cellular handover, temporary loss while locked, long pause/resume, call/Siri interruption, headphone removal, deployment during buffering, HLS→MP3 fallback, AirPlay and CarPlay where claimed. Repeat applicable tests in home-screen mode, Android and desktop; VLC/direct clients cover URL and ICY interoperability.

Initial qualification targets, not current guarantees: healthy-network tap-to-audio p95 under five seconds; short network loss recovery within fifteen seconds after connectivity returns in at least 19/20 controlled trials; no uncommanded restart after user pause; no audio interruption from metadata/API faults; measure HLS end-to-end delay and seek approval if it exceeds thirty seconds or degrades the actual live-chat workflow. Adjust only with recorded baseline/device evidence. Long-outage and origin-loss recovery have separate limits above. Define supported minimum iOS from these results, rather than assuming all versions behave identically.

## References and remaining agreement

- [NTS inspected web implementation](https://www.nts.live/js/app.min.a98998a3aafb663e.js): HLS, direct fallback and explicit Media Session; not evidence for actual locked-iPhone behavior.
- [Apple HLS authoring specification](https://developer.apple.com/documentation/http-live-streaming/hls-authoring-specification-for-apple-devices/): distinguish MUST from SHOULD.
- [RFC 8216](https://www.rfc-editor.org/rfc/rfc8216.html): playlist lifetime, retained segments and continuity.
- [Liquidsoap HLS documentation](https://www.liquidsoap.info/doc-2.4.5/hls_output): verify against installed 2.4.4 in the harness.
- [WebKit background execution](https://webkit.org/blog/8970/how-web-content-can-affect-power-usage/) and [Media Session](https://w3c.github.io/mediasession/): system metadata and background limitations.
- [WebKit ManagedMediaSource notes](https://webkit.org/blog/14735/webkit-features-in-safari-17-1/): native HLS avoids the documented alternative-AirPlay-source restriction; actual routes still need testing.

Claude: please review v3 and record ACCEPT design-v3.md or specific remaining changes in claude.md. Acceptance means agreement on this design and its validation/release gates, not evidence that an unbuilt implementation has passed them. Codex will record its own acceptance separately after final review.
