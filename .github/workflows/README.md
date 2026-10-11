# Integration checks

`epic-ci.yml` runs on every PR to and push on `main`, `dev/312-interim` and
`dev/streaming-architecture`, including documentation-only changes. It has no
path filters or conditional jobs. Both stable check names are always created:

| Check | Commands |
| --- | --- |
| `backend-ci` | `cargo fmt --check`, `cargo test --locked --all-targets`, `cargo clippy --locked --all-targets` |
| `frontend-ci` | `npm run lint`, `npm run typecheck`, `npm test -- --run`, workflow regression tests |

Rust matches the backend Docker build (1.98.0). Node.js matches the existing
frontend workflow (20). Dependency installs use the committed lockfiles.
Lint, typecheck and test failures remain failures; later checks still run
after an earlier check fails, unless the run was cancelled or setup failed.

The workflow only has `contents: read`, does not request secrets, and has no
deploy, image-push or artifact-publish steps. Push runs use the branch ref in
their concurrency group so different branches do not cancel each other.

`frontend.yml` also builds frontend changes on all three branches. PRs and
integration pushes skip Pages setup, SoundCloud secrets and API calls, and
artifact publication. They build from checked-in tracks. Pages deployment
still requires a push or manual run on `main`; only the deploy job can write
Pages or request an OIDC token. The build job has read-only Pages permission
for main's setup step. Backend image builds are described below.

After the workflow has run successfully and reached both integration branches,
add `backend-ci` and `frontend-ci` to the epic guard policy's `required_checks`
and to the applicable branch protection. Select **GitHub Actions** as the
expected source. Use the job names above, not the workflow title. Keep these
names stable when changing the workflow.

GitHub explains why required workflows must not be skipped by path filters in
[Troubleshooting required status checks](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/collaborating-on-repositories-with-code-quality-features/troubleshooting-required-status-checks).

## O1.2.4 verification

Work item: https://github.com/phaabe/live.moafunk.de/issues/381.
PR: https://github.com/phaabe/live.moafunk.de/pull/417.

Run workflow regression tests from the repository root after `npm ci` in
`frontend`:

```sh
node --test .github/workflows/tests/*.test.cjs
```

The tests cover PR/push triggers for all three branches, stable job names,
blocking validation commands, read-only CI, main-only Pages and secret steps,
and exclusion of integration pushes from the backend deployment workflow.
The YAML parser is already pinned by the frontend lockfile through ESLint.

Local results and CI run links belong on the work item. Local failure probes
do not prove GitHub branch protection. Keep O1.2.4 open until deliberate bad
PRs demonstrate red checks on the target branches and the administrator
handoff is recorded. Do not push a failure fixture to `main` or
activate production to gather this evidence. Other O1.2 leaves stay open.

## Backend build-only workflow (O1.2.1)

`backend.yml` builds and publishes images on matching `main` pushes and manual
runs with `build: true` (the default). It no longer deploys the API or restarts Liquidsoap.
The workflow name stays unchanged because existing guards refer to it.
Native amd64 builds, the BuildKit mirror and Dockerfile base mirrors are retained.

Images retain `latest` on the default branch and short `sha-` tags, and add
a full `sha-<commit>` tag. The build
outputs `digest`, `image` and `source-sha`; its summary records the exact image
reference and source commit. Use the digest for any approved deployment.
Tags can move; Docker documents the
[digest output](https://github.com/docker/build-push-action#outputs).

The removed job also generated and copied the production `.env`, synchronized
Compose and host scripts, provisioned nginx, initialized the database and
updated streaming configuration. Those tasks no longer run on push or manual
backend builds. Existing host files are preserved. The removed behavior is in
[the previous main workflow](https://github.com/phaabe/live.moafunk.de/blob/1bae514c6cfd87726cdb47a4281457ea75d1bbac/.github/workflows/backend.yml).
Do not use the old deployment job as a rollback procedure. O1.2.5 in
https://github.com/phaabe/live.moafunk.de/issues/381 supplies the future manual
API deployment path and its configuration contract.

| Event / manual inputs | Image build | Monitoring deploy | API deploy |
| --- | --- | --- | --- |
| Matching push to `main` | Yes | No | No |
| Manual defaults | Yes | No | No |
| `build: false`, `deploy_monitoring: false` | No | No | No |
| `build: false`, `deploy_monitoring: true` | No | Yes | No |
| `build: true`, `deploy_monitoring: true` | Yes | Yes | No |
| Integration push | Workflow does not run | No | No |

`deploy_monitoring: true` still explicitly deploys monitoring and needs operator
approval. It defaults to false and runs independently of `build`. Build
cancellation uses a new ref-scoped job group. Monitoring uses a separate fixed
group with cancellation disabled; GitHub may still replace older pending jobs.
Builds cannot cancel running monitoring or legacy deployment runs.
Build jobs alone have package-write permission. They need only `GITHUB_TOKEN`,
never production secrets or SSH. This uses GitHub's
[job concurrency](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency).

**Rollout evidence remains required.** Retain the first main build's run URL,
source SHA and digest as rollout evidence.
Queued, running and rerun workflows from old revisions can still deploy. Before
any API replacement, list legacy runs across all refs, drain or cancel them,
and reconcile their remote processes. Cancelling Actions does not prove that
an SSH child exited. Until this is verified, deployment exclusion is unproven.
Follow the [maintenance procedure](../../docs/implementation/runbooks/bootstrap-maintenance.md).

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

## Clippy baseline

Recorded on 2026-09-28 with Rust 1.98.0 at source commit
`59edabaa430e24f05f9398d9cd33019eaa60d7f4` on macOS. The workflow-only patch
does not change Rust sources. `cargo clippy --all-targets --locked` exits 0:
38 warning locations, including two test-only warnings. Cargo repeats some
diagnostics for the binary and test targets. `cargo fmt --check` passes.

| Lint | Locations under `backend/src/` |
| --- | --- |
| `to_string_in_format_args` | `bin/hash_password.rs:22,26` |
| `useless_format` | `bin/seed_dummy_artists.rs:331,338`; `pdf.rs:70,206` |
| `double_ended_iterator_last` | `audio.rs:272`; `handlers/recording.rs:1464` |
| `redundant_closure` | `handlers/api.rs:4759` |
| `useless_conversion` | `handlers/recording.rs:1314,1343,1821`; `handlers/stream_test_ws.rs:185`; `handlers/stream_ws.rs:212,281,306` |
| `redundant_pattern_matching` | `handlers/recording.rs:1352` |
| `question_mark` | `handlers/recording.rs:1542` |
| `ptr_arg` | `handlers/recording.rs:1690,1693`; `recording.rs:62` |
| `manual_strip` | `handlers/stream_test_ws.rs:46`; `handlers/stream_ws.rs:63,403` |
| `redundant_locals` | `image_overlay.rs:158` |
| `too_many_arguments` | `image_overlay.rs:852`; `telegram_notify.rs:65,121,175,276` |
| `wildcard_in_or_patterns` | `instagram.rs:772` |
| `vec_init_then_push` | `pdf.rs:143,293` |
| `needless_borrow` | `telegram_notify.rs:631` |
| `bind_instead_of_map` | `telegram_notify.rs:909` |
| `assertions_on_constants` | `video.rs:550,551` (tests only) |
| `needless_borrows_for_generic_args` | `main.rs:893` |

This initial baseline permits warnings without masking Clippy errors. Fixing
the warnings and enabling `-D warnings` requires a separate assigned follow-up.
