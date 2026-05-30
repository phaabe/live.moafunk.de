---
description: Scaffold a new module / component / package using the project's existing conventions.
argument-hint: <kind> <name> — e.g. "react-component UserCard", "py-module auth.session", "go-pkg internal/cache"
allowed-tools: Read, Glob, Write, Edit, Bash(git:*)
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


# /scaffold

Args: $ARGUMENTS  (format: `<kind> <name>`)

## Workflow

1. Parse `<kind>` and `<name>` from $ARGUMENTS.
2. Find an existing example of `<kind>` in the repo. Read it. **Match its conventions exactly** — file layout, naming, imports, test colocation.
3. Generate the new files:
   - Source file with a minimal, idiomatic stub.
   - Test file with at least one passing smoke test.
   - Index / barrel export update if the project uses one.
   - Storybook / docs entry if the project has them.
4. Run the test for the new file to confirm it passes.
5. Print a summary of files created and the next likely TODO.

## Anti-patterns to avoid
- Don't invent a layout the project doesn't use.
- Don't add dependencies the project doesn't already use.
- Don't pre-commit — leave staging to the user.
