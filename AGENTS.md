# AGENTS.md — UN/HEARD (live.moafunk.de)

## What This Is
Live streaming web radio from Moabit, Berlin. Artists submit tracks → scheduled into shows → streamed live → recorded → posted to SoundCloud & Instagram.

## Architecture Decisions

- **Stream uses WebSocket → FFmpeg → RTMP** (not direct RTMP from browser) because browser RTMP support is dead. WebM audio chunks sent via WS, FFmpeg transcodes to RTMP in `stream_bridge.rs`.
- **SQLite, not Postgres** — single-server deployment, low traffic, simplicity wins. DB file lives in `./data/` Docker volume.
- **R2 (S3-compatible), not local disk** — files need to be accessible from both the API and GitHub Pages build. Cloudflare R2 has no egress fees.
- **Telegram bot is the primary admin interface** for quick actions (AI generation, Instagram preview/publish). The Vue SPA is for full CRUD.
- **Instagram posts are scheduled 1/day after a show** via `scheduler.rs` — avoids spam, gives each artist their moment.
- **Recording captures raw stream + timecode markers** — allows post-production merging of original high-quality tracks at exact playback times (instead of re-encoding lossy stream audio).

## Conventions

- Backend: Rust, Axum, `sqlx` with raw SQL (no ORM). Error type is `AppError` (see `error.rs`).
- Frontend admin: Vue 3 Composition API, Pinia stores, composables in `composables/`. No class components.
- Frontend public: Vanilla TypeScript, no framework.
- All timestamps use `Europe/Berlin` timezone.
- File storage keys follow pattern: `artists/{id}/{type}/{filename}` in R2.
- API endpoints: `/api/*` for JSON, session cookie auth.

## Don't

- Don't add Tera/template dependencies — we're migrating *away* from server-rendered templates (Phase 5 cleanup pending).
- Don't use `unwrap()` in handler code — return `AppError` via `?`.
- Don't store secrets in code — everything goes through `.env` / `Config` struct.
- Don't bypass the debounce on cover regeneration — it exists to prevent redundant FFmpeg spawns.

## Deployment

- **Frontend** → GitHub Pages via `frontend.yml` workflow
- **Backend** → Docker image on GHCR → AWS Lightsail via `backend.yml` workflow (SSH deploy)
- Backend runs behind nginx on Lightsail, port 8000 internal
- Manual dispatch available for both workflows
