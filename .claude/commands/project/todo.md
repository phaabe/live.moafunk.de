---
description: List, add, or close TODOs scattered across the codebase.
argument-hint: [list | add "..." | close <id>]
allowed-tools: Read, Grep, Glob, Edit, Bash(git:*)
model: sonnet
---

# /todo

Args: $ARGUMENTS

## Subcommands

### list (default)
1. Find TODO markers: `rg -n '\b(TODO|FIXME|XXX|HACK|NOTE)\b' --type-add 'web:*.{ts,tsx,js,jsx,vue,svelte}'`.
2. Group by file. Show owner via `git blame` if quick.
3. Print as a numbered list.

### add "<text>"
- Append to a top-level `TODO.md` (create if missing) under today's date.
- Format: `- [ ] <text> (added by /todo on <date>)`.

### close <id>
- Locate the TODO at the given id (line in the listing or item index in `TODO.md`).
- Confirm with the user, then mark `[x]` (in `TODO.md`) or remove the in-code marker.

If no args → run `list`.
