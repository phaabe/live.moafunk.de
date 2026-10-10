# Shared production inventory

One inventory for P1, O1.1 and O1.2. Do not create separate host inventories.

Work items: https://github.com/phaabe/live.moafunk.de/issues/338,
https://github.com/phaabe/live.moafunk.de/issues/380,
https://github.com/phaabe/live.moafunk.de/issues/381.

Source review: 2026-10-10, `06fe21c21256dfcf6bd29e2f0694852ceb1a0925`.
Executor: Codex. Read-only host observations were collected on 2026-10-10 with
Anton's authorization. They are point-in-time evidence, not a maintenance
exclusion window or deployment approval. Keep unknowns open.
Use the [maintenance runbook](../runbooks/bootstrap-maintenance.md).

## Host and operator gates

| Item | Repository evidence | Host evidence / decision |
| --- | --- | --- |
| Host, address, owner, deployment directory | Hetzner script defaults to `/opt/unheard-backend` | DNS and authenticated SSH confirm `admin.live.moafunk.de` at `178.104.160.103`; mounted data is under `/opt/unheard-backend`. Window owner remains OPEN. |
| API service and persistence | `backend/docker-compose.prod.yml`: `unheard-api`, loopback `127.0.0.1:8000`, `./data:/app/data` | Verified container `unheard-api`, port `127.0.0.1:8000`, network `unheard-backend_default`, restart `unless-stopped`, data bind `/opt/unheard-backend/data:/app/data` and read-only runtime env bind. All deploy writers remain OPEN. |
| Running and rollback images | Workflow publishes mutable `latest` and SHA tags | Running digest and revision verified below; image also downloaded locally for rehearsal. Retained host rollback image and restore qualification remain OPEN. |
| Ingress | Hetzner script generates a backend catch-all and MP3/status locations | Live HTTP/TLS server and config hash recorded below. Alternate ingress coverage, admission installation and old-worker drain remain OPEN. |
| Direct producer bypasses | Harbor and Icecast services use host networking | OPEN: firewall, direct sources, local/container API clients and exclusion method |
| Scheduler | Poll every 30s; confirmed unstarted prerecorded shows can catch up | OPEN: complete eligible set, UTC intervals, in-flight selections, freeze through rollback and restart |
| Capture/finalization | Status can be idle during upload; grace task, merge, re-export and startup recovery exist | OPEN: per-show/version task ledger, artifact verification, residual files, detached jobs |
| Shared deploy lock | Runbook names `/run/lock/moafunk-api-deploy.lock` | OPEN: every deploy actor honors it; legacy runs and remote children reconciled |
| Window | Show-free interval includes drain, replacement and rollback | OPEN: operator, UTC start/deadline, announcements, bounded/outage choice |
| Uploads and notifications | Admission closure blocks ongoing chunks; Telegram preview checks have bounded daily windows | OPEN: staged upload/conversion ledger, uploader decisions, affected previews and recovery |
| Monitoring | Backend scrape outage raises `BackendMetricsDown` after ten minutes | OPEN: scoped silence ID/owner/deadline, retained media alerts and scrape recovery/removal |
| Snapshot | SQLite plus persisted recordings/checkpoints/config required | OPEN: destination/capacity, integrity/hash results, permissions, restore drill and retention |
| Media services | `docs/stream-rework/prod/` describes Liquidsoap and Icecast | `nginx`, `liquidsoap`, `icecast` units active; containers use `savonet/liquidsoap:v2.4.4` and `moafunk/icecast-kh:kh22`. Media digests/config hashes, output qualification and listener impact remain OPEN. |
| Readiness profile | O1.2.3 defines staged output requirements | OPEN: approved active profile and playback evidence; do not assume fallback or HLS |
| API-only replacement | Existing deploy script provisions host and replaces Compose stack | OPEN: reviewed O1.2.5 path, exact digest support, bootstrap/manual confirmation |
| Production approval | Current work prepares O1.2.2 | OPEN: concrete window, artifact and host procedure approval before execution |

## Read-only host observation: 2026-10-10

At `2026-10-10T21:46:03Z`, the running API matched the successful
[October 9 deployment](https://github.com/phaabe/live.moafunk.de/actions/runs/37998003517):

- Registry digest (`RepoDigests`, also pulled locally):
  `sha256:77c304243b6b7dd048574f6abfea40d9fc053b2a6f9b9aa771bfdedcb4e080fe`.
- Running container `.Image` and host image `.Id`, inspected separately:
  `sha256:77c304243b6b7dd048574f6abfea40d9fc053b2a6f9b9aa771bfdedcb4e080fe`.
- Image: `ghcr.io/phaabe/live.moafunk.de-backend`; configured reference `latest`.
  OCI source revision: `7093fe53488c21030c725f11370e96186a027836`.
- Container start: `2026-10-09T22:25:23.876176715Z`. Health endpoint healthy.
  Stream and recording endpoints both returned `active=false`; capture show ID
  was null. Process-name-only listing showed the backend and no ffmpeg child.
- The scheduler's full SQL candidate predicate returned zero rows. Recording
  version counts: 5 failed, 5 finalized, 12 raw. Top-level recording artifacts:
  zero `*.segs` directories and zero `recording_*` files. These observations do
  not prove absence of detached upload/finalization work or freeze new work.
- nginx loaded one backend site at
  `/etc/nginx/sites-enabled/admin.live.moafunk.de`, with HTTP 80, TLS 443,
  exact `/metrics`, `/live.mp3`, `/test.mp3`, `/status-json.xsl` locations and
  the API catch-all. Effective `nginx -T` SHA-256:
  `7320f58385f515a003f899849196a806baece5675309518e4aa913e90ba5554b`.
- Compose file SHA-256:
  `60d05653d8570927a43d8264bfcca8fad05d8c0ff310976dec2bfb25a66205fe`.
- Host listener and packet-filter observations were recorded privately.
  Public reachability, cloud firewall coverage and direct-producer exclusion
  remain OPEN; do not publish host exposure details in this inventory.

No configuration, service, database, media or admission state was changed.
Secrets and full process arguments were not collected. The initially attempted
default SSH identity failed; Anton supplied the working identity path.

The deployed source differs from the integration baseline: its scheduler calls
`start_prerecorded_show_stream`, without the later occurrence admission path.
Its startup cleanup can race recovery and delete recording artifacts older
than a day. Rehearsal and rollback must use the deployed behavior, with a
separate retained data copy before every restart; integration fixes are not
evidence of production behavior.

## Recorded local evidence

| Check | Result |
| --- | --- |
| O1.2.4 implementation | https://github.com/phaabe/live.moafunk.de/pull/417 merged into `dev/312-interim` |
| Completed integration push | https://github.com/phaabe/live.moafunk.de/actions/runs/38084785375: backend-ci/frontend-ci successful; those jobs have no deployment steps |
| O1.2.4 rollout/negative PR checks | OPEN: target-branch rollout, live failing-test/format evidence and administrator protection verification; this run alone does not prove every workflow is safe |
| O1.2.2 admission/lock rehearsal | 2026-10-10: exact Docker command in the runbook, exit 0; nginx 1.28.0, pinned digest `sha256:30f1c0d78e0ad60901648be663a710bdadf19e4c10ac6782c235200619158284`; 44 route assertions, a pre-reload response continuing while new requests receive 503, complete response drain, concurrent lock refusal and successful acquisition after release |
| Interactive lock-owner regression | 2026-10-10: `python3 docs/stream-rework/local-test-harness/maintenance/rehearse-lock.py`, exit 0; pinned backend image supplies Bash/flock without starting the API. Six groups pass: old strict-shell failure reproduced; revised owner survives failed diagnostics; contender refuses; lost ownership rejected despite contention; replaced pathname rejected; explicit release detected. No mounts/network, owned container removed. Host tmux/reconnect verification remains OPEN. |
| SQLite snapshot/restore rehearsal | 2026-10-10: `bash docs/stream-rework/local-test-harness/maintenance/rehearse-snapshot.sh`, exit 0; SQLite 3.51.0; backup/restore integrity, original data/schema and failed-candidate retention verified on scratch data |
| Legacy backend drain, scheduler, replacement and rollback | 2026-10-10: both paths pass on the exact deployed image with synthetic storage; detailed record below. Candidate uses the same binary with a simulated schema change; future release compatibility remains OPEN. |
| Production/device qualification | Not run |

The first lock rehearsal exited 1 because the nginx user's login shell is
disabled. The fixture now invokes `/bin/sh` explicitly; the full rerun passed.
The separate PTY regression uses Bash 5.2.15 and util-linux flock 2.38.1.
Its SHA-256 is `27688bb96c7b1aea4be709a6ce6df827b1ed6549f725beebbd8b4f7721988849`.
Both shell syntax checks passed. The backend rehearsal uses the exact deployed
image with synthetic data. No production database, audio or credentials were
used locally; production access was read-only.

### Exact-image backend result

Command: `python3 docs/stream-rework/local-test-harness/maintenance/backend/rehearse.py`.
[Fixture instructions and limitations](../../stream-rework/local-test-harness/maintenance/backend/README.md).
Frozen run: `2026-10-10T21:55:33Z` to `21:59:35Z`, exit 0. Images are
pinned in the fixture; the backend is the observed digest above, emulated as
`linux/amd64` locally. Both containers share a network-disabled namespace with
only loopback services. No ports or production credentials are exposed.

| UTC | Observed result |
| --- | --- |
| 21:55:46 | Synthetic producer disconnected; recording stayed active during the real 30-second grace period. |
| 21:56:16 | Raw upload held at local storage; recording status idle and no version row yet. Manual replacement withheld; container unchanged. |
| 21:56:17 | Finalization terminal log, raw object, empty markers and archive verified; no residual recording artifact. Raw bytes match the pending artifact exactly: 1,968,548 bytes. MP3: 2,603,929 bytes, probed duration 65.071 seconds. |
| 21:56:50 | Empty scheduler set; API-only container recreation and rollback passed. Restored files match the snapshot manifest, original schema/data restored, failed candidate retained, integrity `ok`, stream/capture idle. |
| 21:57:21 | API confirmed stopped before the fixture candidate due at 21:59:00. |
| 21:59:02–03 | API stayed stopped across the boundary; restart withheld for catch-up. Separate diagnostic restart logged the real scheduler attempt; missing fixture media prevented playback. |
| 21:59:19 | Outage rollback passed: original synthetic schedule, matching file manifest, integrity `ok`, stream/capture idle. This does not authorize deleting a real scheduled show. |
| 21:59:35 | Legacy startup deleted a two-day-old synthetic recording artifact; the pre-restart backup retained it. Source files stayed unchanged throughout the run. |

Raw SHA-256: `82a7a2a802693a9763de8a79890f610540290d9316291a5afcece37df5da87d1`.
Archive SHA-256: `7f3d9b8d7ff272932e7bba65db23dd176c75838e4af31f458db9ee5fa617a626`.
Runner SHA-256: `1ad17ca1e014b1ef697db5a4fc79340fc193463a897f7e4512717c4a25c96151`.
Client SHA-256: `cd543e4216fec138856a45b9a48f361ed8b640564c468bff729fe3fa72c182b2`.
Storage SHA-256: `acdbb8df88f4e03da1c15bf90df94b5e2c7da784b7dfa3a02cec5c149f8e6ed4`.

Ruff lint/format (Python 3.11 target), Python compilation, shell syntax and
whitespace checks passed. Independent Codex source review found no remaining
blocker. All fixture containers were removed; private synthetic logs, snapshots
and failed-candidate data remain in the local evidence directory printed by the
runner. Early fixture attempts failed on an assertion field and TLS trust;
the final run uses a separate disposable CA and server certificate and passes.

This rehearses manual decisions, not an automated precheck/deploy gate.
Same-binary rollback, fake single-part storage and a producer sink do not
qualify future migrations, real storage, SoundCloud or listener playback.
All host/operator activation conditions above remain open until verified for
the concrete approved window.

## Per-window record

Append an operation record here or link its redacted issue evidence. Required:
operator and approval; source/config hashes; host identity reference; UTC start
and deadline; new/old digests; workflow run reconciliation; lock owner and
second-attempt refusal; ingress checks; scheduler eligible set and resolved
times; task ledger and artifact results; snapshot identity, integrity and
restore result; replacement/rollback result; active-output validation; reopen
time and owner; upload restart/abandonment decisions; notification recovery;
monitoring silence ID, expiry and removal. Never include secret values or
private programme media.

Any failed or unknown gate stays open. A successful PR, `/health`, idle status
or local nginx fixture cannot mark production activation complete.
