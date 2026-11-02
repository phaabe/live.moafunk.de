#!/usr/bin/env bash
# Tests for mon-check-config.sh. Needs docker (pulls the pinned prom/* images).
# Run: docs/stream-rework/prod/monitoring/mon-check-config.test.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/mon-check-config.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
fail=0

printf '%s\n' TELEGRAM_BOT_TOKEN=123456:dummy TELEGRAM_ADMIN_CHAT_ID=-1000000000000 \
  WATCHDOG_PING_URL=https://hc-ping.com/00000000-0000-0000-0000-000000000000 >"$TMP/env"

check() { # description want(pass|fail) dir [env]
  local got=pass
  "$SCRIPT" "$3" "${4:-$TMP/env}" >"$TMP/out" 2>&1 || got=fail
  if [[ "$got" == "$2" ]]; then echo "ok   $1"; else
    echo "FAIL $1 (want $2, got $got)"; sed 's/^/     /' "$TMP/out"; fail=1
  fi
}
copy() { rm -rf "$TMP/cfg" && cp -R "$HERE" "$TMP/cfg"; }

check "repo config is valid" pass "$HERE"

# 2026-11-02: message_thread_id on Alertmanager v0.27.0 crash-looped prod.
copy
sed -i.bak 's#prom/alertmanager:v[0-9.]*#prom/alertmanager:v0.27.0#' "$TMP/cfg/docker-compose.monitoring.yml"
check "alertmanager v0.27.0 rejects message_thread_id" fail "$TMP/cfg"

copy
printf '      - alert: Broken\n        expr: up ==\n' >>"$TMP/cfg/prometheus/rules/stream-alerts.yml"
check "broken alert rule is caught" fail "$TMP/cfg"

copy
sed -i.bak 's#fail_if_not_ssl:#fail_if_not_tls:#' "$TMP/cfg/blackbox/blackbox.yml"
check "unknown blackbox field is caught" fail "$TMP/cfg"

# Without its own route, the always-firing Watchdog would fall through to Telegram.
copy
sed -i.bak '/alertname = "Watchdog"/,/repeat_interval: 5m/d' "$TMP/cfg/alertmanager/alertmanager.yml.tmpl"
check "Watchdog routed to Telegram is caught" fail "$TMP/cfg"

# An empty heartbeat url makes alertmanager.yml invalid.
grep -v '^WATCHDOG_PING_URL=' "$TMP/env" >"$TMP/env-no-watchdog"
check "missing WATCHDOG_PING_URL is caught" fail "$HERE" "$TMP/env-no-watchdog"

copy
sed -i.bak '/prom\/prometheus:/d' "$TMP/cfg/docker-compose.monitoring.yml"
check "missing pinned image is an error" fail "$TMP/cfg"

exit "$fail"
