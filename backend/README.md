# UNHEARD Artists Backend

Rust backend (Axum + SQLx + Tera) for managing UNHEARD artist submissions, shows, and media files.

## Features

- **Form submission API**: Receives artist submissions from the frontend form
- **File storage**: Uploads media files to Cloudflare R2
- **Admin panel**: Web interface for managing artists and shows
- **ZIP download**: Package all media for a show

## Tech Stack

- **Axum** - Web framework
- **SQLx** - Async database with compile-time checked queries
- **Tera** - Template engine (Jinja2-like)
- **Argon2** - Password hashing
- **AWS SDK** - S3-compatible storage (Cloudflare R2)

## Setup

### 1. Prerequisites

- Rust 1.75+ (install via [rustup](https://rustup.rs/))
- Docker & Docker Compose (for deployment)
- Cloudflare R2 bucket

### 2. Cloudflare R2 Setup

1. Create an R2 bucket in your Cloudflare dashboard
2. Create an API token with R2 read/write permissions
3. Note your Account ID, Access Key ID, and Secret Access Key

### 3. Configuration

Local-dev secrets come from **Bitwarden Secrets Manager** — the same source CI/prod use
(`.github/workflows/backend.yml`) — not a hand-edited `.env`. `./scripts/dev-secrets.sh`
generates a gitignored `.env` from Bitwarden; your personal values live in `.env.local`.

Prerequisites: the [`bws` CLI](https://bitwarden.com/help/secrets-manager-cli/) and `jq`.

```bash
# 1. A dev machine-account access token (Bitwarden → Secrets Manager → Machine accounts).
export BWS_ACCESS_TOKEN=<your token>

# 2. Your personal, non-shared values (gitignored). The admin password hash is required;
#    generate it, then add it to backend/.env.local single-quoted (the hash contains '$'):
cargo run --bin hash_password -- "your-password"
#    backend/.env.local:
#      SUPERADMIN_PASSWORD_HASH='$argon2id$v=19$...'
#      # optional overrides: DATABASE_URL=..., RUST_LOG=...

# 3. Generate backend/.env from Bitwarden (re-run anytime to refresh).
./scripts/dev-secrets.sh

# 4. Run the backend.
cargo run
```

`.env` is generated and gitignored — never edit it by hand (re-run the script instead).
No Bitwarden token? Fall back to `cp .env.example .env` and fill it in manually.

Required environment variables (see `.env.example` for the full list):
- `SECRET_KEY`: Random secret for session management (from Bitwarden)
- `SUPERADMIN_PASSWORD_HASH`: Argon2 hash from the password generator (your own, in `.env.local`)
- `R2_ACCOUNT_ID`: Your Cloudflare account ID (from Bitwarden)
- `R2_ACCESS_KEY_ID`: R2 API token access key (from Bitwarden)
- `R2_SECRET_ACCESS_KEY`: R2 API token secret (from Bitwarden)
- `R2_BUCKET_NAME`: Name of your R2 bucket

Optional environment variables (ZIP artist image stamping):
- `ARTIST_LOGO_DIR` (default: `./assets/artist_logos`)
	- If a file exists at `./assets/artist_logos/<artist_id>.png` or `./assets/artist_logos/<artist_name>.png`, it will be used as the logo.
- `DEFAULT_LOGO_PATH` (default: `./assets/brand/moafunk.png`)
	- Used when no per-artist logo is found.
- `OVERLAY_FONT_PATH` (optional)
	- Path to a `.ttf` font file used to render the artist name.
	- If unset, the server tries a few common system font paths.

Optional environment variables (Telegram bot):
- `TELEGRAM_BOT_TOKEN` — BotFather token. If unset, the bot is disabled entirely.
- `TELEGRAM_ADMIN_CHAT_ID` — Numeric chat ID that is allowed to issue commands and receives notifications. Use a negative value for group chats.
- `TELEGRAM_INSTAGRAM_ACCOUNT` — `dev` or `prod` (default: `prod`). Controls which Instagram account is used by `/post_instagram`.

#### How to get the Telegram tokens

1. **Bot token** — Open Telegram, search for [@BotFather](https://t.me/BotFather), send `/newbot`, follow the prompts to pick a name and username. BotFather replies with a token like `123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11`. That is your `TELEGRAM_BOT_TOKEN`.

2. **Admin chat ID** (DM) — Send a `/start` message to your new bot, then open:
   ```
   https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
   ```
   Look for `"chat":{"id": 123456789, ...}` — that number is your `TELEGRAM_ADMIN_CHAT_ID`.

3. **Admin chat ID** (group) — Add the bot to your group, send a `/start` message in the group, then check `getUpdates` the same way. Group chat IDs are negative (e.g. `-1001234567890`). Make sure to **disable privacy mode** via BotFather (`/mybots` → Bot Settings → Group Privacy → Turn off) so the bot can see commands in the group.

4. **Instagram account** — No token needed. `TELEGRAM_INSTAGRAM_ACCOUNT` is just a label (`dev` or `prod`) that tells the backend which configured Instagram account to post to. Defaults to `prod` if unset.

### 4. Local Development

```bash
# Run the development server
cargo run

# Or with auto-reload (install cargo-watch first)
cargo watch -x run
```

### 5. Docker Deployment

```bash
# Build and start the container
docker compose up -d --build

# View logs
docker compose logs -f

# Stop the container
docker compose down
```

### Production Deployment (GHCR + pull)

The repo includes a GitHub Actions workflow that builds and pushes the backend image to GHCR on every push to `main`:

- [.github/workflows/backend-ghcr.yml](../.github/workflows/backend-ghcr.yml)

On the server, use the prebuilt image (no Rust build on the instance):

```bash
REGION=eu-central-1 STATIC_IP=unheard-backend-ip \
PEM=~/.ssh/unheard-key.pem \
GHCR_USER=<github-username> GHCR_TOKEN_FILE=./ghcr-token.key \
UNHEARD_IMAGE=ghcr.io/<owner>/live.moafunk.de-backend UNHEARD_TAG=latest \
./backend/scripts/deploy_lightsail.sh
```

#### Useful Docker/GHCR Commands

Login to GHCR (recommended: use a token piped via stdin so it doesn’t end up in shell history):

```bash
echo "$GHCR_TOKEN" | docker login ghcr.io -u anneoneone --password-stdin
```

Inspect which platforms (architectures) a tag provides (useful when a server is `linux/amd64` and your build accidentally produced `linux/arm64` only):

```bash
docker manifest inspect ghcr.io/anneoneone/unheard-backend:latest
```

Retag an existing local image (common pattern after a CI build that produced a specific version/tag and you want to also publish/update `latest`):

```bash
docker tag "${GHCR_IMAGE}:${TAG}" "${GHCR_IMAGE}:latest"
```

Build and push a Linux/amd64 image to GHCR using BuildKit/buildx (useful when building on Apple Silicon but deploying to an amd64 VM):

```bash
docker buildx build \
	--platform linux/amd64 \
	-t ghcr.io/anneoneone/unheard-backend:latest \
	-f backend/Dockerfile backend \
	--push
```

### 6. Nginx Setup

```bash
# Copy the nginx config
sudo cp nginx.conf.example /etc/nginx/sites-available/admin.live.moafunk.de

# Enable the site
sudo ln -s /etc/nginx/sites-available/admin.live.moafunk.de /etc/nginx/sites-enabled/

# Get SSL certificate
sudo certbot --nginx -d admin.live.moafunk.de

# Reload nginx
sudo systemctl reload nginx
```

## Telegram Bot

The backend optionally runs a Telegram bot (via [teloxide](https://docs.rs/teloxide)) alongside the HTTP server. It is fully disabled when `TELEGRAM_BOT_TOKEN` is not set.

The bot uses long-polling and restricts all commands to the single `TELEGRAM_ADMIN_CHAT_ID`.

### Push Notifications

The bot sends fire-and-forget messages when:
- 🎤 A new artist is submitted (via the public form)
- 📡 A stream starts or stops

### Commands

| Command | Description |
|---------|-------------|
| `/help` | List all commands |
| `/artists` | List unassigned artists with readiness indicators |
| `/shows` | List upcoming shows with artist counts |
| `/artist <id>` | Detailed artist view (tracks, socials, bio/video/caption status) |
| `/show <id>` | Show details with assigned artists |
| `/generate_bio <id>` | Generate AI bio + Instagram caption for an artist |
| `/generate_videos <id>` | Generate track preview videos for an artist |
| `/preview_instagram <id>` | Preview caption text + profile image in chat |
| `/edit_caption <id> <text>` | Update an artist's Instagram caption |
| `/post_instagram <id>` | Publish artist post to Instagram |
| `/post_show_instagram <id>` | Publish show cover to Instagram |
| `/stream_status` | Check if stream is active and who is streaming |
| `/stats` | Summary: total artists, unassigned, upcoming shows, stream |

## API Endpoints

### Public

- `POST /api/submit` - Submit artist form (multipart/form-data)
- `GET /health` - Health check

### Admin (protected)

- `GET /login` - Login page
- `POST /login` - Authenticate
- `GET /logout` - Logout

- `GET /artists` - List all artists
- `GET /artists/:id` - Artist detail
- `POST /artists/:id/status` - Update artist status

- `GET /shows` - List all shows
- `POST /shows` - Create new show
- `GET /shows/:id` - Show detail
- `POST /shows/:id/assign` - Assign artist to show
- `POST /shows/:id/unassign/:artist_id` - Remove artist from show
- `GET /shows/:id/download` - Download show media as ZIP (legacy; same as all-data)
- `GET /shows/:id/download/:package` - Download a show package as ZIP (`recording`, `social-media`, `all-data`)

## Directory Structure

```
backend/
├── src/
│   ├── main.rs           # Entry point, router setup
│   ├── config.rs         # Configuration
│   ├── db.rs             # Database migrations
│   ├── error.rs          # Error types
│   ├── models.rs         # Data models
│   ├── storage.rs        # R2 helpers
│   ├── auth.rs           # Authentication
│   ├── telegram.rs       # Telegram bot commands & dispatcher
│   ├── telegram_notify.rs # Fire-and-forget notifications
│   ├── handlers/
│   │   ├── mod.rs
│   │   ├── submit.rs     # Form submission
│   │   ├── auth.rs       # Login/logout
│   │   ├── admin.rs      # Admin panel
│   │   └── download.rs   # ZIP download
│   └── bin/
│       └── hash_password.rs
├── templates/            # Tera templates
├── data/                 # SQLite database (created at runtime)
├── Cargo.toml
├── Dockerfile
├── docker-compose.yml
└── .env.example
```
