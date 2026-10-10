# Backend build-only hotfix

`backend.yml` builds and publishes images on matching `main` pushes and manual
runs with `build: true`. It no longer deploys the API or restarts Liquidsoap.
The workflow name stays unchanged because existing guards refer to it.
Native amd64 builds, the BuildKit mirror and Dockerfile base mirrors are retained.

Images retain `latest` and short SHA tags and add a full SHA tag. The build
outputs `digest`, `image` and `source-sha`; its summary records the exact image
reference and source commit. Use the digest for any approved deployment.

The removed job also generated and copied the production `.env`, synchronized
Compose and host scripts, provisioned nginx, initialized the database and
updated streaming configuration. Those tasks no longer run on push or manual
backend builds. Existing host files are preserved. The removed behavior is in
[the previous main workflow](https://github.com/phaabe/live.moafunk.de/blob/1bae514c6cfd87726cdb47a4281457ea75d1bbac/.github/workflows/backend.yml).
Do not use the old deployment job as a rollback procedure. O1.2.5 in
https://github.com/phaabe/live.moafunk.de/issues/381 supplies the future manual
API deployment path and its configuration contract.

`deploy_monitoring: true` still explicitly deploys monitoring and needs operator
approval. It defaults to false and runs independently of `build`. Build
cancellation uses a new ref-scoped job group. Monitoring uses a separate fixed
group with cancellation disabled; GitHub may still replace older pending jobs.
Builds cannot cancel running monitoring or legacy deployment runs.

**Prepare only; merge needs Anton's approval.** After an approved merge, retain
the first main build's run URL, source SHA and digest as rollout evidence.
Queued, running and rerun workflows from old revisions can still deploy. Before
any API replacement, list legacy runs across all refs, drain or cancel them,
and reconcile their remote processes. Cancelling Actions does not prove that
an SSH child exited. Until this is verified, deployment exclusion is unproven.

`backend-build-tests.yml` runs on every PR to and push on `main`. It has only
`contents: read`, no production secrets and no image build or publish step.
It checks job selection, permissions, deployment removal, independent
concurrency, preserved mirrors and digest reporting. Run locally:

```sh
npm ci --ignore-scripts --prefix frontend
node --test .github/workflows/tests/backend-build.test.cjs
```

The parser comes from the committed frontend lockfile. The tests exercise the
summary script with a synthetic digest and reject a missing digest; they do
not contact production or prove the first main build has run.
