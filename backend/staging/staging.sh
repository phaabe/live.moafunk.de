#!/usr/bin/env bash
# Staging on its own server (staging.live.moafunk.de). Run as root on the
# staging box from /opt/unheard-staging. Only export-db runs on the production
# box. Every command that starts something first proves that staging cannot
# reach production data or real accounts.
#
#   staging.sh setup                one time (safe to repeat): packages, Icecast image, icecast.xml, nginx, TLS
#   staging.sh install-env          read STAGING_*=value lines on stdin, write .env and stream.env
#   staging.sh check-env [env] [stream-env]
#                                   refuse settings that reach production or real accounts
#   staging.sh check-r2             the R2 token reaches the staging buckets and no production bucket
#   staging.sh up sha-<commit> [sha256:<digest>]
#                                   check, then pull and start that image with the stream chain
#   staging.sh hold <reason>        pause CI deploys (for release checks); staging.sh unhold resumes them
#   staging.sh ci-deploy            forced command of the CI key: "status" or "deploy sha-<commit> sha256:<digest>"
#                                   (and for the chaos test show: e2e-hold, e2e-unhold, e2e-restart-liquidsoap, e2e-cut,
#                                   e2e-recordings)
#   staging.sh install-ci-key       let the CI key (ci-deploy.pub) run only ci-deploy (part of setup)
#   staging.sh down                 stop staging (keeps its data)
#   staging.sh status               containers, running tag, health
#   staging.sh export-db            ON PRODUCTION: write a scrubbed copy of the production DB to stdout
#   staging.sh seed-db <file|->     replace the staging DB with a scrubbed copy
#   staging.sh users                set the staging logins (staging-admin, staging-host) from .env
set -euo pipefail

STAGING_DIR="${STAGING_DIR:-/opt/unheard-staging}"
PROD_DB="${PROD_DB:-/opt/unheard-backend/data/unheard.db}"
DOMAIN=staging.live.moafunk.de
PROJECT=unheard-staging
API_PORT=8000
HARBOR_PORT=8005
ICECAST_PORT=8010
ICECAST_IMAGE=moafunk/icecast-kh:kh22
API_IMAGE=ghcr.io/phaabe/live.moafunk.de-backend
AUTHORIZED_KEYS="${STAGING_AUTHORIZED_KEYS:-/root/.ssh/authorized_keys}"
HOLD_EXIT=3 # ci-deploy: staging is on hold, nothing deployed
STAGING_BUCKETS="unheard-artists-staging moafunk-staging"
PROD_BUCKETS="unheard-artists-prod unheard-artists-dev moafunk-prod"

# Values install-env reads from stdin (push-secrets.sh sends them from Bitwarden).
SECRET_NAMES="STAGING_SECRET_KEY STAGING_SUPERADMIN_PASSWORD STAGING_HOST_PASSWORD STAGING_R2_ACCESS_KEY_ID
STAGING_R2_SECRET_ACCESS_KEY STAGING_HARBOR_TEST_PASSWORD STAGING_HARBOR_LIVE_PASSWORD
STAGING_ICECAST_SOURCE_PASSWORD STAGING_ICECAST_ADMIN_PASSWORD STAGING_ICECAST_RELAY_PASSWORD"

# Login passwords: stored only as argon2 hashes, so any characters are fine.
LOGIN_PASSWORDS="STAGING_SUPERADMIN_PASSWORD STAGING_HOST_PASSWORD"

STREAM_PASSWORD_KEYS="HARBOR_TEST_PASSWORD HARBOR_LIVE_PASSWORD ICECAST_SOURCE_PASSWORD
ICECAST_ADMIN_PASSWORD ICECAST_RELAY_PASSWORD"

die() {
  echo "staging: $*" >&2
  exit 1
}

# One temp dir per run, removed on every exit (it can hold secrets or a DB copy).
TMP_DIR=""
cleanup() {
  [[ -z "$TMP_DIR" ]] || rm -rf "$TMP_DIR"
}
trap cleanup EXIT
make_tmp() {
  TMP_DIR="$(mktemp -d)"
}

compose() {
  local file="$STAGING_DIR/docker-compose.staging.yml"
  # `up` sets the tag; other commands reuse the running one (or just parse the file).
  if [[ -z "${STAGING_TAG:-}" ]]; then
    STAGING_TAG="$(cat "$STAGING_DIR/tag" 2>/dev/null || echo unused)"
  fi
  export STAGING_TAG
  if docker compose version >/dev/null 2>&1; then
    docker compose -p "$PROJECT" -f "$file" "$@"
  elif command -v docker-compose >/dev/null 2>&1; then
    docker-compose -p "$PROJECT" -f "$file" "$@"
  else
    die "neither 'docker compose' nor 'docker-compose' found"
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

problems=0
problem() {
  echo "  $*" >&2
  problems=$((problems + 1))
}

finish_checks() {
  ((problems == 0)) || die "$problems unsafe setting(s); nothing was changed"
}

# Mistakes dotenvy turns into silently missing settings, and leftover placeholders.
check_env_file_syntax() {
  local file="$1" name line key value dups n=0
  name="$(basename "$file")"
  dups="$(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$file" | cut -d= -f1 | sort | uniq -d)"
  [[ -z "$dups" ]] || problem "$name: duplicate keys (dotenvy keeps the first): $(echo "$dups" | tr '\n' ' ')"
  while IFS= read -r line || [[ -n "$line" ]]; do
    n=$((n + 1))
    [[ -z "$line" || "$line" == \#* ]] && continue
    if [[ ! "$line" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]]; then
      problem "$name: line $n is not KEY=value (no 'export', no leading spaces)"
      continue
    fi
    key="${line%%=*}"
    value="${line#*=}"
    if [[ "$value" == *"<"*">"* ]]; then
      problem "$name: $key still has a <placeholder>"
    fi
    case "$value" in
      \"* | \'*) ;;
      *" "*) problem "$name: $key has an unquoted value with a space (dotenvy skips every later line)" ;;
    esac
  done <"$file"
}

# True when KEY reaches real accounts or production workflows. Prefixes, so a
# setting added later is refused too. An empty RECOVERY_* value fails startup.
forbidden_key() {
  case "$1" in
    SOUNDCLOUD_AUTO_UPLOAD | TELEGRAM_ENABLED) return 1 ;;
    SOUNDCLOUD_* | TELEGRAM_* | INSTAGRAM_* | FACEBOOK_* | OPENAI_* | RECOVERY_* | GITHUB_DISPATCH_TOKEN) return 0 ;;
  esac
  return 1
}

check_env() {
  local file="${1:-$STAGING_DIR/.env}" pair key want value
  [[ -f "$file" ]] || die "missing $file"
  check_env_file_syntax "$file"

  # Unset bucket names fall back to production buckets, and SoundCloud
  # auto-upload defaults to on, so every value here is required explicitly.
  for pair in APP_ENV=staging "ADMIN_BASE_URL=https://$DOMAIN" STREAM_OUTPUT=icecast \
    SOUNDCLOUD_AUTO_UPLOAD=false TELEGRAM_ENABLED=false \
    R2_BUCKET_NAME=unheard-artists-staging R2_SHOWS_BUCKET_NAME=moafunk-staging \
    "ICECAST_STATUS_URL=http://host.docker.internal:$ICECAST_PORT/status-json.xsl"; do
    key="${pair%%=*}"
    want="${pair#*=}"
    value="$(env_value "$key" "$file")"
    [[ "$value" == "$want" ]] || problem "$key must be '$want' (is '$value')"
  done

  # The producer must push to this box's own harbor, never to another host.
  for pair in ICECAST_URL=live ICECAST_TEST_URL=test; do
    key="${pair%%=*}"
    value="$(env_value "$key" "$file")"
    [[ "$value" =~ ^icecast://source:[^@/]+@host\.docker\.internal:$HARBOR_PORT/${pair#*=}$ ]] ||
      problem "$key must be icecast://source:<password>@host.docker.internal:$HARBOR_PORT/${pair#*=}"
  done

  # The staging logins (see staging_users).
  for key in SUPERADMIN_USERNAME STAGING_HOST_USERNAME; do
    [[ "$(env_value "$key" "$file")" =~ ^[a-z0-9-]+$ ]] || problem "$key must be set (lowercase letters, digits, -)"
  done
  for key in SUPERADMIN_PASSWORD_HASH STAGING_HOST_PASSWORD_HASH; do
    [[ "$(env_value "$key" "$file")" == '$argon2'* ]] || problem "$key must be an argon2 hash"
  done

  for key in $(grep -oE '^[A-Za-z_][A-Za-z0-9_]*=' "$file" | cut -d= -f1); do
    if forbidden_key "$key"; then problem "$key must not be set in staging"; fi
  done
}

check_stream_env() {
  local file="${1:-$STAGING_DIR/stream.env}" key value
  [[ -f "$file" ]] || die "missing $file"
  check_env_file_syntax "$file"
  [[ "$(env_value HARBOR_PORT "$file")" == "$HARBOR_PORT" ]] || problem "HARBOR_PORT must be $HARBOR_PORT"
  [[ "$(env_value ICECAST_PORT "$file")" == "$ICECAST_PORT" ]] || problem "ICECAST_PORT must be $ICECAST_PORT"
  for key in $STREAM_PASSWORD_KEYS; do
    value="$(env_value "$key" "$file")"
    # The harbor passwords sit unescaped in ICECAST_URL, so letters and digits only.
    [[ "$value" =~ ^[A-Za-z0-9]{20,}$ ]] || problem "$key must be at least 20 letters and digits"
  done
}

# The API pushes with the harbor passwords from stream.env (values never printed).
check_env_pair() {
  local env="${1:-$STAGING_DIR/.env}" stream="${2:-$STAGING_DIR/stream.env}" pair url
  [[ -f "$env" && -f "$stream" ]] || return 0
  for pair in ICECAST_URL=HARBOR_LIVE_PASSWORD ICECAST_TEST_URL=HARBOR_TEST_PASSWORD; do
    url="$(env_value "${pair%%=*}" "$env")"
    url="${url#icecast://source:}"
    [[ "${url%%@*}" == "$(env_value "${pair#*=}" "$stream")" ]] ||
      problem "${pair%%=*} must use ${pair#*=} from stream.env"
  done
}

check_all_env() {
  check_env "${1:-}"
  check_stream_env "${2:-}"
  check_env_pair "${1:-}" "${2:-}"
  finish_checks
}

# HTTP status of an R2 list request with the staging token (keys go through curl's stdin, never argv).
r2_status() {
  local env="$STAGING_DIR/.env" account code
  account="$(env_value R2_ACCOUNT_ID "$env")"
  code="$(printf 'user = "%s:%s"\n' "$(env_value R2_ACCESS_KEY_ID "$env")" "$(env_value R2_SECRET_ACCESS_KEY "$env")" |
    curl -sS -o /dev/null -w '%{http_code}' --max-time 20 -K - \
      --aws-sigv4 "aws:amz:auto:s3" \
      -H "x-amz-content-sha256: e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855" \
      "https://$account.r2.cloudflarestorage.com/$1?list-type=2&max-keys=1" || true)"
  printf '%s' "${code:-000}"
}

check_r2() {
  local bucket code
  for bucket in $STAGING_BUCKETS; do
    code="$(r2_status "$bucket")"
    [[ "$code" == 200 ]] || problem "R2 token cannot list $bucket (HTTP $code)"
  done
  for bucket in $PROD_BUCKETS; do
    code="$(r2_status "$bucket")"
    [[ "$code" == 403 ]] || problem "R2 token is not refused by production bucket $bucket (HTTP $code, want 403)"
  done
  finish_checks
  echo "R2 ok: the token reaches $STAGING_BUCKETS and is refused by $PROD_BUCKETS"
}

# Write .env and stream.env from the examples and the STAGING_*=value lines on
# stdin. The superadmin password is stored only as an argon2 hash.
cmd_install_env() {
  local tmp name line value
  command -v argon2 >/dev/null || die "argon2 missing: run staging.sh setup first"
  for name in staging.env.example stream.env.example; do
    [[ -f "$STAGING_DIR/$name" ]] || die "missing $STAGING_DIR/$name"
  done
  umask 077
  make_tmp
  tmp="$TMP_DIR"
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" ]] && continue
    name="${line%%=*}"
    value="${line#*=}"
    case " ${SECRET_NAMES//$'\n'/ } " in
      *" $name "*) ;;
      *) die "unknown name on stdin: '$name'" ;;
    esac
    case " $LOGIN_PASSWORDS " in
      *" $name "*) [[ -n "$value" ]] || die "$name is empty" ;;
      *) [[ "$value" =~ ^[A-Za-z0-9._~+/=-]+$ ]] || die "$name is empty or has characters a .env cannot hold unquoted" ;;
    esac
    [[ ! -e "$tmp/$name" ]] || die "$name was sent twice"
    printf '%s' "$value" >"$tmp/$name"
  done
  for name in $SECRET_NAMES; do
    [[ -s "$tmp/$name" ]] || die "$name missing on stdin; nothing was written"
  done
  for name in $LOGIN_PASSWORDS; do
    argon2 "$(head -c 16 /dev/urandom | base64 | tr -d '/+=')" -id -t 2 -m 16 -p 1 -e \
      <"$tmp/$name" >"$tmp/${name}_HASH"
    rm -f "$tmp/$name"
    [[ "$(cat "$tmp/${name}_HASH")" == '$argon2id$'* ]] || die "argon2 did not return a hash for $name"
  done

  python3 - "$tmp" "$STAGING_DIR/staging.env.example" "$tmp/.env" "$STAGING_DIR/stream.env.example" "$tmp/stream.env" <<'PY'
import os, re, sys

secrets = sys.argv[1]
for src, dst in ((sys.argv[2], sys.argv[3]), (sys.argv[4], sys.argv[5])):
    def fill(match):
        path = os.path.join(secrets, match.group(1))
        if not os.path.exists(path):
            return match.group(0)  # check-env reports the leftover placeholder
        return open(path).read().strip()
    text = re.sub(r"<(STAGING_[A-Z0-9_]+)>", fill, open(src).read())
    with open(dst, "w") as out:
        out.write(text)
PY
  check_all_env "$tmp/.env" "$tmp/stream.env"
  mv "$tmp/.env" "$STAGING_DIR/.env"
  mv "$tmp/stream.env" "$STAGING_DIR/stream.env"
  echo "wrote $STAGING_DIR/.env and stream.env (mode 600). Next: staging.sh setup"
}

staging_running() {
  [[ -n "$(compose ps -q api 2>/dev/null)" ]]
}

wait_healthy() {
  local _
  for _ in $(seq 1 45); do
    if curl -fsS --max-time 3 "http://127.0.0.1:$API_PORT/health" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  echo "--- last staging API log lines" >&2
  docker logs --tail 80 unheard-api-staging >&2 || echo "  (no container logs)" >&2
  die "staging API did not become healthy"
}

# Liquidsoap serves silence (mksafe) on both mounts as soon as it reaches Icecast.
wait_mounts() {
  local _ status
  for _ in $(seq 1 30); do
    status="$(curl -fsS --max-time 3 "http://127.0.0.1:$ICECAST_PORT/status-json.xsl" 2>/dev/null || true)"
    if [[ "$status" == *'/live.mp3"'* && "$status" == *'/test.mp3"'* ]]; then
      return 0
    fi
    sleep 2
  done
  docker logs --tail 40 moafunk-liquidsoap-staging >&2 || true
  die "Icecast does not list /live.mp3 and /test.mp3"
}

install_packages() {
  local pkg missing=""
  for pkg in docker.io docker-compose-v2 nginx certbot python3-certbot-nginx argon2 sqlite3 curl python3; do
    dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "install ok installed" || missing="$missing $pkg"
  done
  if [[ -n "$missing" ]]; then
    echo "installing:$missing"
    apt-get update -q
    # shellcheck disable=SC2086 # word splitting is the point
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends $missing
  fi
  systemctl enable --now docker nginx >/dev/null
}

# Keys only. Root has no password on Hetzner images, but say it explicitly.
harden_ssh() {
  local conf=/etc/ssh/sshd_config.d/10-staging.conf
  [[ ! -f "$conf" ]] || return 0
  printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\n' >"$conf"
  if ! sshd -t; then
    rm -f "$conf"
    die "sshd config test failed; $conf removed again"
  fi
  systemctl reload ssh
}

build_icecast() {
  if docker image inspect "$ICECAST_IMAGE" >/dev/null 2>&1; then
    return 0
  fi
  echo "building $ICECAST_IMAGE (a few minutes)"
  docker build -q -f "$STAGING_DIR/icecast-kh.Dockerfile" -t "$ICECAST_IMAGE" "$STAGING_DIR"
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
  # The staging dir itself is root-only.
  chmod 644 "$STAGING_DIR/icecast.xml"
}

public_ipv4() {
  [[ -n "${STAGING_BOX_IP:-}" ]] && { printf '%s' "$STAGING_BOX_IP"; return 0; }
  curl -fsS --max-time 3 http://169.254.169.254/hetzner/v1/metadata/public-ipv4 || true
}

install_vhost() {
  local available="/etc/nginx/sites-available/$DOMAIN" enabled="/etc/nginx/sites-enabled/$DOMAIN" ip box_ip email
  # Never overwrite: certbot adds the TLS server block to this file. To change
  # the vhost, delete the file and run setup again.
  [[ -f "$available" ]] || install -m 644 "$STAGING_DIR/nginx-staging.conf" "$available"
  ln -sf "$available" "$enabled"
  rm -f /etc/nginx/sites-enabled/default
  if ! nginx -t; then
    rm -f "$enabled"
    die "nginx config test failed; the staging vhost is disabled again"
  fi
  systemctl reload nginx
  if [[ -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ]]; then
    echo "TLS certificate for $DOMAIN exists (certbot.timer renews it)"
    return 0
  fi
  box_ip="$(public_ipv4)"
  ip="$(getent ahostsv4 "$DOMAIN" | awk '{print $1; exit}' || true)"
  if [[ -z "$box_ip" || "$ip" != "$box_ip" ]]; then
    echo "DNS for $DOMAIN is '${ip:-none}', this box is '${box_ip:-unknown}': TLS skipped. Run setup again once the A record exists."
    return 0
  fi
  # Let's Encrypt no longer sends expiry mails, so an account email is optional.
  email=(--register-unsafely-without-email)
  [[ -z "${CERTBOT_EMAIL:-}" ]] || email=(-m "$CERTBOT_EMAIL")
  certbot --nginx -d "$DOMAIN" --redirect --agree-tos -n "${email[@]}"
  nginx -t
  systemctl reload nginx
}

cmd_setup() {
  local file
  for file in docker-compose.staging.yml icecast.staging.xml nginx-staging.conf moafunk.liq \
    icecast-kh.Dockerfile icecast-web/status-json.xsl icecast-web/xml2json.xslt \
    staging.env.example stream.env.example ci-deploy.pub; do
    [[ -f "$STAGING_DIR/$file" ]] || die "missing $STAGING_DIR/$file (copy the files first, see README)"
  done
  chmod 700 "$STAGING_DIR"
  harden_ssh
  cmd_install_ci_key
  install_packages
  build_icecast
  if [[ ! -f "$STAGING_DIR/.env" || ! -f "$STAGING_DIR/stream.env" ]]; then
    echo "packages and Icecast image are ready. Now run push-secrets.sh on your Mac, then staging.sh setup again."
    return 0
  fi
  check_all_env
  chmod 600 "$STAGING_DIR/.env" "$STAGING_DIR/stream.env"
  check_r2
  mkdir -p "$STAGING_DIR/data"
  render_icecast
  install_vhost
  echo "setup done. Start staging with: staging.sh up sha-<commit>"
}

cmd_up() {
  local tag="${1:-}" digest="${2:-}"
  [[ "$tag" =~ ^sha-[0-9a-f]{7,40}$ ]] || die "usage: staging.sh up sha-<commit> [sha256:<digest>]"
  [[ -z "$digest" || "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || die "digest must be sha256:<64 hex>"
  [[ -f "$STAGING_DIR/icecast.xml" ]] || die "run staging.sh setup first"
  docker image inspect "$ICECAST_IMAGE" >/dev/null 2>&1 || die "Icecast image missing: run staging.sh setup"
  # One deploy at a time (CI and a person can meet here).
  exec 9>"$STAGING_DIR/.deploy.lock"
  flock -w 900 9 || die "another deploy is still running"
  [[ ! -f "$STAGING_DIR/hold" ]] || echo "note: staging is on hold ($(cat "$STAGING_DIR/hold")); deploying by hand anyway"
  check_all_env
  check_r2
  STAGING_TAG="$tag"
  compose pull api
  if [[ -n "$digest" ]]; then
    docker image inspect --format '{{join .RepoDigests "\n"}}' "$API_IMAGE:$tag" | grep -qxF "$API_IMAGE@$digest" ||
      die "$API_IMAGE:$tag is not $digest; nothing was started"
  fi
  compose up -d
  echo "$tag" >"$STAGING_DIR/tag"
  wait_healthy
  staging_users "$STAGING_DIR/data/unheard.db"
  wait_mounts
  echo "staging runs $tag: https://$DOMAIN"
}

cmd_down() {
  compose down
  echo "staging stopped; data kept in $STAGING_DIR/data"
}

cmd_status() {
  echo "tag: $(cat "$STAGING_DIR/tag" 2>/dev/null || echo none)"
  echo "hold: $(cat "$STAGING_DIR/hold" 2>/dev/null || echo "no (CI deploys every push to the staging branch)")"
  compose ps
  if curl -fsS --max-time 3 "http://127.0.0.1:$API_PORT/health" >/dev/null 2>&1; then
    echo "API healthy: https://$DOMAIN"
  else
    echo "API not running"
  fi
}

# Pause CI deploys, so a release candidate stays on staging while it is tested.
cmd_hold() {
  local reason="$*"
  [[ -n "$reason" ]] || die 'usage: staging.sh hold <reason>, e.g. staging.sh hold "R1 release checks"'
  printf '%s | %s | %s\n' "$(date -u +%Y-%m-%dT%H:%MZ)" "$(cat "$STAGING_DIR/tag" 2>/dev/null || echo none)" "$reason" \
    >"$STAGING_DIR/hold"
  echo "staging is on hold: CI deploys are skipped until staging.sh unhold"
}

cmd_unhold() {
  rm -f "$STAGING_DIR/hold"
  echo "hold removed: the next push to the staging branch deploys again"
}

# Requests of the chaos test show (e2e/, workflow e2e-staging.yml). It holds
# staging only when nobody else did, removes only its own hold, and can cut
# only its own connection, for at most a minute.
ci_e2e() {
  local request="$1" run secs ip
  if [[ "$request" =~ ^e2e-hold\ ([0-9]{1,20})$ ]]; then
    run="${BASH_REMATCH[1]}"
    if [[ -f "$STAGING_DIR/hold" ]]; then
      echo "already on hold: $(cat "$STAGING_DIR/hold")"
    else
      cmd_hold "e2e run $run" >/dev/null
      echo "held for e2e run $run"
    fi
  elif [[ "$request" =~ ^e2e-unhold\ ([0-9]{1,20})$ ]]; then
    run="${BASH_REMATCH[1]}"
    if [[ "$(cat "$STAGING_DIR/hold" 2>/dev/null)" == *" | e2e run $run" ]]; then
      cmd_unhold >/dev/null
      echo "hold of e2e run $run removed"
    else
      echo "not held by e2e run $run: hold left as it is"
    fi
  elif [[ "$request" == e2e-restart-liquidsoap ]]; then
    compose restart liquidsoap
  elif [[ "$request" =~ ^e2e-cut\ ([1-9]|[1-5][0-9]|60)$ ]]; then
    secs="${BASH_REMATCH[1]}"
    ip="${SSH_CLIENT:-}"
    ip="${ip%% *}"
    [[ "$ip" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || die "e2e-cut needs the caller's IPv4 address"
    local rule=(INPUT -s "$ip" -p tcp -m multiport --dports "80,443" -j REJECT --reject-with tcp-reset)
    # The timer comes first: it removes the rule even if the caller never returns.
    systemd-run --quiet --on-active="$secs" iptables -D "${rule[@]}"
    iptables -I "${rule[@]}"
    echo "connections from $ip to ports 80 and 443 are reset for ${secs}s"
  elif [[ "$request" =~ ^e2e-recordings\ ([0-9]{1,12})$ ]]; then
    e2e_recordings "${BASH_REMATCH[1]}"
  else
    die "the CI key may only run: status, deploy sha-<commit> sha256:<digest>, e2e-hold <run>, e2e-unhold <run>, e2e-restart-liquidsoap, e2e-cut <1-60>, e2e-recordings <show>"
  fi
}

# The recordings of a test episode ("e2e-…"), one line each: status,
# duration in ms, and 1 when its mp3 is in the shows archive. Read-only.
e2e_recordings() {
  python3 - "$STAGING_DIR/data/unheard.db" "$1" <<'PY'
import sqlite3, sys
con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
show = int(sys.argv[2])
title = con.execute("SELECT title FROM shows WHERE id = ?", (show,)).fetchone()
if not title or not (title[0] or "").startswith("e2e-"):
    sys.exit(f"show {show} is not a test episode (e2e-...)")
for status, ms, archive in con.execute(
    "SELECT status, COALESCE(duration_ms, 0), archive_key IS NOT NULL"
    " FROM recording_versions WHERE show_id = ? ORDER BY id",
    (show,),
):
    print(status, ms, int(archive))
PY
}

# Forced command of the CI deploy key (see install-ci-key). The key can say
# what staging runs, deploy one exact image unless staging is on hold, and
# serve the chaos test show (ci_e2e).
cmd_ci_deploy() {
  local request="${SSH_ORIGINAL_COMMAND:-}" tag digest
  if [[ "$request" == e2e-* ]]; then
    ci_e2e "$request"
    return
  fi
  if [[ "$request" == status ]]; then
    echo "tag: $(cat "$STAGING_DIR/tag" 2>/dev/null || echo none)"
    echo "hold: $(cat "$STAGING_DIR/hold" 2>/dev/null || echo no)"
    return 0
  fi
  if [[ "$request" =~ ^deploy\ (sha-[0-9a-f]{7,40})\ (sha256:[0-9a-f]{64})$ ]]; then
    tag="${BASH_REMATCH[1]}"
    digest="${BASH_REMATCH[2]}"
  else
    die "the CI key may only run: status, or deploy sha-<commit> sha256:<digest>"
  fi
  if [[ -f "$STAGING_DIR/hold" ]]; then
    echo "staging is on hold ($(cat "$STAGING_DIR/hold")); $tag was not deployed"
    exit "$HOLD_EXIT"
  fi
  cmd_up "$tag" "$digest"
}

# Allow the CI key in ci-deploy.pub, but only for `staging.sh ci-deploy`.
cmd_install_ci_key() {
  local key blob line
  key="$(cat "$STAGING_DIR/ci-deploy.pub")"
  [[ "$key" =~ ^ssh-ed25519\ [A-Za-z0-9+/=]+\ ci-staging-deploy$ ]] || die "ci-deploy.pub is not the CI deploy key"
  blob="$(echo "$key" | awk '{print $2}')"
  line="command=\"$STAGING_DIR/staging.sh ci-deploy\",restrict $key"
  mkdir -p "$(dirname "$AUTHORIZED_KEYS")"
  touch "$AUTHORIZED_KEYS"
  if grep -qxF "$line" "$AUTHORIZED_KEYS"; then
    return 0
  fi
  # Replace any older line for this key; keep every other key as it is.
  { grep -vF "$blob" "$AUTHORIZED_KEYS" || true; } >"$AUTHORIZED_KEYS.new"
  printf '%s\n' "$line" >>"$AUTHORIZED_KEYS.new"
  chmod 600 "$AUTHORIZED_KEYS.new"
  mv "$AUTHORIZED_KEYS.new" "$AUTHORIZED_KEYS"
  echo "CI key allowed for staging.sh ci-deploy only"
}

# Remove everything that works against production or a real account, then
# prove it. Fails when a column that looks like a credential holds data the
# scrub does not know about, so a new table cannot leak by accident.
scrub_db() {
  python3 - "$1" <<'PY'
import sqlite3, sys

WIPE = [
    "sessions",  # login tokens, valid on production
    "recovery_tokens",  # password-reset and email-verify links
    "recovery_emails",  # personal emails; staging sends no mail
    "pending_file_uploads",
    "pending_submissions",  # anonymous upload sessions of the public form
    "pending_recording_chunks",
    "pending_recording_uploads",
]
KEEP_SETTINGS = ("notifications_enabled",)  # all other app_settings rows go (SoundCloud token)
HANDLED = {("users", "password_hash"), ("users", "must_change_password")}
CREDENTIAL_WORDS = ("token", "secret", "password", "api_key", "apikey")

con = sqlite3.connect(sys.argv[1])
if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
    sys.exit("staging: the DB copy is damaged (integrity_check failed)")
con.execute("PRAGMA journal_mode=DELETE")
tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
if "users" not in tables:
    sys.exit("staging: this is not an unheard DB (no users table)")

def count(sql):
    return con.execute(sql).fetchone()[0]

removed = []
for table in WIPE:
    if table in tables:
        n = count(f'SELECT COUNT(*) FROM "{table}"')
        con.execute(f'DELETE FROM "{table}"')
        removed.append(f"{table}={n}")
if "app_settings" in tables:
    marks = ",".join("?" * len(KEEP_SETTINGS))
    n = con.execute(f"DELETE FROM app_settings WHERE key NOT IN ({marks})", KEEP_SETTINGS).rowcount
    removed.append(f"app_settings={n}")
n = con.execute("UPDATE users SET password_hash = '!'").rowcount
removed.append(f"passwords={n}")
con.commit()
# Deleted rows stay in free pages until VACUUM rewrites the file.
con.execute("VACUUM")

problems = []
for table in WIPE:
    if table in tables and count(f'SELECT COUNT(*) FROM "{table}"'):
        problems.append(f"{table} is not empty")
if count("SELECT COUNT(*) FROM users WHERE password_hash != '!'"):
    problems.append("a user still has a password hash")
for table in tables:
    if table in WIPE:
        continue
    for column in [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]:
        if (table, column) in HANDLED or not any(w in column.lower() for w in CREDENTIAL_WORDS):
            continue
        if count(f'SELECT COUNT(*) FROM "{table}" WHERE "{column}" IS NOT NULL AND "{column}" != \'\''):
            problems.append(f"{table}.{column} looks like a credential; add it to the scrub in staging.sh")
con.close()
if problems:
    sys.exit("staging: scrub incomplete: " + "; ".join(problems))
print("scrubbed: " + " ".join(removed), file=sys.stderr)
PY
}

# Runs on the production box, read-only: Anton pipes this script in over ssh.
cmd_export_db() {
  local tmp
  [[ -r "$PROD_DB" ]] || die "cannot read $PROD_DB"
  [[ ! -t 1 ]] || die "stdout is a terminal; pipe the copy into 'staging.sh seed-db -' on the staging box"
  make_tmp
  tmp="$TMP_DIR"
  python3 - "$PROD_DB" "$tmp/copy.db" <<'PY'
import sqlite3, sys

source = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
copy = sqlite3.connect(sys.argv[2])
source.backup(copy)
copy.close()
source.close()
PY
  scrub_db "$tmp/copy.db"
  cat "$tmp/copy.db"
}

# The staging logins from .env, with passwords from Bitwarden: staging-admin
# (superadmin) and staging-host (host). Needed because the backend seeds a
# superadmin only into an empty users table, and every user in a production
# copy has an unusable password. Resets both to the Bitwarden password.
staging_users() {
  local env="$STAGING_DIR/.env"
  [[ -f "$1" ]] || die "missing $1 (start staging first)"
  ADMIN_NAME="$(env_value SUPERADMIN_USERNAME "$env")" \
    ADMIN_HASH="$(env_value SUPERADMIN_PASSWORD_HASH "$env")" \
    HOST_NAME="$(env_value STAGING_HOST_USERNAME "$env")" \
    HOST_HASH="$(env_value STAGING_HOST_PASSWORD_HASH "$env")" \
    python3 - "$1" <<'PY'
import os, sqlite3, sys

logins = [
    (os.environ["ADMIN_NAME"], os.environ["ADMIN_HASH"], "superadmin"),
    (os.environ["HOST_NAME"], os.environ["HOST_HASH"], "host"),
]
for name, hashed, role in logins:
    if not name or not hashed.startswith("$argon2"):
        sys.exit(f"staging: the {role} login needs a username and an argon2 hash in .env (run push-secrets.sh)")
# The API may be running: wait for its write lock instead of failing.
con = sqlite3.connect(sys.argv[1], timeout=30)
if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'").fetchone():
    sys.exit("staging: the DB has no users table yet")
columns = [r[1] for r in con.execute("PRAGMA table_info(users)")]
for name, hashed, role in logins:
    con.execute(
        "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?) "
        "ON CONFLICT(username) DO UPDATE SET password_hash = excluded.password_hash, "
        "role = excluded.role, expires_at = NULL",
        (name, hashed, role),
    )
    for column, value in (("must_change_password", 0), ("login_date", None)):
        if column in columns:
            con.execute(f"UPDATE users SET {column} = ? WHERE username = ?", (value, name))
con.commit()
con.close()
print("staging logins: " + ", ".join(f"{name} ({role})" for name, _, role in logins), file=sys.stderr)
PY
}

cmd_seed_db() {
  local src="${1:-}" data="$STAGING_DIR/data" was_running=0
  [[ -n "$src" ]] || die "usage: staging.sh seed-db <file>, or - to read the copy from stdin"
  check_env
  finish_checks
  mkdir -p "$data"
  umask 077
  if [[ "$src" == - ]]; then
    cat >"$data/seed.tmp.db"
  else
    [[ -f "$src" ]] || die "missing $src"
    cp "$src" "$data/seed.tmp.db"
  fi
  [[ -s "$data/seed.tmp.db" ]] || { rm -f "$data/seed.tmp.db"; die "the DB copy is empty"; }
  # Never trust the copy: scrub again, whoever made it.
  if ! scrub_db "$data/seed.tmp.db" || ! staging_users "$data/seed.tmp.db"; then
    rm -f "$data/seed.tmp.db"
    die "staging DB not changed"
  fi
  if staging_running; then
    was_running=1
    compose stop api
  fi
  mv "$data/seed.tmp.db" "$data/unheard.db"
  rm -f "$data/unheard.db-wal" "$data/unheard.db-shm"
  [[ "$src" == - ]] || rm -f "$src"
  if ((was_running)); then
    compose start api
    wait_healthy
  fi
  echo "staging DB is now a scrubbed copy of production: no sessions, no stored tokens, no usable passwords."
  echo "Log in as staging-admin or staging-host; to test as a production user, reset that user's password in the admin."
}

main() {
  local cmd="${1:-}"
  [[ $# -gt 0 ]] && shift
  case "$cmd" in
    setup) cmd_setup ;;
    install-env) cmd_install_env ;;
    check-env)
      check_all_env "${1:-}" "${2:-}"
      echo "env ok"
      ;;
    check-r2) check_r2 ;;
    up) cmd_up "${1:-}" "${2:-}" ;;
    hold) cmd_hold "$@" ;;
    unhold) cmd_unhold ;;
    ci-deploy) cmd_ci_deploy ;;
    install-ci-key) cmd_install_ci_key ;;
    down) cmd_down ;;
    status) cmd_status ;;
    export-db) cmd_export_db ;;
    seed-db) cmd_seed_db "${1:-}" ;;
    users) staging_users "$STAGING_DIR/data/unheard.db" ;;
    *)
      sed -n '7,23p' "$0" >&2
      exit 2
      ;;
  esac
}

main "$@"
