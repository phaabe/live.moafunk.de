#!/usr/bin/env bash
# Tests for staging.sh: every unsafe setting stops staging, install-env writes
# safe env files, and the production DB copy is scrubbed. Runs anywhere with
# bash 3.2+ and python3; argon2 and docker are stubs, no server needed:
#   bash backend/staging/staging.test.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
BOX="$TMP/box"
failures=0

LIVE_PW=liveliveliveliveliveLIVE1
TEST_PW=testtesttesttesttestTEST1
ADMIN_PW=AdminTypedByHand2026xyz

mkdir -p "$TMP/bin" "$BOX"
cp "$HERE/staging.env.example" "$HERE/stream.env.example" "$BOX/"
# Stub: a fake argon2 hash that does not contain the password.
cat >"$TMP/bin/argon2" <<'EOF'
#!/usr/bin/env bash
printf '$argon2id$v=19$m=65536,t=2,p=1$c2FsdA$stub%s\n' "$(wc -c | tr -d ' ')"
EOF
# Stub: compose works, no staging container runs.
cat >"$TMP/bin/docker" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$TMP/bin/argon2" "$TMP/bin/docker"

staging() {
  PATH="$TMP/bin:$PATH" STAGING_DIR="$BOX" bash "$HERE/staging.sh" "$@"
}

write_good() {
  cat >"$BOX/.env" <<EOF
APP_ENV=staging
ADMIN_BASE_URL=https://staging.live.moafunk.de
SECRET_KEY=test-secret
SUPERADMIN_USERNAME=staging-admin
SUPERADMIN_PASSWORD_HASH='\$argon2id\$v=19\$m=19456,t=2,p=1\$c2FsdA\$aGFzaA'
R2_ACCOUNT_ID=account
R2_ACCESS_KEY_ID=key
R2_SECRET_ACCESS_KEY=secret
R2_BUCKET_NAME=unheard-artists-staging
R2_SHOWS_BUCKET_NAME=moafunk-staging
STREAM_OUTPUT=icecast
ICECAST_URL=icecast://source:$LIVE_PW@host.docker.internal:8005/live
ICECAST_TEST_URL=icecast://source:$TEST_PW@host.docker.internal:8005/test
ICECAST_STATUS_URL=http://host.docker.internal:8010/status-json.xsl
SOUNDCLOUD_AUTO_UPLOAD=false
TELEGRAM_ENABLED=false
EOF
  cat >"$BOX/stream.env" <<EOF
HARBOR_PORT=8005
ICECAST_PORT=8010
HARBOR_TEST_PASSWORD=$TEST_PW
HARBOR_LIVE_PASSWORD=$LIVE_PW
ICECAST_SOURCE_PASSWORD=sourcesourcesourcesource1
ICECAST_ADMIN_PASSWORD=adminadminadminadminadmin1
ICECAST_RELAY_PASSWORD=relayrelayrelayrelayrelay1
EOF
}

# Replace KEY's line in FILE (or delete it when VALUE is "-"), or append it.
set_key() {
  local file="$1" key="$2" value="$3"
  grep -v -E "^$key=" "$file" >"$file.new" || true
  [[ "$value" == "-" ]] || printf '%s=%s\n' "$key" "$value" >>"$file.new"
  mv "$file.new" "$file"
}

pass() {
  echo "ok   $1"
}

fail() {
  echo "FAIL $1"
  failures=$((failures + 1))
}

# Run "$@"; expect success.
expect_ok() {
  local name="$1" out
  shift
  if out="$("$@" 2>&1)"; then pass "$name"; else fail "$name: expected ok, got: $out"; fi
}

# Run "$@"; expect a refusal whose output contains MESSAGE.
expect_refused() {
  local name="$1" message="$2" out
  shift 2
  if out="$("$@" 2>&1)"; then
    fail "$name: expected a refusal"
  elif [[ "$out" != *"$message"* ]]; then
    fail "$name: refused, but without '$message': $out"
  else
    pass "$name"
  fi
}

check() {
  staging check-env "$BOX/.env" "$BOX/stream.env"
}

case_env() { # name message key value
  write_good
  set_key "$BOX/.env" "$3" "$4"
  expect_refused "$1" "$2" check
}

case_stream() { # name message key value
  write_good
  set_key "$BOX/stream.env" "$3" "$4"
  expect_refused "$1" "$2" check
}

echo "--- check-env"
write_good
expect_ok "safe staging settings pass" check

case_env "missing shows bucket falls back to production" "R2_SHOWS_BUCKET_NAME must be 'moafunk-staging'" R2_SHOWS_BUCKET_NAME -
case_env "production recordings bucket" "R2_BUCKET_NAME must be 'unheard-artists-staging'" R2_BUCKET_NAME unheard-artists-prod
case_env "production shows bucket" "R2_SHOWS_BUCKET_NAME must be" R2_SHOWS_BUCKET_NAME moafunk-prod
case_env "SoundCloud auto-upload not off" "SOUNDCLOUD_AUTO_UPLOAD must be 'false'" SOUNDCLOUD_AUTO_UPLOAD -
case_env "Telegram switched on" "TELEGRAM_ENABLED must be 'false'" TELEGRAM_ENABLED true
case_env "production admin URL" "ADMIN_BASE_URL must be" ADMIN_BASE_URL https://admin.live.moafunk.de
case_env "production app env" "APP_ENV must be 'staging'" APP_ENV production
case_env "producer pushes to the production box" "ICECAST_URL must be" ICECAST_URL "icecast://source:$LIVE_PW@178.104.160.103:8005/live"
case_env "test stream goes to the live mount" "ICECAST_TEST_URL must be" ICECAST_TEST_URL "icecast://source:$TEST_PW@host.docker.internal:8005/live"
case_env "status from the production Icecast" "ICECAST_STATUS_URL must be" ICECAST_STATUS_URL "https://admin.live.moafunk.de/status-json.xsl"
case_env "GitHub dispatch token set" "GITHUB_DISPATCH_TOKEN must not be set" GITHUB_DISPATCH_TOKEN ghp_x
case_env "Telegram bot token set" "TELEGRAM_BOT_TOKEN must not be set" TELEGRAM_BOT_TOKEN 123:abc
case_env "Telegram setting added later" "TELEGRAM_NEW_SETTING must not be set" TELEGRAM_NEW_SETTING x
case_env "SoundCloud secret set" "SOUNDCLOUD_CLIENT_SECRET must not be set" SOUNDCLOUD_CLIENT_SECRET x
case_env "Instagram token set" "INSTAGRAM_ACCESS_TOKEN_PROD must not be set" INSTAGRAM_ACCESS_TOKEN_PROD x
case_env "Facebook secret set" "FACEBOOK_APP_SECRET must not be set" FACEBOOK_APP_SECRET x
case_env "OpenAI key set" "OPENAI_API_KEY must not be set" OPENAI_API_KEY sk-x
case_env "empty recovery mail key" "RECOVERY_MAIL_FROM must not be set" RECOVERY_MAIL_FROM ""
case_env "placeholder left in .env" "SECRET_KEY still has a <placeholder>" SECRET_KEY "<STAGING_SECRET_KEY>"
case_env "unquoted value with a space" "has an unquoted value with a space" SUPERADMIN_USERNAME "Staging Admin"
case_env "harbor password differs from stream.env" "ICECAST_URL must use HARBOR_LIVE_PASSWORD" ICECAST_URL \
  "icecast://source:otherotherotherotherother1@host.docker.internal:8005/live"

write_good
printf 'R2_BUCKET_NAME=unheard-artists-prod\n' >>"$BOX/.env"
expect_refused "duplicate key (dotenvy keeps the first)" "duplicate keys" check

write_good
printf 'export TELEGRAM_BOT_TOKEN=123:abc\n' >>"$BOX/.env"
expect_refused "'export' line hides a key from the checks" "is not KEY=value" check

case_stream "wrong harbor port" "HARBOR_PORT must be 8005" HARBOR_PORT 8015
case_stream "wrong Icecast port" "ICECAST_PORT must be 8010" ICECAST_PORT 8020
case_stream "missing stream password" "ICECAST_RELAY_PASSWORD must be at least 20 letters and digits" ICECAST_RELAY_PASSWORD -
case_stream "stream password with URL characters" "ICECAST_ADMIN_PASSWORD must be at least 20 letters and digits" \
  ICECAST_ADMIN_PASSWORD "abc@def:ghi/jklmnopqrstuvw"

cp "$HERE/staging.env.example" "$BOX/.env"
cp "$HERE/stream.env.example" "$BOX/stream.env"
expect_refused "the untouched examples are refused" "still has a <placeholder>" check

echo "--- install-env"
secrets() {
  cat <<EOF
STAGING_SECRET_KEY=secretsecretsecretsecretsecret
STAGING_SUPERADMIN_PASSWORD=$ADMIN_PW
STAGING_R2_ACCESS_KEY_ID=0123456789abcdef0123456789abcdef
STAGING_R2_SECRET_ACCESS_KEY=fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210
STAGING_HARBOR_TEST_PASSWORD=$TEST_PW
STAGING_HARBOR_LIVE_PASSWORD=$LIVE_PW
STAGING_ICECAST_SOURCE_PASSWORD=sourcesourcesourcesource1
STAGING_ICECAST_ADMIN_PASSWORD=adminadminadminadminadmin1
STAGING_ICECAST_RELAY_PASSWORD=relayrelayrelayrelayrelay1
EOF
}

install_env() { # secrets on stdin
  staging install-env
}

rm -f "$BOX/.env" "$BOX/stream.env"
expect_ok "install-env writes env files from the secrets" install_env < <(secrets)
expect_ok "installed env files pass check-env" check
if grep -q "^SUPERADMIN_PASSWORD_HASH='\$argon2id\\$" "$BOX/.env"; then pass "superadmin hash is single-quoted"; else fail "superadmin hash is single-quoted"; fi
if grep -rq "$ADMIN_PW" "$BOX"; then fail "plaintext superadmin password was written"; else pass "plaintext superadmin password is not stored"; fi
if [[ "$(ls -l "$BOX/.env" "$BOX/stream.env" | cut -c1-10 | sort -u)" == "-rw-------" ]]; then
  pass "env files are mode 600"
else
  fail "env files are mode 600: $(ls -l "$BOX/.env" "$BOX/stream.env")"
fi

rm -f "$BOX/.env" "$BOX/stream.env"
expect_refused "a missing secret writes nothing" "STAGING_R2_SECRET_ACCESS_KEY missing on stdin" install_env \
  < <(secrets | grep -v R2_SECRET)
if [[ -e "$BOX/.env" || -e "$BOX/stream.env" ]]; then fail "nothing written after a refusal"; else pass "nothing written after a refusal"; fi
expect_refused "an unknown name is refused" "unknown name on stdin: 'GITHUB_DISPATCH_TOKEN'" install_env \
  < <(secrets && echo GITHUB_DISPATCH_TOKEN=ghp_x)
expect_refused "a value with a space is refused" "STAGING_SECRET_KEY is empty or has characters" install_env \
  < <(secrets | sed 's/^STAGING_SECRET_KEY=.*/STAGING_SECRET_KEY=two words/')
expect_refused "a secret sent twice is refused" "STAGING_HARBOR_LIVE_PASSWORD was sent twice" install_env \
  < <(secrets && echo "STAGING_HARBOR_LIVE_PASSWORD=$LIVE_PW")
expect_refused "a short harbor password is refused" "HARBOR_TEST_PASSWORD must be at least 20" install_env \
  < <(secrets | sed 's/^STAGING_HARBOR_TEST_PASSWORD=.*/STAGING_HARBOR_TEST_PASSWORD=short/')
if [[ -e "$BOX/.env" ]]; then fail "a failed check writes nothing"; else pass "a failed check writes nothing"; fi

echo "--- export-db and seed-db"
# A small production-like DB with every kind of credential the scrub removes.
make_prod_db() { # path [extra SQL]
  rm -f "$1"
  python3 - "$1" "${2:-}" <<'PY'
import sqlite3, sys

con = sqlite3.connect(sys.argv[1])
con.execute("PRAGMA journal_mode=WAL")
con.executescript("""
CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'host', expires_at TEXT, must_change_password INTEGER NOT NULL DEFAULT 0, login_date TEXT);
CREATE TABLE sessions (token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires_at TEXT NOT NULL);
CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE recovery_emails (user_id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE);
CREATE TABLE recovery_tokens (hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, kind TEXT NOT NULL, expires_at INTEGER NOT NULL);
CREATE TABLE pending_submissions (session_id TEXT PRIMARY KEY, artist_name TEXT NOT NULL);
CREATE TABLE pending_file_uploads (file_session_id TEXT PRIMARY KEY, parent_session_id TEXT NOT NULL);
CREATE TABLE pending_recording_uploads (session_id TEXT PRIMARY KEY, show_id INTEGER NOT NULL);
CREATE TABLE pending_recording_chunks (session_id TEXT NOT NULL, chunk_index INTEGER NOT NULL);
CREATE TABLE artists (id INTEGER PRIMARY KEY, name TEXT NOT NULL, instagram TEXT);
INSERT INTO users (username, password_hash, role, must_change_password) VALUES
  ('host1', '$argon2id$PRODHASH-host1', 'host', 1), ('boss', '$argon2id$PRODHASH-boss', 'superadmin', 0);
INSERT INTO sessions VALUES ('PRODSESSION-token', 1, '2099-01-01');
INSERT INTO app_settings VALUES ('notifications_enabled', 'true'), ('soundcloud_access_token', 'PRODSOUNDCLOUD-token');
INSERT INTO recovery_emails VALUES (1, 'host1@example.com');
INSERT INTO recovery_tokens VALUES ('PRODRESET-hash', 1, 'reset', 4102444800);
INSERT INTO pending_submissions VALUES ('PRODUPLOAD-session', 'Someone');
INSERT INTO pending_file_uploads VALUES ('PRODFILE-session', 'PRODUPLOAD-session');
INSERT INTO pending_recording_uploads VALUES ('PRODREC-session', 1);
INSERT INTO pending_recording_chunks VALUES ('PRODREC-session', 0);
INSERT INTO artists (name, instagram) VALUES ('Artist A', '@artist_a');
""")
if sys.argv[2]:
    con.executescript(sys.argv[2])
con.commit()
PY
}

export_db() { # prod-db out
  PROD_DB="$1" bash "$HERE/staging.sh" export-db >"$2"
}

# Facts about a DB copy, one per line.
facts() {
  python3 - "$1" <<'PY'
import sqlite3, sys

con = sqlite3.connect(sys.argv[1])
q = lambda sql: con.execute(sql).fetchall()
print("journal", q("PRAGMA journal_mode")[0][0])
for t in ("sessions", "recovery_emails", "recovery_tokens", "pending_submissions", "pending_file_uploads",
          "pending_recording_uploads", "pending_recording_chunks"):
    print(t, q(f"SELECT COUNT(*) FROM {t}")[0][0])
print("settings", ",".join(r[0] for r in q("SELECT key FROM app_settings ORDER BY key")))
for name, hashed, role in q("SELECT username, password_hash, role FROM users ORDER BY username"):
    print("user", name, role, hashed)
print("artists", q("SELECT COUNT(*) FROM artists")[0][0])
PY
}

expect_fact() { # name db fact
  if facts "$2" | grep -qxF "$3"; then pass "$1"; else fail "$1: no '$3' in: $(facts "$2" | tr '\n' ';')"; fi
}

make_prod_db "$TMP/prod.db"
expect_ok "export-db writes a copy" export_db "$TMP/prod.db" "$TMP/copy.db"
for t in sessions recovery_emails recovery_tokens pending_submissions pending_file_uploads pending_recording_uploads pending_recording_chunks; do
  expect_fact "export-db empties $t" "$TMP/copy.db" "$t 0"
done
expect_fact "export-db keeps only the notifications setting" "$TMP/copy.db" "settings notifications_enabled"
expect_fact "export-db makes every password unusable" "$TMP/copy.db" "user host1 host !"
expect_fact "export-db keeps show data" "$TMP/copy.db" "artists 1"
expect_fact "export-db leaves no WAL dependency" "$TMP/copy.db" "journal delete"
if LC_ALL=C grep -aqE 'PRODHASH|PRODSESSION|PRODSOUNDCLOUD|PRODRESET|PRODUPLOAD|host1@example' "$TMP/copy.db"; then
  fail "removed values are gone from the file bytes (VACUUM)"
else
  pass "removed values are gone from the file bytes (VACUUM)"
fi
expect_fact "export-db leaves the production DB alone" "$TMP/prod.db" "sessions 1"

make_prod_db "$TMP/prod-new.db" "CREATE TABLE webhooks (url TEXT, signing_secret TEXT); INSERT INTO webhooks VALUES ('https://x', 'whsec');"
expect_refused "export-db refuses an unknown credential column" "webhooks.signing_secret looks like a credential" \
  export_db "$TMP/prod-new.db" "$TMP/copy-new.db"
if [[ -s "$TMP/copy-new.db" ]]; then fail "a refused export writes nothing to stdout"; else pass "a refused export writes nothing to stdout"; fi

seed_from_stdin() { # file
  staging seed-db - <"$1"
}

write_good
mkdir -p "$BOX/data"
expect_ok "seed-db installs a copy from stdin" seed_from_stdin "$TMP/copy.db"
expect_fact "seed-db adds the staging superadmin" "$BOX/data/unheard.db" \
  "user staging-admin superadmin \$argon2id\$v=19\$m=19456,t=2,p=1\$c2FsdA\$aGFzaA"
expect_fact "seed-db keeps production users unusable" "$BOX/data/unheard.db" "user boss superadmin !"

make_prod_db "$TMP/raw.db"
expect_ok "seed-db scrubs a copy that was not scrubbed" seed_from_stdin "$TMP/raw.db"
expect_fact "seed-db empties sessions itself" "$BOX/data/unheard.db" "sessions 0"
expect_fact "seed-db drops stored tokens itself" "$BOX/data/unheard.db" "settings notifications_enabled"

printf 'not a database' >"$TMP/broken.db"
expect_refused "seed-db refuses a damaged copy" "staging DB not changed" seed_from_stdin "$TMP/broken.db"
expect_fact "a refused seed leaves the staging DB alone" "$BOX/data/unheard.db" "artists 1"

if ((failures > 0)); then
  echo "$failures staging test(s) failed"
  exit 1
fi
echo "all staging tests passed"
