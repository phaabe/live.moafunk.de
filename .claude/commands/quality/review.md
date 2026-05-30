---
description: Run a full code review on the current branch using the code-reviewer subagent.
argument-hint: [optional base branch, default trunk]
allowed-tools: Bash(git:*), Bash(.claude/hooks/scripts/gitnexus-ensure-fresh.sh), Read
model: opus
---

## Pre-flight: GitNexus first

Before any Grep / Read / Glob, **always**:

0. Run the **synchronous freshness gate** so the review reflects the current code:
   `bash "$CLAUDE_PROJECT_DIR/.claude/hooks/scripts/gitnexus-ensure-fresh.sh"`.
1. Read `gitnexus://repo/{name}/context` to confirm the index is fresh.
2. If your task involves a concept → `gitnexus_query({query: "..."})`.
3. If your task is about a specific symbol → `gitnexus_context({name: "..."})`.
4. If your task is "what breaks if I change X" → `gitnexus_impact({name: "...", depth: 2})`.
5. If you are looking at uncommitted changes → `gitnexus_detect_changes({})`.

Only fall back to Grep / Read / Glob for things the graph cannot answer (string search, comments, formatting, raw text).


# /review

Delegate to the **code-reviewer** subagent for a full review of the current branch's diff against the trunk.

Pass-through: $ARGUMENTS (used as base branch override).

After the subagent reports back, surface the review verbatim and offer to:
1. Fix the "Must fix" items (delegate to test-runner / refactorer as needed).
2. Open / update a PR (`/pr`).
3. Squash & merge once green.
