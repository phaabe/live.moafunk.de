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
   - `frontend/src/admin/**` (Vue admin SPA: pages, composables, components) → `type::frontend` + `project::admin_dashboard`.
2. **Read the live label set**: `gh label list --limit 60`. Never invent labels — choose only from what exists.
   Apply **one `type::*`** (backend, frontend or ci) and **the most specific `project::*`**:
   `project::Stream`, `project::recording`, `project::Instagram`, `project::Telegram`, `project::Soundcloud`,
   `project::ImgGen`, `project::Upload`, `project::Backup`, `project::Infrastructure`, `project::ExternalShows`,
   `project::Ai`, `project::unheard-artist-form`, `project::UNHEARD`. Add `bug`/`enhancement`/`documentation`/`later` as fitting.
3. **Pick the parent goal**: `gh issue list --label goal --state open --json number,title`. Choose the goal
   whose topic fits; a follow-up takes the goal of its source ticket. If none fits, ask the user. Never create a goal.
   **Milestone**: always ask the user if the ticket goes into a milestone (a milestone is one feature inside a topic). Propose one from `gh api repos/{owner}/{repo}/milestones` (or "none") with a short reason. Never set one without the answer. A parked ticket gets `later` and no milestone.
4. **Title**: imperative, Conventional-Commit-flavoured when natural — e.g. `feat(stream): add pre-listen page`.
5. **Body** (this template):
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
6. **Show the full draft** (title + labels + parent goal + milestone + body) to the user and ask for confirmation. Only then:
   ```sh
   gh issue create --title "<title>" --label "type::…" --label "project::…" --parent <goal number> --body "<body>"
   ```
   Add `--milestone "<name>"` only when step 3 set one.
7. Print the created issue URL.

## Hard rules
- Never create the issue without explicit confirmation of the rendered draft.
- Every issue MUST be part of a topic: it gets a goal parent (`--parent`). An issue without one drops out of board 3 and the vault tree.
- Always ask the user about the milestone, with a proposal. Never set or skip it silently.
- Only use labels returned by `gh label list` — if none fit a dimension, say so rather than guessing.
- Keep it to ONE issue; if the task is clearly multi-part, recommend the decomposition workflow.

Task: $ARGUMENTS
