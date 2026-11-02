#!/usr/bin/env bash
# Make sure Prometheus is on BOTH of its networks. Run by monitoring.service
# (ExecStartPost) on every start: deploys and reboots.
#
# Prometheus needs the project default net (icecast-exporter, blackbox-exporter,
# alertmanager) AND the external backend net (unheard-api). On the box
# (docker-compose v2.24.6, Docker 29), compose attaches it to only ONE of the two
# when it creates the container, and not always the same one. In June it missed the
# backend net. After the 2026-09-25 reboot it missed the default net: every exporter
# scrape and every alert to Alertmanager failed ("server misbehaving") for 38 days.
#
# Connects each missing net (idempotent). Any failure exits non-zero, so the unit
# fails loudly instead of leaving monitoring blind.
set -euo pipefail

PROJECT=moafunk-monitoring
SERVICE=prometheus
NETWORKS=("${PROJECT}_default" "${BACKEND_NETWORK:-unheard-backend_default}")

cid=$(docker ps -q \
  -f "label=com.docker.compose.project=$PROJECT" \
  -f "label=com.docker.compose.service=$SERVICE" | head -n 1)
if [[ -z "$cid" ]]; then
  echo "mon-attach-networks: no running $SERVICE container in project $PROJECT" >&2
  exit 1
fi

attached=" $(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$cid") "
for net in "${NETWORKS[@]}"; do
  if [[ "$attached" == *" $net "* ]]; then
    continue
  fi
  echo "mon-attach-networks: connecting $SERVICE to $net"
  # The alias keeps `prometheus` resolvable on that net (Grafana's datasource uses it).
  docker network connect --alias "$SERVICE" "$net" "$cid"
done
echo "mon-attach-networks: $SERVICE is on ${NETWORKS[*]}"
