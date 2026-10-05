# Architecture epic rules

These rules apply to all work on the architecture epic
(https://github.com/phaabe/live.moafunk.de/issues/312). Claude and Codex follow
them in every session. This file is the only full copy; `CLAUDE.md` and
`AGENTS.md` only point here. Change it only through a reviewed PR.

Plan: [plan-v4.md](plan-v4.md) (jointly accepted). Execution status: the epic
and its project (https://github.com/users/anneoneone/projects/2).

## 0. Temporary integration branch (until branch protection is fixed)

Branch protection on `dev/streaming-architecture` requires an approving GitHub
review. Both agents use the PR author's account, so no agent PR can get one.
Until the repository admin sets required approvals to 0 on that branch:

- `dev/312-interim` replaces `dev/streaming-architecture` in these rules and in
  every issue of the epic: branch from it, target it, merge into it.
- Review and merge rules (sections 4 and 5) apply unchanged.
- No release PR to `main` from `dev/312-interim`.

When the admin has changed the setting: one PR from `dev/312-interim` into
`dev/streaming-architecture`, approved by Anton and merged with a merge commit.
Then open PRs are retargeted, `dev/312-interim` is deleted and this section is
removed in a reviewed PR.

## 1. Before you start

- Work only on leaves that are **Ready** and assigned to you (project field
  Executor). Ready applies only to the leaves named in the issue's readiness
  comment; the rest of the issue stays blocked.
- Claim the leaf on its issue: comment with the leaf IDs, the files you will
  edit and your branch, then set Status to In progress.
- Check the file owner first. One editor per shared file (see section 2). If a
  file you need belongs to the other lane, ask its owner; do not edit it.
- Recheck the code anchors against the current branch and run GitNexus impact
  before editing code.

## 2. Lanes and file ownership

| Lane | Owner | Files |
| --- | --- | --- |
| Backend integration | Claude | `backend/src/main.rs`, `backend/src/db.rs`, `backend/src/handlers/api.rs`, `backend/src/stream_bridge.rs` and backend leaves assigned to Claude |
| Ops | Codex | `.github/workflows/**`, deployment scripts, nginx, systemd and Liquidsoap configuration |
| Setup | as assigned on the epic | Rule, template, lane-map and guard files, per the epic's setup comment |

Split and shared work:

- **O1.2.5** is split when it starts: Codex owns the deployment workflow, the
  host script and the nginx rule; Claude owns the backend precheck endpoint.
  Agree the endpoint's response and refusal behavior on the issue before
  wiring the two together.
- **P1 and O1.1** use one shared production inventory, not two. Missing
  operator decisions and host observations stay visibly open in it.

The machine-readable map is `.github/epic-lanes.yml` once it exists. Until
then, this table and the assignments on the epic apply. New assignments are
recorded on the epic before anyone edits.

## 3. Branches and PRs

- Branch `<type>/<issue>-<slug>` from `dev/streaming-architecture`, in its own
  worktree.
- Every feature PR targets `dev/streaming-architecture`. Never target `main`.
  Only two kinds of PR target `main`: release PRs from
  `dev/streaming-architecture`, and the one approved `epic-guard` setup PR
  from Codex's branch `ci/312-epic-guard` (Anton's exception before the first
  release). A `main` hotfix needs Anton's approval; the operator then sets
  `CLAUDE_ALLOW_MAIN_PR=1` in the hook environment (for example
  `.claude/settings.local.json` under `env`). An inline assignment on the
  command does not work.
- Run `gh pr create` and `gh pr merge` as one plain `gh` command: no wrappers,
  shell operators, substitutions or flags before the subcommand. Write short
  options with a separate value (`-B main`, not `-Bmain`). A PR body may
  come from a file or a heredoc with a quoted delimiter (`<<'EOF'`). Do not
  create or merge PRs through `gh api` or the GitHub MCP merge tool.
- Use the PR template. Its body has one line each, at the start of the line:
  `Epic:` (the epic URL), `Executor:` (Claude or Codex), `Lane:`, `Reviewer:`
  (the other agent), `Leaf IDs:` (or `setup`) and `Issue:` (the issue this PR
  implements). Only the `Issue:` line links a PR to its work item; other issue
  links, such as dependencies, do not. Both
  agents use one GitHub account, so `Executor:` is how the loop (section 8) and
  the `epic-guard` check tell whose PR it is.
- Open the PR as a draft while working. Mark it ready (`gh pr ready`) only when
  it is ready for review; drafts are not reviewed.
- Keep a feature and its tests in the same PR. Link evidence on the issue.
- Before a PR is ready, every suite its files need (the table under "Test
  proof" in section 8) is green on its head. A red test blocks the PR, also
  when it fails on the base too: there is no base comparison. Fix it in the
  PR, or open a ticket and get Anton's approval before you go on.
- While a release is pending, merge only work that belongs to it. Later-wave
  work waits on its branch. Documentation-only changes may merge.

## 4. Review

- The other agent reviews every PR: Codex reviews Claude's PRs, Claude reviews
  Codex's PRs.
- Review the PR's actual head commit, not a local copy.
- The verdict is a standalone comment whose whole body is exactly one line:
  - `Review: APPROVED by Codex at <40-char head SHA>`
  - `Review: CHANGES REQUESTED by Codex at <40-char head SHA>`
  - (`Claude` in the same form for Codex's PRs.)
- Put findings in separate comments.
- **Never write the other agent's verdict.** Both agents share one GitHub
  account, so GitHub cannot stop this; it is a hard rule.
- Do not edit or delete a verdict. A changed verdict counts as no verdict.
- Every new push needs a new verdict for the new head SHA.

Body review of a Refinement ticket (read by the Tickets dashboard; who
writes it is decided in a later ticket):

- A standalone issue comment whose whole body is exactly one line, with no
  trailing newline:
  - `Body review: APPROVED <digest>`
  - `Body review: CHANGES REQUESTED <digest>`
- `<digest>` is the first 12 hex characters of the SHA-256 of the raw issue
  body (no normalization). Any body edit needs a new review.
- Edited comments do not count. The newest valid one wins. The body counts as
  reviewed when it is `APPROVED` and its digest matches the current body.

## 5. Merge

A feature PR into `dev/streaming-architecture` may be merged by its author when
all of this holds:

1. It targets the right branch and changes only files in its lane.
2. The latest verdict from the other agent is `APPROVED` for the current head
   SHA, with no later `CHANGES REQUESTED`.
3. All required checks are green for that head.

Merge with the expected head: `gh pr merge <n> --squash --match-head-commit <sha>`.
A mismatch aborts; get a new review.

Release PRs to `main` and every production action need Anton's approval and
the evidence the plan requires. A merged PR is never activation evidence.

## 6. After merge

- Record commit, tests and result on the issue. Tick a leaf only when its
  evidence is attached.
- Close an issue only when all its leaves have evidence. PRs into
  `dev/streaming-architecture` do not close issues automatically.

## 7. Enforcement

Status on 2026-09-28. Design agreed; parts are still pending.

| Layer | Status |
| --- | --- |
| Branch protection on `main` and `dev/streaming-architecture` | Active since 2026-09-27, but it requires an approving review that the agents cannot give. Requested: 0 required approvals on `dev/streaming-architecture`. Until then see section 0 |
| Required `epic-guard` check (reads rules and lane map from the target branch, never from the PR) | Pending: Codex's setup PR `ci/312-epic-guard` to `main` |
| Shared review and merge checker (lane map, latest counterpart verdict for the real head, green checks) | Pending: Codex's setup work |
| Claude local hook `.claude/hooks/scripts/epic-guard.sh` | Installed with this file. It accepts PR create/merge only as one plain `gh` command and checks: no verdict in Codex's name, PR base, and `--match-head-commit` with a 40-char SHA. It is a guard against mistakes, not a security boundary. It does not check lanes, the actual approval or checks yet; it will call the shared checker once that exists |
| Codex local hook `.codex/hooks/scripts/epic-guard.sh` | Merged. Active only after it is trusted with `/hooks` in each Codex worktree |
| Loop decision script `scripts/epic/next_action.py` | Read-only; picks one action per tick (section 8). Tested per priority row |
| Separate agent identities | Deferred until needed. Until then the verdict author is self-declared |

Until the pending layers exist, the author checks lanes, the counterpart
verdict for the current head and green checks by hand before merging.

## 8. Autonomous loop

Each agent works in ticks. A tick runs
`python3 scripts/epic/next_action.py --agent <claude|codex>`, does exactly the
one action it prints, and ends. All state is on GitHub, so ticks can stop and
restart at any time.

Priority, first match wins:

1. `stop`: the pause file `~/.epic-pause` exists. Anton creates or removes it.
2. `escalate`: the other agent requested changes for the current head for the
   third time on this PR. Add label `needs-anton`, summarise the open question,
   and stop working on that PR. The loop skips PRs and issues with that label.
3. `merge`: the other agent approved the current head and checks are green.
4. `fix`: the other agent requested changes for the current head.
5. `fix-checks`, then `resolve-conflict`, for the agent's own PRs. The
   `epic-guard` status reports waiting (draft, missing verdict, running
   checks) as pending, so the loop waits on it; a failed guard is a real
   break and gets `fix-checks`. On a PR that GitHub reports as conflicting,
   `resolve-conflict` comes before `fix` and `fix-checks` (only `escalate`
   comes first): work on a conflicted head is redone after the rebase.
6. `review`: the other agent's ready (non-draft) PR has no verdict from this
   agent for its current head. A conflicting PR gets no review; the status
   shows it as waiting for the owner to resolve the conflict. A PR whose head
   has failed checks (a failed `epic-guard` included) gets no review either:
   the owner's `fix-checks` changes the head first. Mergeability
   `UNKNOWN` (GitHub is still computing it) does not block a review; a failed
   or malformed read is an error, never `UNKNOWN`.
7. `continue`: the agent's draft PR, or its In progress issue without a PR.
   Not while it is waiting (see Waiting work below).
8. `adopt`: a focus PR with no owner line (`Executor:`, `Author:` or
   `Reviewer:`, with any value) whose files route to this agent (see Routing below). The agent
   adds the `Epic:`, `Executor:`, `Lane:`, `Reviewer:`, `Leaf IDs:` and `Issue:`
   lines and keeps the rest of the body. A PR with any owner line is never
   adopted.
9. `claim`: a Ready issue with Executor set to this agent. Not while the agent
   has work to continue or two open PRs.
10. `idle`.

New actions: `adopt` (and later actions of
https://github.com/phaabe/live.moafunk.de/issues/487) are emitted only when
the comma list `EPIC_FOCUS_ACTIONS` names them, for example `adopt`. An action
is added to the list of both runners only after both its Claude and its Codex
leaves are merged.

Routing, for an item without an owner (`scripts/epic/routing.py`):
an existing Executor (project field or PR line) always wins. Otherwise each
file's owner comes from `file_rules` in `.github/epic-lanes.yml` (first
matching pattern; a renamed file counts with its old and new path). A board
Executor counts only for a PR of this repository. One agent owns all files: that agent. Owners differ, a file
has no rule, or the files share no lane: `needs-anton`, no action. No files
known yet (an issue before refinement): Claude, for `refine` only.

Order inside one step: priority label first (`priority::high`, then
`priority::medium` or no priority label, then `priority::low`; with several,
the highest counts), then the oldest (lowest PR or issue number). A PR takes
the highest priority of its own labels and its `Issue:` tickets. `continue` is
one queue for draft PRs and In progress issues. Claims sort by priority, then
lowest wave, then number. Priority only orders work: it never skips a step and
never bypasses pause, escalation, focus, "Start after", the batch order, lanes
or the two-open-PR limit. `--status` shows each item's priority.

Focus: Anton can limit both loops to some labels by writing them into
`~/.epic-focus`, one per line (for example `project::Stream`). Then only issues
with one of these labels, and PRs whose own labels or `Issue:` ticket have one,
get actions. A PR with a focus label of its own is in focus without an `Issue:`
line. All other PRs are frozen: no review, fix or merge. They still count
toward the two-open-PR limit. No file or an empty file means all work, and no
`adopt`. Focus issues that are not on the project board are found by a REST
search per label (`search/issues`, all pages, no duplicates; a stored quota
wait stops it). `--status` lists every focus item without an action and why:
no owner (with the routing result), draft of the other agent, not on board,
blocked (status, other work, two open PRs), `needs-anton`.

Per-target lock: every runner on this machine takes an `flock` on one file per
issue or PR number in `~/.local/state/epic-loop/target-locks/` (override:
`EPIC_LOCK_DIR`; it does not follow `EPIC_STATE_DIR`). An action with a PR and
an issue locks both, lowest number first. The tick opens the lock files on
descriptors 8 and 9 and keeps them until it ends; its children inherit them, so
a killed tick keeps the lock until its model session has exited too. The OS
frees the lock when the last holder dies: no stale lock, no age reclaim. Lock
files are never deleted. After taking the lock, `tick_gate.py check` rechecks
the start state: a target that was closed or changed (`updated_at`) since
selection is skipped.

Fairness: the selector lists all actions in order (`--candidates`). The tick
runs the first one whose target is free, unchanged and no suppressed repeat.
It tries every candidate, with no cap, so later ones are never starved. A
repeat record is kept per target, so one blocked target never blocks the
others.

Claude runs ticks headless with `scripts/epic/claude-tick.sh` (or `/epic-tick`
by hand). Codex runs the same script from its own runner. A runner starts a
model session only for real work: pause, idle, stop and a repeat of the last
no-op action (`scripts/epic/tick_gate.py`: same action, no change on GitHub,
younger than 3 hours) start none. Claims follow the "Start after" lines and the
order in the epic's batch table ("Scope, in order"; "then" or an arrow starts
the next stage). A "Start after" line names leaf IDs or ticket URLs. Write it
only in a readiness comment: a comment whose body starts with `**Ready` or
`Ready` (for example `**Ready, executor Claude:** ... Start after B1.1.6.`).
"Start after" in any other comment (review, discussion) is ignored. `--status`
shows which readiness comment named each dependency. A leaf counts as done when a merged PR lists it in `Leaf IDs:` or its box is ticked.
So Anton can set a whole queue to Ready at once and let each readiness comment
name the ticket before it.

When a ticket counts as done depends on the switch
`EPIC_REQUIRE_COMPLETED_TICKETS` (temporary; removing it is later work):

- Unset or `0` (default, old rule): a merged PR names the ticket in its
  `Issue:` line.
- `1`: the ticket's issue is closed now with reason `completed`. A merged PR or
  board Status Done is not enough. Open, reopened, not planned, duplicate
  (its target is not followed) or an unknown reason blocks. Board membership is
  not needed: a ticket off the board or archived is read through the REST
  issue endpoint, once per snapshot. A 404 or 410, or an issue moved to another
  repository, blocks only the tickets that start after it; fix the "Start
  after" URL. Auth errors, 5xx and rate limits stop the whole tick (exit 5 or
  the quota wait). Reopening blocks new claims and their fresh check; work
  already In progress continues. Closed issues left Ready or In progress get no
  claim or continue action. A board Status that disagrees with the issue state
  is a warning in `--status` and the monitor, never a blocker.
- Any other value: exit 2 before any GitHub read.

Selection, the fresh check before the model and the write checks read the same
switch from the runner's environment. `--status` prints the mode. The monitor
uses the switch from its own environment (`monitor.py` passes it to its
`--fetch-state` child) and exports `epic_completed_tickets_rule` and
`epic_dependency_warning_info`. Activation, only after Anton records the
rollout audit on https://github.com/phaabe/live.moafunk.de/issues/520:
create the pause file, set `EPIC_REQUIRE_COMPLETED_TICKETS=1` in both runner
environments and the monitor's, check that `--status` in each shows `on`, then
remove the pause file. Never change the mode inside a tick.

Waiting work: a draft PR or an In progress issue without a PR can wait
without blocking new claims. Only its owner or Anton marks it, in this order:

1. Post a new comment, exactly three lines:

   ```text
   Waiting: Claude
   Reason: <one line>
   Resume after: <full issue URL>, <full issue URL>
   ```

   The first line names the writer (`Claude`, `Codex` or `Anton`). For a
   wait only Anton can end, the last line is `Resume: Anton`.
2. Add the label `waiting` (color `FBCA04`) to the issue or the PR. A PR also
   waits through the label on its `Issue:` tickets, each with its own comment.

The selector reads the newest comment that starts with `Waiting:` on each
labeled source. It never falls back to an older one: to change a wait, post a
new comment; an edited one counts as invalid. Waiting work gets a status-only
`wait` entry (`--status`, monitor) and starts no model. All its sources must
resolve before it continues.

- A `Resume after:` wait ends when every named ticket is done under the active
  "Start after" rule (both `EPIC_REQUIRE_COMPLETED_TICKETS` modes). The label
  may stay; `--status` then shows a warning. If a ticket is reopened while the
  label is still there, the work waits again.
- A `Resume: Anton` wait ends only when Anton removes the label from its
  source. A PR label does not clear a label on its issue.
- A label with a missing, edited or malformed comment, or a comment by someone
  other than the owner or Anton, parks that work and shows the problem.
- Removing the label by hand ends any wait. Nothing removes it automatically.
- A ready (non-draft) PR ignores the label: it keeps review, fix, checks,
  conflict and merge actions, with a warning.
- Waiting work keeps its Status, PR slot, worktree and reservations. Capacity,
  focus, pause, quota, lanes and priority still apply.

Waiting work frees claims only with `EPIC_SHARED_READER=1`, because only then
does each runner recheck the claim fresh before the model. The recheck reads
every labeled draft PR and In progress issue, their comments and dependency
evidence again; a wait that ended after selection makes the claim stale. With
the switch off, waiting work still starts no model, but claims stay held as
before. A comment read that fails stops the tick (exit 5); it never counts as
a missing comment.

`EPIC_SHARED_READER` has one resolver, `github_state.enabled()`. Unset or
empty means the default in `github_state.DEFAULT_ENABLED` (off today), `0` is
off and `1` is on. Any other value is a configuration error (exit 2). The
runner, standalone selection and `monitor.py --fetch-state` all use it. Each
runner resolves the switch once per tick (`github_state.py resolve`) and exports
an explicit `0` or `1` to every child. When on, it also exports the recheck
budget. A runner child (an action file is set) never applies the default: a
missing, empty or invalid value refuses every write in the hook and in the
permission gate. Without an action file, hooks behave as before. To check a
fresh runner process, read its log: each tick prints
`tick: shared reader=<0|1> recheck=<N>s`, and `tick: <action> fresh check
passed` before the model when on. Run `python3 scripts/epic/github_state.py
resolve` in the runner's environment to see the value the next tick uses.

Each runner checkout only runs ticks and holds no work. Every
tick starts with `git pull --ff-only` there, so merged changes to
`scripts/epic/`, `.claude/commands/epic/` or `.codex/` apply on the next tick;
a failed pull stops the tick. Before the pull, `claude-tick.sh` runs
`scripts/epic/gitnexus_noise.py`: it restores changes that sit only inside the
GitNexus block of `AGENTS.md` / `CLAUDE.md`, and stops the tick on any other
tracked change without touching it. A headless session cannot answer permission
prompts. The project settings ask before every push, rebase and merge; the
runner-only settings `scripts/epic/claude-runner-settings.json` (passed with
`--settings`; the shared `.claude/settings.json` is unchanged) also ask before
every `git -<option> ...`, so `git -C <path> push` and `git -c k=v push` cannot
skip the prompt. `claude-tick.sh` hands those prompts to
`scripts/epic/permission_gate.py` and sets `GIT_EDITOR=true`. `G` is
`git -C <W>` or `git -C<W>` (exactly one `-C`, no other global option), `W` the
action's runner worktree `<dir>/<B>` and `B` its feature branch (`feat/`, `fix/`,
`chore/`, `docs/`, `test/`, `refactor/`). The runner writes the action's
context (branch, base, PR, worktree) before the model starts
(`runner_worktree.py --context-file`); missing or stale context refuses. The
gate approves only (`scripts/epic/git_gate.py`):

- `G push [-u] [-q] origin B`, also before a PR exists (claimed issue branch).
- `G rebase [-q] origin/<base>`: a PR action (`fix`, `fix-checks`,
  `resolve-conflict`, `continue`); the PR is open, its head is `B` in this
  repository, its base is `<base>` and an epic base, `Executor: Claude`, and its
  head still equals the action's `sha`. The tree is clean and no other
  operation (rebase, merge, cherry-pick, revert, bisect) is in progress. The
  gate pins that head as `S` in `claude-rebases.json` in the state directory.
- `G rebase --continue` / `--abort`: only the rebase the runner recorded (same
  worktree, branch, onto commit and original head). An approved abort retires
  the record: it approves nothing more, not even a second abort, because a
  rebase started again by hand looks the same. A failed abort needs a human. `--skip` is refused: dropping a commit needs a separate explicit
  decision.
- `G push [-q] --force-with-lease=refs/heads/B:S origin HEAD:refs/heads/B`: the
  recorded `S`, the rebase finished onto the recorded commit, the PR head still
  `S`. The gate never
  renews `S`; if the remote moved, the push fails and the tick stops.
- `G push origin --delete B`: a `merge` tick, after the PR is merged.
- `G add -- <paths>`, `G commit --file <file>`, `G fetch [-q] origin` in `W`.
  `add` and `commit` need `B` checked out; during the recorded rebase only
  `add` works in the detached HEAD.
- Read-only `G status|log|diff|show|rev-parse|ls-files|merge-base` with
  `--short`, `--porcelain`, `--oneline`, `-n <N>`, `-<N>`, `--stat`,
  `--name-only`, `--name-status`, `--no-color`, `--abbrev-ref`,
  `--show-toplevel`; revisions `HEAD`, a full SHA, `origin/<epic base or
  feature branch>` and `A..B` / `A...B`; paths after `--` inside the worktree.
  Also in the runner checkout or another worktree under `<dir>`.
- `gh pr merge <n> --repo phaabe/live.moafunk.de --squash [--delete-branch]
  --match-head-commit <sha>`, and, only in an `adopt` tick for PR `<n>`,
  `gh api --method PATCH repos/phaabe/live.moafunk.de/pulls/<n> -F
  body=@<file>`. `<file>` is an absolute path to a regular file directly in
  the tick's `EPIC_BODY_DIR` (a fresh temp directory the runner creates for
  `adopt` and removes after the tick), not a symlink or hard link. The gate
  checks the directory by the device and inode the runner recorded
  (`EPIC_BODY_DIR_ID`), so a replaced directory does not match.

Every `W` must really be `<dir>/B` (no symlink below `<dir>`; the runner's
worktree step refuses one too), be a worktree of the runner checkout, and have
this repository as every origin fetch and push URL: the full GitHub URL
(`git@github.com:`, `https://github.com/` or `ssh://git@github.com/`, after
`insteadOf` rewrites), not only a matching path. Plain `git push` and
`git rebase` are refused: the gate cannot see the shell's directory. It also
refuses `-c`, repeated `-C`, other global options, force flags, `+` refspecs,
bare or other leases, extra refspecs, other remotes, tags, `--all`,
`--mirror`, protected branches, `rebase -i`/`--exec`/`--onto`, shell chains,
substitutions, inline environment assignments and wrappers. It logs each
decision to `claude-permissions.log` in the state directory. A refused push or
an unfinished rebase fails verification: `tick_verify.py --worktree` counts a
moved PR head only when it is the worktree's HEAD with no rebase left, so
another writer's push does not count. The repeat gate then suppresses it until
the PR changes or the repeat TTL ends; the next tick resumes an unfinished
rebase in the same worktree.
`python3 scripts/epic/next_action.py --status`
shows the queue for both agents.

Rebase policy (`scripts/epic/rebase_policy.py`, shared by both runners). Only a
PR that GitHub reports as `CONFLICTING` gets `resolve-conflict`; a PR that is
only behind its base is not rebased. Three values of one attempt stay apart:

- **Old head**: the PR head before the rebase. It is also the lease value,
  `--force-with-lease=refs/heads/<branch>:<old head>`, after local and remote
  agree on it.
- **Target tip**: the base tip the runner pins for this attempt before the
  model starts. It is the rebase destination and part of the attempt key. The
  gate approves the rebase, `--continue` and the lease push only onto it: a
  rebase left by an earlier tick onto an older tip is aborted and started
  again. It is never the lease value.
- **Old series base**: `git merge-base <old head> <target tip>`, the base the
  reviewed patch series was built on.

Test proof. After the rebase and before the push, the owner runs
`rebase_policy.py prove` from the runner checkout. It needs a clean worktree
and index (no staged, unstaged or untracked change) before and after the
tests, runs the suites the PR's files require (the files changed from the
target tip to `HEAD`), and writes the proof (commit, tree, target tip,
commands, results) to `rebase-proofs/` in the state directory. A failed or
skipped suite (command not found) gives no valid proof. The gate refuses every
lease push unless the proof is for the current `HEAD` commit and tree on the
recorded onto commit, lists every required suite as passed, and the worktree
is still clean. A later commit or an uncommitted edit makes the proof invalid.

| Changed path | Suite | Command (in the worktree) |
| --- | --- | --- |
| `scripts/epic/**` | epic | `python3 scripts/epic/run_tests.py scripts/epic` |
| `.codex/**` | codex | `python3 scripts/epic/run_tests.py .codex/tests` |
| `scripts/epic_guard/**`, `.claude/hooks/**` | epic-guard | `python3 -m unittest discover -s scripts/epic_guard` |
| `scripts/gh_checks/**` | gh-checks | `python3 -m unittest discover -s scripts/gh_checks` |
| `backend/**` | backend | `cargo test --locked` in `backend/` |
| `frontend/**` | frontend | `npm test -- --run` in `frontend/` |

Other paths need no suite. `EPIC_REBASE_SUITES` (runner environment) may name a
JSON file with another table of the same shape.

`run_tests.py` runs a test directory in parallel parts (`-j`, default: CPU
count). Each part runs under `isolated_env.py`, like a serial run. A module
with more than 20 tests is split into parts of at most 15; a module with
`load_tests`, `setUpClass` or `setUpModule` runs whole. The run fails when a
test fails, a part fails to load, or a listed test does not run exactly once.
All runs on the machine share `EPIC_TEST_SLOTS` slots (default: CPU count), so
parallel suites of both runners and manual sessions wait instead of
overloading the machine; a part's timeout starts once it has its slot.
The serial run is still `python3 scripts/epic/isolated_env.py <dir>`.

Rebase record. After the model's session, the owner's runner posts one
comment for a proven push (`rebase_policy.py publish`), at most once per new
head. Exact body, no verdict, no trailing newline:

```text
Rebase record by <Owner>
Repository: phaabe/live.moafunk.de
PR: <number>
Old head: <40-char SHA>
New head: <40-char SHA>
Old series base: <40-char SHA>
Target tip: <40-char SHA>
Conflicted files: <paths added during the rebase, ", "-separated, or none>
Proof: <commit> tree <tree>; <suite>=passed, ... (or "no suites required")
```

For `resolve-conflict`, `tick_verify.py` needs a moved head, a valid proof for
the new head and a valid record posted during the tick. A moved head alone is
no success. This applies to the Claude runner now and to the Codex runner once
https://github.com/phaabe/live.moafunk.de/issues/537 lands.

Focused re-review. Before a `review`, the reviewer's runner computes the scope
(`rebase_policy.py scope`) and appends it to the prompt. It is `focused` only
when a valid record exists for the current head, its old head is the head of
the reviewer's last verdict on this PR, the record's SHAs and ancestry check
out locally (repository, PR, old series base = merge-base of old head and
target tip, new head built on the target tip), all old objects are present,
and the base has not advanced past the target tip since. The focused scope is:
(1) `git range-diff` of the old and new patch series; (2) the base changes
from the old series base to the target tip that touch the PR's files or
symbols the PR calls or is called by; (3) the conflicted files; (4) every
finding since the reviewer's last approval, when the last verdict requested
changes (a later review need not repeat a finding for it to stay open). Anything else,
or unclear impact, means a full review. The verdict still names the new head
in the exact format of section 4.

Attempt limit. One store for both runners, `rebase-attempts.json` in the
shared state directory, keyed by PR, head and target tip. Each started
`resolve-conflict` attempt counts once, also when it fails or times out; only a
landed one does not count, and one whose model reported the GitHub quota is
void. Pause, quota or read waits and lock skips start no attempt. Cooldown
expiry and comments do not reset the count; only a new head or a new target
tip does. At `EPIC_REBASE_ATTEMPT_LIMIT` counted attempts (default 2) no runner
starts a model for that key, and the owner's runner adds the label
`needs-anton`. If that post fails, the key stays suppressed and only the label
post is retried, without a model. The Claude cooldown (`tick_cooldown.py`) is
separate. The Codex runner uses the same store once
https://github.com/phaabe/live.moafunk.de/issues/537 lands; until then a
reviewer without a valid record simply does a full review.

Runner worktrees: the Claude runner edits feature branches only in
`<dir>/<branch>`, where `<dir>` is one fixed directory per runner
(`EPIC_WORKTREE_DIR`, default `live.moafunk.de-<agent id>-wt` next to the runner
checkout). For `claim`, `continue`, `fix`, `fix-checks` and `resolve-conflict`,
`scripts/epic/runner_worktree.py` runs after the gate check and before the
model. It checks the repository, the branch (a PR's head in this repository,
a feature branch, an epic base, `Executor: Claude`; for an issue its one
`<type>/<issue>-...` branch or a new `feat/<issue>-<slug>` from
`origin/dev/312-interim`). Then it resumes the matching checkout unchanged or
runs `git worktree add`. The model gets the path and may edit only there.
When Git refuses a branch checked out elsewhere (for example a human session),
no model starts and the log says `handoff needed: <branch> in <path>`. The
same stop for the same action is reported once
(`<agent>-handoff.json`, repeat TTL). Handoff is manual: Anton releases that
checkout, and a later tick finds the branch free and creates the runner
checkout. Nobody uses `--force` or `--ignore-other-worktrees`, or switches,
resets, stashes or removes another session's checkout.

GitHub GraphQL quota: both runners share one wait file,
`github-quota-wait.json` in the state directory (`scripts/epic/github_quota.py`).
When a GitHub read hits the GraphQL quota, also inside an HTTP 200 response,
the script stores the reset time (UTC, from one `rateLimit { resetAt }` query,
never from REST `rate_limit`) and exits 4; the runner ends the tick with exit 75
and starts no model. Until reset plus 60 seconds, a tick makes zero GitHub calls
and starts zero models; it logs the retry time. If the reset time is unknown,
the wait is 15 minutes. A quota wait writes no cooldown or repeat-gate record.
Runners call `github_quota.py check --state-dir <dir>` before each GitHub read
and before starting a model (exit 0 proceed, 3 deferred, 2 bad file), because
the other runner can store a wait at any time, and `record` to store a wait.
The loop never releases to `main`, touches production, or changes the plan or
lanes; those go to Anton.
