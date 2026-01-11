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
