# Shared streaming design review

Anton asked Codex and Claude to work in parallel through documents until both agree on the best practical design for robust, interoperable streaming with mobile metadata, especially iOS. NTS is a reference, not a specification.

## Shared location

Both agents must use this physical directory, regardless of their current checkout:

`/Users/anton/git/2_jobs/live.moafunk.de/.claude/worktrees/mobile-playback-plan/docs/streaming-design/`

Branch: `docs/mobile-playback-plan`. Do not commit, change branches, or edit application code as part of this design exchange.

## Coordination

- Codex owns `codex.md` and drafts named `design-vN.md` that Codex creates.
- Claude owns `claude-review.md` (first written as `claude.md`) and may create review or evidence files prefixed `claude-`.
- Read the other agent's file between rounds. Append numbered rounds to your own file; do not replace the other agent's text.
- Design drafts are immutable once submitted for review. The next draft incorporates responses and lists what changed.
- Each reviewer must explicitly record `ACCEPT design-vN.md` or `CHANGES REQUESTED design-vN.md` in their own file, with unresolved items if any.
- Silence, elapsed time, a tool result, or agreement with the general direction is not acceptance.
- Stop when both reviewers accept the same exact draft and no design blocker remains. Device validation can remain a clearly named implementation gate; agreement is not proof that an unbuilt system works.
- The accepted draft becomes the source of truth; older plans remain research inputs.

## Starting documents

- Claude's existing draft: [mobile-playback-plan.md](../stream-rework/mobile-playback-plan.md).
- Codex's existing research: `/Users/anton/git/2_jobs/live.moafunk.de/docs/stream-rework/mobile-listening-plan.md`.
- [Codex review and messages](codex.md).
- [Claude review and messages](claude-review.md).
- Accepted design: [design-v3.md](design-v3.md). Earlier versions remain for the review history.

## Agreement — review complete

Both agents explicitly accepted the same design on 2026-09-26:

- Codex: `ACCEPT design-v3.md`, recorded in codex.md Round 7.
- Claude: `ACCEPT design-v3.md`, recorded in claude-review.md Round 6.
- Accepted file SHA-256: `765dac2bb330c32e4d52d822d2ac441870933609a25a9d23d462ba307ebca3e3`.

Anton selected continuous station playback. The agreed design combines approved fallback programme audio, planned native HLS for qualified Apple clients, permanent MP3 compatibility, stable public URLs, authoritative programme metadata and immutable artwork.

The draft's submission-time wording is preserved because reviewed drafts are immutable; this agreement record states its current accepted status. Both agents agree this is the best practical design for the stated goals and known constraints. Design work stops here. Implementation, real-device qualification, approved fallback assets and any infrastructure deployment remain future work. No application code was changed or deployed during this review.

Note: the Claude review log was renamed from `claude.md` to `claude-review.md` after agreement. On case-insensitive file systems `claude.md` is read as a `CLAUDE.md` agent-instructions file. Older rounds and the signed drafts still name `claude.md`.
