# Deployment Guide

## Overview

This project has two deployment targets:

| Component | URL | Platform |
|-----------|-----|----------|
| **Main site** | `live.moafunk.de` | GitHub Pages |
| **Admin panel + API** | `admin.live.moafunk.de` | AWS Lightsail (Docker) |

---

## Frontend (Main Site)

**Workflow:** `.github/workflows/frontend.yml`

**Triggers:**
- Push to `main` branch
- Manual dispatch

**What it does:**
1. Builds the Vite frontend (`npm run build`)
2. Generates `re-listen.html` from SoundCloud API (optional, with fallback)
3. Deploys `dist/` to GitHub Pages

**Output:** Static site at `live.moafunk.de`

---

## Backend + Admin SPA

**Workflow:** `.github/workflows/backend.yml`

**Triggers:**
- Push to `main` with changes in:
  - `backend/**`
  - `frontend/src/admin/**`
  - `frontend/package*.json`
- Manual dispatch

**What it does:**
1. Builds multi-stage Docker image:
   - Stage 1: Compile Rust backend
   - Stage 2: Build admin SPA with Node.js
   - Stage 3: Runtime image with both
2. Pushes to GitHub Container Registry (GHCR)
3. Deploys to Lightsail via SSH

**Docker build context:** Repository root (`.`)

**Dockerfile:** `backend/Dockerfile`

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                  live.moafunk.de                    │
│                  (GitHub Pages)                     │
│  - Main landing page                                │
│  - Re-listen page                                   │
│  - Artist submission form                           │
│  - Tech rider                                       │
└─────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────┐
│              admin.live.moafunk.de                  │
│               (Lightsail Docker)                    │
│                                                     │
│  ┌─────────────────────────────────────────────┐   │
│  │           Rust Backend (Axum)               │   │
│  │  - /api/* → JSON API                        │   │
│  │  - /artists/:id/download/* → Downloads      │   │
│  │  - /shows/:id/download/* → Downloads        │   │
│  │  - /ws/stream → WebSocket                   │   │
│  │  - /* → Admin SPA (static files)            │   │
│  └─────────────────────────────────────────────┘   │
│                                                     │
│  ┌─────────────────────────────────────────────┐   │
│  │        Admin SPA (Vue 3 + Vite)             │   │
│  │  - Served from /static/admin/               │   │
│  │  - Client-side routing via fallback         │   │
│  └─────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────┘
```

---

## Manual Deployment

### Backend only (skip Docker build)
```bash
# Trigger workflow with deploy=true, build=false
gh workflow run backend.yml -f deploy=true -f build=false
```

### Full rebuild and deploy
```bash
gh workflow run backend.yml
```

---

## Local Development

### Frontend (with API proxy)
```bash
cd frontend
npm install
npm run dev  # Runs at localhost:3000, proxies /api to localhost:8000
```

### Backend
```bash
cd backend
cargo run  # Runs at localhost:8000
```

### Admin SPA in backend (for testing)
```bash
cd frontend && npm run build
cp -r dist/admin ../backend/static/admin/
cp -r dist/assets ../backend/static/admin/assets/
cd ../backend && cargo run
# Visit http://localhost:8000
```

---

## Environment Variables (Backend)

See `backend/.env.example` for required configuration:
- Database connection
- R2/S3 credentials
- Secret key
- RTMP streaming settings

---

## Cloudflare R2 Storage

### Bucket Setup

Create three R2 buckets in Cloudflare dashboard:

| Bucket | Purpose | Environment |
|--------|---------|-------------|
| `unheard-artists-dev` | Media storage | Development |
| `unheard-artists-prod` | Media storage | Production |
| `unheard-backups` | Database and R2 backups | All |

### Environment Configuration

Set `R2_BUCKET_NAME` per environment:

```bash
# Development (.env)
R2_BUCKET_NAME=unheard-artists-dev

# Production (.env)
R2_BUCKET_NAME=unheard-artists-prod
```

### CORS Configuration

Run for each bucket:

```bash
BUCKET=unheard-artists-dev ./backend/scripts/configure_r2_cors.sh
BUCKET=unheard-artists-prod ./backend/scripts/configure_r2_cors.sh
```

---

## Backup System

### Overview

Backups are triggered:
- **On artist submission**: Database backup via GitHub Actions dispatch
- **Weekly (Sunday 3am UTC)**: Full database + R2 backup via cron
- **Manually**: Via GitHub Actions UI or CLI

### Setup rclone on Lightsail

After deploying, run on the Lightsail instance:

```bash
cd /opt/unheard-backend
./scripts/backup/setup_rclone.sh
```

This configures rclone with two remotes:
- `r2-prod`: Production R2 bucket
- `r2-backup`: Backup R2 bucket

### Enable Backup Trigger on Submission

Add to production `.env`:

```bash
# GitHub Personal Access Token with 'repo' scope
GITHUB_DISPATCH_TOKEN=ghp_xxxxxxxxxxxx
GITHUB_REPO=phaabe/live.moafunk.de
```

### Manual Backup

SSH into Lightsail:

```bash
cd /opt/unheard-backend

# Database only
./scripts/backup/backup-db.sh

# R2 incremental sync
./scripts/backup/backup-r2.sh

# R2 full dated snapshot
./scripts/backup/backup-r2.sh --full

# Both
./scripts/backup/backup-all.sh
```

Or via GitHub Actions:

```bash
# Database + incremental R2
gh workflow run backup.yml

# Full backup (dated R2 snapshot)
gh workflow run backup.yml -f full_r2_backup=true
```

### Restore from Backup

```bash
cd /opt/unheard-backend

# List available database backups
./scripts/backup/restore-db.sh --list

# Restore latest database
./scripts/backup/restore-db.sh --latest

# Restore specific database backup
./scripts/backup/restore-db.sh unheard-db-2025-01-11_12-00-00.db.gz

# List available R2 snapshots
./scripts/backup/restore-r2.sh --list

# Restore R2 from latest incremental
./scripts/backup/restore-r2.sh --latest

# Restore R2 from dated snapshot
./scripts/backup/restore-r2.sh 2025-01-11
```

### Backup Retention

- **Database**: 28 daily backups (4 weeks)
- **R2 snapshots**: 2 weekly snapshots

---

## R2 Management Scripts

Scripts for managing R2 storage directly:

```bash
cd /opt/unheard-backend

# List all objects
./scripts/r2/list.sh

# List with prefix filter
./scripts/r2/list.sh artists/

# Delete specific objects
./scripts/r2/delete.sh artists/123/pic.jpg

# Delete by prefix
./scripts/r2/delete.sh --prefix pending/

# Move/rename object
./scripts/r2/move.sh old/path.jpg new/path.jpg

# Rename prefix (bulk)
./scripts/r2/move.sh --prefix artists/old/ artists/new/

# Check DB-R2 sync (find orphans and missing files)
./scripts/r2/sync-check.sh

# Delete orphaned R2 files (not in database)
./scripts/r2/sync-check.sh --fix-orphans
```
