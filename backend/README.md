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

```bash
# Copy the example env file
cp .env.example .env

# Generate an admin password hash
cargo run --bin hash_password -- "your-password"

# Copy the hash to .env
# Edit .env with your R2 credentials
```

Required environment variables:
- `SECRET_KEY`: Random secret for session management
- `ADMIN_PASSWORD_HASH`: Argon2 hash from the password generator
- `R2_ACCOUNT_ID`: Your Cloudflare account ID
- `R2_ACCESS_KEY_ID`: R2 API token access key
- `R2_SECRET_ACCESS_KEY`: R2 API token secret
- `R2_BUCKET_NAME`: Name of your R2 bucket

Optional environment variables (ZIP artist image stamping):
- `ARTIST_LOGO_DIR` (default: `./assets/artist_logos`)
	- If a file exists at `./assets/artist_logos/<artist_id>.png` or `./assets/artist_logos/<artist_name>.png`, it will be used as the logo.
- `DEFAULT_LOGO_PATH` (default: `./assets/brand/moafunk.png`)
	- Used when no per-artist logo is found.
- `OVERLAY_FONT_PATH` (optional)
	- Path to a `.ttf` font file used to render the artist name.
	- If unset, the server tries a few common system font paths.

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
