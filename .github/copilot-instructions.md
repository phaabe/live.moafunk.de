# Copilot Instructions — UN/HEARD (live.moafunk.de)

## What This Is

Live streaming web radio from Moabit, Berlin. Artists submit tracks → scheduled into shows → streamed live → recorded → posted to SoundCloud & Instagram. Two deployments: public site on GitHub Pages, admin backend on AWS Lightsail.

## Architecture

- **Backend** (`backend/`): Rust 2021 + Axum 0.7, SQLite via `sqlx` (raw SQL, no ORM), Cloudflare R2 for file storage. Single binary serves both the JSON API (`/api/*`) and the admin SPA (built Vue app in `static/admin/`).
- **Frontend** (`frontend/`): Multi-entry Vite build — public pages (vanilla TS) and admin SPA (Vue 3 + Pinia). Admin uses hash routing (`createWebHashHistory`) and proxies `/api` + `/ws` to the backend in dev.
- **Streaming**: Browser captures WebM audio → WebSocket → backend (`stream_bridge.rs`) pipes to FFmpeg → RTMP. No direct browser RTMP.
- **Recording**: Raw stream chunks are tee'd to disk with timecoded markers (`recording.rs`), enabling post-production merging of original tracks at exact playback times.
- **Telegram bot** (`telegram.rs`): Primary quick-action interface — AI bio generation, Instagram preview/publish, show notifications. Runs as long-polling alongside the HTTP server.
- **Schedulers** (in `main.rs`): Daily Telegram previews at 16:00/19:00 Berlin time, weekly expired-user cleanup. All times use `Europe/Berlin`.

## Key Conventions

### Rust Backend

- All handlers return `Result<impl IntoResponse>` using `AppError` from `error.rs` — **never use `unwrap()` in handlers**, propagate with `?`.
- Database: raw `sqlx::query`/`sqlx::query_as` — migrations are hand-written in `db::run_migrations()` using `CREATE TABLE IF NOT EXISTS` + `add_column_if_missing()` helper.
- Config loaded from env vars via `envy` into the `Config` struct (`config.rs`). All secrets come from `.env` / Bitwarden — never hardcode.
- R2 storage keys: `artists/{id}/{type}/{filename}` pattern. Upload/download via `storage.rs` using the AWS S3 SDK with `force_path_style(true)`.
- Background tasks (Instagram post, SoundCloud upload, cover generation) are fire-and-forget `tokio::spawn` — failures log warnings but never propagate to API responses.
- Cover regeneration is debounced via `AppState.cover_debounce` — don't bypass the debounce.
- Superadmin seeded on first boot if `users` table is empty (`db::seed_superadmin`). Password hash is Argon2, generated with `cargo run --bin hash_password -- "password"`.

### Vue Frontend (Admin SPA)

- Vue 3 Composition API with `<script setup lang="ts">` — no Options API or class components.
- State management: Pinia store in `admin/stores/auth.ts`.
- Reusable logic: composables in `admin/composables/` (e.g., `useStreamSocket`, `useAudioCapture`, `useRecordingSession`).
- API calls: centralized `ApiClient` class in `admin/api/index.ts` — uses `credentials: 'include'` for cookie auth.
- Shared code between public pages and admin in `src/shared/` — import with `@shared/` alias.
- Routes lazy-loaded: `const Page = () => import('./pages/Page.vue')`.
- Path aliases: `@` → `src/`, `@admin` → `src/admin/`, `@shared` → `src/shared/`.

### Public Frontend

- Vanilla TypeScript — no framework. Entry points in `src/pages/` (artist form, re-listen archive, tech rider).
- Stream detection (`streamDetector.ts`): HLS for iOS, FLV for desktop.

## Development Commands

```bash
# Backend
cd backend
cargo run                              # Dev server on :8000
cargo watch -x run                     # Auto-reload
cargo run --bin hash_password -- "pw"  # Generate Argon2 hash for .env
cargo test                             # Run tests
cargo clippy                           # Lint

## ssh into Lightsail instance (after initial setup):
 ssh -i ~/.ssh/unheard-backend-key.pem -o StrictHostKeyChecking=accept-new ubuntu@18.157.219.113

# Frontend
cd frontend
npm run dev                            # Dev server on :3000 (proxies /api to :8000)
npm test                               # Vitest
npm run build                          # Production build (runs generate:html first)
npm run typecheck                      # tsc --noEmit

# Docker (production-like)
docker compose up -d --build           # Local Docker
```

## Deployment

- **Frontend** → GitHub Pages via `frontend.yml` (triggers on `frontend/**` changes to `main`)
- **Backend** → Docker image built in CI → pushed to GHCR → deployed to Lightsail via SSH (`backend.yml`). Triggers on `backend/**` or `frontend/src/admin/**` changes (admin SPA is baked into the Docker image).
- Secrets managed in Bitwarden Secrets Manager, fetched in CI via `bitwarden/sm-action`.
- Deploy script: `scripts/deploy_lightsail.sh` — uploads `.env`, pulls image, runs `docker compose up -d`.
- Server scripts in `scripts/{backup,db,r2}/` for DB backup/restore, R2 sync, rclone setup.
- Manual workflow dispatch available with options: `init_db`, `setup_nginx`, custom image tag.

## Don't

- Don't add Tera/template dependencies — migrating away from server-rendered templates.
- Don't store secrets in code — everything via `.env` / `Config` struct.
- Don't use `unwrap()` in handler code — return `AppError` via `?`.
- Don't bypass cover regeneration debounce.
- Don't use Options API or class components in Vue.
