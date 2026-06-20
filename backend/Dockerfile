# Build stage
FROM rust:1.92-slim-bookworm AS builder

WORKDIR /app

# Install dependencies for building
RUN apt-get update && apt-get install -y pkg-config libssl-dev && rm -rf /var/lib/apt/lists/*

# Copy manifests
COPY backend/Cargo.toml ./

# Create dummy source to cache dependencies
RUN mkdir src && echo "fn main() {}" > src/main.rs
RUN cargo build --release
RUN rm -rf src

# Copy actual source
COPY backend/src ./src

# Build the actual binary
RUN touch src/main.rs && cargo build --release

# Frontend build stage for admin SPA
FROM node:20-slim AS frontend-builder

WORKDIR /app

# Copy frontend files
COPY frontend/package*.json ./
RUN npm ci

COPY frontend ./
# Stream URLs are baked into the admin SPA at build time (Vite inlines
# import.meta.env.VITE_*). Without these the admin's stream config is empty —
# e.g. "Test Your Stream" reports "not configured". Passed as build args from CI
# (same repo Variables the Pages build uses); empty is a safe no-op fallback.
ARG VITE_STREAM_ICECAST_LIVE_URL=""
ARG VITE_STREAM_ICECAST_TEST_URL=""
ARG VITE_STREAM_STATUS_URL=""
ENV VITE_STREAM_ICECAST_LIVE_URL=$VITE_STREAM_ICECAST_LIVE_URL \
    VITE_STREAM_ICECAST_TEST_URL=$VITE_STREAM_ICECAST_TEST_URL \
    VITE_STREAM_STATUS_URL=$VITE_STREAM_STATUS_URL
RUN npm run build

# Runtime stage
FROM debian:bookworm-slim

WORKDIR /app

# Install runtime dependencies (FFmpeg for video composition via showwaves filter)
RUN apt-get update && apt-get install -y ca-certificates curl ffmpeg && \
  rm -rf /var/lib/apt/lists/*

# Copy binary from builder
COPY --from=builder /app/target/release/unheard-backend /app/unheard-backend

# Copy stamping assets (fonts/logos)
COPY backend/assets ./assets

# Copy admin SPA from frontend builder
# 1. Admin SPA HTML entry point (index.html for client-side routing)
COPY --from=frontend-builder /app/dist/admin ./static/admin
# 2. Vite-built JS/CSS bundles (shared across all entry points)
COPY --from=frontend-builder /app/dist/assets ./static/admin/assets
# 3. Public assets needed by admin SPA (logo, fonts, favicon)
COPY --from=frontend-builder /app/dist/moafunk.png ./static/admin/moafunk.png
COPY --from=frontend-builder /app/dist/Shoika-font ./static/admin/Shoika-font
COPY --from=frontend-builder /app/dist/icons ./static/admin/icons

# Create data directory
RUN mkdir -p /app/data

EXPOSE 8000

CMD ["./unheard-backend"]
