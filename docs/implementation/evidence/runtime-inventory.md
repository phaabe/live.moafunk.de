# Shared production inventory

One inventory for P1, O1.1 and O1.2. Do not create separate host inventories.

Work items: https://github.com/phaabe/live.moafunk.de/issues/338,
https://github.com/phaabe/live.moafunk.de/issues/380,
https://github.com/phaabe/live.moafunk.de/issues/381.

Source review: 2026-10-10, `06fe21c21256dfcf6bd29e2f0694852ceb1a0925`.
Executor: Codex. No production host was contacted. Repository defaults below
are observations about source, **not verified host values**. Keep unknowns open.
Use the [maintenance runbook](../runbooks/bootstrap-maintenance.md).

## Host and operator gates

| Item | Repository evidence | Host evidence / decision |
| --- | --- | --- |
| Host, address, owner, deployment directory | Hetzner script defaults to `/opt/unheard-backend` | OPEN: operator confirms identity and paths privately |
| API service and persistence | `backend/docker-compose.prod.yml`: `unheard-api`, loopback `127.0.0.1:8000`, `./data:/app/data` | OPEN: actual bindings, networks, mounts, container/image IDs and all deploy writers |
| Running and rollback images | Workflow publishes mutable `latest` and SHA tags | OPEN: immutable digests, deployed source revision, local retained rollback image |
| Ingress | Hetzner script generates a backend catch-all and MP3/status locations | OPEN: current config hash, every TLS/HTTP vhost, alias, alternate ingress, rewrites and old-worker drain |
| Direct producer bypasses | Harbor and Icecast services use host networking | OPEN: firewall, direct sources, local/container API clients and exclusion method |
| Scheduler | Poll every 30s; confirmed unstarted prerecorded shows can catch up | OPEN: complete eligible set, UTC intervals, in-flight selections, freeze through rollback and restart |
| Capture/finalization | Status can be idle during upload; grace task, merge, re-export and startup recovery exist | OPEN: per-show/version task ledger, artifact verification, residual files, detached jobs |
| Shared deploy lock | Runbook names `/run/lock/moafunk-api-deploy.lock` | OPEN: every deploy actor honors it; legacy runs and remote children reconciled |
| Window | Show-free interval includes drain, replacement and rollback | OPEN: operator, UTC start/deadline, announcements, bounded/outage choice |
| Snapshot | SQLite plus persisted recordings/checkpoints/config required | OPEN: destination/capacity, integrity/hash results, permissions, restore drill and retention |
| Media services | `docs/stream-rework/prod/` describes Liquidsoap and Icecast | OPEN: actual units/images/config hashes, active mounts and listener impact |
| Readiness profile | O1.2.3 defines staged output requirements | OPEN: approved active profile and playback evidence; do not assume fallback or HLS |
| API-only replacement | Existing deploy script provisions host and replaces Compose stack | OPEN: reviewed O1.2.5 path, exact digest support, bootstrap/manual confirmation |
| Production approval | Current work prepares O1.2.2 | OPEN: concrete window, artifact and host procedure approval before execution |

## Recorded local evidence

| Check | Result |
| --- | --- |
| O1.2.4 implementation | https://github.com/phaabe/live.moafunk.de/pull/417 merged into `dev/312-interim` |
| Completed integration push | https://github.com/phaabe/live.moafunk.de/actions/runs/38084785375: backend-ci/frontend-ci successful; those jobs have no deployment steps |
| O1.2.4 rollout/negative PR checks | OPEN: target-branch rollout, live failing-test/format evidence and administrator protection verification; this run alone does not prove every workflow is safe |
| O1.2.2 admission/lock rehearsal | 2026-10-10: exact Docker command in the runbook, exit 0; nginx 1.28.0, pinned digest `sha256:30f1c0d78e0ad60901648be663a710bdadf19e4c10ac6782c235200619158284`; 44 route assertions, concurrent lock refusal and successful acquisition after release |
| SQLite snapshot/restore rehearsal | 2026-10-10: `bash docs/stream-rework/local-test-harness/maintenance/rehearse-snapshot.sh`, exit 0; SQLite 3.51.0; backup/restore integrity, original data/schema and failed-candidate retention verified on scratch data |
| Full backend drain, scheduler, install and rollback | OPEN: both runbook paths on the exact deployed image with fixture storage |
| Production/device qualification | Not run |

The first lock rehearsal exited 1 because the nginx user's login shell is
disabled. The fixture now invokes `/bin/sh` explicitly; the full rerun passed.
Both shell syntax checks passed. These results apply to the fixture files in
this revision; no production snapshot or backend image was used.

## Per-window record

Append an operation record here or link its redacted issue evidence. Required:
operator and approval; source/config hashes; host identity reference; UTC start
and deadline; new/old digests; workflow run reconciliation; lock owner and
second-attempt refusal; ingress checks; scheduler eligible set and resolved
times; task ledger and artifact results; snapshot identity, integrity and
restore result; replacement/rollback result; active-output validation; reopen
time and owner. Never include secret values or private programme media.

Any failed or unknown gate stays open. A successful PR, `/health`, idle status
or local nginx fixture cannot mark production activation complete.
