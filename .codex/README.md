# Codex epic guard

The tracked `hooks.json` loads the guard from the current Git root, including
linked worktrees and sessions started in subdirectories. It requires Bash 3.2
and Python 3.10+. Existing machine-global hooks still load separately.

After this branch is reviewed and merged, update each worktree to a commit
containing these files. Existing worktrees on older commits do not gain them
automatically. If a checkout already has an untracked `.codex/hooks.json`, back
it up and reconcile its local registrations before updating; do not overwrite it.

Trust the project, then review and trust the new hook using `/hooks` in Codex.
Codex skips new or changed hook definitions until they are trusted. Check
`/hooks` in each worktree; tracking a file does not grant runtime trust. See the
[Codex hook documentation](https://learn.chatgpt.com/docs/hooks).

The guard blocks:

- Claude verdicts containing a real 40-character SHA in tool input, a `gh`
  body file, a typed API field file (`-F key=@path` / `--field key=@path`), or
  a `gh api --input` file on any endpoint. Codex may only write its own verdict.
  API file paths resolve against the command's working directory; unreadable
  files, `@-` and `--input -` (stdin) are refused.
- PR creation without an explicit base. Feature PRs temporarily target
  `dev/312-interim` under epic-rules.md section 0. The normal base
  `dev/streaming-architecture` remains allowed; only that branch may create a
  release into `main`. The interim branch cannot create a release into `main`.
  The approved setup exception is exactly `ci/312-epic-guard` → `main`.
- PR merges without a 40-character `--match-head-commit` value.
- REST PR writes through `gh api`, except the selected adoption body edit below.
  Implicit POSTs and opaque `--input` bodies remain blocked. GET requests work.
- MCP merges: use `gh pr merge --match-head-commit` instead. MCP PR creation
  follows the same base and head rules.

Use a single literal `gh pr create` or `gh pr merge` command. Put flags after
the verb. Compound commands, wrappers and shell expansion are refused for these
operations. Flags before or between subcommands are refused. Quoted values,
`--flag=value` and backslash-newline continuations work. Short option values
must be separate (`-B main`, not `-Bmain`); repeated base/head options are refused.
Known value flags accept quoted values starting with a dash, such as
`--body '- fix workflow'`. Shell command payloads must be strings; arrays and
other types are refused.
Body files must exist before the command; stdin and heredoc bodies are refused,
including heredocs passed through shell wrappers. For `gh api`, `-F` is a typed
API field, not a body-file path. There is no main-branch override.

This is a command guard, not a security boundary against arbitrary scripts,
GitHub API clients or aliases. Shell `gh api` calls must also be single literal
invocations, with flags after `api`. It does not
verify GitHub approval, lane ownership or green checks. Those remain mandatory
under the epic rules; the shared server checker is separate setup work.

During an `adopt` tick the runner exports `EPIC_ACTION_FILE`, an absolute path
to its locked selection. The guard permits exactly
`gh api --method PATCH repos/phaabe/live.moafunk.de/pulls/<n> -F body=@<absolute-path>`
for that selected PR. The action must include its head SHA and original body
digest. Other targets, extra fields, relative paths and other write forms stay
blocked. Body files still pass the verdict check. This command guard is not a
replacement for checking the current head, owner and body before the write.

## Recovery if the guard blocks every tool

The hook fails closed: missing `git` or `python3`, unreadable files and parser
errors block the tool call. The operator can disable this hook through `/hooks`
in Codex, repair the dependency or guard from a separate terminal, and run the
tests below. If `/hooks` is unavailable, back up `.codex/hooks.json` and remove
only this guard's registration from that worktree in a separate terminal, then
restart the session. Restore the registration and trust it through `/hooks`
after the repair. There is no environment-variable bypass.

While disabled, make only the repair: do not create or merge PRs or post review
verdicts through the unguarded session. Check the epic rules manually. Disabling
the guard does not grant approval for `main` PRs, merges or production actions.

Run the regression tests (no GitHub writes):

```sh
python3 -m unittest discover -s .codex/hooks/scripts -p 'test_*.py' -v
/bin/bash -n .codex/hooks/scripts/epic-guard.sh
```

The tests invoke the registered command through macOS-compatible `/bin/bash`,
including from a fresh linked worktree with a space in its path.

## One-tick runner

Run `/bin/bash /path/to/checkout/.codex/codex-tick.sh` from any directory.
The runner lives in Codex's setup lane. It uses Bash 3.2, Python 3.10+, GNU
`timeout` (or `gtimeout`), authenticated `gh`, and an authenticated Codex CLI.
Use a dedicated runner checkout on `dev/312-interim` with that branch as its
upstream. Keep feature work in separate worktrees. Trust its project hooks
before unattended use, as described above.

Edit actions (`claim`, `continue`, `fix`, `fix-checks`, `resolve-conflict`) use
`.codex/feature_worktree.py` before model launch. Their fixed directory is the
runner's sibling, named by removing a final `-runner` and adding `-wt`.
For `~/git/2_jobs/live.moafunk.de-codex-runner`, it is
`~/git/2_jobs/live.moafunk.de-codex-wt/<branch>`. Each runner checkout therefore
has its own directory. Keep that directory free of symlinks. The model starts
with this worktree as `--cd`; the runner checkout holds no feature work.

The helper checks the origin, current Codex ownership, issue status, allowed
base, branch name and PR head. A PR must have one `Executor: Codex` line and
one `Issue:` link; forks and branches for another issue are refused. Issue-only
claims and continuations always use `feat/<issue>-codex-work`, deliberately
independent of the issue title. New branches start from `origin/dev/312-interim`.
Other issue branches may belong to a human or Claude and are ignored. To resume
one, select its open PR with `Executor: Codex`. The preceding runner pull
refreshes origin refs; a PR head that does not match those refs is refused.
Existing worktrees retain local edits and unpublished commits; a local PR
branch must contain the selected head.
The helper never rebases or resets a checkout to make it match.

A target-specific refusal exits the helper with code 7, stores the normal
blocked-target cooldown and tries the next candidate. This covers invalid PR
metadata, missing refs, a local branch behind its PR and occupied destinations.
The runner retries after `EPIC_BLOCKED_RETRY_SECONDS`; repeated ticks do not
extend the cooldown. A wrong runner repository or a failed GitHub read still
stops the tick.

If normal `git worktree add` refuses a branch held elsewhere, the tick logs
`handoff needed: <branch> in <path>` and starts no model for it. The existing
repeat gate stores a handoff fingerprint containing the branch and occupying
path, including for `continue`. This replaces that target's previous gate
result. It suppresses another request for the normal repeat TTL while the
condition is unchanged. Later ticks check the recorded branch's local
occupancy before making extra ownership reads. Release, changed GitHub state
or TTL expiry requires fresh ownership checks. No blocked-target cooldown is
created. The human must release their checkout manually; the next tick checks
Git again and can acquire the branch immediately after release. The runner
never forces acquisition or switches, resets, stashes or removes another
checkout. If the checkout was deleted outside Git, the message asks its operator
to check the registration and run `git worktree prune`. The runner never prunes
it automatically. Feature-worktree deletion and detached-review cleanup are
separate work. No installed Git permissions are changed by this preparation step.

A scheduler must set `PATH` so `gh`, `codex`, Python and GNU `timeout` are
available. For Apple Silicon Homebrew, include `/opt/homebrew/bin` along with
`/usr/bin:/bin:/usr/sbin:/sbin`; use `/usr/local/bin` for Intel Homebrew. Add the
actual Codex installation directory if it is elsewhere. launchd's default
PATH does not include Homebrew.

Each invocation takes the shared `~/.local/state/epic-loop/codex.lock`
directory using `mkdir`, checks `~/.epic-pause`, and runs `git pull --ff-only`
before calling the shared decision script once. The updated selector and helpers
apply in that tick, including focus rules. Runner shell changes apply on the
next invocation. A failed or timed-out pull logs the error and stops before
selection or a model session; it creates no target cooldown or repeat record.
The runner never resets, stashes or discards local edits. Resolve pull failures
in the operator checkout before retrying. Pause and lock contention skip the
pull. Lock contention, pause, idle and stop exit 0 without starting Codex.
Before a session, `scripts/epic/tick_gate.py check --agent codex` checks the
selected action. Exit 3 tries the next candidate; other gate errors stop
the tick. An unchanged action and GitHub timestamp are skipped for three hours
after the last completed or valid blocked result (`EPIC_REPEAT_TTL_SECONDS`
overrides this).
The shared gate never skips ordinary `continue` actions. A handoff records its
separate fingerprint before model launch. Otherwise, only a session that exits
0 and reports a valid completed or blocked result calls `record` with the same action file.
It records the GitHub state seen before
the session, so feedback arriving during it triggers another tick.
Gate state uses `~/.local/state/epic-loop/codex-gate*.json`, or `EPIC_STATE_DIR`
if set. The runner's log and lock use that same directory. Registered agents
use its `agents/<id>` subdirectory while quota waits remain shared at the root.

With `EPIC_SHARED_READER=1`, the runner calls `next_action.py --recheck` after
the gate and before starting Codex. Exit 6 skips the stale candidate; exit 5
or a recheck timeout ends the tick as blocked (exit 75). These paths discard
temporary gate state and write no completion, repeat or cooldown record.
The shared-reader switch remains off by default.

The enabled write hook reads GitHub again before claims, pushes, comments,
verdicts and merges. It loads the checker from the runner's trusted checkout.
Direct pushes and installed feature-helper pushes both require an In progress
issue owned by Codex. Failed reads block the write. The hook timeout is 90 seconds.

Before that gate, `.codex/tick_backoff.py` delays blocked or failed targets for
15 minutes (`EPIC_BLOCKED_RETRY_SECONDS` overrides this). This also applies to
`continue`, including a blocked `claim` followed by `continue` on the same issue.
Cooldowns persist per target in `~/.local/state/epic-loop/codex-backoff.json`.
When a blocked issue becomes a draft PR, a `continue` tick reads the PR's exact
`Issue:` field and checks its head. If it links the blocked issue, the helper
transfers that cooldown to the selected PR head and skips the session. The
original expiry and reason are preserved. The issue entry is removed, so a
later PR head can run immediately. Failed metadata reads, ambiguous issue
fields or a changed head stop before starting the model and leave state intact.
This lookup runs only when a PR continuation could inherit an active issue
cooldown. Ticks without active issue cooldowns need no extra PR request.
A different selected target or a new PR head can run immediately. Comments do
not reset a cooldown. Adding `updated_at` to actions changes shared repeat-gate
fingerprints, but not these cooldown keys or expiry times.

The selector lists candidates once with `--candidates`. The tick tries them in
priority order, without a count limit, and starts at most one model session.
Busy target locks, active cooldowns, stale selections and suppressed repeats
fall through to the next candidate. Pause and shared quota waits stop the tick.

Target locks use `scripts/epic/target_lock.py` and the shared directory
`~/.local/state/epic-loop/target-locks/`, independent of `EPIC_STATE_DIR` and
agent IDs. If overriding `EPIC_LOCK_DIR`, every Claude and Codex runner on the
machine must use the same path. The shell opens descriptors 8 and 9 in target
number order and keeps them through the session and result recording. The
timeout supervisor inherits them and holds them while waiting for Codex. If
only the tick shell is killed, the surviving supervisor keeps the target locked
until Codex exits. The OS releases the lock after the last holder exits. Lock
files are never deleted. The repeat gate rechecks the target after locking.

The model writes `status` (`completed` or `blocked`), `summary`, and nullable
`reason_code` and `retry_at` fields through Codex's output schema to
`codex-result.json` in the same state directory. Ordinary results set the last
two fields to `null`.
The runner clears that file before each session. Blocked, missing or malformed
results exit 75 and start cooldown; a nonzero model exit starts cooldown and
preserves its exit code. Completed work clears only that target's cooldown.
Valid blocked results also retain the shared gate's three-hour suppression
after cooldown expires, unless GitHub changed. Model failures and invalid
results do not create a shared gate record. A blocked `continue` still retries
after cooldown because local progress cannot be inferred from GitHub state.

A completed `adopt` result also runs the shared `tick_verify.py` before clearing
cooldown or recording completion. Missing metadata or changed original body
fails the tick and starts target cooldown. A quota wait during verification
stops without writing cooldown or repeat records. Other actions retain their
existing result handling.

Keep `adopt` out of `EPIC_FOCUS_ACTIONS` until both agent implementations are
reviewed and merged. Then update and trust the changed Codex hook in the runner
checkouts, verify the exact body-edit command is permitted, and add `adopt` to
the comma list in both runner environments. Preserve any other enabled actions.
Do not enable extra runner instances or remove a pause as part of this change.
Observe the first adoption and its verifier result before treating it as active.

Actions admitted by the gate start one fresh `codex exec` session using `epic-tick.md` plus
the selected JSON. It uses the workspace-write sandbox with explicit network
access (`-c sandbox_workspace_write.network_access=true`) for GitHub calls and
existing approval settings; it never bypasses approvals or hook trust.
Permission failures must return a blocked result and stop the tick. See
[Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode).

Each tick passes `EPIC_STATE_DIR`, `EPIC_QUOTA_DIR`, `EPIC_ACTION_FILE` and
`EPIC_TRUSTED_ROOT` through
individual `shell_environment_policy.set` overrides. Exporting them into the
CLI process alone is insufficient when Codex uses `inherit = "core"` for tool
commands. These overrides preserve other configured values and inheritance
settings. If a shell environment include filter is configured, it must also
allow these names. When the shared reader is enabled, the runner also forwards
`EPIC_SHARED_READER` and configured cache, snapshot,
recheck, selection and focus-action settings. The include filter must allow
those names too. See [Codex shell environment policy](https://learn.chatgpt.com/docs/config-file/config-advanced#shell-environment-policy).

Provision pinned build tools before starting unattended ticks. For the current
Rust 1.98.0 pin, install it with `rustup toolchain install 1.98.0 --profile minimal
--component rustfmt --component clippy`, then verify `cargo +1.98.0 fmt --check`
from `backend/` inside the runner sandbox. An installed default `stable`
toolchain does not satisfy an explicit version pin; otherwise rustup tries to
install into its home directory during the tick and may be blocked.

Output and errors append to `~/.local/state/epic-loop/codex.log`. Pull and
selection each have a 120-second limit; Codex has a 1,800-second limit, with a
10-second TERM-to-KILL grace. Override these with positive integer values in
`EPIC_PULL_TIMEOUT_SECONDS`, `EPIC_SELECT_TIMEOUT_SECONDS` and
`EPIC_TICK_TIMEOUT_SECONDS`. Errors and timeout exit codes propagate; a later
invocation refreshes, selects again and checks the cooldown.
Feature-worktree preparation has a separate `EPIC_PULL_TIMEOUT_SECONDS` limit,
included in the runner's lock budget. A timeout stops before model launch.
Issue claims and continuations also receive fresh REST assignment evidence from
the selector's project board, with a separate 60-second limit in that budget.
The evidence identifies the project, repository, issue, read time, Status and
Executor. Complete absence, changed assignment and unknown/read failure stop
before model launch. Failed reads do not record a target cooldown or repeat
result. The model must use this evidence, not GraphQL `issue.projectItems`.
The existing claim write checks run again against current REST state in both
reader modes. This does not enable the shared snapshot reader or satisfy missing
interface confirmations.
With the shared reader enabled, fresh backoff lookup and action recheck each
have a separate `EPIC_RECHECK_TIMEOUT_SECONDS` limit (default 60). Both limits
are included in lock and agent-registration budgets. Snapshot lock wait plus
refresh must fit below the selection timeout; invalid settings exit 2 before
work starts.
The lock stores the runner PID, Unix start time and original timeout budget in
`owner.json`. It is held through child termination and removed on normal exit,
errors and handled signals. A later tick reclaims and logs an abandoned lock
only when the owner PID is dead and its age exceeds the larger of the stored
and current timeout budgets, so a shorter later tick cannot reclaim while an
old child may still run. A live PID
always keeps its lock, including a reused PID. A short OS file lock on
`codex.guard` serializes recovery; keep that guard file in place.

Missing/invalid owner metadata or unexpected lock contents require manual
review. Confirm no tick or child session is running before removing such a
lock. Do not clear another worktree's lock. All checkouts share it.

Run the local tests without real GitHub or model calls:

```sh
python3 -m unittest discover -s .codex/tests -v
python3 .codex/tests/test_codex_tick.py -v
/bin/bash -n .codex/codex-tick.sh
```

Plain discovery and individual test-file runs are safe inside a tick. Every
`.codex/tests/test_*.py` imports `scripts/epic/isolated_env.py` before production
modules or fixtures that import them. Keep this import first in new tests too.
The helper removes inherited runner settings and credentials, uses a temporary
home for import-time defaults, and gives each test a fresh temporary home.
Tests set only the runner switches they exercise and use fake GitHub/model
commands. The isolation regression checks empty and future-quota-wait stand-ins
and verifies that their files, contents and modification times stay unchanged.

## Review worktrees and saved evidence

Review ticks prepare `/private/tmp/moafunk-review-<pr>-<full-head-sha>` before
starting Codex. Preparation checks the repository, open Claude PR and exact
head. It reuses an existing path only when it is the registered, clean,
unlocked detached checkout at that head. A path collision is reported, never
reset or replaced. The runner and target locks remain held through child exit
and cleanup.

The commit is retained at `refs/remotes/codex-review/<pr>/<full-head-sha>`.
An existing ref must match; it is never overwritten. After success, failure,
timeout or a handled signal, the runner invokes the installed helper:

```text
python3 -I /Users/anton/.local/libexec/codex-cleanup-git.py --worktree <runner-checkout> remove-worktree <review-path>
```

Preparation and cleanup each use the `EPIC_PULL_TIMEOUT_SECONDS` deadline.
It then runs `git worktree prune`. There is no force removal or raw-removal
fallback. Dirty, locked, mismatched or refused checkouts remain, with their
paths and reasons in the tick log. The retaining ref stays until removal
succeeds and no pending review needs it. Cleanup does not alter the review's
result or repeat-gate record: a posted verdict remains completed.

Evidence lives in `<EPIC_STATE_DIR>/reviews/<pr>/<full-head-sha>/`, outside the
repository's checkouts. Use a state path without symlinks (on macOS,
`/private/tmp` instead of `/tmp`). Only this review directory is added to the
model's writable roots.
`context.json` records the checked review inputs; `bundle.json` is the shared
version 1 artifact for review and later delivery. Each attempt has its own
`attempts/<id>/model.log` and `result.json`, retained on failure and timeout.
Review output streams directly into the attempt's log. The runner appends it
to the normal tick log after the child stops, before cleanup and final metrics.

The bundle contains `repo`, `pr`, `reviewer`, `sha`, `head`, `base`, `inputs`
(title, body, draft flag and labels),
`findings`, an explicit `verdict`, `status` and ordered `comments` containing
exact `body` text and a confirmed `url` or null. The model saves draft findings
as it works and uses `review_worktree.py save-bundle --context-file <context>
--bundle-file <candidate>` to persist the completed bundle atomically before
its first comment write. Finding comments precede the standalone verdict.
Later saves may add delivery URLs and mark it published, but cannot replace
completed analysis. A completed or published bundle prevents a second model
review. Automatic publication retry and input revalidation belong to
https://github.com/phaabe/live.moafunk.de/issues/535; until then, pending delivery
requires operator handling. There is no second review database.

For a one-time backlog cleanup, pause scheduled ticks and wait for their
children to stop. List known review paths first:

```sh
python3 .codex/review_worktree.py sweep --runner /absolute/runner-checkout --state-dir /absolute/runner-state
```

Run the same command with `--apply` to remove eligible checkouts. It holds the
runner lock, checks the PR is closed or merged, and sends each accepted path
through the same helper. Paths that cannot be identified or validated are
reported and retained. Feature worktrees and unknown checkouts are not removed.
This command does not resume the scheduler.

## GitHub quota waits

The runner checks `scripts/epic/github_quota.py` before selection and before
starting Codex. A shared wait skips both operations and exits 0. A new quota
block exits 75; unreadable or invalid quota state exits 2. These paths do not
record a completed action, a target cooldown or a repeat-gate entry.

Both runners use the same `EPIC_QUOTA_DIR`, separate from per-agent result and
cooldown directories. Registered Codex agents retain the registry's shared
quota directory. A GraphQL response can contain a rate-limit error even when
HTTP succeeds; stop on that error and defer until the shared reset time.
Do not retry another target to work around the account's quota.

If GitHub refuses a request during a model session, return a blocked result
with `reason_code: "github_rate_limit"` and a UTC `retry_at` when known. Use
`null` when the reset is unknown; the shared helper obtains or supplies the wait.
Quota waits leave existing target cooldowns unchanged. Other failures retain
the target cooldown and repeat-gate behavior described above.
Valid quota results also record the shared wait when Codex exits nonzero;
the runner preserves that nonzero exit code.
An active shared wait is reused without another reset query. Model retry times
more than one hour plus the safety margin ahead require a fresh reset query
or the shared fallback. Quota stops use the monitor's `quota` phase.

If another agent creates a wait during a model session, ordinary results still
write their local cooldown and repeat records. Completed actions stay recorded,
and failed sessions retain their exit code. The next tick observes the wait.

## launchd scheduler example

The tracked template is
`launchd/de.moafunk.codex-epic-loop.plist.example` in this directory. It schedules
one invocation every 180 seconds, with no `KeepAlive` restart loop. It installs
nothing automatically. Keep `~/.epic-pause` in place while preparing or adopting
the schedule; loading the job does not remove it.

The measured selector read costs 109 GraphQL points. At 180 seconds, each
runner can spend about 2,180 points per hour; two can spend 4,360 before any
other requests. This reduces traffic but does not guarantee staying below the
shared quota. The quota wait is still required.

For a fresh installation, copy the template to a temporary file and replace
every placeholder with an absolute path using a plist editor:

- `__HOME__`: your home directory.
- `__RUNNER_CHECKOUT__`: the dedicated runner checkout, containing this script.
- `__CODEX_BIN_DIR__`: the directory containing your authenticated `codex` CLI.

launchd does not expand `~`, `$HOME` or shell substitutions inside plist values.
Keep each `ProgramArguments` entry as a separate string, including paths with
spaces. Escape XML characters when editing raw XML. The template PATH includes
Apple Silicon Homebrew (`/opt/homebrew/bin`) and Intel Homebrew (`/usr/local/bin`).
Check that the chosen PATH also finds Python 3.10+, `gh` and GNU `timeout` or
`gtimeout`. Create the log directory before loading:

```sh
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/.local/state/epic-loop"
```

For an existing installation, back up the installed plist and edit a copy of
that file. Preserve its checkout, CLI path, environment and log destinations.
Set `StartInterval` to integer `180`; remove any `KeepAlive` restart policy.
Do not replace a configured plist with the unrendered template.

```sh
cp "$HOME/Library/LaunchAgents/de.moafunk.codex-epic-loop.plist" \
  "$HOME/Library/LaunchAgents/de.moafunk.codex-epic-loop.plist.backup-$(date +%Y%m%dT%H%M%S)"
```

Before installing the edited file, validate it. Replace the sample path below
with your rendered plist's absolute path. The check refuses leftover template
markers and the wrong interval type; `plutil` checks plist syntax.

```sh
codex_plist='/absolute/path/to/rendered-codex-loop.plist'
python3 - "$codex_plist" <<'PY'
from pathlib import Path
import plistlib
import sys

raw = Path(sys.argv[1]).read_bytes()
assert b"__" not in raw, "Replace every template placeholder"
data = plistlib.loads(raw)
assert type(data["StartInterval"]) is int and data["StartInterval"] == 180
assert "KeepAlive" not in data, "Remove the restart policy"
PY
plutil -lint "$codex_plist"
```

For adoption, wait for any running tick to finish, then unload the existing job
before replacing its plist. Run this only if the job is loaded; a fresh
installation skips this command:

```sh
launchctl bootout "gui/$(id -u)/de.moafunk.codex-epic-loop"
```

Install the validated file and load it once:

```sh
cp "$codex_plist" "$HOME/Library/LaunchAgents/de.moafunk.codex-epic-loop.plist"
launchctl bootstrap "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/de.moafunk.codex-epic-loop.plist"
launchctl print "gui/$(id -u)/de.moafunk.codex-epic-loop"
```

These are manual operator commands. Do not clear `~/.epic-pause` as part of
installation or adoption. Resume the loops separately after checking the setup.

## Unattended feature Git operations

Install a reviewed copy of `.codex/feature_git.py` outside agent-writable
workspaces as `~/.local/libexec/codex-feature-git.py`. Beside it, place
`codex-feature-git.json` with `trusted_checkout` set to the absolute trusted
checkout path and `allowed_origin_urls` set to its exact approved origin URLs.
Allow only the literal command prefix `python3 -I /absolute/home/.local/libexec/codex-feature-git.py`
in the operator's Codex rules. Keep both installed files outside writable roots;
updates require operator approval. Do not allow arbitrary Python commands.

The helper accepts `--worktree <path> commit --message-file <path>` or
`--worktree <path> push`. It checks the common Git directory, requires an issue
branch such as `feat/381-integration-ci`, and rejects changed or multiple origin
destinations. Pushes publish only the current branch without force or tags.
Commits and pushes retain Git hooks. The isolated Python interpreter and removed
`GIT_*` variables prevent environment overrides of the helper's imports or Git
target. This is a restriction on routine operations, not a security boundary
against malicious repository hooks. Raw critical Git commands retain their
approval requirements.
Normal commit and push keep their existing contract; lane ownership is checked
by the epic workflow. A pending recorded rebase requires a lease push.
An unreadable or malformed record blocks normal pushes until the operator
repairs it: its worktree cannot be identified safely. The error names a malformed
record instead of printing a traceback.

For unattended conflict work, extend the installed JSON with `runner_checkout`
(the absolute dedicated runner path) and `context_file` (the absolute
`<EPIC_STATE_DIR>/feature-git-context.json` path, including `agents/<id>` when
using a registered runner). The runner, installed helper and configured context
must agree. Keep the context directory and its `rebase-<pr>.json` records outside
all model-writable roots, including additional writable directories. Never put
them under `/tmp`, a repository or its common Git directory in a live setup.
No caller environment variable or command option selects this trusted context.

The runner checks installed helper bytes against `.codex/feature_git.py` before
a conflict session. Missing or old code, invalid configuration and worktree
refusals block the tick and start the target cooldown. After fresh PR validation,
it writes the selected action, PR, branch, actual base, original head and fixed
worktree to the protected context file. The tick removes this transient context
on exit. Only the installed helper creates or changes the persistent rebase
record. An interrupted tick can resume the matching Git rebase, including its
detached HEAD and staged resolutions. An unrelated rebase is refused.

The existing literal prefix also accepts these exact suffixes:

```text
--worktree <path> rebase --base <actual-PR-base> --expected-head <40-char-SHA>
--worktree <path> rebase-continue
--worktree <path> rebase-abort
--worktree <path> push-with-lease --expected-remote-sha <40-char-SHA>
```

The allowed base is currently `dev/312-interim`, matching section 0 of the epic
rules. The helper fetches only that base and records its commit and the original
PR head before starting the rebase. A new rebase needs the exact selected head,
a clean tree and no existing Git operation. It disables autostash and updates
to other branches. Hooks still run. Git environment overrides are removed before
the helper sets its own noninteractive editors.

Lease publication uses only
`git push --no-follow-tags --recurse-submodules=no --force-with-lease=refs/heads/<branch>:<original-SHA> origin HEAD:refs/heads/<branch>`.
Fetching never changes this pin. A changed remote fails and preserves local
work. A successful push removes the record and requires a new counterpart review
for the new head. Continue/abort without a matching record, interactive rebase,
exec/onto/skip, extra refs, remotes, tags, deletion and force flags are refused.
There is no raw-command fallback.
If no PR commits remain beyond the fetched base, publication is refused. Keep
the local result and record for operator review; do not publish the base as the
PR head or add an empty commit to bypass the check.

If the process stops before Git starts, `rebase-abort` clears the pending record
only when the branch still has its original head and a clean tree. If Git has
already completed, `rebase-continue` reports that publication is pending. A
changed PR head or mismatching Git operation requires manual recovery; keep
the record and worktree. Do not delete records to refresh a stale lease.

A process killed after a successful push but before record removal also leaves
an old record. Keep the runner paused. The operator must verify the PR and remote
head equal the local head, the recorded base is its ancestor, and the worktree
is clean. Only then archive that PR's record outside the record directory and
request a new review for the published head. If any check fails, preserve the
record and investigate; never replace its expected SHA.

### Installation and runtime evidence

After source review, the operator installs the reviewed helper and extends
its adjacent JSON. Compare SHA-256 digests of source and installed helper. Keep
the helper, config and literal-prefix rule outside agent-writable roots. Retain
the existing narrow rule; add no raw Git or general Python permission.

Before marking installation complete, keep schedulers paused and run a fresh
real `codex exec` with the runner's `--sandbox workspace-write`, network setting,
approval configuration and effective rules. Use a disposable local bare remote
and trusted fixture provisioned by the operator; authorize only that fixture
for the test, then restore and verify the live installed configuration. Run an
allowed helper rebase and lease push, then a denied request. Record CLI version,
helper digest, exact executed tool calls, exit outcomes, before/after local and
remote heads, and absence of permission prompts. Verify that the denied request
changes neither head and that raw critical Git commands gain no permission.
Parser tests and a fake Codex binary do not satisfy this check. Do not claim
activation from the source tests alone or resume schedulers as part of testing.
