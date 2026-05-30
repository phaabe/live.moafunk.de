---
description: Full ship pipeline: format -> lint -> typecheck -> test -> review -> commit -> PR.
argument-hint: [optional commit context]
allowed-tools: Bash, Read, Edit, Grep, Glob
model: opus
---

## Pre-flight: GitNexus first

Before any Grep / Read / Glob, **always**:

0. Run the **synchronous freshness gate**: `bash "$CLAUDE_PROJECT_DIR/.claude/hooks/scripts/gitnexus-ensure-fresh.sh"`.
1. Read `gitnexus://repo/{name}/context` to confirm the index is fresh.
2. If your task involves a concept → `gitnexus_query({query: "..."})`.
3. If your task is about a specific symbol → `gitnexus_context({name: "..."})`.
4. If your task is "what breaks if I change X" → `gitnexus_impact({name: "...", depth: 2})`.
5. If you are looking at uncommitted changes → `gitnexus_detect_changes({})`.

Only fall back to Grep / Read / Glob for things the graph cannot answer (string search, comments, formatting, raw text).


# /ship

End-to-end "ship this branch" pipeline.

## Steps

1. `/format`  (skip if no changes)
2. `/lint`    (auto-fix where safe)
3. `/typecheck`
4. `/test`    (focused on changed files first, then full suite)
5. `/review`  (delegate to code-reviewer subagent — read-only)
6. **GATE:** ask the user before continuing if any step has unresolved findings.
7. `/commit $ARGUMENTS`
8. `/pr`

If any step fails, stop and report. Do not push past failures.

Extra commit context: $ARGUMENTS
