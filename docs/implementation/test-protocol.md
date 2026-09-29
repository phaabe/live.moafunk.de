# Isolated media and fault test protocol

Work item: https://github.com/phaabe/live.moafunk.de/issues/339
Leaf: P1.2.3. Status: protocol recorded; executable isolation proof pending.

This is an acceptance protocol, not a qualified harness launcher. Do not run
the legacy Compose examples as proof of isolation under this protocol.

## Current harness gaps

Inspected at `e319efe17e376e5a0cdaa26b8ebefe10d4114418`:

| File under `docs/stream-rework/local-test-harness/` | Gap |
| --- | --- |
| `docker-compose.yml` | Fixed project name and host ports 1935, 8000 and 8010; port bindings are not loopback-only. |
| `docker-compose.no-nms.yml` | Fixed project name and host port 8010; no per-run resource namespace. |
| `push-test-tone.sh` | Fixed localhost port 1935; may feed an unrelated local listener if the intended harness did not start. |
| `README.md` launch commands | Plain `docker compose` inherits Docker host/context and Compose environment defaults; no local-daemon refusal gate. |

Synthetic input and local service names alone do not prove isolation. There is
no recorded test that rejects a remote Docker context, missing run identity or
production endpoint before starting processes. The files above belong to the
ops lane; this documentation claim does not authorize edits to them. Record a
fixture/launcher file claim and the PR lane agreement before implementation.

## Preconditions for an executable run

| Boundary | Required behavior |
| --- | --- |
| Daemon | Require an explicit local Unix-socket endpoint. Reject TCP/SSH endpoints and unknown contexts before any daemon call; never fall back to the current Docker context. |
| Environment | Construct an allowlisted child environment. Do not inherit `DOCKER_HOST`, `DOCKER_CONTEXT`, `DOCKER_CONFIG`, `COMPOSE_FILE`, `COMPOSE_PROJECT_NAME`, `COMPOSE_PROFILES`, application endpoints, proxy settings or cloud/bot credentials. Use an explicit empty environment file and explicit Compose file set. |
| Names and ports | Require a fresh run ID and unique Compose project. Bind only loopback; allocate per-run host ports and discover the actual bindings. Abort on collisions. The producer consumes the checked run endpoint, never a fixed port. |
| Storage | Use new temporary directories/volumes per run. Mount only synthetic media and reviewed config; no host programme, recording, backup or credential directories. Record resources owned by the run. |
| Network | Keep service traffic on an isolated internal network. Reject external endpoints and host networking. Do not mount the Docker socket into test containers. Resolve image/build dependencies before the isolated execution phase. |
| Side effects | No publisher, Telegram bot, scheduler or production backend. Test storage, callbacks and outputs point only to the run's local fakes and media services. Missing configuration must refuse startup, not select application defaults. |
| Artifacts | Record source commit, image IDs/digests, rendered config hash, fixture hash and actual FFmpeg/FFprobe/Liquidsoap/Icecast versions. A floating tag is not an artifact identity. |
| Lifetime | Bound startup, each fault and total runtime. On error or signal, stop owned children and remove only that run's containers, networks and volumes. Never use global prune or wildcard process kills. |

For physical-device tests, a reviewed LAN binding is a separate explicit mode.
Record the interface, port and access scope; loopback-only tests do not prove
phone playback. Production tests need Anton's explicit approval.

## Refusal tests before media execution

Use a fake Docker/process boundary first. Each rejected case must exit nonzero
with no daemon call, child launch or publication:

- Omit run ID, daemon socket, output endpoint or temporary storage root.
- Supply a remote Docker context/host, inherited Compose file/profile, proxy or
  production-like application endpoint; verify rejection or complete removal
  from the constructed child environment before launch.
- Supply a non-loopback host binding, host networking, external volume or
  endpoint that resolves outside the run's allowlist.
- Reuse an existing run ID or occupy a requested port; verify no connection to
  the process that already owns it.

Separately, inject a startup interruption and a dependency failure after launch;
verify teardown preserves another concurrent run and unrelated containers.

Then run two isolated instances concurrently and record distinct projects,
ports and storage. Demonstrate cleanup of one while the other remains healthy.
An environment-cleaning convention without these tests is not a passing gate.

## Media and fault evidence

Use generated tone/silence only. Capture a finite sample and independently
decode it; HTTP success alone cannot distinguish silence from useful audio.

| Scenario | Record and check |
| --- | --- |
| Normal path | Input/output format, sample rate, channel count, bitrate, non-silent decode and source identity. |
| Producer stop or crash | Bounded detection, expected silence/fallback behavior, cleanup and recovery; do not claim seamless continuity unless measured. |
| Liquidsoap or Icecast loss | Exit/restart sequence, listener result and independent output decode after recovery. |
| Slow or partial object read/write | Fake response sequence, retry count, final state and absence of publication of incomplete media. |
| Metadata/API outage | Healthy audio continues where required; stale metadata is reported separately. |
| Clock jump/DST or stale callback | Controlled timestamps/order, expected admission/identity result and no use of real-time sleeps as the assertion. |

Run only scenarios supported by the tested implementation. Later O3/O4/R1
features remain pending until their own claims and prerequisites are ready.
This document neither enables HLS nor declares fallback or recovery implemented.

## Teardown and report

Register teardown before the first child starts. Stop the producer, stop/remove
the exact project and its temporary volumes, verify no owned process/port is
left, then remove its temporary directories. Keep redacted logs and evidence
outside the deleted directory. Report teardown failures as failed runs.

Each report names: leaf, executor, date, source commit, artifact/configuration
identity, tool versions, fixture provenance/hash, daemon and resource scope,
exact command, expected result, observations, pass/fail counts, time limits,
refusal-test results and teardown result. Link it from the implementing issue.
Device observations use [the device matrix](device-matrix.md); host observations
stay in the shared P1/O1.1 inventory.
