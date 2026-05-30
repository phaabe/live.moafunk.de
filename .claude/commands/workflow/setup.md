---
description: Bootstrap a fresh checkout — install deps, copy env templates, run setup scripts.
allowed-tools: Bash, Read, Glob
model: sonnet
---

# /setup

Bootstrap a freshly cloned project.

## Workflow

1. Detect package managers and run installs:
   - **This repo**: `frontend/` → `npm ci`; `backend/` → `cargo fetch`; CI scripts use `uv`.
   - Generic fallbacks: `package.json` → `pnpm install`/`yarn`/`npm ci`; `pyproject.toml` → `uv sync`/`poetry install`/`pip install -e .`; `go.mod` → `go mod download`; `Cargo.toml` → `cargo fetch`.
2. `.env.example` → copy to `.env` if `.env` missing. Show the user which vars they need to fill in (this repo also uses `backend/.env`).
3. **GitNexus + Claude config** (do this for every fresh clone):
   - `npx gitnexus setup` — register the GitNexus MCP server for this machine (or rely on the committed `.mcp.json`; approve the project MCP server when prompted).
   - `npx gitnexus analyze --skip-agents-md` — build the code-intelligence index without dirtying tracked docs.
   - `git config core.hooksPath .githooks` — activate the git-native post-commit reindex so the index stays fresh for commits made outside Claude Code.
4. Look for setup scripts: `make setup`, `./scripts/setup`, `bin/setup`, `npm run setup`. Run if found.
5. Run a smoke test (`cd frontend && npm test --silent`, `cd backend && cargo check`) to confirm the install worked.
6. Print a "ready" summary with next-step suggestions.
