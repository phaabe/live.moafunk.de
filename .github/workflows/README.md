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
