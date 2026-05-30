# live.moafunk.de — Project Guide for Claude

> Hand-authored project conventions. The GitNexus code-intelligence block below
> (between the `<!-- gitnexus:* -->` markers) is auto-generated — edit only *outside* it.

## What this is

A live-radio / show platform with two deployables:

- **`backend/`** — Rust 2021 · Axum 0.7 · Tokio · SQLx + SQLite · Cloudflare R2 (S3) · Telegram bot.
  Areas: HTTP handlers, live-stream + recording, SoundCloud, Instagram posting, image overlay/generation, uploads.
  Deploys via GHCR → Lightsail.
- **`frontend/`** — Vue 3 + Vite + Pinia admin SPA (`src/admin/`) + vanilla-TS public pages.
  Deploys to GitHub Pages. TypeScript strict.
- **CI scripts** — Python 3.13 (uv).

GitNexus clusters: Handlers (backend), Pages / Composables / Components (Vue admin), Flow, Scripts, Api.

## Commands

| Task | Frontend (`cd frontend`) | Backend (`cd backend`) |
|------|--------------------------|------------------------|
| Dev | `npm run dev` | `cargo run` |
| Build | `npm run build` | `cargo build` |
| Test | `npm test` (Vitest) | `cargo test` |
| Lint | `npm run lint` / `lint:fix` (ESLint) | `cargo clippy` |
| Format | `npm run format` (Prettier) | `cargo fmt` |
| Types | `npm run typecheck` (tsc) | — |

## Conventions

- **Commits**: Conventional Commits with scope — `feat(stream): …`, `fix(imgGen): …`. Subject ≤ 72 chars, imperative. Use `/git.commit`.
- **Branches**: never commit on `main` — branch first (`/git.branch` or `git switch -c <type>/<slug>`). Enforced by `branch-guard.sh`.
- **Merging is PR-only.** Never `git merge <ref>` locally — open a PR (`/git.pr`) and squash-merge (`gh pr merge --squash --delete-branch`). Enforced by `merge-guard.sh`.
- **Issues**: one ticket → `/project.issue "<task>"`; a big task → the `decompose-issue` workflow. Label taxonomy: exactly one `type::backend` / `type::admin_dashboard`, plus the most specific `project::*` (Stream, recording, Instagram, Telegram, Soundcloud, ImgGen, Upload, Backup, Infrastructure, ExternalShows, Ai, unheard-artist-form, UNHEARD).
- **GitNexus-first**: before grepping, query the graph (see the managed block below). Impact-analyse before editing a symbol; `detect_changes` before committing.

## GitNexus index — fresh & clean (how it works here)

- The index lives in `.gitnexus/` (gitignored) and is refreshed automatically: at session start, on edits (debounced), and after every commit — always with `gitnexus analyze --skip-agents-md`, so **a reindex never dirties a tracked file**.
- `.claude/hooks/scripts/gitnexus-reindex.sh` self-heals any managed doc a stray reindex touches; a failed reindex drops `.gitnexus/.stale`, surfaced loudly at session start.
- **Decision points are guaranteed fresh**: `/git.commit`, `/workflow.ship`, `/quality.review` and impact analysis run `gitnexus-ensure-fresh.sh` (synchronous) first.
- Fresh clone: `npx gitnexus setup && npx gitnexus analyze --skip-agents-md && git config core.hooksPath .githooks` (see `/workflow.setup`).

## Setup notes

- Claude config is checked in under `.claude/` (shared with the team). Per-developer overrides go in `.claude/settings.local.json` (gitignored).
- If you maintain a **machine-global** `~/.claude` with its own GitNexus hooks, make those use `--skip-agents-md` too — otherwise a global *bare* `gitnexus analyze` will rewrite the stats line below and dirty the tree.

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **live.moafunk.de** (3760 symbols, 7637 relationships, 300 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> If any GitNexus tool warns the index is stale, run `npx gitnexus analyze` in terminal first.

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `gitnexus_impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `gitnexus_detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `gitnexus_query({query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `gitnexus_context({name: "symbolName"})`.

## Never Do

- NEVER edit a function, class, or method without first running `gitnexus_impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `gitnexus_rename` which understands the call graph.
- NEVER commit changes without running `gitnexus_detect_changes()` to check affected scope.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/live.moafunk.de/context` | Codebase overview, check index freshness |
| `gitnexus://repo/live.moafunk.de/clusters` | All functional areas |
| `gitnexus://repo/live.moafunk.de/processes` | All execution flows |
| `gitnexus://repo/live.moafunk.de/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
