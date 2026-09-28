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
- REST PR writes through `gh api`, including implicit POSTs from field flags
  and opaque `--input` bodies. Use `gh pr` commands instead. GET requests work.
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
selected action. Exit 3 skips the session and exits 0; other gate errors stop
the tick. An unchanged action and GitHub timestamp are skipped for three hours
after the last completed or valid blocked result (`EPIC_REPEAT_TTL_SECONDS`
overrides this).
The shared gate never skips `continue`. Only a session that exits 0 and reports
a valid completed or blocked result calls `record` with the same action file.
It records the GitHub state seen before
the session, so feedback arriving during it triggers another tick.
Gate state uses `~/.local/state/epic-loop/codex-gate*.json`, or `EPIC_STATE_DIR`
if set; the runner's log and lock still use the default state directory.

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
not reset a cooldown. The selector still chooses one action per tick; a skipped
target does not cause the runner to choose lower-priority work.

The model writes `{ "status": "completed" | "blocked", "summary": "..." }`
through Codex's output schema to `codex-result.json` in the same state directory.
The runner clears that file before each session. Blocked, missing or malformed
results exit 75 and start cooldown; a nonzero model exit starts cooldown and
preserves its exit code. Completed work clears only that target's cooldown.
Valid blocked results also retain the shared gate's three-hour suppression
after cooldown expires, unless GitHub changed. Model failures and invalid
results do not create a shared gate record. A blocked `continue` still retries
after cooldown because local progress cannot be inferred from GitHub state.

Actions admitted by the gate start one fresh `codex exec` session using `epic-tick.md` plus
the selected JSON. It uses the workspace-write sandbox with explicit network
access (`-c sandbox_workspace_write.network_access=true`) for GitHub calls and
existing approval settings; it never bypasses approvals or hook trust.
Permission failures must return a blocked result and stop the tick. See
[Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode).

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
The lock stores the runner PID, Unix start time and original timeout budget in
`owner.json`. It is held through child termination and removed on normal exit,
errors and handled signals. A later tick reclaims and logs an abandoned lock
only when the owner PID is dead and its age exceeds pull + selection + tick
timeout + 10-second grace. It uses the larger of the stored and current budgets, so a
shorter later tick cannot reclaim while an old child may still run. A live PID
always keeps its lock, including a reused PID. A short OS file lock on
`codex.guard` serializes recovery; keep that guard file in place.

Missing/invalid owner metadata or unexpected lock contents require manual
review. Confirm no tick or child session is running before removing such a
lock. Do not clear another worktree's lock. All checkouts share it.

This PR installs no scheduler. Run the local tests without GitHub writes or
model calls:

```sh
python3 -m unittest discover -s .codex/tests -v
/bin/bash -n .codex/codex-tick.sh
```

## Unattended feature commits and pushes

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
The helper does not check the PR's Executor; lane ownership remains enforced
by the epic workflow and must be checked before editing or publishing a branch.
