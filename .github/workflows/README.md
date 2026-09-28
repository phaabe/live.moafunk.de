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
for main's setup step. The backend deployment workflow is unchanged.

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
