# Shared test slots

Status: contract for https://github.com/phaabe/live.moafunk.de/issues/664.
Both code parts exist: the Codex binding
(https://github.com/phaabe/live.moafunk.de/pull/669) and the shared test runner
`scripts/epic/run_tests.py`. Nothing is active until the operator rollout below.

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

The shared runner (`scripts/epic/run_tests.py`) checks the slot settings
before the listing and before any part starts:

- With `EPIC_TEST_SLOTS_DIR` it applies the checks above: canonical absolute
  path, a directory of this user with mode 0700, no group or world writable
  parent (sticky ones excepted), `capacity.json` exactly as above (a private,
  regular single-link file), and only `slot-N.lock` entries with N below the
  capacity. The capacity is the slot count. A set `EPIC_TEST_SLOTS` must be
  exactly that number.
- It creates missing lock files with mode 0600 and opens lock files without
  following symlinks. It refuses hardlinks and a lock file replaced during the
  run. It never truncates, replaces or deletes a file there.
- Any slot error fails the run with exit 1: invalid settings, or a folder or
  lock file that cannot be created, opened or locked. No part runs without a
  slot and no part starts after the error. A held slot is no error; the part
  waits.
- Nested disposable runners (started by a test) keep their no-slot behavior.
- Without `EPIC_TEST_SLOTS_DIR` the runner keeps its local default,
  `<user temp dir>/epic-test-slots` with `EPIC_TEST_SLOTS` slots (default: CPU
  count). Slot errors fail the run there too. Such callers are not part of the
  machine-wide limit until they use the shared settings.
- `python3 scripts/epic/run_tests.py --check-slots` prints the folder and count
  a run would use, or the error, and runs no test.
- The Claude runner passes its environment to the model's commands unchanged.
  Rebase proofs (`rebase_policy.py`) drop runner settings but pass these two
  variables to the suite's runner; `isolated_env.py` removes them from every
  test.

Merge order: once the shared-runner part is merged, a slot error fails every
top-level suite instead of running it without slots. The protected Codex
runner reported its local default folder as unwritable
(https://github.com/phaabe/live.moafunk.de/issues/503#issuecomment-6019510017),
so its suites will likely fail until its binding and grant are active. Merge
that part during the drained rollout below, and activate before the loop
resumes.

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
these values in both runner environments (the `EnvironmentVariables` of both
launchd jobs), the nightly test job and the manual suite environment:

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
both environments: `python3 scripts/epic/run_tests.py --check-slots` on the
host and in a native sandbox command must both print
`run_tests: 4 slots (capacity.json) in <directory>`. Only then reload both jobs
and resume the loop.

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
