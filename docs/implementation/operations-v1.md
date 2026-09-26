# Operations implementation taskbook v1

Status: first Codex submission for Claude review. Plan only. Baseline `13e73de53f02248feede47e1eced6ac2086e38dd`. The architecture and streaming contracts remain authoritative. All new paths below are proposals, not claims that files already exist. Production inventory is read-only; applying configurations, domain changes and fault exercises are separate activation tasks.

One operations implementer owns workflow, shell, nginx, systemd and media-config changes. Backend admission, recording and output-authority modules remain backend-owned; coordinate their interfaces before wiring them. Preserve existing secret management; never include secret values in fixtures, logs or evidence. An implementation PR may satisfy several related leaves, but cannot claim device/production gates from unit tests. Record commit, deployed image/config digests, environment, command and sanitized result for each exercised leaf.

## O1 — Establish the runtime baseline and stop uncontrolled replacement

**Owner/files:** operations; existing `.github/workflows/backend.yml`, `backend/scripts/deploy_hetzner.sh`, `backend/docker-compose.prod.yml`, `docs/stream-rework/prod/`; proposed `docs/implementation/evidence/runtime-inventory.md` and `docs/implementation/runbooks/bootstrap-maintenance.md`.
**Dependencies:** coding and read-only inventory start immediately. First gate activation needs the backend maintenance-gate implementation, but must not depend on future HLS or fallback. **PR boundary:** inventory/runbook plus a separate workflow-only protection PR. **Rollback:** retain automatic builds; never restore automatic service replacement as a rollback convenience.

### O1.1 — Record deployed facts and operator prerequisites

- [ ] **O1.1.1** Inventory running image digests and effective Liquidsoap/Icecast/nginx/API versions, network bindings, Docker bridge access, persistent mounts and current public/preview URLs. Read the production host only through approved access; verify each result against the repository and mark unavailable checks as unknown, including the retired NMS/domain-owner handoff.
- [ ] **O1.1.2** Record actual CPU, memory, usable disk/inodes, fd limits, Icecast client limit, nginx connection/timeouts, link allowance and billing period; capture a baseline live/idle resource sample and identify the runtime clock service. Verify that the proposed 200 listeners is a planning load, not a measured capacity claim, and a proxy read-idle timeout is not mislabeled a total playback duration.
- [ ] **O1.1.3** Assign named operator owners for continuous-use fallback/ident approval, stream DNS/TLS, real-device access, external observer and retained backup destination; record approved asset revisions and remaining cost/account-loss limits. Verify local harness work can proceed with synthetic fixtures while activation remains blocked on missing approvals/resources.

### O1.2 — Remove the unsafe deployment trigger and bootstrap the gate

- [ ] **O1.2.1** Change the main-push workflow to build/push images without replacing API or restarting Liquidsoap; preserve image SHA/digest output and allow build cancellation independently. Test push/manual input cases and assert no mutating SSH, nginx provisioning, database initialization or media restart can execute from a build-only event.
- [ ] **O1.2.2** Define a one-time, explicitly logged legacy maintenance procedure for installing the first gate-capable image: operator reserves a show-free window, temporarily denies new producer/capture requests and schedule mutations at the ingress, verifies no runnable/catch-up scheduled start can occur, and drains producer/rehearsal/capture/finalisation before snapshot and replacement. Specify exact deployed routes and keep stop/diagnostic paths usable. If the legacy scheduler cannot be bounded safely, use an announced bootstrap API outage after work drains, rather than inventing a pause control. Retain DB/config snapshot and rollback image; rehearse both paths without assuming the not-yet-installed gate, fallback or HLS already works.
- [ ] **O1.2.3** Define release-stage readiness profiles: legacy MP3 with finalisation drained, continuous MP3 with approved fallback, then qualified HLS+MP3; promote only against recorded evidence and require only activated formats at each stage. Verify that profile changes cannot silently waive a previously activated output and the final profile enforces the complete architecture readiness contract.

## O2 — Run deployment through one fenced host executor

**Owner/files:** operations; existing workflow/deploy script/Compose; proposed `.github/workflows/deploy-backend.yml`, `backend/scripts/deploy/execute.sh`, `backend/scripts/deploy/recover.sh`, executor systemd unit and tests. Backend owns persistent gate endpoints and admission transactions.
**Dependencies:** interface/schema agreement with backend gate tasks for coding; O1 bootstrap plus deployed gate for activation. Recording-resume relaxation is disabled until the backend recovery proof passes. **PR boundary:** executor with adversarial tests, then workflow wiring, then operator activation. **Rollback:** previous compatible image/config pair through this executor; keep gate closed on failed verification.

### O2.1 — Separate immutable release inputs from host provisioning

- [ ] **O2.1.1** Split routine replacement from `deploy_hetzner.sh` package installation, Compose-wide down/up, database initialization, nginx setup and Liquidsoap synchronization; require an immutable image digest and versioned config pair. Test routine replacement touches only the API service and leaves media/HLS/asset volumes and unrelated services unchanged.
- [ ] **O2.1.2** Create a root-owned host executor with a lifetime kernel lock, operation ID, durable host operation journal and systemd-cgroup fencing of all mutation-capable descendants; CI submits work and reads status rather than owning mutation lifetime. Verify two submissions cannot overlap and CI cancellation/lost SSH cannot release fencing while children can mutate; the API receives neither Docker socket access nor a second SQLite writer.
- [ ] **O2.1.3** Wire manual deployment to the executor with separate non-cancelling deployment concurrency and minimal command permissions; prohibit direct replacement in all remaining normal scripts/workflows. Test arbitrary tag/config/path inputs are rejected and build cancellation never cancels an accepted host operation.

### O2.2 — Implement the gate-to-replacement protocol

- [ ] **O2.2.1** Under the host lock, acquire persistent `draining` with operation ID and a reserved deploy+rollback+margin window whose total is initially at least 30 minutes; consume transactional backend readiness rather than public `active`. Verify active starts, rehearsals, capture, unsafe finalisation and conflicting schedule edits prevent replacement and cannot race acquisition.
- [ ] **O2.2.2** Immediately before each service mutation, revalidate operation ownership, schedule/window and current readiness; progress `ready → replacing → verifying` using compare-and-set. Verify a stale success response, delayed executor, window overrun or ownership change aborts mutation and leaves a visible closed gate, with no late automatic show start.
- [ ] **O2.2.3** After replacement verify API/schema compatibility, persistent gate ownership and audio for the active readiness profile; require recording reconciliation and fresh current-Liquidsoap epoch handshake once those capabilities are activated. Deliberately reopen only after applicable gates pass. Test missing previously activated capabilities and unhealthy output block normal deployment without requiring future components during bootstrap.

### O2.3 — Recover failed operations without releasing a stale executor

- [ ] **O2.3.1** Implement privileged recovery that first terminates/fences the previous executor and all mutation-capable children, verifies this under the host lock, then reconciles or replaces its operation ID. Reconcile operation-labeled target container/service IDs and settle any Docker mutation already accepted by the daemon before recovery proceeds; retain the host lock and refuse takeover while its outcome is unknown. Killing the client does not cancel an accepted daemon operation. Inject a paused old executor and a lost Docker reply after acceptance, then release both after attempted recovery; prove neither can replace the new service or reopen admission.
- [ ] **O2.3.2** Add compatible-image rollback using a pre-migration consistent snapshot and recorded schema compatibility; distinguish additive rollback from destructive restore. For API-down recovery, use the fenced host journal to recover the service, then let the restored API reconcile/write its gate; agree the read-only trusted journal/startup fail-closed contract with the backend owner and never edit SQLite from the executor. Inject image-start/migration/postcheck failures and prove recovery works without API access while admission remains closed and media processes keep running.
- [ ] **O2.3.3** Publish service-specific maintenance runbooks and a separately audited incident override with operator/reason/expected interruption; expiry alerts without automatic reopening. Rehearse API, media, nginx graceful reload and host reboot cases, explicitly recording chat/bot/admin interruption, possible MP3 disconnects and single-host outage limits.

## O3 — Qualify source selection, local fallback and output reporting

**Owner/files:** operations media owner; existing `docs/stream-rework/prod/moafunk.liq`, `liquidsoap.service`, `local-test-harness/`; proposed `backend/scripts/media/stage-fallback.sh` and media fixture/tests. Backend owns programme identities, callback receiver and prerecorded admission/staging logic.
**Dependencies:** O3.1.1 capability/supervisor-proof spike starts after P1/O1 inventory and supplies evidence to P2.2.1; subsequent integration uses the frozen P2 contract. Public activation needs approved assets, output-authority backend, deployed F1/F2 legacy-compatible player and a guarded media window. F3 may be built but continuous behavior stays disabled until server verification. **PR boundary:** harness/source transport; selector/descriptor integration; production media config. **Rollback:** staged last-good media config/assets; keep public MP3 URL and explain any source/connection interruption.

### O3.1 — Test the installed media runtime and source formats

- [ ] **O3.1.1** Extend the existing local harness using the production Liquidsoap 2.4.4 image and Icecast build; record runtime capabilities and exact verified operator signatures. Test configuration startup, decoded fixture output and a candidate fresh challenge/supervisor-instance proof before freezing P2.2.1 or proposing an image/version change; nearby-version documentation alone cannot pass this leaf.
- [ ] **O3.1.2** Test browser Opus packet-copy ingest and a lossless prerecorded input candidate such as Ogg/FLAC through the existing harbor, coordinating the backend ffmpeg arguments. Compare decode/timestamp/channel behavior, reconnects and CPU, proving prerecorded audio avoids an extra Opus/MP3 lossy hop and both outputs consume the same decoded selector.
- [ ] **O3.1.3** Exercise source isolation and encoder failure paths: rehearsal never reaches public output; a failed Icecast sink does not block HLS or the selector; optional callbacks cannot block encoders. Save bounded decode results for disconnect, reconnect, malformed input and unavailable destination cases.

### O3.2 — Publish approved local fallback and emergency assets

- [ ] **O3.2.1** Stage explicit approved asset revisions and descriptors to dedicated persistent local directories using temporary download, checksum/decodability validation and atomic activation; mount them read-only to playout. Verify missing/corrupt/partial downloads leave the last-good selection intact and R2/API loss cannot prevent playback of activated assets.
- [ ] **O3.2.2** Change the public chain to fallible qualified producer → approved playlist → emergency ident, with final safety silence only an alerted failure; keep `mksafe` after selection. Test producer absence, all fallback files invalid, ident failure and rehearsal activity; assert selected mode/title and actual decoded signal agree.
- [ ] **O3.2.3** Implement/tune the initial 3-second loss grace and 2-second valid-return threshold without discarding buffered encoded tails; coordinate prerecorded completion and producer generation changes. Verify show end moves into fallback without closing the station output and source flapping cannot repeatedly reset listeners.

### O3.3 — Make the final selector the output authority

- [ ] **O3.3.1** Add bounded authenticated selector events/snapshots with process epoch, sequence, delivery generation, mode, broadcast ID and effective UTC; preserve fallback descriptors locally. Test API down/slow/return conditions, fixed buffer/retry limits and no audio stall; callback connection state is never treated as selected programme evidence.
- [ ] **O3.3.2** Implement the media side of verified-current-service epoch activation and 5-second snapshot reconciliation with the backend owner; define how the trusted supervisor proves its current process. Test old/unknown epochs, reordered callbacks and superseded reconcilers cannot reactivate retired output after either process restarts; API marks 15-second-stale output unknown.
- [ ] **O3.3.3** Insert selected programme title/presenter into Liquidsoap metadata for ICY, including fallback and ident resets; retain one writer instead of competing Icecast admin updates. Verify a direct ICY-requesting client sees correct transitions and does not retain the previous live show after fallback.

## O4 — Publish stable HLS/MP3 routes with restart-safe media state

**Owner/files:** operations; existing media service/config and `backend/nginx.conf.example`; proposed `docs/stream-rework/prod/nginx-stream.conf`, HLS persistence adapter only if needed, dedicated cleaner and integration tests. Backend owns public JSON/artwork handlers.
**Dependencies:** O3 harness/selector for O4.1 encoder integration; O4.2 MP3/metadata/artwork/alias routes can be prepared independently after P2 and activated when their handlers and domain/TLS are ready; native-HLS default additionally requires real-device gates. **PR boundary:** HLS publisher/persistence, then public routes, then measured activation. **Rollback:** explicit media config rollback; permanent MP3 and old aliases remain available; frontend transport rollback is a separate rebuilt deployment.

### O4.1 — Produce and retain the HLS timeline

- [ ] **O4.1.1** Encode AAC-LC 128 kbps stereo/48 kHz beside MP3 256 kbps/44.1 kHz, initially MPEG-TS with target 4-second segments and 10-entry playlist (minimum 6 when ready). Validate actual codec, durations, clipping/resampling, measured master `BANDWIDTH`, `CODECS="mp4a.40.2"` and end-to-end delay; document the tested departure from Apple's SHOULD defaults.
- [ ] **O4.1.2** Provide one HLS writer with persistent publication state and atomic file replacement: stable `/hls/live.m3u8`, monotonic media sequence, correct discontinuity sequence, PROGRAM-DATE-TIME and unique `/hls/segments/<epoch>/...` names. Test graceful restart and process kill; implement an adapter if installed Liquidsoap cannot preserve these invariants, rather than expecting clients to reload a master after 404/ENDLIST.
- [ ] **O4.1.3** Add a scoped cleaner retaining removed segments at least 120 seconds and at least their duration plus the longest containing playlist; preserve recently referenced files across restarts. Test older-segment fetches, changed durations, concurrent publication and path traversal/symlink protection; cleanup cannot reach recording or programme directories, and full disk-loss recovery is recorded as nonseamless.

### O4.2 — Serve the public projection and protect internal routes

- [ ] **O4.2.1** Provision persistent media paths with one writer and read-only nginx access; add HTTPS routes for `/live.mp3`, `/live.m3u8`, `/hls/live.m3u8`, epoch segments, `/live.m3u`, `/live.pls`, `/now-playing.json` and approved immutable artwork. Verify MP3/HLS continue with API stopped and existing working URLs remain proxy aliases with no HLS redirect chain.
- [ ] **O4.2.2** Configure accurate content types, public credential-free CORS, exposed relevant ICY headers and cache contracts: playlists max-age 1, immutable uniquely named segments max-age 60, metadata max-age 5 with ETag; exact successful artwork revisions may cache for one year. Test errors/private responses never enter artwork cache, bounded nginx cache eviction preserves origin bytes, and unknown metadata is not fabricated during API loss.
- [ ] **O4.2.3** Deny callback/service-control/admin/metrics/private-object routes on every public virtual host; retain authenticated internal callback over host `127.0.0.1:8000` and only required container-to-harbor bridge access. Test requests from outside and the API container, preview access policy, negative path probes and firewall rules without exposing credentials or changing the source path accidentally.

### O4.3 — Exercise maintenance and stage activation

- [ ] **O4.3.1** Run nginx configuration validation and graceful reload while long-lived MP3 and HLS clients listen; check old-worker lifetime/timeouts and certificate renewal behavior. Record any interrupted connection and fix the local reload policy before claiming reload continuity.
- [ ] **O4.3.2** Run locked-real-iPhone Liquidsoap graceful/crash restart, source transitions and older-segment requests, then Icecast-only failure with HLS active. Attach device/OS/config hashes and decoded-network evidence; a desktop user-agent change cannot pass this gate and unsuccessful continuity leaves HLS unqualified.
- [ ] **O4.3.3** Activate stable aliases first, continuous MP3 after server/asset verification followed by the compatible player profile, and native HLS only after its full device gate; update O1 readiness profile each time. Rehearse both media-config rollback and frontend rebuild-to-MP3, proving no released direct URL is removed and failed HLS qualification does not block permanent MP3 service.

## O5 — Enforce resource, storage and clock operating limits

**Owner/files:** operations with backend pool/resource-policy owners; existing Compose, media systemd units and monitoring rules; proposed resource budget evidence and host limit/clock checks. Application semaphore, admission reserve checks and SQLite connection changes are backend-owned.
**Dependencies:** inventory for limit selection; recording/staging/HLS paths for full load exercise. **PR boundary:** operational limits/metrics distinct from backend behavior. **Rollback:** restore measured previous limits without deleting unverified recordings or lowering DB durability as a contention workaround.

### O5.1 — Protect storage and critical process resources

- [ ] **O5.1.1** Define per-class accounting and protected free-byte/inode reserve from the longest supported capture plus processing copies, staged programmes, HLS retention, logs and images. Test reserve calculations at measured recording bitrate and reject unsafe new capture through backend admission while never age-deleting unverified local recordings.
- [ ] **O5.1.2** Configure measured CPU/memory/I/O limits and log/image/temp cleanup, preserving current process supervision and media independence; bound backup transfers and optional media work. Stress optional work until its bound and verify capture/encoders retain headroom; separate directories alone must not be reported as resource isolation.
- [ ] **O5.1.3** Exercise low free space, inode exhaustion and OOM pressure in the harness; stop optional writes before the protected reserve, emit actionable alarms and keep cleanup scoped. Verify pinned prerecorded files, approved fallback, HLS publication state and unverified recording segments survive cleanup attempts.

### O5.2 — Validate SQLite and clock assumptions in the deployed runtime

- [ ] **O5.2.1** Query `sqlite_version()` and effective connection PRAGMAs through the actual deployed application build; verify the WAL-reset fix (3.51.3 or a documented applicable fixed/backported build) and local filesystem suitability. Do not infer library safety from a Rust crate version or the host `sqlite3` executable; record evidence before enabling the backend WAL target.
- [ ] **O5.2.2** Assign the backend integrator to implement WAL, synchronous FULL and 5-second busy timeout in the actual pool construction in `backend/src/main.rs` (`SqlitePoolOptions` at this baseline), after O5.2.1 passes, and verify every pooled connection; stress concurrent short writes, backups and checkpoints. Verify bounded WAL growth/busy errors and successful consistent backups while refusing a change that silently trades durability for lower contention.
- [ ] **O5.2.3** Monitor the actual time-sync daemon, offset, loss of sync and unexpected steps; verify UTC timestamps and monotonic durations with scheduler/backend owners. Inject clock steps and Europe/Berlin ambiguous/nonexistent times in isolated tests, asserting no duplicate starts or invalid HLS mappings; clock alarms must not deliberately stop healthy audio.

### O5.3 — Measure capacity and publish evidence-based limits

- [ ] **O5.3.1** Run bounded load at 200 mixed MP3/HLS listeners with live capture, both encoders and one permitted background job; collect connection/fd use, CPU/RAM, disk latency, packet loss, stalls and encoder health. Keep generators outside the origin and report actual failure thresholds rather than treating configured client counts as benchmark results.
- [ ] **O5.3.2** Compare measured traffic with payload estimates (50/50 mix: 38.4 Mbit/s and 12.44 TB/30 days), adding protocol, archive and backup traffic; record the confirmed account allowance. Verify per-transport listener-hours/egress metrics and 70%-of-allowance plus projected-month-end warnings using fixture billing periods.
- [ ] **O5.3.3** Publish measured operating limits, show/capture maximums, supported concurrent heavy jobs and resource escalation triggers. Verify release checks fail when required capacity evidence is missing and do not claim origin redundancy, a recovery-time promise or unlimited continuous listening capacity.

## O6 — Observe decoded audio and missed programmes from outside

**Owner/files:** operations; existing monitoring Prometheus/blackbox/Alertmanager files and `backend/scripts/deploy_monitoring.sh`; proposed `backend/scripts/monitoring/audio-probe` harness, external deployment description and alert runbook. Backend owns authenticated scheduled/output data projections.
**Dependencies:** current MP3 observation starts immediately; HLS checks after publication; external service choice/activation needs O1 named resource. **PR boundary:** checks and rule fixtures, then delivery/external activation. **Rollback:** retain existing monitoring, disclose any missing decoder coverage rather than replacing it with HTTP-200 claims.

### O6.1 — Implement bounded independent media checks

- [ ] **O6.1.1** Build bounded MP3 and HLS probes that fetch and decode actual media, detect stale playlists/missing segments/decode failures and measure sustained unexpected silence; choose initial 60-second cadence and three-failure warning. Test against valid tone/music, intentional silence grace, stalled HTTP-200 bodies and missing segments; cap time, bytes, concurrency and process resources.
- [ ] **O6.1.2** Run checks from an independent failure domain without requiring an origin API response to decide whether to probe; publish last-success/time/failure class. Disable origin and local monitoring together in an approved exercise and verify the external check still detects and reports outage.
- [ ] **O6.1.3** Add separate expected-show checks using a cached schedule with explicit freshness and observed selected mode/producer state; distinguish healthy fallback, missing show and unknown/stale evidence. Test unavailable API, intentionally empty schedule, maintenance-blocked start and fallback during an expected show without suppressing media checks.

### O6.2 — Route useful alerts and maintenance suppression

- [ ] **O6.2.1** Extend local rules for disk/inodes, memory/CPU, encoder/output failures, recording backlog/incomplete capture, metadata age, WAL/busy growth, clock sync and backup age/lag. Validate rule fixtures and tune thresholds/grace against transitions so live producer connectivity cannot mask silent output.
- [ ] **O6.2.2** Verify a human receives audio-failure and host-down alerts through an external notification path independent of the origin Telegram bot; record recipient, delivery time and acknowledgment. Inject broken notification credentials/routes in staging and prove notification failure is observable without exposing secret values.
- [ ] **O6.2.3** Add deduplication and operation-ID-linked maintenance suppression with explicit expiry; keep outage visibility and missed-show records distinct from paging policy. Test abandoned maintenance expires, a stale executor cannot extend suppression and recording/disk safety alarms are not silently suppressed forever.

### O6.3 — Make monitoring part of release and incident decisions

- [ ] **O6.3.1** Persist sanitized probe/alert results for API replacement/crash, R2 outage, source loss, encoder failure and origin loss. Verify each fault has an observable condition and one runbook action, with the same tested alert pipeline used in production readiness.
- [ ] **O6.3.2** Define the explicit fallback for an unavailable external decoder service: recorded manual media checks with an open coverage gap and owner/date, never full external-audio protection claims. Keep coding unblocked but require working external decode and delivery before declaring the corresponding release protection complete.
- [ ] **O6.3.3** Publish escalation rules: one unrecovered origin failure during an announced show prompts independent-origin review; repeated gate obstruction prompts ingest-boundary review. Verify incident records distinguish detected outage, recovery, listener impact and remaining one-host risk rather than calling client HLS→MP3 fallback redundancy.

## O7 — Retain both media buckets and prove isolated restoration

**Owner/files:** operations; existing `.github/workflows/backup.yml`, `backend/scripts/backup/{backup-all,backup-db,backup-r2,restore-db,restore-r2,setup_rclone}.sh`; proposed backup inventory/verification fixtures and restore runbook. Backend owns any new recording-success backup notification.
**Dependencies:** existing-media protection starts before HLS; immutable artwork inventory and archive notifications integrate when available; destination/account/retention activation needs O1 resource decision. **PR boundary:** retained backup implementation; safe restore tooling; recovery exercise. **Rollback:** preserve all verified prior copies; never reinstate a mirror that deletes the only retained version.

### O7.1 — Create consistent retained inventories

- [ ] **O7.1.1** Replace artists-only coverage with explicit artists and finalized-show bucket inventories, immutable artwork versions, fallback/ident originals and required nonsecret app/media configuration references. Test both bucket namespaces and DB object references; document local unuploaded recording/manifest host-loss exposure and exclude transient HLS segments from archival promises.
- [ ] **O7.1.2** Keep SQLite's consistent backup mechanism, add integrity/checksum validation and retain 30 daily snapshots with manifest/version references; move the existing weekly schedule to the daily metadata target. Test concurrent writes and a failed snapshot/upload so neither a corrupt backup nor a misleading success timestamp replaces the last verified snapshot.
- [ ] **O7.1.3** Implement incremental object copying plus at least 30-day retention of replaced/deleted versions instead of deletion-propagating `rclone sync` as the sole copy; capture per-run object version/checksum inventory. Delete/replace source fixture objects both before and between backup runs and prove the prior bytes and matching DB references remain recoverable without a full copy per daily snapshot.
- [ ] **O7.1.4** Assign the backend storage integrator to inventory all media overwrite/delete paths and implement any immutable-version, tombstone or pre-delete-retention hooks needed by the backup policy. Do not assume provider version history. Verify an object created, replaced or deleted between copy runs retains recoverable prior bytes and matching inventory; failed retention prevents destructive purge and alerts.

### O7.2 — Bound backup lag and protect retained copies

- [ ] **O7.2.1** Add incremental copying of newly remote-verified archives with periodic reconciliation so a missed event cannot omit a recording; integrate the backend success boundary rather than capture start. Test duplicate/lost events, multipart objects and provider outage, measuring lag to a verified retained copy without treating multipart ETag as a universal checksum.
- [ ] **O7.2.2** Separate routine backup identity from destructive retention/restore privileges and record independent-account/provider status; configure bounded transfer concurrency/rates outside recording reserves. Verify ordinary credentials cannot silently delete the only retained good copy and document account-loss exposure if resources only allow same-account storage.
- [ ] **O7.2.3** Add daily snapshot age, per-bucket coverage, archive backup lag, verification failures and destination-capacity alerts. Test alerts for a successful DB backup with a failed show-bucket copy so partial success is never summarized as full backup protection.

### O7.3 — Restore safely and measure recovery

- [ ] **O7.3.1** Refactor restore tools to require explicit isolated destination DB path, remote and bucket mapping; remove hardcoded production targets from the default drill path. Test production-target refusal, path traversal, incomplete inventory and dry-run output; production restore remains a distinct privileged procedure with services stopped and consistent pre-restore protection.
- [ ] **O7.3.2** Restore a selected DB snapshot and its matching retained object versions into an isolated environment with bot, publishing, scheduler and live playout disabled and outbound side effects blocked. Verify DB integrity, referenced object checksums, restored recording decode, exact artwork revisions and approved fallback through an isolated media harness.
- [ ] **O7.3.3** Record elapsed restoration time, observed recovery point, missing objects and manual steps after backup changes and monthly initially; separately exercise full-host configuration recovery. Report measurements without inventing an RTO, and prove the drill neither sends public messages nor replaces live data/audio.
