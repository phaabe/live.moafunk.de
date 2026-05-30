---
description: Run static type checking for the project.
argument-hint: [optional path]
allowed-tools: Bash, Read
model: sonnet
---

## Pre-flight: GitNexus first

Before any Grep / Read / Glob, **always**:

1. Read `gitnexus://repo/{name}/context` to confirm the index is fresh.
2. If your task involves a concept → `gitnexus_query({query: "..."})`.
3. If your task is about a specific symbol → `gitnexus_context({name: "..."})`.
4. If your task is "what breaks if I change X" → `gitnexus_impact({name: "...", depth: 2})`.
5. If you are looking at uncommitted changes → `gitnexus_detect_changes({})`.

Only fall back to Grep / Read / Glob for things the graph cannot answer (string search, comments, formatting, raw text).


# /typecheck

## Workflow

1. Detect:
   - TypeScript: `tsc --noEmit` (or `tsc -b` for project refs). Check `package.json` scripts.
   - Python: `pyright` (preferred), else `mypy`.
   - Go: type errors caught by `go build ./...`.
   - Rust: `cargo check`.
2. Run. Group errors by file. Identify the top 3 root causes (often a shared bad type fans out).
3. Suggest fixes for the root causes; don't try to silence individual errors.

Args: $ARGUMENTS
