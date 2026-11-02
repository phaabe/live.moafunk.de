#!/usr/bin/env bash
# Check the monitoring config with the exact image versions the compose file pins.
#
# A config field the pinned version doesn't know makes that service crash-loop.
# 2026-11-02: alertmanager.yml gained telegram `message_thread_id`, which
# Alertmanager v0.27.0 rejects, and the deploy took Alertmanager down.
# deploy_monitoring.sh runs this on the staged config BEFORE it goes live.
#
# Usage: mon-check-config.sh DIR ENV_FILE
#   DIR       the monitoring config dir (this folder, or the deploy staging dir)
#   ENV_FILE  env with TELEGRAM_* for rendering alertmanager.yml.tmpl
set -euo pipefail

DIR="${1:?usage: mon-check-config.sh DIR ENV_FILE}"
ENV_FILE="${2:?usage: mon-check-config.sh DIR ENV_FILE}"
DIR=$(cd "$DIR" && pwd)

image_of() { # image name -> the "name:tag" pinned in the compose file
  local ref
  ref=$(sed -nE "s#^[[:space:]]*image:[[:space:]]*($1:[^[:space:]]+).*#\1#p" \
    "$DIR/docker-compose.monitoring.yml" | head -n 1)
  if [[ -z "$ref" ]]; then
    echo "mon-check-config: no pinned $1 image in docker-compose.monitoring.yml" >&2
    exit 1
  fi
  echo "$ref"
}
am_image=$(image_of prom/alertmanager)
prom_image=$(image_of prom/prometheus)
bbx_image=$(image_of prom/blackbox-exporter)

# Rendered with the real token on the box: mktemp is 0600, removed on exit.
rendered=$(mktemp)
trap 'rm -f "$rendered"' EXIT
(
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  : "${TELEGRAM_TOPIC_ID:=26}"
  set +a
  envsubst <"$DIR/alertmanager/alertmanager.yml.tmpl" >"$rendered"
)

# --user 0: the images run as nobody, which can't read the 0600 rendered file.
echo "==> $am_image: amtool check-config"
docker run --rm --user 0 --entrypoint amtool \
  -v "$rendered:/cfg/alertmanager.yml:ro" "$am_image" check-config /cfg/alertmanager.yml
echo "==> $prom_image: promtool check config (+ rules)"
docker run --rm --user 0 --entrypoint promtool \
  -v "$DIR/prometheus:/etc/prometheus:ro" "$prom_image" check config /etc/prometheus/prometheus.yml
echo "==> $bbx_image: --config.check"
docker run --rm --user 0 \
  -v "$DIR/blackbox:/etc/blackbox:ro" "$bbx_image" --config.file=/etc/blackbox/blackbox.yml --config.check
echo "mon-check-config: all configs valid"
