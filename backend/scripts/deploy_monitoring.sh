#!/usr/bin/env bash
#
# Deploy the stream observability stack (#178) to the Hetzner box.
#
# Ships docs/stream-rework/prod/monitoring/ to /etc/moafunk/monitoring, writes
# monitoring.env from the passed-in secrets, installs the systemd unit, and
# (re)starts it. The unit renders alertmanager.yml via envsubst and runs
# `docker compose up -d` (Prometheus/Alertmanager/Blackbox/icecast_exporter/
# Grafana). All UIs bind to 127.0.0.1 on the box — reach them via SSH tunnel.
#
# This is additive and safe to re-run: it never touches the backend, the stream
# stack, or DNS. Idempotent — a second run just re-syncs config and restarts.
#
# Requirements (local/runner): ssh + scp. The box must already run docker and
# the backend (the `backend` docker network must exist — see BACKEND_NETWORK).
#
# Driven by the `deploy-monitoring` CI job (secrets from Bitwarden), or run
# locally:
#   IP=1.2.3.4 SSH_KEY=~/.ssh/hetzner.pem \
#   TELEGRAM_BOT_TOKEN=... TELEGRAM_ADMIN_CHAT_ID=... \
#   ./backend/scripts/deploy_monitoring.sh
set -euo pipefail

IP="${IP:?IP is required (Hetzner box public IP)}"
SSH_KEY="${SSH_KEY:?SSH_KEY is required (path to the private key)}"
SSH_USER="${SSH_USER:-root}"
REMOTE_DIR="${REMOTE_DIR:-/etc/moafunk/monitoring}"

# Monitoring config inputs (all but the Telegram pair have safe defaults).
TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:?TELEGRAM_BOT_TOKEN is required}"
TELEGRAM_ADMIN_CHAT_ID="${TELEGRAM_ADMIN_CHAT_ID:?TELEGRAM_ADMIN_CHAT_ID is required}"
ICECAST_STATUS_URI="${ICECAST_STATUS_URI:-http://host.docker.internal:8010/status-json.xsl}"
BACKEND_NETWORK="${BACKEND_NETWORK:-unheard-backend_default}"
# Grafana is localhost-only (SSH tunnel); default kept weak deliberately — set
# GRAFANA_ADMIN_PASSWORD to override. Grafana is the least-sensitive component.
GRAFANA_ADMIN_PASSWORD="${GRAFANA_ADMIN_PASSWORD:-admin}"

SSH_HOST="$SSH_USER@$IP"
SSH_OPTS=(-i "$SSH_KEY" -o StrictHostKeyChecking=accept-new)

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
LOCAL_MON_DIR="$REPO_ROOT/docs/stream-rework/prod/monitoring"

if [[ ! -f "$LOCAL_MON_DIR/docker-compose.monitoring.yml" ]]; then
  echo "ERROR: monitoring dir not found at $LOCAL_MON_DIR" >&2
  exit 1
fi

echo "==> Deploying observability stack to $SSH_HOST:$REMOTE_DIR"

# 1. Ship the config tree to a staging dir (scp -r preserves the layout).
ssh "${SSH_OPTS[@]}" "$SSH_HOST" "rm -rf /tmp/moafunk-monitoring && mkdir -p /tmp/moafunk-monitoring"
scp "${SSH_OPTS[@]}" -r "$LOCAL_MON_DIR/." "$SSH_HOST:/tmp/moafunk-monitoring/" >/dev/null

# 2. Install: move into place, write env (chmod 600), enable the unit. The env
#    values are piped over stdin (never on the command line / process list).
printf '%s\n' \
  "TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN" \
  "TELEGRAM_ADMIN_CHAT_ID=$TELEGRAM_ADMIN_CHAT_ID" \
  "ICECAST_STATUS_URI=$ICECAST_STATUS_URI" \
  "BACKEND_NETWORK=$BACKEND_NETWORK" \
  "GRAFANA_ADMIN_PASSWORD=$GRAFANA_ADMIN_PASSWORD" \
  | ssh "${SSH_OPTS[@]}" "$SSH_HOST" \
      "REMOTE_DIR='$REMOTE_DIR' bash -s" <<'REMOTE_SCRIPT'
set -euo pipefail

# Capture the env piped on stdin before anything else reads it.
ENV_CONTENT="$(cat)"

sudo mkdir -p "$REMOTE_DIR"
# Sync config (preserve the gitignored monitoring.env / rendered alertmanager.yml
# across redeploys by writing env separately below; --delete would wipe them, so
# copy without deleting and let the env write be authoritative).
sudo cp -r /tmp/moafunk-monitoring/. "$REMOTE_DIR/"
rm -rf /tmp/moafunk-monitoring

# Write monitoring.env (root:600 — holds the Telegram token).
printf '%s\n' "$ENV_CONTENT" | sudo tee "$REMOTE_DIR/monitoring.env" >/dev/null
sudo chmod 600 "$REMOTE_DIR/monitoring.env"

# Verify the backend network exists (Prometheus joins it to scrape unheard-api).
BACKEND_NET="$(grep -E '^BACKEND_NETWORK=' "$REMOTE_DIR/monitoring.env" | cut -d= -f2-)"
if ! sudo docker network inspect "$BACKEND_NET" >/dev/null 2>&1; then
  echo "WARNING: docker network '$BACKEND_NET' not found — Prometheus 'backend' job"
  echo "         will fail to start until the backend compose is up. Existing nets:"
  sudo docker network ls --format '  {{.Name}}'
fi

# Install / refresh the systemd unit and (re)start. The unit calls
# mon-compose.sh (resolves `docker compose` vs `docker-compose`) — make it
# executable before starting.
sudo chmod +x "$REMOTE_DIR/mon-compose.sh"
sudo cp "$REMOTE_DIR/monitoring.service" /etc/systemd/system/monitoring.service
sudo systemctl daemon-reload
sudo systemctl enable monitoring >/dev/null 2>&1 || true

# Fail loudly if the unit doesn't come up — a green deploy must mean a running
# stack (the previous version's readiness check was non-fatal and hid a failure).
if ! sudo systemctl restart monitoring; then
  echo "ERROR: 'systemctl restart monitoring' failed. Recent journal:" >&2
  sudo journalctl -u monitoring -n 40 --no-pager >&2 || true
  exit 1
fi
if ! sudo systemctl is-active --quiet monitoring; then
  echo "ERROR: monitoring.service is not active after restart. Status + journal:" >&2
  sudo systemctl status monitoring --no-pager >&2 || true
  sudo journalctl -u monitoring -n 40 --no-pager >&2 || true
  exit 1
fi

echo "==> monitoring.service active; containers:"
sleep 5
sudo "$REMOTE_DIR/mon-compose.sh" -f "$REMOTE_DIR/docker-compose.monitoring.yml" ps || true
REMOTE_SCRIPT

# 3. Smoke-check Prometheus readiness over the localhost-bound port. Retry for
#    up to 60s — on a first deploy the upstream images are still being pulled.
echo "==> Waiting for Prometheus readiness (localhost:9090 on the box, up to 60s)"
ready=0
for _ in $(seq 1 12); do
  if ssh "${SSH_OPTS[@]}" "$SSH_HOST" \
       "curl -fsS --max-time 5 http://127.0.0.1:9090/-/ready >/dev/null 2>&1"; then
    ready=1
    break
  fi
  sleep 5
done
if [[ "$ready" == 1 ]]; then
  echo "==> Prometheus is ready."
else
  echo "WARNING: Prometheus not ready after 60s — check 'journalctl -u monitoring' and"
  echo "         'mon-compose.sh -f $REMOTE_DIR/docker-compose.monitoring.yml logs' on the box."
fi

echo "==> Done. Tunnel to view UIs, e.g.:"
echo "    ssh -i <key> -L 9090:localhost:9090 -L 3000:localhost:3000 $SSH_HOST"
