---
description: Draft and create a well-formed GitHub issue with this repo's label taxonomy.
argument-hint: <short task description>
allowed-tools: Bash(gh issue:*), Bash(gh label list:*), Bash(.claude/hooks/scripts/gitnexus-ensure-fresh.sh), mcp__gitnexus__query, mcp__gitnexus__context, Read
model: opus
---

# /issue

Create a single, well-scoped GitHub issue on `phaabe/live.moafunk.de` from `$ARGUMENTS`.

> For a **big** task that should become a parent issue + several labeled sub-issues, use the
> decomposition workflow instead: `.claude/workflows/decompose-issue.js` (run via the Workflow tool).

## Workflow

1. **Scope via GitNexus** (don't grep): `gitnexus_query({query: "<the concept from $ARGUMENTS>", repo: "live.moafunk.de"})`
   to find which functional area / files the work touches. Use the result to pick labels and write a precise body.
   - `backend/**` (Rust/Axum: handlers, recording, soundcloud, image_overlay, telegram) → `type::backend`.
   - `frontend/src/admin/**` (Vue admin SPA: pages, composables, components) → `type::admin_dashboard`.
2. **Read the live label set**: `gh label list --limit 60`. Never invent labels — choose only from what exists.
   Apply **exactly one `type::*`** (backend vs admin_dashboard) and **the most specific `project::*`**:
   `project::Stream`, `project::recording`, `project::Instagram`, `project::Telegram`, `project::Soundcloud`,
   `project::ImgGen`, `project::Upload`, `project::Backup`, `project::Infrastructure`, `project::ExternalShows`,
   `project::Ai`, `project::unheard-artist-form`, `project::UNHEARD`. Add `bug`/`enhancement`/`documentation`/`later` as fitting.
3. **Title**: imperative, Conventional-Commit-flavoured when natural — e.g. `feat(stream): add pre-listen page`.
4. **Body** (this template):
   ```md
   ## Context
   <why this matters / where it surfaced>

   ## Scope (GitNexus)
   Affected area(s): <cluster/files from gitnexus_query>

   ## Acceptance criteria
   - [ ] <observable outcome 1>
   - [ ] <observable outcome 2>

   ## Notes
   <constraints, links, related issues>
   ```
5. **Show the full draft** (title + labels + body) to the user and ask for confirmation. Only then:
   ```sh
   gh issue create --title "<title>" --label "type::…" --label "project::…" --body "<body>"
   ```
6. Print the created issue URL.

## Hard rules
- Never create the issue without explicit confirmation of the rendered draft.
- Only use labels returned by `gh label list` — if none fit a dimension, say so rather than guessing.
- Keep it to ONE issue; if the task is clearly multi-part, recommend the decomposition workflow.

Task: $ARGUMENTS
