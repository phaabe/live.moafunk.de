# Overall architecture review

Status: joint review complete. Codex and Claude accepted architecture v5 on 2026-09-26. Design work has stopped as Anton requested.

## Shared workspace

Use this physical directory from either agent session:

`/Users/anton/git/2_jobs/live.moafunk.de/.claude/worktrees/docs-architecture-diagrams/docs/architecture/`

Codex fetched origin/main and rebased `worktree-docs-architecture-diagrams` onto `13e73de` on 2026-09-26. The existing untracked architecture proposals were preserved. The latest streaming documents now live in `../stream-rework/`.

## Inputs and ownership

- [Accepted streaming design](../stream-rework/streaming-design.md) and [decision record](../stream-rework/streaming-design-decisions.md).
- [Architecture proposal v3](live-moafunk.proposal-v3.md), with v1/v2 and their diagrams retained as history.
- Codex owns [codex-review.md](codex-review.md) and drafts it submits.
- Accepted architecture: [v5](live-moafunk.proposal-v5.md). [V4](live-moafunk.proposal-v4.md) is the first integrated draft, retained unchanged.
- Claude owns `claude-review.md` and documents prefixed `claude-`. Do not name a review log `claude.md`: case-insensitive systems treat it as agent instructions.
- Append numbered rounds to your own log. Do not replace the other agent's text or edit submitted drafts. Resolve changes in a new version. Claude may produce the final HTML/JSON diagram after the text is settled; diagram and text must agree.

## Agreement rule

Each agent records `ACCEPT <filename>` with the same SHA-256, or `CHANGES REQUESTED <filename>` with concrete blockers. General support or silence is not acceptance. Internal Codex subagent reviews do not count as Claude's review.

The accepted streaming contract remains binding unless a proposed change is explicitly identified and agreed. Continuous station playback is Anton's confirmed choice. The v3 architecture's 200-listener, one-server, small-budget baseline is a working planning assumption, not a new spending approval or availability guarantee.

This exchange changes documentation only. No commits, deployments, purchases, secrets access or application edits. Stop design work after both accept the same draft and all design blockers are resolved. Implementation measurements, asset supply and real-device qualification may remain named release gates.

## Final agreement

Both reviewers explicitly accepted the same file:

- Codex: `ACCEPT live-moafunk.proposal-v5.md`, [Round 6](codex-review.md#round-6--final-codex-acceptance).
- Claude: `ACCEPT live-moafunk.proposal-v5.md`, [Round 3](claude-review.md#round-3--review-of-live-moafunkproposal-v5md).
- SHA-256: `228c4934ddef24bcc5b10f46b5854581097ef503924a545743c8e7cb3b550442`.

The accepted architecture retains one host and SQLite, with independent station delivery, continuous local fallback, native HLS/direct MP3, guarded API deployment, durable recording handoff, local prerecorded preparation, resource limits, external audio checks and retained backups with restore tests. The API still owns live ingest; an API crash can interrupt the live host while station fallback continues. Whole-host loss remains an outage.

This is the best practical design under the stated scope and planning assumptions. Implementation, production inventory, approved assets, device/load/restore tests and infrastructure changes remain future work. The draft's submission-time status remains unchanged to preserve the signed file; this README records its final acceptance.

V5 includes a Mermaid topology diagram. Existing v1–v3 HTML/JSON diagrams are historical and do not represent the accepted integrated architecture. No new HTML/JSON diagram is included in the signatures. The Mermaid API-to-Liquidsoap label illustrates live Opus; the text also requires lossless input for prerecorded files.
