# Pinned runner runtime

Contract for running the Claude and Codex runners from one tested runtime
revision (https://github.com/phaabe/live.moafunk.de/issues/583). This file
covers the shared part (https://github.com/phaabe/live.moafunk.de/issues/584).
The adapters wire it in: Claude
https://github.com/phaabe/live.moafunk.de/issues/585, Codex
https://github.com/phaabe/live.moafunk.de/issues/586. Promotion and recovery:
https://github.com/phaabe/live.moafunk.de/issues/587.

Code: `scripts/epic/runtime.py` (contract version `CONTRACT = 1`),
`scripts/epic/lockhold`, `scripts/epic/epic-tick`, `scripts/epic/smoke.py`.
Pinned mode stays off until bootstrap writes `configured.json`.

## Roots

| Root | Variable | Meaning |
| --- | --- | --- |
| Runtime root | `EPIC_RUNTIME_ROOT` | The pinned install. All runner code, settings, schemas, prompts and hooks load from here. Changes only on promotion. |
| Repo root | `EPIC_TRUSTED_ROOT` | The runner's git checkout. Git only: fetch, pull, worktree registration and the `git_gate.py` worktree checks. No runner code runs from it. |

Worktree, lock and state paths keep their configured locations
(`EPIC_STATE_DIR`, `EPIC_LOCK_DIR`, `EPIC_WORKTREE_DIR` or the folder next to
the checkout). Promotion never moves or rewrites them.

Code loaders that import from a root (`write_checks` in the Claude hook,
`github_state.trusted_root()` for the merge guard and lane policy) use
`runtime.code_root()`: `EPIC_RUNTIME_ROOT` when set; refused when pinned mode
is configured without it; otherwise today's fallback (legacy mode).

<details>
<summary>Assignment of every current repo-root use</summary>

`claude-tick.sh` (adapter https://github.com/phaabe/live.moafunk.de/issues/585):

| Use | Root |
| --- | --- |
| `repo_root` from the script's own path (line 72) | runtime root for code; the repo root comes from configuration |
| default `worktree_dir` next to the checkout (82) | repo root (path unchanged) |
| `tick_events.py`, `agents.py`, `github_quota.py`, `.codex/epic_lock.py` (105-185) | runtime |
| `--launch-dir` for the transcript path (205) | repo root (the session's cwd) |
| `cd "$repo_root"` and `git pull --ff-only` (266-275) | repo root, git only |
| relative helper calls after `cd` (`next_action`, `target_lock`, `tick_gate`, `runner_worktree`, `rebase_policy`, `tick_cooldown`, `tick_verify`, `gitnexus_noise`) | runtime (absolute paths) |
| `runner_worktree.py --repo` (459), attempt prompt (503), `--repo-dir` (511) | repo root |
| `permission_gate.py` path in the gate config (598) | runtime |
| `EPIC_TRUSTED_ROOT` for the gate and the model (586, 606) | repo root |
| `--settings`, `--json-schema` (610-613) | runtime |

`codex-tick.sh` (adapter https://github.com/phaabe/live.moafunk.de/issues/586):

| Use | Root |
| --- | --- |
| `repo_root` from the script's own path (line 5) | runtime root for code; the repo root comes from configuration |
| `tick_events.py`, `scripts/epic` imports, `agents.py`, `epic_lock.py`, `github_quota.py`, `review_worktree.py` (37-201) | runtime |
| `--runner "$repo_root"` arguments (105, 344, 488, 545) | repo root |
| `cd "$repo_root"` and the pull (221-241) | repo root, git only |
| `model_root` default and worktree switch (311, 491, 549) | repo root or the action's worktree |
| rebase attempt helper argument (394) | repo root |
| `EPIC_TRUSTED_ROOT` export and model variables (588, 611) | repo root |
| prompt worktree text (642-646) | repo root |
| installed feature helper, `--repo-dir "$model_root"` (656-666) | helper code: runtime; directory: repo root or worktree |
| `codex exec --cd "$model_root"` (703) | worktree or repo root |
| `--output-schema`, `.codex` result and publish helpers (706-854) | runtime |

Line numbers: `dev/312-interim` at `da308d5`.

</details>

## Install home

`EPIC_RUNTIME_HOME`, default `~/.local/share/epic-runtime`, outside every checkout.

| Path | Content |
| --- | --- |
| `revisions/<sha>/` | One install per revision. Order: `git archive <sha>`, copy the pinned executables and their resource files, write the manifest, make everything read-only. Never changed after its manifest is written. Old installs stay. |
| `revisions/<sha>/runtime-manifest.json` | Manifest (below). |
| `pin.json` | `{schema: 1, revision, manifest_sha256, previous: {revision, manifest_sha256} \| null, promotion_id, promoted_at}`. Written atomically by the promoter. |
| `configured.json` | Written once at bootstrap. No runner command removes it. |
| `bin/epic-tick` | Launcher, copied once at bootstrap. |

### Manifest

`schema` (1), `revision` (40-hex), `contract` (int), `created_at`, and:

- `files`: install-relative path → SHA-256 of every file, except the manifest itself (the pin holds its hash).
- `executables`: `claude`, `codex`, `codex_launcher`, `gitnexus`, `python3`, `gtimeout` → `{path, version, sha256}`, absolute paths. Model binaries are copied into the install.
- `helpers`: installed Git helper path → `{sha256, config: null | {path, sha256, values}}`. `values` binds keys of the helper's JSON config.
- `settings`: name → SHA-256 of a fixed install path (`runtime.SETTINGS`).

`runtime.py validate --manifest F` prints `{ok, failures: [{item, expected, actual}]}`.
Exit 0 ok, 1 mismatch, 2 unreadable or unknown schema. Mismatches: a changed,
missing or extra file, a symlink or other non-regular file, a writable path,
a changed executable, helper, helper config value or settings file, and a
different `contract`.

### Entry modes

- **Pinned:** launchd runs `<home>/bin/epic-tick claude|codex`. The launcher
  reads the pin once, checks the manifest hash against the pin, the
  validator and `python3` against the manifest, runs the validator, then
  execs the install's tick entry with `EPIC_RUNTIME_ROOT`,
  `EPIC_RUNTIME_REVISION`, `EPIC_RUNTIME_MANIFEST` and `EPIC_RUNTIME_HOME`.
  It has no legacy path: any failure exits 78 before a model or write.
- **Legacy:** only with `EPIC_RUNTIME_LEGACY=1` in the launchd job, and only
  while neither `configured.json` nor a pin exists. `runtime.py mode` checks
  this; tick entries call it before anything else (adapters).
- Anything else exits 78.

## Admission lock

Files in `EPIC_LOCK_DIR` (shared by all runners):

| File | Use |
| --- | --- |
| `runtime.lock` | Ticks and commands `LOCK_SH`; promotion `LOCK_EX`. Never deleted. |
| `runtime-promotion.json` | Promotion marker. |
| `admitted/<tick_id>.json` | `{tick_id, agent, revision, processes: [{pid, start}]}` of an admitted tick. |

Descriptors: fd 17 in the tick shell, fd 29 in a command prefix. Target
locks keep fd 8/9; releasing them (`exec 8>&- 9>&-`) leaves admission held.

Tick admission (`runtime.py admit --fd 17 --tick-id ID --agent A --pid $$`):

1. `LOCK_SH` on fd 17 without waiting; busy → exit 75.
2. Write the admission record with the tick shell's pid and start time.
3. Check the marker; present or unreadable → remove the record, exit 75.

The tick keeps fd 17 open through verification, review delivery and exit
cleanup, then runs `runtime.py release --tick-id ID`. Records of ticks that
died without cleanup are removed at the next admission (housekeeping only).

**Children (shared rule):** every child that can write holds the admission
lock, inherited or its own `LOCK_SH`. Each adapter picks and proves its
mechanism. Claude: `CLAUDE_CODE_SHELL_PREFIX=<runtime>/scripts/epic/lockhold`
gives every hook, MCP server and tool command its own `LOCK_SH` on fd 29.
A broken prefix makes Claude hooks fail open, so the tick validates the
prefix against the manifest before the model starts.

Known limit: a child that calls `setsid` and closes its inherited
descriptors escapes any descriptor lock
(https://github.com/phaabe/live.moafunk.de/issues/589).

## Promotion marker and write barrier

`runtime.begin_promotion(candidate, previous)` creates the marker
(`O_EXCL`; an existing marker refuses), phase `prepared`, then snapshots the
admission records into `marker.admitted`. Because a tick writes its record
before it checks the marker, every tick that passed the check is in the
snapshot. `set_phase` and `clear_marker` act only on the marker with the
caller's `promotion_id`. The marker stays after a promoter crash, so admission
stays blocked until `recover` (https://github.com/phaabe/live.moafunk.de/issues/587).

Write barrier (`runtime.write_barrier()`): no marker → allowed. Marker →
allowed only when the caller or one of its ancestors matches an admitted
`(pid, start)` of the snapshot (`ps` start times normalised). The environment
never counts. An unreadable marker or process table refuses. Callers:

- `git_gate.py`: every git command except the read-only ones.
- `permission_gate.py`: `gh pr` and `gh api` prompts.
- `write_checks.promotion_refusal()`: every GitHub or git write, and every
  command that may write but cannot be read; also run first in `guard()`.
- `.claude/hooks/scripts/epic_guard.py` rule 8: every session, also
  interactive. Without a marker it costs one `stat()`.
- `lockhold`: a command outside an admitted tick does not start.

After the admitted tick shell dies, its children no longer match, so their
writes are refused while the marker exists; their lock still keeps the
promotion waiting.

## Smoke check

`<runtime>/scripts/epic/smoke.py --agent claude|codex --manifest <file>`

Prints `{ok, agent, failures}`. Exit 0 pass, 1 fail, 2 usage error or
unreadable manifest. Shared part: the manifest validates and lists the agent's
tick entry. Agent part: `check(install, manifest) -> list[str]` in
`scripts/epic/smoke_claude.py` or `.codex/smoke_codex.py`, provided by each
adapter. A missing or broken part fails. Parts only read, parse and hash: no
model, app server, hook or MCP server.

## Tick events

Raw `start` and `finish` events carry `runtime`: `EPIC_RUNTIME_REVISION`
(40-hex) or null in legacy mode. Event readers ignore unknown keys; tick
checkpoints (`ticks.py`, exact key sets) do not store it.

## State inventory

`R` = registry dir (`EPIC_STATE_DIR`, default `~/.local/state/epic-loop`).
`S` = per-agent dir (`R`, or `R/agents/<id>` with `EPIC_AGENT_ID`).

- **Exact key sets** (a new field breaks the old reader): `S/rebase-<pr>.json`, review `bundle.json` comments, `S/codex-result.json`, `S/feature-git-context.json`, GitHub state snapshots (`SCHEMA = 1`). Changing one needs a migration and blocks a revert.
- **Explicit version, unknown keys ignored:** rebase proofs (`PROOF_VERSION = 1`), raw tick events (`"v": 1`), agent registry (`"v": 1`), review `context.json` (`version == 1`).
- **No version, unknown keys ignored:** quota wait, Claude cooldown, Codex backoff, gate records, handoff, `claude-rebases.json`, `codex-rebases.json`, `rebase-attempts.json`.
- **Pending review delivery** needs `S/reviews/<pr>/<sha>/{context,bundle}.json`, the ref `refs/remotes/codex-review/<pr>/<sha>` and the target lock.
- **Not runner state:** monitor checkpoints `ticks-*.json`, monitor runtime files, `leases/v1`.

A revert to the previous runtime is allowed only when its readers accept all
retained state (one old-reader fixture per row below). Otherwise recovery
moves forward.

<details>
<summary>Full inventory (retained state, <code>dev/312-interim</code> at <code>da308d5</code>)</summary>

| Location | Writer | Reader | Version / keys |
| --- | --- | --- | --- |
| `R/github-quota-wait.json` | `github_quota.record`, `tick_backoff.record`, `review_delivery.api` | `github_quota.read_wait/check`, `feature_worktree.check_quota` | none; needs `retry_at`; extras ignored |
| `R/claude-cooldown.json` + `.lock` | `tick_cooldown.save` | `tick_cooldown.load/check/status` | none; extras ignored |
| `S/codex-backoff.json` | `tick_backoff.save_entries`, `feature_worktree.record_refusal` | `tick_backoff.load_entries`, monitor | none; extras ignored |
| `S/<agent>-gate.json` | `tick_gate.record` (not atomic), `feature_worktree.handoff_record` | `tick_gate.check`, `feature_worktree.repeated_handoff`, monitor | none; extras ignored |
| `S/<agent>-gate-seen.json` | `tick_gate.check` | `tick_gate.record`, `feature_worktree` | none |
| `S/claude-handoff.json` | `runner_worktree.remember` (not atomic) | same | none |
| `S/claude-rebases.json` | `git_gate.save_record` | `git_gate.records`, `rebase_policy.publish` | none; extras ignored |
| `S/codex-rebases.json` | `feature_git.record_publication` | `rebase_policy.publish` | none; extras ignored |
| `S/rebase-<pr>.json` + `.lock` | `feature_git` | `feature_git.load_rebase`, `feature_worktree.prepare` | exact keys |
| `S/rebase-proofs/pr-<n>-<sha>.json` | `rebase_policy.prove`, `rebase_proof.main` | `rebase_policy.load_proof`, `git_gate`, `feature_git` | `PROOF_VERSION = 1`; extras ignored |
| `R/rebase-attempts.json` + `.lock` | `rebase_policy.locked` | same | none; extras ignored |
| `S/codex-result.json` | `codex-tick.sh` | `tick_backoff.result_outcome` | exact keys |
| `S/reviews/<pr>/<sha>/context.json` | `review_worktree.prepare` | `review_delivery`, `review_worktree.load_context` | `version == 1`; extras ignored |
| `S/reviews/<pr>/<sha>/bundle.json` | `review_worktree`, `review_delivery.deliver` | `review_delivery`, `review_worktree.validate_bundle` | comments exact keys |
| `S/reviews/<pr>/<sha>/attempts/`, `archive/` | `review_worktree`, `codex-tick.sh` | operator | none |
| `R/agents/<id>/agent.json` | `agents.register/retire` | `agents.load/discover`, monitor | `"v": 1`; extras ignored |
| `S/<agent>.log`, `S/claude-permissions.log` | tick shell, `permission_gate` | `tick_events`, `ticks`, monitor | text |
| `S/<agent>-ticks.jsonl` | `tick_events.append` | `ticks`, monitor | `"v": 1`; extras ignored |
| `S/<agent>.guard` | `epic_lock.acquire` | same | flock file |
| `$EPIC_LOCK_DIR/<n>.lock` | `target_lock`, fd 8/9 | `target_lock`, `review_delivery.local_gate` | flock file |
| `$EPIC_LOCK_DIR/runtime.lock`, `runtime-promotion.json`, `admitted/` | `runtime.py` | `runtime.py`, promotion | this contract |
| `<shared root>/github-cache/` | `github_state` | `github_state.load_snapshot/load_entry` | `SCHEMA = 1` + key equality |
| feature worktrees, `refs/remotes/codex-review/<pr>/<sha>` | `runner_worktree`, `feature_worktree`, `review_worktree.retain` | same, `git_gate.owned` | git |

Operator-installed and read-only for the runner:
`~/.local/libexec/codex-feature-git.{py,json}` (exact keys),
`~/.local/libexec/codex-cleanup-git.py`, `~/.epic-pause`, `~/.epic-focus`.
Transient tick-lock files are removed at tick end and not needed to resume.

</details>

## Manual sessions

Decided by Anton (2026-10-01): evidence plus operator ack, and the write
barrier where pinned hooks load.

- Relevant: sessions using runner paths (runner checkout, runtime root,
  worktree folders, `EPIC_STATE_DIR`).
- Evidence: no process with a working directory under those paths and no
  holder of the admission lock. Promotion refuses otherwise.
- Ack: a file naming the `promotion_id`, newer than the marker.
- Accepted gap: a manual session in a feature or review worktree loads that
  worktree's hooks, so only evidence and ack apply.
