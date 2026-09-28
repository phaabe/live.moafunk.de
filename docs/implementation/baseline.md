# Test baseline and agent handoff

Work item: https://github.com/phaabe/live.moafunk.de/issues/339
Epic: https://github.com/phaabe/live.moafunk.de/issues/312

Status: claimed; verification pending. This document starts the evidence
checklist. It does not establish a passing test baseline or qualify a device.

## Scope and ownership

- Executor: Codex. Reviewer: Claude. Lane: coordination.
- Claimed leaves: P1.2.1, P1.2.2, P1.2.3 and P1.2.5.
- Claimed files: `docs/implementation/baseline.md`,
  `docs/implementation/device-matrix.md` and
  `docs/implementation/test-protocol.md`.
- Branch: `feat/339-test-handoff-baseline`, based on `dev/312-interim` at
  `162556178ff83cea104d7cc36babe3ad8a0321fc`.
- Claim: https://github.com/phaabe/live.moafunk.de/issues/339#issuecomment-5879306146

Follow [the epic rules](epic-rules.md). GitHub issues and project fields hold
execution status and file claims. This file holds reproducible test evidence.
Use the shared P1/O1.1 production inventory for host observations; do not create
a second inventory here. Fixture code needs a file claim and owner agreement
before editing another lane's files.

## Evidence still required

| Leaf | Evidence before completion |
| --- | --- |
| P1.2.1 | Record claims, PR boundaries and coding/activation status on GitHub. Demonstrate two independent worker claims with no shared integration-file edits. |
| P1.2.2 | Run focused tests from a clean checkout. Record commands, tool versions, failures and required local dependencies. Identify deterministic clocks, process/object-store fakes and synthetic media only where needed. |
| P1.2.3 | Document unique ports, temporary volumes, disabled publishing/bot/live output and teardown. Prove omitted parameters and inherited environment cannot select production. Record artifact, configuration and media-tool versions for each run. |
| P1.2.5 | Post the leaf checklist on the epic. Verify a fresh worker can follow the issue and current anchors through a Wave 0 leaf. |

P1.2.4 is already done except revision maintenance, per the
[readiness comment](https://github.com/phaabe/live.moafunk.de/issues/339#issuecomment-5855257018).
No accepted plan revision is changed by this work. Keep its source links and
unique leaf IDs intact.

## Baseline capture format

For each focused test run, record the commit, working directory, exact command,
tool versions, exit status, passed/failed/skipped counts and evidence link.
Distinguish pre-existing failures from failures introduced by the change.
Keep credentials and private programme audio out of fixtures and evidence.

The current test commands, failure inventory and isolation protocol remain
unverified. The device matrix and test protocol files will be added with their
evidence during implementation. No runtime test or media harness was run for
this initial claim document.
