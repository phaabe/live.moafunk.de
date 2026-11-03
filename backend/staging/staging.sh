#!/usr/bin/env bash
# Staging on the production box: a stopgap until staging gets its own server.
# Run on the box as root, from /opt/unheard-staging. Every command that starts
# something first proves that staging cannot hurt production.
#
#   staging.sh setup                one time: render icecast.xml, install the nginx vhost, get TLS
#   staging.sh check-env [env] [stream-env]
#                                   refuse settings that reach production or real accounts
#   staging.sh check                production idle, enough disk and memory
#   staging.sh up sha-<commit>      check, then pull and start that image with the stream chain
#   staging.sh down                 stop staging (keeps its data)
#   staging.sh seed-db              scrubbed copy of the production DB (staging must be down)
#   staging.sh status               show the staging containers and health
set -euo pipefail

STAGING_DIR="${STAGING_DIR:-/opt/unheard-staging}"
PROD_DIR="${PROD_DIR:-/opt/unheard-backend}"
PROD_STREAM_ENV="${PROD_STREAM_ENV:-/etc/moafunk/stream.env}"
BOX_IP="${STAGING_BOX_IP:-178.104.160.103}"
DOMAIN=staging.live.moafunk.de
PROJECT=unheard-staging
API_PORT=8001
HARBOR_PORT=8015
ICECAST_PORT=8020
MIN_FREE_GB=10
MIN_AVAILABLE_MB=1500
SHOW_GUARD_HOURS=2

# Secrets that reach real accounts or production workflows. They must not
# appear in the staging .env at all (an empty RECOVERY_* value fails startup).
FORBIDDEN_KEYS="GITHUB_DISPATCH_TOKEN SOUNDCLOUD_CLIENT_ID SOUNDCLOUD_CLIENT_SECRET
SOUNDCLOUD_ACCESS_TOKEN TELEGRAM_BOT_TOKEN TELEGRAM_ADMIN_CHAT_ID TELEGRAM_LIVE_CHAT_ID
TELEGRAM_INSTAGRAM_ACCOUNT INSTAGRAM_ACCESS_TOKEN_DEV INSTAGRAM_BUSINESS_ACCOUNT_ID_DEV
INSTAGRAM_ACCESS_TOKEN_PROD INSTAGRAM_BUSINESS_ACCOUNT_ID_PROD FACEBOOK_APP_ID
FACEBOOK_APP_SECRET OPENAI_API_KEY"

STREAM_PASSWORD_KEYS="HARBOR_TEST_PASSWORD HARBOR_LIVE_PASSWORD ICECAST_SOURCE_PASSWORD
ICECAST_ADMIN_PASSWORD ICECAST_RELAY_PASSWORD"

die() {
  echo "staging: $*" >&2
  exit 1
}

compose() {
  local file="$STAGING_DIR/docker-compose.staging.yml"
  # Only `up` needs a real tag; other commands just parse the file.
  export STAGING_TAG="${STAGING_TAG:-unused}"
  if docker compose version >/dev/null 2>&1; then
    docker compose -p "$PROJECT" -f "$file" "$@"
  elif command -v docker-compose >/dev/null 2>&1; then
    docker-compose -p "$PROJECT" -f "$file" "$@"
  else
    die "docker compose not found"
  fi
}

# Value of KEY in an env file without surrounding quotes; empty when absent.
env_value() {
  local line
  line="$(grep -E "^$1=" "$2" | head -n 1 || true)"
  line="${line#*=}"
  line="${line%\"}"
  line="${line#\"}"
  line="${line%\'}"
  line="${line#\'}"
  printf '%s' "$line"
}

has_key() {
  grep -qE "^$1=" "$2"
}

problems=0
problem() {
  echo "  $*" >&2
  problems=$((problems + 1))
}

# Mistakes dotenvy turns into silently missing settings, and leftover placeholders.
check_env_file_syntax() {
  local file="$1" line key value dups
  dups="$(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$file" | cut -d= -f1 | sort | uniq -d)"
  [[ -z "$dups" ]] || problem "$(basename "$file"): duplicate keys (dotenvy keeps the first): $(echo "$dups" | tr '\n' ' ')"
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]] || continue
    key="${line%%=*}"
    value="${line#*=}"
    if [[ "$value" == *"<"*">"* ]]; then
      problem "$(basename "$file"): $key still has a <placeholder>"
    fi
    case "$value" in
      \"* | \'*) ;;
      *" "*) problem "$(basename "$file"): $key has an unquoted value with a space (dotenvy skips every later line)" ;;
    esac
  done <"$file"
}

check_env() {
  local file="${1:-$STAGING_DIR/.env}" pair key want value
  [[ -f "$file" ]] || die "missing $file"
  check_env_file_syntax "$file"

  for pair in APP_ENV=staging SOUNDCLOUD_AUTO_UPLOAD=false TELEGRAM_ENABLED=false \
    STREAM_OUTPUT=icecast "ADMIN_BASE_URL=https://$DOMAIN"; do
    key="${pair%%=*}"
    want="${pair#*=}"
    value="$(env_value "$key" "$file")"
    [[ "$value" == "$want" ]] || problem "$key must be '$want' (is '$value')"
  done

  # Unset bucket names fall back to production buckets in the backend.
  for key in R2_BUCKET_NAME R2_SHOWS_BUCKET_NAME; do
    value="$(env_value "$key" "$file")"
    if [[ "$value" != *staging* || "$value" == *prod* ]]; then
      problem "$key must name a staging bucket (is '$value')"
    fi
  done

  for key in ICECAST_URL ICECAST_TEST_URL; do
    value="$(env_value "$key" "$file")"
    [[ "$value" == *"@host.docker.internal:$HARBOR_PORT/"* ]] ||
      problem "$key must target the staging harbor host.docker.internal:$HARBOR_PORT"
  done
  value="$(env_value ICECAST_STATUS_URL "$file")"
  [[ "$value" == *":$ICECAST_PORT/status-json.xsl" ]] ||
    problem "ICECAST_STATUS_URL must use the staging Icecast port $ICECAST_PORT"

  for key in $FORBIDDEN_KEYS; do
    if has_key "$key" "$file"; then problem "$key must not be set in staging"; fi
  done
  if grep -qE '^RECOVERY_[A-Z0-9_]*=' "$file"; then
    problem "RECOVERY_* must not be set in staging"
  fi
}

check_stream_env() {
  local file="${1:-$STAGING_DIR/stream.env}" key value prod_key
  [[ -f "$file" ]] || die "missing $file"
  check_env_file_syntax "$file"
  [[ "$(env_value HARBOR_PORT "$file")" == "$HARBOR_PORT" ]] || problem "HARBOR_PORT must be $HARBOR_PORT"
  [[ "$(env_value ICECAST_PORT "$file")" == "$ICECAST_PORT" ]] || problem "ICECAST_PORT must be $ICECAST_PORT"
  for key in $STREAM_PASSWORD_KEYS; do
    value="$(env_value "$key" "$file")"
    if [[ -z "$value" ]]; then
      problem "$key must be set"
      continue
    fi
    # Production's passwords must never be reused (values are never printed).
    if [[ -r "$PROD_STREAM_ENV" ]]; then
      for prod_key in $STREAM_PASSWORD_KEYS; do
        if [[ "$value" == "$(env_value "$prod_key" "$PROD_STREAM_ENV")" ]]; then
          problem "$key reuses a production password ($prod_key)"
        fi
      done
    fi
  done
}

finish_checks() {
  ((problems == 0)) || die "$problems unsafe setting(s); nothing was started"
}

check_production_idle() {
  local status
  status="$(curl -fsS --max-time 5 http://127.0.0.1:8000/api/stream/status)" ||
    die "production API not reachable on 127.0.0.1:8000"
  [[ "$status" == *'"active":false'* ]] || die "production is on air: $status"
  python3 - "$PROD_DIR/data/unheard.db" "$SHOW_GUARD_HOURS" <<'PY'
import datetime, sqlite3, sys, zoneinfo

db, hours = sys.argv[1], int(sys.argv[2])
tz = zoneinfo.ZoneInfo("Europe/Berlin")
now = datetime.datetime.now(tz)
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
rows = con.execute(
    "SELECT id, title, date, start_time, end_time FROM shows "
    "WHERE start_time IS NOT NULL AND date BETWEEN date('now', '-1 day') AND date('now', '+1 day')"
).fetchall()
busy = []
for show_id, title, date, start, end in rows:
    begin = datetime.datetime.fromisoformat(f"{date}T{start}").replace(tzinfo=tz)
    stop = (
        datetime.datetime.fromisoformat(f"{date}T{end}").replace(tzinfo=tz)
        if end
        else begin + datetime.timedelta(hours=2)
    )
    if stop <= begin:
        stop += datetime.timedelta(days=1)
    if begin - datetime.timedelta(hours=hours) <= now < stop:
        busy.append(f"{show_id} '{title}' {date} {start}-{end or '?'}")
if busy:
    sys.exit(f"staging: production show live or due within {hours} h: " + "; ".join(busy))
PY
}

check_resources() {
  local free_gb available_mb
  free_gb="$(df -BG --output=avail / | tail -n 1 | tr -dc '0-9')"
  ((free_gb >= MIN_FREE_GB)) || die "only ${free_gb} GB free on /, need $MIN_FREE_GB"
  available_mb="$(awk '/^MemAvailable:/ {print int($2 / 1024)}' /proc/meminfo)"
  ((available_mb >= MIN_AVAILABLE_MB)) || die "only ${available_mb} MB memory available, need $MIN_AVAILABLE_MB"
}

check_ports_free() {
  local port
  for port in "$API_PORT" "$HARBOR_PORT" "$ICECAST_PORT"; do
    if ss -ltnH "( sport = :$port )" | grep -q .; then
      die "port $port is already in use"
    fi
  done
}

staging_running() {
  [[ -n "$(compose ps -q)" ]]
}

wait_healthy() {
  local _
  for _ in $(seq 1 30); do
    if curl -fsS --max-time 3 "http://127.0.0.1:$API_PORT/health" >/dev/null; then
      return 0
    fi
    sleep 2
  done
  echo "--- last staging API log lines" >&2
  docker logs --tail 80 unheard-api-staging >&2 || echo "  (no container logs)" >&2
  die "staging API did not become healthy"
}

render_icecast() {
  local stream="$STAGING_DIR/stream.env"
  umask 077
  # Passwords go through the environment, never argv, and are XML-escaped.
  SOURCE_PW="$(env_value ICECAST_SOURCE_PASSWORD "$stream")" \
    RELAY_PW="$(env_value ICECAST_RELAY_PASSWORD "$stream")" \
    ADMIN_PW="$(env_value ICECAST_ADMIN_PASSWORD "$stream")" \
    python3 - "$STAGING_DIR/icecast.staging.xml" "$STAGING_DIR/icecast.xml" <<'PY'
import os, sys
from xml.sax.saxutils import escape

src, dst = sys.argv[1], sys.argv[2]
text = open(src).read()
for placeholder, var in (
    ("CHANGE_ME_SOURCE", "SOURCE_PW"),
    ("CHANGE_ME_RELAY", "RELAY_PW"),
    ("CHANGE_ME_ADMIN", "ADMIN_PW"),
):
    if text.count(placeholder) != 1:
        sys.exit(f"staging: {src} must contain {placeholder} exactly once")
    text = text.replace(placeholder, escape(os.environ[var], {'"': "&quot;"}))
with open(dst, "w") as out:
    out.write(text)
PY
  # The Icecast image runs as user icecast; the file must stay readable for it.
  chmod 644 "$STAGING_DIR/icecast.xml"
}

install_vhost() {
  local available="/etc/nginx/sites-available/$DOMAIN" enabled="/etc/nginx/sites-enabled/$DOMAIN" ip
  # Never overwrite: certbot adds the TLS server block to this file.
  [[ -f "$available" ]] || install -m 644 "$STAGING_DIR/nginx-staging.conf" "$available"
  ln -sf "$available" "$enabled"
  if ! nginx -t; then
    rm -f "$enabled"
    die "nginx config test failed; the staging vhost is disabled again"
  fi
  systemctl reload nginx
  ip="$(getent ahostsv4 "$DOMAIN" | awk '{print $1; exit}' || true)"
  if [[ "$ip" != "$BOX_IP" ]]; then
    echo "DNS for $DOMAIN is '${ip:-none}', not $BOX_IP: TLS skipped. Run setup again once the A record exists."
    return 0
  fi
  certbot --nginx -d "$DOMAIN" --redirect -m "$CERTBOT_EMAIL" --agree-tos -n
  nginx -t
  systemctl reload nginx
}

cmd_setup() {
  local file
  [[ -n "${CERTBOT_EMAIL:-}" ]] || die "set CERTBOT_EMAIL for the TLS certificate"
  for file in docker-compose.staging.yml icecast.staging.xml nginx-staging.conf moafunk.liq .env stream.env; do
    [[ -f "$STAGING_DIR/$file" ]] || die "missing $STAGING_DIR/$file"
  done
  check_env
  check_stream_env
  finish_checks
  chmod 600 "$STAGING_DIR/.env" "$STAGING_DIR/stream.env"
  mkdir -p "$STAGING_DIR/data"
  render_icecast
  install_vhost
  echo "setup done. Start staging with: staging.sh up sha-<commit>"
}

cmd_check() {
  check_env
  check_stream_env
  finish_checks
  check_production_idle
  check_resources
  echo "ok: staging may start"
}

cmd_up() {
  local tag="${1:-}"
  [[ "$tag" =~ ^sha-[0-9a-f]{7,40}$ ]] || die "usage: staging.sh up sha-<commit>"
  [[ -f "$STAGING_DIR/icecast.xml" ]] || die "run staging.sh setup first"
  cmd_check
  staging_running || check_ports_free
  export STAGING_TAG="$tag"
  compose pull api
  compose up -d
  wait_healthy
  echo "staging runs $tag: https://$DOMAIN"
}

cmd_down() {
  compose down
  echo "staging stopped; data kept in $STAGING_DIR/data"
}

cmd_seed_db() {
  local target="$STAGING_DIR/data/unheard.db"
  ! staging_running || die "stop staging first: staging.sh down"
  mkdir -p "$STAGING_DIR/data"
  rm -f "$target" "$target-wal" "$target-shm"
  python3 - "$PROD_DIR/data/unheard.db" "$target" <<'PY'
import sqlite3, sys

source = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
copy = sqlite3.connect(sys.argv[2])
source.backup(copy)
source.close()
# Production credentials must not work in staging: no sessions, no stored
# tokens (the SoundCloud token lives in app_settings), no usable passwords.
copy.execute("DELETE FROM sessions")
copy.execute("DELETE FROM app_settings WHERE key LIKE '%token%' OR key LIKE '%secret%'")
copy.execute("UPDATE users SET password_hash = '!'")
copy.commit()
copy.close()
PY
  echo "staging DB is now a scrubbed copy of production: no sessions, no stored tokens, no usable passwords."
  echo "Log in with the staging superadmin and create staging test accounts."
}

cmd_status() {
  compose ps
  if curl -fsS --max-time 3 "http://127.0.0.1:$API_PORT/health" >/dev/null; then
    echo "staging API healthy: https://$DOMAIN"
  else
    echo "staging API not running"
  fi
}

main() {
  local cmd="${1:-}"
  [[ $# -gt 0 ]] && shift
  case "$cmd" in
    setup) cmd_setup ;;
    check-env)
      check_env "${1:-}"
      check_stream_env "${2:-}"
      finish_checks
      echo "env ok"
      ;;
    check) cmd_check ;;
    up) cmd_up "${1:-}" ;;
    down) cmd_down ;;
    seed-db) cmd_seed_db ;;
    status) cmd_status ;;
    *)
      sed -n '5,13p' "$0" >&2
      exit 2
      ;;
  esac
}

main "$@"
