# Exact legacy backend rehearsal

Run from the repository root with Python 3.11+, OpenSSL and local Docker Desktop:

```sh
docker --context desktop-linux pull --platform linux/amd64 ghcr.io/phaabe/live.moafunk.de-backend@sha256:77c304243b6b7dd048574f6abfea40d9fc053b2a6f9b9aa771bfdedcb4e080fe
docker --context desktop-linux pull python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c
python3 docs/stream-rework/local-test-harness/maintenance/backend/rehearse.py
```

The runner creates uniquely named containers and private temporary data. Both
containers share one `--network none` namespace; every fixture service binds
loopback. No ports are published. Only synthetic files and this fixture directory
are mounted. The backend uses `APP_ENV=production`, fake credentials, a fixture
session inserted into synthetic SQLite, and a disposable CA for local HTTPS.
Telegram and SoundCloud are disabled. No real credentials, database or audio are
read. Cleanup removes only the containers created by this invocation; evidence,
backups and failed candidate data remain in the printed temporary directory.

The assertions cover:

- A real WebSocket producer and 65 seconds of synthetic tone pass through the
  deployed binary's recorder. Disconnect enters its 30-second grace period.
- Local storage holds the multipart response. Recording status becomes idle
  while the artifact is pending and no version row exists. The runner records
  the manual decision to withhold replacement and keeps the container unchanged.
- Releasing storage completes raw upload, markers, the recording row and MP3
  archive. Raw bytes match the pending artifact exactly. The archive has a stored
  hash and a probed duration of at least 60 seconds. Terminal finalization and
  absence of residual capture artifacts are required before proceeding.
- With no scheduler candidates, the API is stopped, its data is copied and
  SQLite backed up. The API container is removed and recreated at the pinned
  digest with a simulated candidate schema/data change. Rollback restores every
  persisted file by hash, excludes that schema change and recreates the old image.
- A future confirmed fixture show becomes due while the API is stopped. Its
  started field stays unset. The manual restart check finds the catch-up candidate.
  A separate diagnostic restart proves the real startup scheduler attempts it;
  missing media prevents successful playback. The procedure would keep production
  stopped at this point. Restoring the earlier synthetic schedule demonstrates
  rollback only; it does not authorize deleting a real scheduled show.
- Starting the exact legacy image deletes a two-day-old synthetic recording
  artifact. Its pre-restart backup remains intact. Never boot this image against
  the only copy of unresolved artifacts.

This is manual procedure evidence, not an automated deployment refusal gate.
The candidate uses the same binary with a simulated schema change, so this does
not prove a future release's migration compatibility. Storage is a small local S3
boundary fake: it supports one small multipart part and rejects additional parts;
it does not validate signatures or CRC32C. The sink accepts producer bytes but
does not emulate Liquidsoap, Icecast listener playback, fallback or HLS. Admission
and old-worker behavior are tested by the separate nginx fixture. Host activation,
all production ingress paths and external writers still need their own evidence.

Checks:

```sh
ruff check --target-version py311 docs/stream-rework/local-test-harness/maintenance/backend
ruff format --check docs/stream-rework/local-test-harness/maintenance/backend
python3 -m py_compile docs/stream-rework/local-test-harness/maintenance/backend/{client,storage,rehearse}.py
```
