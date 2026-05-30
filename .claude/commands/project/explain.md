---
description: Explain a file, directory, or symbol — what it does and how it fits into the codebase.
argument-hint: <file / directory / symbol>
allowed-tools: Read, Grep, Glob, Bash(git log:*), Bash(git blame:*)
model: opus
---

## Pre-flight: GitNexus first

Before any Grep / Read / Glob, **always**:

1. Read `gitnexus://repo/{name}/context` to confirm the index is fresh.
2. If your task involves a concept → `gitnexus_query({query: "..."})`.
3. If your task is about a specific symbol → `gitnexus_context({name: "..."})`.
4. If your task is "what breaks if I change X" → `gitnexus_impact({name: "...", depth: 2})`.
5. If you are looking at uncommitted changes → `gitnexus_detect_changes({})`.

Only fall back to Grep / Read / Glob for things the graph cannot answer (string search, comments, formatting, raw text).


# /explain

Target: $ARGUMENTS

## Workflow

1. Resolve the target — file path? directory? symbol name (function / class / constant)?
2. Read the target in full.
3. Find call sites: `rg -n '<symbol>' --type-add ...` (filter by language). For directories: enumerate exports / public surface.
4. Look at recent history: `git log --oneline -10 -- <path>` for evolution; `git blame` for context if a specific question.
5. Write the explanation:

   ```
   ## <target>

   **Purpose**: 1-line summary of what it does.

   **Inputs**: <what comes in>
   **Outputs / effects**: <what comes out / what mutates>
   **Owners**: <where it's called from — top 3>

   **How it works**:
   <2–6 paragraphs of prose, walking through the implementation>

   **Gotchas**:
   - <non-obvious behaviour>

   **Related**:
   - <links to nearby symbols / docs>
   ```
