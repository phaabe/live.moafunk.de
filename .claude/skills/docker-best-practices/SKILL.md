---
name: docker-best-practices
description: Use whenever writing, reviewing, or debugging Dockerfiles, docker-compose files, or container-based deployments. Covers multi-stage builds, layer caching, security, image size, and compose patterns.
---

# Docker Best Practices Skill

## Image hygiene

- **Pin base image versions.** `python:3.12-slim` not `python:latest`. Even better: pin by digest (`python:3.12-slim@sha256:...`).
- **Prefer `-slim` / `-alpine` / distroless** over full distros. Use full distros only when you need build tooling at runtime.
- **Run as a non-root user.** Last `USER appuser` (uid > 1000). Most apps don't need root.
- **One process per container.** No `supervisord`, no `&&` daemon stacks. If you need cron + app, use an orchestrator-level pattern.
- **Minimise layers.** Combine related `RUN` steps with `&&`. But split aggressively where caching matters (deps before source).

## Multi-stage builds (always for compiled / bundled apps)

```dockerfile
# syntax=docker/dockerfile:1.7

FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json pnpm-lock.yaml ./
RUN --mount=type=cache,target=/root/.local/share/pnpm/store \
    corepack enable && pnpm install --frozen-lockfile

FROM node:22-alpine AS build
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN pnpm build

FROM node:22-alpine AS runtime
WORKDIR /app
ENV NODE_ENV=production
RUN addgroup -S app && adduser -S app -G app
COPY --from=build --chown=app:app /app/dist ./dist
COPY --from=deps --chown=app:app /app/node_modules ./node_modules
USER app
EXPOSE 3000
CMD ["node", "dist/index.js"]
```

Same template applies to Python (build stage compiles wheels, runtime copies them) and Go (build stage produces the static binary, runtime is `FROM scratch` or `gcr.io/distroless/static`).

## Cache discipline (what makes `docker build` fast)

1. **Order from least to most volatile.** Base image → system deps → language deps → app source → build artefact.
2. **Copy lockfiles before source.** `COPY package.json package-lock.json ./` then `RUN npm ci` then `COPY .`. Source changes don't bust the deps layer.
3. **`.dockerignore`** is mandatory — at minimum: `.git`, `node_modules`, `__pycache__`, `*.log`, `.env*`, `dist`, `build`, `.venv`.
4. Use **BuildKit cache mounts** for package caches: `RUN --mount=type=cache,target=...`.
5. Use `--mount=type=secret` for secrets — never `ARG SECRET=` (bakes into image).

## Security

- Don't run as root. Don't `chmod 777`.
- Scan images: `docker scout cves` (or `trivy`).
- Don't embed secrets in `ENV` / `LABEL` / image. Use runtime secrets (Docker Swarm secrets, K8s secrets, AWS Secrets Manager).
- `HEALTHCHECK` is a security signal too — orchestrators kill unhealthy containers.
- Drop capabilities at runtime: `--cap-drop=ALL --cap-add=NET_BIND_SERVICE`.
- Read-only root filesystem: `--read-only --tmpfs /tmp`.

## Compose v2 patterns

```yaml
# compose.yaml  (NOT docker-compose.yaml — v2 uses compose.yaml)
name: my-app

services:
  app:
    build:
      context: .
      target: runtime
    image: my-app:dev
    environment:
      DATABASE_URL: postgres://app:app@db:5432/app
    depends_on:
      db: { condition: service_healthy }
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "wget", "-qO-", "http://localhost:3000/health"]
      interval: 10s
      timeout: 3s
      retries: 5

  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: app
      POSTGRES_PASSWORD: app
      POSTGRES_DB: app
    volumes: ["pgdata:/var/lib/postgresql/data"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U app"]
      interval: 5s
      timeout: 3s
      retries: 5

volumes:
  pgdata:
```

- Use `depends_on: condition: service_healthy` — never assume `up` order alone is enough.
- Profiles for opt-in services (`profiles: [dev]`).
- Override files (`compose.override.yaml`) for local-only tweaks.
- Don't expose internal services (`db`) on host ports unless you need to.

## Image size targets

| Stack | Target |
|-------|--------|
| Static binary (Go / Rust) | < 30 MB (distroless / scratch) |
| Node app | < 200 MB (alpine, multi-stage) |
| Python app | < 200 MB (slim, multi-stage) |
| JVM app | < 250 MB (jlink + distroless) |

If you're far over, look for: dev deps in runtime image, unstripped binaries, build caches copied in.

## Anti-patterns

- `apt-get update` without `&& apt-get install ... && rm -rf /var/lib/apt/lists/*` in the same `RUN`.
- `COPY . .` early in the Dockerfile (cache-busts on every change).
- No `.dockerignore`.
- `latest` tag in production.
- Building inside the runtime stage.
- `EXPOSE` lying about the port.
