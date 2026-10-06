# Shared test slots

Status: proposed contract for
https://github.com/phaabe/live.moafunk.de/issues/664. The Codex binding is one
part. Claude's shared test runner must also read capacity and fail on slot
errors before activation. Do not enable this from the Codex patch alone.

## Contract

Use one canonical directory for Claude, Codex and manual top-level suites.
The operator creates it, owns it and sets mode 0700. Suggested location:
`/Users/anton/.local/state/epic-loop/test-slots`.

Its `capacity.json` contains exactly `{"schema":1,"slots":4}`. Four is the
proposed operator setting, not a code default. Use a positive integer.
The only other entries allowed are regular, single-link `slot-N.lock` files,
where N starts at zero and is less than capacity. No subdirectories, symlinks
or hardlinks are allowed. Missing lock files may be created by the test runner.
Never unlink or replace the directory, capacity or locks while suites run.

The protected binding may contain:

```json
"test_slots": {
  "directory": "/Users/anton/.local/state/epic-loop/test-slots",
  "count": 4
}
```

Add only that directory to the protected config's static writable roots.
For `epic-source-edit`, add it to the profile's filesystem table with `write`.
For `workspace-write`, append it to `sandbox_workspace_write.writable_roots`.
Do not grant the state parent. Preflight rejects overlap with code, home,
binding directory, existing static grants or allocation parents. Existing
bindings without `test_slots` keep their old behavior unless slot environment
variables are supplied; unbound overrides are refused.

Codex forwards `EPIC_TEST_SLOTS_DIR` and `EPIC_TEST_SLOTS` from the validated
binding into tool commands. Inherited values, when present, must match exactly.
Different per-tick `TMPDIR` values do not change the shared directory.

Claude's shared runner reads capacity before starting parts. An explicit
`EPIC_TEST_SLOTS`, if present, must match capacity. Invalid configuration or
failed lock access stops a required top-level suite. Nested disposable runners
keep their existing no-slot behavior. Manual callers without an explicit
shared directory retain the local default, so they are not covered by this
machine-wide contract until they use the shared settings.

This coordinates cooperating test processes. It does not prevent an operator
from replacing a lock file or an arbitrary process from ignoring the protocol.

## Operator rollout after both reviewed parts merge

Drain both runners and stop all manual suites first. Keep the loop paused
through preflight. Deploy only the reviewed code. Back up both runner plists,
the protected config and binding using the existing rollout procedure.
Retain those exact paths for rollback.

Create a new directory; refuse to reuse an unknown directory:

```bash
(
set -euo pipefail
slot_dir="$HOME/.local/state/epic-loop/test-slots"
mkdir -m 700 "$slot_dir"
printf '%s\n' '{"schema":1,"slots":4}' > "$slot_dir/capacity.json"
chmod 600 "$slot_dir/capacity.json"
)
```

Add the binding and exact config grant described above. Re-seal the reviewed
foundation files and config using the protected-home setup procedure. Set
these values in both runner environments and the manual suite environment:

```bash
export EPIC_TEST_SLOTS_DIR="$HOME/.local/state/epic-loop/test-slots"
export EPIC_TEST_SLOTS=4
```

Run the Codex preflight with its dedicated home and binding:

```bash
EPIC_RUNTIME_LEGACY=1 \
CODEX_HOME="$HOME/.local/share/codex-runner/home" \
EPIC_CODEX_PROTECTED_CONFIG="$HOME/.local/share/codex-runner/binding.json" \
python3 -I .codex/protected_home.py check --mode legacy \
  --repo "$PWD" --code-root "$PWD"
```

Run this from the deployed Codex runner checkout. A failure blocks restart.
Then run the disposable native slot controls and simultaneous host/sandbox
proof. Confirm the shared runner reports the same directory and capacity in
both environments. Only then reload both jobs and resume the loop.

## Rollback

Drain both runners and manual suites again. Restore the saved plists, config
and binding, together with the reviewed foundation version named by that
binding. Restore the old environment values. Run its preflight before
restarting. Keep the slot directory and lock files in place until every user
of them has stopped; deleting locks can create two independent lock domains.
Rollback may restore the old slot limitation and is not stability evidence.

## Evidence needed to finish the issue

- Both permission profiles grant the slot directory and deny its parent.
- Configuration errors and aliases refuse before model startup.
- Validated values reach the model's tool environment.
- Host and native sandbox suites overlap, use different temporary directories,
  and never exceed the shared capacity.
- Interrupted holders release slots; denied access stops required suites.
- Claude agrees the contract and implements the shared-runner side.
- Operator activation and a live observation confirm the deployed settings.
