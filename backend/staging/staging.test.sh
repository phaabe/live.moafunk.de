#!/usr/bin/env bash
# Tests for `staging.sh check-env`: every unsafe setting must stop staging.
# Runs anywhere (bash 3.2+), no Docker or production access needed:
#   bash backend/staging/staging.test.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
failures=0

write_good() {
  cat >"$TMP/.env" <<'EOF'
APP_ENV=staging
ADMIN_BASE_URL=https://staging.live.moafunk.de
SECRET_KEY=test-secret
SUPERADMIN_USERNAME=staging-admin
SUPERADMIN_PASSWORD_HASH='$argon2id$v=19$m=19456,t=2,p=1$c2FsdA$aGFzaA'
R2_ACCOUNT_ID=account
R2_ACCESS_KEY_ID=key
R2_SECRET_ACCESS_KEY=secret
R2_BUCKET_NAME=unheard-artists-staging
R2_SHOWS_BUCKET_NAME=moafunk-staging
STREAM_OUTPUT=icecast
ICECAST_URL=icecast://source:live-pw@host.docker.internal:8015/live
ICECAST_TEST_URL=icecast://source:test-pw@host.docker.internal:8015/test
ICECAST_STATUS_URL=http://host.docker.internal:8020/status-json.xsl
SOUNDCLOUD_AUTO_UPLOAD=false
TELEGRAM_ENABLED=false
EOF
  cat >"$TMP/stream.env" <<'EOF'
HARBOR_PORT=8015
ICECAST_PORT=8020
HARBOR_TEST_PASSWORD=test-pw
HARBOR_LIVE_PASSWORD=live-pw
ICECAST_SOURCE_PASSWORD=source-pw
ICECAST_ADMIN_PASSWORD=admin-pw
ICECAST_RELAY_PASSWORD=relay-pw
EOF
  cat >"$TMP/prod-stream.env" <<'EOF'
HARBOR_TEST_PASSWORD=prod-test
HARBOR_LIVE_PASSWORD=prod-live
ICECAST_SOURCE_PASSWORD=prod-source
ICECAST_ADMIN_PASSWORD=prod-admin
EOF
}

# Replace KEY's line in FILE (or delete it when VALUE is "-"), or append it.
set_key() {
  local file="$1" key="$2" value="$3"
  grep -v -E "^$key=" "$file" >"$file.new" || true
  [[ "$value" == "-" ]] || printf '%s=%s\n' "$key" "$value" >>"$file.new"
  mv "$file.new" "$file"
}

run_check() {
  PROD_STREAM_ENV="$TMP/prod-stream.env" bash "$HERE/staging.sh" check-env "$TMP/.env" "$TMP/stream.env"
}

expect_ok() {
  local name="$1" out
  if out="$(run_check 2>&1)"; then
    echo "ok   $name"
  else
    echo "FAIL $name: expected ok, got: $out"
    failures=$((failures + 1))
  fi
}

# Expect a refusal whose output contains MESSAGE.
expect_refused() {
  local name="$1" message="$2" out
  if out="$(run_check 2>&1)"; then
    echo "FAIL $name: expected a refusal"
    failures=$((failures + 1))
  elif [[ "$out" != *"$message"* ]]; then
    echo "FAIL $name: refused, but without '$message': $out"
    failures=$((failures + 1))
  else
    echo "ok   $name"
  fi
}

case_env() { # name message key value
  write_good
  set_key "$TMP/.env" "$3" "$4"
  expect_refused "$1" "$2"
}

case_stream() { # name message key value
  write_good
  set_key "$TMP/stream.env" "$3" "$4"
  expect_refused "$1" "$2"
}

write_good
expect_ok "safe staging settings pass"

case_env "missing shows bucket falls back to production" "R2_SHOWS_BUCKET_NAME must name a staging bucket" R2_SHOWS_BUCKET_NAME -
case_env "production recordings bucket" "R2_BUCKET_NAME must name a staging bucket" R2_BUCKET_NAME unheard-artists-prod
case_env "SoundCloud auto-upload not off" "SOUNDCLOUD_AUTO_UPLOAD must be 'false'" SOUNDCLOUD_AUTO_UPLOAD -
case_env "production admin URL" "ADMIN_BASE_URL must be" ADMIN_BASE_URL https://admin.live.moafunk.de
case_env "production app env" "APP_ENV must be 'staging'" APP_ENV production
case_env "producer pushes to the production harbor" "ICECAST_URL must target the staging harbor" ICECAST_URL "icecast://source:x@host.docker.internal:8005/live"
case_env "status from the production Icecast" "ICECAST_STATUS_URL must use the staging Icecast port" ICECAST_STATUS_URL "http://host.docker.internal:8010/status-json.xsl"
case_env "GitHub dispatch token set" "GITHUB_DISPATCH_TOKEN must not be set" GITHUB_DISPATCH_TOKEN ghp_x
case_env "Telegram bot token set" "TELEGRAM_BOT_TOKEN must not be set" TELEGRAM_BOT_TOKEN 123:abc
case_env "OpenAI key set" "OPENAI_API_KEY must not be set" OPENAI_API_KEY sk-x
case_env "empty recovery mail key" "RECOVERY_* must not be set" RECOVERY_MAIL_FROM ""
case_env "placeholder left in .env" "SECRET_KEY still has a <placeholder>" SECRET_KEY "<STAGING_SECRET_KEY>"
case_env "unquoted value with a space" "has an unquoted value with a space" SUPERADMIN_USERNAME "Staging Admin"

write_good
printf 'R2_BUCKET_NAME=unheard-artists-prod\n' >>"$TMP/.env"
expect_refused "duplicate key (dotenvy keeps the first)" "duplicate keys"

case_stream "wrong harbor port" "HARBOR_PORT must be 8015" HARBOR_PORT 8005
case_stream "missing stream password" "ICECAST_RELAY_PASSWORD must be set" ICECAST_RELAY_PASSWORD -
case_stream "production harbor password reused" "HARBOR_LIVE_PASSWORD reuses a production password" HARBOR_LIVE_PASSWORD prod-live

cp "$HERE/staging.env.example" "$TMP/.env"
expect_refused "the untouched example is refused" "still has a <placeholder>"

if ((failures > 0)); then
  echo "$failures staging check test(s) failed"
  exit 1
fi
echo "all staging check tests passed"
