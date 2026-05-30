---
description: Format the project (or a path) with the right formatter for each language.
argument-hint: [optional path]
allowed-tools: Bash, Read
model: haiku
---

## Pre-flight: GitNexus first

Before any Grep / Read / Glob, **always**:

1. Read `gitnexus://repo/{name}/context` to confirm the index is fresh.
2. If your task involves a concept → `gitnexus_query({query: "..."})`.
3. If your task is about a specific symbol → `gitnexus_context({name: "..."})`.
4. If your task is "what breaks if I change X" → `gitnexus_impact({name: "...", depth: 2})`.
5. If you are looking at uncommitted changes → `gitnexus_detect_changes({})`.

Only fall back to Grep / Read / Glob for things the graph cannot answer (string search, comments, formatting, raw text).


# /format

## Workflow

1. Detect formatters:
   - Node: prettier (check `.prettierrc*`), biome.
   - Python: `ruff format` (preferred), else `black`.
   - Go: `gofmt -w`.
   - Rust: `cargo fmt`.
   - Terraform: `terraform fmt`.
2. Run on $ARGUMENTS if provided, else the whole project.
3. Show what changed (`git diff --stat`).
