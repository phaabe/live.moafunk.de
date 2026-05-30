---
description: Run lint across the project (or a path), and fix auto-fixable findings.
argument-hint: [optional path]
allowed-tools: Bash, Read, Edit
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


# /lint

## Workflow

1. Detect linter:
   - Node: `eslint` (check `package.json` scripts first), `biome`.
   - Python: `ruff check`. Fall back: `flake8`.
   - Go: `golangci-lint run`.
   - Rust: `cargo clippy --all-targets -- -D warnings`.
   - Shell: `shellcheck`.
2. Run on $ARGUMENTS if provided, else the whole project.
3. If safe `--fix` flags exist (`ruff check --fix`, `eslint --fix`), apply them.
4. Print remaining findings grouped by severity.
