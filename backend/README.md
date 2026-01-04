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
- `GET /shows/:id/download` - Download show media as ZIP

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
