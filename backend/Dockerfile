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
RUN npm run build

# Runtime stage
FROM debian:bookworm-slim

WORKDIR /app

# Install runtime dependencies (including FFmpeg for audio streaming)
RUN apt-get update && apt-get install -y ca-certificates curl ffmpeg && rm -rf /var/lib/apt/lists/*

# Copy binary from builder
COPY --from=builder /app/target/release/unheard-backend /app/unheard-backend

# Copy stamping assets (fonts/logos)
COPY backend/assets ./assets

# Copy admin SPA from frontend builder
COPY --from=frontend-builder /app/dist/admin ./static/admin
COPY --from=frontend-builder /app/dist/assets ./static/admin/assets

# Create data directory
RUN mkdir -p /app/data

EXPOSE 8000

CMD ["./unheard-backend"]
