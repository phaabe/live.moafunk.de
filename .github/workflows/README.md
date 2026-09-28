# Integration PR checks

`epic-ci.yml` runs on every PR to `dev/312-interim` and
`dev/streaming-architecture`, including documentation-only PRs. It has no
path filters or conditional jobs. Both stable check names are always created:

| Check | Commands |
| --- | --- |
| `backend-ci` | `cargo test --locked --all-targets`, `cargo clippy --locked --all-targets` |
| `frontend-ci` | `npm run lint`, `npm run typecheck`, `npm test -- --run` |

Rust matches the backend Docker build (1.98.0). Node.js matches the existing
frontend workflow (20). Dependency installs use the committed lockfiles.
Lint, typecheck and test failures remain failures; later checks still run
after an earlier check fails, unless the run was cancelled or setup failed.

The workflow only has `contents: read`, does not request secrets, and has no
deploy, image-push or artifact-publish steps. Existing deployment workflows
are unchanged.

After the workflow has run successfully and reached both integration branches,
add `backend-ci` and `frontend-ci` to the epic guard policy's `required_checks`
and to the applicable branch protection. Select **GitHub Actions** as the
expected source. Use the job names above, not the workflow title. Keep these
names stable when changing the workflow.

GitHub explains why required workflows must not be skipped by path filters in
[Troubleshooting required status checks](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/collaborating-on-repositories-with-code-quality-features/troubleshooting-required-status-checks).

## O1.2.4 verification still required

The existing integration PR checks cover only part of
https://github.com/phaabe/live.moafunk.de/issues/381. The leaf remains open
until the following evidence is recorded:

- PR and push checks cover `main`, `dev/312-interim` and
  `dev/streaming-architecture`, including backend changes.
- Backend checks use the pinned Rust toolchain and run `cargo fmt --check`,
  `cargo clippy --all-targets --locked` and `cargo test --locked`.
- The Clippy baseline is recorded. Existing warnings are listed explicitly;
  any later move to `-D warnings` has its own follow-up.
- Frontend PR and push triggers include both integration branches. Pages
  deployment remains limited to `main`.
- Deliberate test and formatting failures produce failed checks for backend
  and frontend changes on each target branch.
- Integration pushes cannot deploy, and CI checks need no secrets or
  production access.
- The administrator receives the stable required check names and evidence.

This checklist records outstanding verification, not completed leaf evidence.
