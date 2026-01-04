#!/usr/bin/env bash
set -euo pipefail

# Deploy the backend to a Lightsail instance by pulling the latest GHCR image.
#
# Requirements (local machine):
# - aws CLI configured (optional; only needed if you want IP lookup via STATIC_IP)
# - ssh + scp
#
# Requirements (Lightsail instance):
# - Ubuntu (script installs docker + compose plugin if missing)
#
# Example:
#   REGION=eu-central-1 STATIC_IP=unheard-backend-ip \
#   PEM=~/.ssh/unheard-key.pem \
#   GHCR_USER=phaabe GHCR_TOKEN=*** \
#   UNHEARD_IMAGE=ghcr.io/phaabe/live.moafunk.de-backend UNHEARD_TAG=latest \
#   ./backend/scripts/deploy_lightsail.sh

REGION="${REGION:-$(aws configure get region 2>/dev/null || true)}"
STATIC_IP="${STATIC_IP:-unheard-backend-ip}"
IP="${IP:-}"
SSH_USER="${SSH_USER:-ubuntu}"
PEM="${PEM:-}"
REMOTE_DIR="${REMOTE_DIR:-/opt/unheard-backend}"

GHCR_USER="${GHCR_USER:-}"
GHCR_TOKEN="${GHCR_TOKEN:-}"
GHCR_TOKEN_FILE="${GHCR_TOKEN_FILE:-}"

# Optional: upload a local env file to the server as $REMOTE_DIR/.env
ENV_FILE_PATH="${ENV_FILE_PATH:-}"

UNHEARD_IMAGE="${UNHEARD_IMAGE:-ghcr.io/phaabe/live.moafunk.de-backend}"
UNHEARD_TAG="${UNHEARD_TAG:-latest}"

# Optional: configure nginx (80/443) to reverse-proxy to the backend on 127.0.0.1:8000.
# This is meant to be used together with docker-compose.prod.yml mapping 127.0.0.1:8000:8000.
SETUP_NGINX="${SETUP_NGINX:-0}"
NGINX_DOMAIN="${NGINX_DOMAIN:-}"
CERTBOT_EMAIL="${CERTBOT_EMAIL:-}"
RUN_CERTBOT="${RUN_CERTBOT:-1}"

# Optional: run post-deploy smoke tests.
RUN_TESTS="${RUN_TESTS:-1}"

if [[ -z "$PEM" ]]; then
  echo "PEM is not set. Set PEM to the path of your Lightsail .pem key."
  exit 1
fi

if [[ -n "$GHCR_TOKEN" && -z "$GHCR_USER" ]]; then
  echo "GHCR_TOKEN is set but GHCR_USER is empty. Set GHCR_USER to your GitHub username."
  exit 1
fi

# Prefer reading the token from a file when provided.
if [[ -z "$GHCR_TOKEN" && -n "$GHCR_TOKEN_FILE" ]]; then
  if [[ ! -f "$GHCR_TOKEN_FILE" ]]; then
    echo "GHCR_TOKEN_FILE is set but not found: $GHCR_TOKEN_FILE"
    exit 1
  fi
  GHCR_TOKEN=$(<"$GHCR_TOKEN_FILE")
fi

# Convenience: also allow GHCR_TOKEN itself to be a file path.
if [[ -n "$GHCR_TOKEN" && -f "$GHCR_TOKEN" ]]; then
  GHCR_TOKEN=$(<"$GHCR_TOKEN")
fi

# Trim CR/LF that often appear in token files.
if [[ -n "$GHCR_TOKEN" ]]; then
  GHCR_TOKEN=${GHCR_TOKEN//$'\r'/}
  GHCR_TOKEN=${GHCR_TOKEN//$'\n'/}
fi

aws() {
  command aws --region "$REGION" "$@"
}

if [[ -z "$IP" ]]; then
  if [[ -z "$REGION" ]]; then
    echo "IP is not set, and REGION is not set. Set IP directly, or set REGION so STATIC_IP lookup works."
    exit 1
  fi
  if [[ -z "$STATIC_IP" ]]; then
    echo "IP is not set and STATIC_IP is empty. Set IP directly or set STATIC_IP."
    exit 1
  fi
  IP=$(aws lightsail get-static-ip --static-ip-name "$STATIC_IP" --query 'staticIp.ipAddress' --output text)
fi

SSH_HOST="$SSH_USER@$IP"

echo "Deploy target: $SSH_HOST"
echo "Remote dir:   $REMOTE_DIR"
echo "Image:        $UNHEARD_IMAGE:$UNHEARD_TAG"
if [[ "$SETUP_NGINX" == "1" ]]; then
  echo "Nginx:        enabled"
  echo "Domain:       ${NGINX_DOMAIN:-<unset>}"
fi

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
LOCAL_COMPOSE_FILE="$REPO_ROOT/backend/docker-compose.prod.yml"

if [[ ! -f "$LOCAL_COMPOSE_FILE" ]]; then
  echo "Missing compose file: $LOCAL_COMPOSE_FILE"
  exit 1
fi

TMP_REMOTE_COMPOSE="/tmp/docker-compose.prod.yml"

TMP_REMOTE_ENV="/tmp/unheard-backend.env"

echo "Uploading compose file..."
scp -i "$PEM" -o StrictHostKeyChecking=accept-new "$LOCAL_COMPOSE_FILE" "$SSH_HOST:$TMP_REMOTE_COMPOSE" >/dev/null

if [[ -n "$ENV_FILE_PATH" ]]; then
  if [[ ! -f "$ENV_FILE_PATH" ]]; then
    echo "ENV_FILE_PATH is set but not found: $ENV_FILE_PATH"
    exit 1
  fi
  echo "Uploading env file..."
  scp -i "$PEM" -o StrictHostKeyChecking=accept-new "$ENV_FILE_PATH" "$SSH_HOST:$TMP_REMOTE_ENV" >/dev/null
fi

echo "Preparing server and deploying..."
ssh -i "$PEM" -o StrictHostKeyChecking=accept-new "$SSH_HOST" \
  "REMOTE_DIR='$REMOTE_DIR' \
   SSH_USER='$SSH_USER' \
   TMP_REMOTE_COMPOSE='$TMP_REMOTE_COMPOSE' \
  TMP_REMOTE_ENV='$TMP_REMOTE_ENV' \
  HAS_ENV_UPLOAD='${ENV_FILE_PATH:+1}' \
  SETUP_NGINX='${SETUP_NGINX}' \
  NGINX_DOMAIN='${NGINX_DOMAIN}' \
  CERTBOT_EMAIL='${CERTBOT_EMAIL}' \
  RUN_CERTBOT='${RUN_CERTBOT}' \
   GHCR_USER='${GHCR_USER}' \
   GHCR_TOKEN='${GHCR_TOKEN}' \
   UNHEARD_IMAGE='${UNHEARD_IMAGE}' \
   UNHEARD_TAG='${UNHEARD_TAG}' \
   bash -s" <<'REMOTE_SCRIPT'
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
export LANG=C.UTF-8
export LC_ALL=C.UTF-8

sudo apt-get update -y >/dev/null
sudo apt-get install -y docker.io curl ca-certificates locales >/dev/null
sudo locale-gen en_US.UTF-8 >/dev/null 2>&1 || true
sudo update-locale LANG=en_US.UTF-8 >/dev/null 2>&1 || true
sudo systemctl enable --now docker >/dev/null

# Ensure Docker Compose v2 is available.
# Prefer the apt v2 plugin; otherwise install the v2 binary directly.
if ! sudo docker compose version >/dev/null 2>&1; then
  if ! sudo apt-get install -y docker-compose-plugin >/dev/null 2>&1; then
    arch=$(uname -m)
    case "$arch" in
      x86_64|amd64) asset_arch=x86_64 ;;
      aarch64|arm64) asset_arch=aarch64 ;;
      *)
        echo "Unsupported architecture for docker compose: $arch"
        exit 1
        ;;
    esac

    # Install compose v2 directly (pinned to a stable version).
    # Place it as a standalone binary so we can call it even if `docker compose` isn't wired up.
    sudo curl -fsSL "https://github.com/docker/compose/releases/download/v2.24.6/docker-compose-linux-$asset_arch" \
      -o /usr/local/bin/docker-compose
    sudo chmod +x /usr/local/bin/docker-compose
  fi
fi

COMPOSE=""

# Prefer Compose v2 via docker CLI if available.
if sudo docker compose version >/dev/null 2>&1; then
  COMPOSE="sudo --preserve-env=UNHEARD_IMAGE,UNHEARD_TAG -E docker compose"
fi

# Fallback: use the v2 binary directly (works even when docker CLI lacks `compose`).
if [[ -z "$COMPOSE" ]]; then
  for candidate in \
    /usr/local/lib/docker/cli-plugins/docker-compose \
    /usr/local/bin/docker-compose \
    /usr/libexec/docker/cli-plugins/docker-compose \
    /usr/lib/docker/cli-plugins/docker-compose
  do
    if sudo test -x "$candidate"; then
      if sudo "$candidate" version >/dev/null 2>&1; then
        COMPOSE="sudo --preserve-env=UNHEARD_IMAGE,UNHEARD_TAG -E $candidate"
        break
      fi
    fi
  done
fi

if [[ -z "$COMPOSE" ]]; then
  echo "Docker Compose v2 is not available after installation attempts."
  echo "Refusing to use legacy docker-compose v1 (can fail with KeyError: ContainerConfig)."
  exit 1
fi

sudo mkdir -p "$REMOTE_DIR/data"
sudo chown -R "$SSH_USER":"$SSH_USER" "$REMOTE_DIR"

mv "$TMP_REMOTE_COMPOSE" "$REMOTE_DIR/docker-compose.prod.yml"

if [[ "${HAS_ENV_UPLOAD:-}" == "1" ]]; then
  mv "$TMP_REMOTE_ENV" "$REMOTE_DIR/.env"
  chown "$SSH_USER":"$SSH_USER" "$REMOTE_DIR/.env"
  chmod 600 "$REMOTE_DIR/.env"
fi

cd "$REMOTE_DIR"

if [[ ! -f .env ]]; then
  echo "WARNING: .env not found in $REMOTE_DIR (.env is required by the container)."
  echo "         Create it from backend/.env.example before relying on this deployment."
fi

DOCKER_CONFIG_DIR=""
cleanup() {
  if [[ -n "$DOCKER_CONFIG_DIR" && -d "$DOCKER_CONFIG_DIR" ]]; then
    sudo rm -rf "$DOCKER_CONFIG_DIR" || true
  fi
}
trap cleanup EXIT

if [[ -n "${GHCR_TOKEN:-}" ]]; then
  # Avoid storing credentials on disk in /root/.docker/config.json
  DOCKER_CONFIG_DIR=$(mktemp -d)
  export DOCKER_CONFIG="$DOCKER_CONFIG_DIR"
  echo "$GHCR_TOKEN" | sudo -E docker login ghcr.io -u "$GHCR_USER" --password-stdin >/dev/null 2>/dev/null
fi

export UNHEARD_IMAGE
export UNHEARD_TAG

echo "Checking image availability: ${UNHEARD_IMAGE}:${UNHEARD_TAG}"
sudo -E docker pull "${UNHEARD_IMAGE}:${UNHEARD_TAG}" >/dev/null

$COMPOSE -f docker-compose.prod.yml pull
$COMPOSE -f docker-compose.prod.yml down --remove-orphans || true
$COMPOSE -f docker-compose.prod.yml up -d

diagnose_backend() {
  echo "Backend health check failed. Diagnostics:" >&2
  echo "--- compose ps" >&2
  $COMPOSE -f docker-compose.prod.yml ps || true
  echo "--- docker ps -a" >&2
  sudo docker ps -a || true
  echo "--- container logs (unheard-api)" >&2
  sudo docker logs --tail 200 unheard-api || true
}

# Verify the service is reachable locally on the instance.
ok=0
for _ in $(seq 1 30); do
  if curl -fsS --max-time 3 http://127.0.0.1:8000/health >/dev/null; then
    ok=1
    break
  fi
  sleep 1
done

if [[ "$ok" != "1" ]]; then
  diagnose_backend
  exit 1
fi

if [[ "${SETUP_NGINX:-0}" == "1" ]]; then
  if [[ -z "${NGINX_DOMAIN:-}" ]]; then
    echo "SETUP_NGINX=1 but NGINX_DOMAIN is empty."
    exit 1
  fi

  sudo apt-get install -y nginx >/dev/null
  sudo systemctl enable --now nginx >/dev/null

  site_avail="/etc/nginx/sites-available/${NGINX_DOMAIN}"
  site_enabled="/etc/nginx/sites-enabled/${NGINX_DOMAIN}"

  sudo tee "$site_avail" >/dev/null <<EOF
server {
  listen 80;
  server_name ${NGINX_DOMAIN};

  client_max_body_size 120m;

  location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Upgrade \$http_upgrade;
    proxy_set_header Connection 'upgrade';
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;

    proxy_connect_timeout 300;
    proxy_send_timeout 300;
    proxy_read_timeout 300;
  }
}
EOF

  sudo ln -sf "$site_avail" "$site_enabled"
  sudo rm -f /etc/nginx/sites-enabled/default || true
  sudo nginx -t >/dev/null
  sudo systemctl reload nginx

  if [[ "${RUN_CERTBOT:-1}" == "1" ]]; then
    if [[ -z "${CERTBOT_EMAIL:-}" ]]; then
      echo "RUN_CERTBOT=1 but CERTBOT_EMAIL is empty."
      exit 1
    fi

    # Only attempt certbot if DNS already points to this instance.
    ip_from_dns=$(getent ahostsv4 "${NGINX_DOMAIN}" 2>/dev/null | awk '{print $1; exit}' || true)
    instance_ip=$(curl -fsS http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || true)
    if [[ -n "$ip_from_dns" && -n "$instance_ip" && "$ip_from_dns" == "$instance_ip" ]]; then
      sudo apt-get install -y certbot python3-certbot-nginx >/dev/null
      sudo certbot --nginx -d "${NGINX_DOMAIN}" --redirect -m "${CERTBOT_EMAIL}" --agree-tos -n >/dev/null
      sudo nginx -t >/dev/null
      sudo systemctl reload nginx
    else
      echo "Skipping certbot: DNS for ${NGINX_DOMAIN} does not point to this instance yet."
      echo "  DNS:     ${ip_from_dns:-<none>}"
      echo "  Instance:${instance_ip:-<unknown>}"
    fi
  fi
fi

echo 'Deployed.'
REMOTE_SCRIPT

if [[ "$RUN_TESTS" == "1" ]]; then
  if ! command -v curl >/dev/null 2>&1; then
    echo "NOTE: curl not found locally; skipping client-side tests."
  else
    echo ""
    echo "Smoke tests:"
    echo "- Instance local health (via SSH)"
    ssh -i "$PEM" -o StrictHostKeyChecking=accept-new "$SSH_HOST" "curl -fsS http://127.0.0.1:8000/health >/dev/null" \
      && echo "  OK"

    if [[ "$SETUP_NGINX" == "1" && -n "$NGINX_DOMAIN" ]]; then
      echo "- Nginx HTTP via IP (no DNS)"
      curl -fsS --max-time 10 --resolve "${NGINX_DOMAIN}:80:${IP}" "http://${NGINX_DOMAIN}/health" >/dev/null \
        && echo "  OK"

      echo "- Domain check (HTTP)"
      if curl -fsS --max-time 10 "http://${NGINX_DOMAIN}/health" >/dev/null; then
        echo "  OK"
      else
        echo "  SKIP/FAIL (DNS not propagated or firewall issue)"
      fi

      echo "- Domain check (HTTPS)"
      if curl -fsS --max-time 10 "https://${NGINX_DOMAIN}/health" >/dev/null; then
        echo "  OK"
      else
        echo "  SKIP/FAIL (TLS not set up yet or DNS not propagated)"
      fi
    fi
  fi
fi

echo "Done."
