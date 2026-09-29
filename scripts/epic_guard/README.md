# Architecture PR checks

The shared checker checks PR metadata, changed-file ownership, the latest
counterpart verdict for the current head, and required check results.
Rules: https://github.com/phaabe/live.moafunk.de/issues/312.

## Policy and review

`.github/epic-lanes.yml` uses the JSON subset of YAML; no YAML dependency is
needed. File rules apply in order. The first matching rule must allow both
the executor and lane. Unmatched files are refused. `frontend/**` is
intentionally unmapped until frontend issues have an executor. Record new
assignments on the epic and review a policy PR before editing newly assigned paths.

Use the PR template's `Executor`, `Lane`, `Reviewer` and `Leaf IDs` fields.
The reviewer must be the other agent. Include the full epic URL. For assigned
setup work use `Leaf IDs: setup`; otherwise name the claimed leaf IDs.
Readiness and claims still need the issue's current assignment and evidence.
The checker validates declared metadata and file ownership. It does not
read the project's Ready or Executor fields, prove assignment, or prove
that a leaf is complete.

A verdict must be a standalone, unedited comment from a configured login:

```text
Review: APPROVED by Claude at <40-char head SHA>
```

Use `Codex` when reviewing Claude's work; use `CHANGES REQUESTED` for a refusal.
Post findings separately. Every new head needs a new verdict. Both agents
currently share a GitHub account, so agent identity is self-declared. The
login allowlist does not prove which agent wrote a comment. Never write the
other agent's verdict.

The checker matches REST comments against paginated GraphQL edit markers.
Equal creation and update timestamps alone do not establish an unedited
comment. Missing or inconsistent edit evidence refuses the check.

An edit to any trusted reviewer's comment after approval requires a fresh
approval: the API does not expose its previous body. Deleted verdicts cannot
be reconstructed from current comments. Never edit or delete verdicts.

Current required checks are `Vercel` and `Vercel Preview Comments`. Change the
policy in a reviewed PR when required CI changes. These checks do not replace
the tests or activation evidence required by the implementation plan.
Required checks must succeed. Completed optional checks may also be `skipped`
or `neutral`; pending and failed checks still refuse the gate.

## Install and activate

1. Claude reviews `ci/312-epic-guard` against its actual head. This approved
   setup exception targets `main`; phaabe must provide the required human
   review. The checker cannot certify its own first installation.
2. After that PR merges, propagate only the setup changes through reviewed
   PRs into `dev/312-interim`, later `dev/streaming-architecture`. Do not move
   unrelated implementation or plan changes into `main` to install the guard.
3. Verify the guard on each target branch before calling it enforced there.
   The workflow loads the checker and policy from the PR's immutable base
   SHA. A PR cannot change the policy used to evaluate itself. The privileged
   workflow never executes the PR's head.
4. The repository owner configures required status `epic-guard`, restricted
   to the **GitHub Actions** app as its **expected source**, on the active
   integration branch. Do not select **any source**. Confirm the app is
   selectable after the first workflow publishes the status. Keep
   `main`'s human review requirement. Do not require this epic-only status
   on `main` before a reviewed policy covers releases and ordinary PRs.
   Opening or merging this setup PR does not change branch protection.
5. The repository owner checks the public repository's Actions event policy
   and explicitly allows `pull_request_target` for this trusted workflow
   where required. GitHub's default policy is moving from evaluation to
   enforcement; see [GitHub's event-policy guidance](https://docs.github.com/en/actions/reference/security/securely-using-pull_request_target).
6. In each Codex session, open `/hooks` and trust the new or changed hook.
   Trust is an operator UI action; an untrusted hook is skipped. Do not change
   configuration or bypass a guard to simulate this approval.

While section 0 of `docs/implementation/epic-rules.md` applies, feature PRs
branch from and target `dev/312-interim`. Allowing feature PRs into the
canonical `dev/streaming-architecture` branch needs a reviewed policy change
when section 0 is retired.
There is no release path from `dev/312-interim` to `main`. Release PRs remain
refused by this checker until a reviewed policy adds verification of Anton's
approval and release evidence; the setup exception does not authorize them.

## Merge and hook handoff

Run the shared checker immediately before a merge, then use
`gh pr merge` with `--match-head-commit` and the checked head SHA. It is a
read-only check; it does not merge or write a review verdict.

Run the installed runner from a trusted checkout. It fetches the checker
and policy from the PR's immutable base SHA. Substitute the PR number and
full head SHA:

```sh
python3 scripts/epic_guard/run.py \
  --pr PR_NUMBER \
  --expected-head FULL_HEAD_SHA
```

Success returns exit code 0 and an empty error list for the PR in its JSON
output. A refusal returns exit code 1 with errors. Resolve them before
merging. `check.py` is the lower-level interface for tests or a checkout
already verified to match the trusted base; do not pass it a PR-head policy.

Claude owns `.claude/` and must wire its hook to the shared checker in a
separate reviewed change. Codex's hook also needs that integration before it
can claim to check lanes, verdicts and CI. Until then both agents run the
shared check explicitly before merging.

The `Epic guard` workflow's `epic-guard-runner` job writes the `epic-guard`
status. It refreshes on PR-target events, issue comments, statuses, checks,
completed named workflows, manual dispatch and a ten-minute schedule.
The per-PR `epic-guard` status is the verdict; `epic-guard-runner` succeeds
once all statuses are published and fails only when the run itself fails
(event selection or status publication errors).
Verification errors for an individual PR publish a failure verdict and let
the remaining PRs refresh; if its head cannot be read, no status can be
published and the run fails.
The workflow uses an explicit `workflow_run.workflows` list, including
`EPIC - Integration checks`; update it when adding or renaming CI workflows.
After installation on the default branch, finish a CI run and confirm it
starts the guard. GitHub documents [named workflow completion triggers](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_run);
the default-branch installation is required to verify delivery.
It binds the result to the checked head and rechecks PR state before posting.
Refresh is eventually consistent: a comment, body or check may change after
a successful run. `--match-head-commit` protects the head, not an atomic
snapshot of approval and checks. Keep the final local check and GitHub branch
protection; do not treat a previous green status as permanent approval.
