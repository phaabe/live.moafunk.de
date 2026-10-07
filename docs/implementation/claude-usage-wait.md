# Claude usage wait

Status: https://github.com/phaabe/live.moafunk.de/issues/677. Code:
`scripts/epic/claude_usage.py`, used by `scripts/epic/claude-tick.sh`.

When the Claude CLI reports the account's session limit, the runner stores a
wait for that Claude account. Until the reset, no Claude runner on the same
account starts a model. Transient errors (529 and other API errors) keep the
old behavior: the next tick retries. Codex is not affected.

## Account and store

- `EPIC_CLAUDE_ACCOUNT_KEY` names the account: `[a-zA-Z0-9_-]{1,64}`, default
  `default`. It is only an alias; it does not find or log in to an account.
  Runners on one account use the same key and the same state dir (one launchd
  job, or `EPIC_AGENT_ID` agents under it). Separate accounts set different
  keys.
- Store: `<EPIC_QUOTA_DIR>/claude-usage/<key>/`. The tick sets
  `EPIC_QUOTA_DIR` to the shared state dir, so the live store is
  `~/.local/state/epic-loop/claude-usage/default/`.
- The helper creates missing folders (mode 0700). It refuses an existing
  folder with another mode or owner, a symlink, a group or world writable
  parent (sticky ones excepted) and a bad `state.json` (it must be 0600 with
  one link). It never deletes or rewrites a bad file: a bad store stops the
  tick with exit 1 until it is fixed by hand.

## What counts as a session limit

Only the terminal result that the wrapper itself captured from
`claude -p --output-format json`:

- `type` is `result`, `is_error` is exactly `true`, `terminal_reason` is
  `api_error`, and `session_id` is this admission's session;
- the whole `result` text is the observed message, for example
  `You've hit your session limit · resets 3pm (Europe/Berlin)`.

A quoted message, a summary, zero cost or a 529 alone is not a session limit.
Other limit messages (weekly, per model) are not covered yet.

## How long it waits

- Known reset: the clock time on the receipt's date in the named zone, plus
  60 seconds. The wrapper records the receipt time (UTC) when the CLI exits.
- Unknown reset: unknown zone, an ambiguous or missing local time (clock
  changes), a reset at or before the receipt, or no receipt. Then the
  fallback: 15, then 30, then 60 minutes from the receipt. The runner never
  guesses "tomorrow".
- Only a recovery probe that hits the limit again moves to the next fallback
  step. A wait that has not ended is only extended, never shortened.

## The tick

1. Before selection, `claude_usage.py check` (read only). A wait ends the
   tick with exit 0: no selection, no model.
2. Right before the model (after the GitHub quota check, before
   `attempt-start`), `admit` records the admission under the account lock.
   The admission ID is the session ID (`--session-id`). The tick keeps the
   admission lock (fd 19) and the probe lock (fd 18). The model never gets
   them.
3. Right after the CLI exits, `finish` stores the result, before any check
   that can end the tick. A session limit ends the tick blocked (exit 75,
   phase `usage`) unless the work landed.
4. A wait writes no target cooldown and no repeat-gate record. It starts no
   rebase attempt. A started attempt becomes void only when the CLI refused
   the session before any model work (zero tokens, cost and API time, no
   denied tool call). Otherwise the existing rules apply. Verified work stays
   done.

After the wait the first admission is the only recovery probe. A probe that
completes without an error clears the wait. A probe that ends with another
API error or without a result keeps recovery mode, and the next probe comes
180 seconds later.

## Status and repair

```bash
python3 scripts/epic/claude_usage.py status
```

It shows the reason (`session_limit`), the retry time and every admission.

A tick that was killed (SIGKILL, crash) leaves its admission unresolved, and
no model starts until it is repaired. Such a tick also leaves its
`claude.lock` folder for manual review, as before. After that review:

```bash
python3 scripts/epic/claude_usage.py repair --id <session id>
```

`repair` refuses while the admission's lock is still held. With
`--result-file <path> --receipt <UTC time>` it stores that session's result.
Without them it counts as no result: a probe then waits 180 seconds.

A failed write of a result is an error, not success: the admission stays
unresolved, and the tick exits 1.

## Rollout

No configuration change is needed for the existing single Claude account.
The live runner pulls `dev/312-interim` every tick, so the merge takes
effect on the next tick. The folders appear on the first admission.
