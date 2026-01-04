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

UNHEARD_IMAGE="${UNHEARD_IMAGE:-}"
UNHEARD_TAG="${UNHEARD_TAG:-latest}"

if [[ -z "$PEM" ]]; then
  echo "PEM is not set. Set PEM to the path of your Lightsail .pem key."
  exit 1
fi

if [[ -z "$UNHEARD_IMAGE" ]]; then
  echo "UNHEARD_IMAGE is not set. Example: UNHEARD_IMAGE=ghcr.io/<owner>/live.moafunk.de-backend"
  exit 1
fi

if [[ -n "$GHCR_TOKEN" && -z "$GHCR_USER" ]]; then
  echo "GHCR_TOKEN is set but GHCR_USER is empty. Set GHCR_USER to your GitHub username."
  exit 1
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

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
LOCAL_COMPOSE_FILE="$REPO_ROOT/backend/docker-compose.prod.yml"

if [[ ! -f "$LOCAL_COMPOSE_FILE" ]]; then
  echo "Missing compose file: $LOCAL_COMPOSE_FILE"
  exit 1
fi

TMP_REMOTE_COMPOSE="/tmp/docker-compose.prod.yml"

echo "Uploading compose file..."
scp -i "$PEM" -o StrictHostKeyChecking=accept-new "$LOCAL_COMPOSE_FILE" "$SSH_HOST:$TMP_REMOTE_COMPOSE" >/dev/null

echo "Preparing server and deploying..."
ssh -i "$PEM" -o StrictHostKeyChecking=accept-new "$SSH_HOST" \
  "set -euo pipefail

   sudo apt-get update -y >/dev/null
   sudo apt-get install -y docker.io docker-compose-plugin >/dev/null
   sudo systemctl enable --now docker >/dev/null

   sudo mkdir -p '$REMOTE_DIR/data'
   sudo chown -R '$SSH_USER':'$SSH_USER' '$REMOTE_DIR'

   mv '$TMP_REMOTE_COMPOSE' '$REMOTE_DIR/docker-compose.prod.yml'

   cd '$REMOTE_DIR'

   if [[ ! -f .env ]]; then
     echo 'WARNING: .env not found in $REMOTE_DIR (.env is required by the container).'
     echo '         Create it from backend/.env.example before relying on this deployment.'
   fi

   if [[ -n '${GHCR_TOKEN}' ]]; then
     echo '${GHCR_TOKEN}' | sudo docker login ghcr.io -u '${GHCR_USER}' --password-stdin >/dev/null
   fi

   export UNHEARD_IMAGE='${UNHEARD_IMAGE}'
   export UNHEARD_TAG='${UNHEARD_TAG}'

   sudo --preserve-env=UNHEARD_IMAGE,UNHEARD_TAG docker compose -f docker-compose.prod.yml pull
   sudo --preserve-env=UNHEARD_IMAGE,UNHEARD_TAG docker compose -f docker-compose.prod.yml up -d

   echo 'Deployed.'
  "

echo "Done."