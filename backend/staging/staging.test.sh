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
HOST_PW='Host pw! with #symbols 2026'
HOST_HASH='$argon2id$v=19$m=19456,t=2,p=1$c2FsdA$aG9zdA'
E2E_PW=e2ee2ee2ee2ee2ee2ee2ee2eE2E1
E2E_HASH='$argon2id$v=19$m=19456,t=2,p=1$c2FsdA$ZTJl'

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
echo "docker $*" >>"$CALLS"
exit 0
EOF
# Stubs: the firewall and the timer only log what they were asked.
for tool in iptables systemd-run; do
  printf '#!/usr/bin/env bash\necho "%s $*" >>"$CALLS"\n' "$tool" >"$TMP/bin/$tool"
  chmod +x "$TMP/bin/$tool"
done
export CALLS="$TMP/calls"
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
STAGING_HOST_USERNAME=staging-host
STAGING_HOST_PASSWORD_HASH='$HOST_HASH'
STAGING_E2E_HOST_USERNAME=e2e-host
STAGING_E2E_HOST_PASSWORD_HASH='$E2E_HASH'
R2_ACCOUNT_ID=account
R2_ACCESS_KEY_ID=key
R2_SECRET_ACCESS_KEY=secret
R2_BUCKET_NAME=unheard-artists-staging
R2_SHOWS_BUCKET_NAME=moafunk-staging
STREAM_OUTPUT=icecast
ICECAST_URL=icecast://source:$LIVE_PW@host.docker.internal:8005/live
ICECAST_TEST_URL=icecast://source:$TEST_PW@host.docker.internal:8005/test
ICECAST_STATUS_URL=http://host.docker.internal:8010/status-json.xsl
SOUNDCLOUD_CLIENT_ID=stagingclientid
SOUNDCLOUD_CLIENT_SECRET=stagingclientsecret
SOUNDCLOUD_ACCOUNT=moafunk-test
SOUNDCLOUD_REDIRECT_URI=https://admin.live.moafunk.de/api/soundcloud/callback
SOUNDCLOUD_AUTO_UPLOAD=true
SOUNDCLOUD_KEEP_TRACKS=10
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=8880403786:AAtest-token_x
TELEGRAM_LIVE_CHAT_ID=-1005857809589
TELEGRAM_ANNOUNCE_CHAT_ID=-1005857809589
INSTAGRAM_ACCOUNT=dev
INSTAGRAM_ACCESS_TOKEN_DEV=testtoken
INSTAGRAM_BUSINESS_ACCOUNT_ID_DEV=17841400000000000
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
case_env "SoundCloud auto-upload unset" "SOUNDCLOUD_AUTO_UPLOAD must be 'true'" SOUNDCLOUD_AUTO_UPLOAD -
case_env "SoundCloud login skips the app's redirect URI" "SOUNDCLOUD_REDIRECT_URI must be 'https://admin.live.moafunk.de/api/soundcloud/callback'" \
  SOUNDCLOUD_REDIRECT_URI https://staging.live.moafunk.de/api/soundcloud/callback
case_env "SoundCloud login returns to the localhost default" "SOUNDCLOUD_REDIRECT_URI must be" SOUNDCLOUD_REDIRECT_URI -
case_env "SoundCloud cleanup unset" "SOUNDCLOUD_KEEP_TRACKS must be '10'" SOUNDCLOUD_KEEP_TRACKS -
case_env "SoundCloud station account" "SOUNDCLOUD_ACCOUNT must not be the station account radio-moafunk" SOUNDCLOUD_ACCOUNT radio-moafunk
case_env "SoundCloud test account unset" "SOUNDCLOUD_ACCOUNT must be set" SOUNDCLOUD_ACCOUNT -
case_env "SoundCloud app secret unset" "SOUNDCLOUD_CLIENT_SECRET must be set" SOUNDCLOUD_CLIENT_SECRET -
case_env "Telegram switched off" "TELEGRAM_ENABLED must be 'true'" TELEGRAM_ENABLED false
case_env "production live chat group" "TELEGRAM_LIVE_CHAT_ID must be '-1005857809589'" TELEGRAM_LIVE_CHAT_ID -1006079057775
case_env "live chat group unset" "TELEGRAM_LIVE_CHAT_ID must be '-1005857809589'" TELEGRAM_LIVE_CHAT_ID -
case_env "on-air message to the Moafunk channel" "TELEGRAM_ANNOUNCE_CHAT_ID must be '-1005857809589'" TELEGRAM_ANNOUNCE_CHAT_ID -1001899120046
case_env "on-air message chat unset" "TELEGRAM_ANNOUNCE_CHAT_ID must be '-1005857809589'" TELEGRAM_ANNOUNCE_CHAT_ID -
case_env "production bot token" "TELEGRAM_BOT_TOKEN must be the test bot's token" TELEGRAM_BOT_TOKEN 123:abc
case_env "bot token unset" "TELEGRAM_BOT_TOKEN must be the test bot's token" TELEGRAM_BOT_TOKEN -
case_env "admin chat set (technical alerts)" "TELEGRAM_ADMIN_CHAT_ID must not be set" TELEGRAM_ADMIN_CHAT_ID -1003507800985
case_env "production admin URL" "ADMIN_BASE_URL must be" ADMIN_BASE_URL https://admin.live.moafunk.de
case_env "production app env" "APP_ENV must be 'staging'" APP_ENV production
case_env "producer pushes to the production box" "ICECAST_URL must be" ICECAST_URL "icecast://source:$LIVE_PW@178.104.160.103:8005/live"
case_env "test stream goes to the live mount" "ICECAST_TEST_URL must be" ICECAST_TEST_URL "icecast://source:$TEST_PW@host.docker.internal:8005/live"
case_env "status from the production Icecast" "ICECAST_STATUS_URL must be" ICECAST_STATUS_URL "https://admin.live.moafunk.de/status-json.xsl"
case_env "GitHub dispatch token set" "GITHUB_DISPATCH_TOKEN must not be set" GITHUB_DISPATCH_TOKEN ghp_x
case_env "Telegram setting added later" "TELEGRAM_NEW_SETTING must not be set" TELEGRAM_NEW_SETTING x
case_env "SoundCloud static token set" "SOUNDCLOUD_ACCESS_TOKEN must not be set" SOUNDCLOUD_ACCESS_TOKEN x
case_env "SoundCloud setting added later" "SOUNDCLOUD_NEW_SETTING must not be set" SOUNDCLOUD_NEW_SETTING x
case_env "Instagram token set" "INSTAGRAM_ACCESS_TOKEN_PROD must not be set" INSTAGRAM_ACCESS_TOKEN_PROD x
case_env "Instagram prod account id set" "INSTAGRAM_BUSINESS_ACCOUNT_ID_PROD must not be set" INSTAGRAM_BUSINESS_ACCOUNT_ID_PROD 1
case_env "Instagram station account" "INSTAGRAM_ACCOUNT must be 'dev'" INSTAGRAM_ACCOUNT prod
case_env "Instagram account unset" "INSTAGRAM_ACCOUNT must be 'dev'" INSTAGRAM_ACCOUNT -
case_env "Facebook secret set" "FACEBOOK_APP_SECRET must not be set" FACEBOOK_APP_SECRET x
case_env "OpenAI key set" "OPENAI_API_KEY must not be set" OPENAI_API_KEY sk-x
case_env "empty recovery mail key" "RECOVERY_MAIL_FROM must not be set" RECOVERY_MAIL_FROM ""
case_env "placeholder left in .env" "SECRET_KEY still has a <placeholder>" SECRET_KEY "<STAGING_SECRET_KEY>"
case_env "host login has no hash" "STAGING_HOST_PASSWORD_HASH must be an argon2 hash" STAGING_HOST_PASSWORD_HASH -
case_env "host login has no name" "STAGING_HOST_USERNAME must be set" STAGING_HOST_USERNAME -
case_env "e2e login has no hash" "STAGING_E2E_HOST_PASSWORD_HASH must be an argon2 hash" STAGING_E2E_HOST_PASSWORD_HASH -
case_env "e2e login is the people's host login" "STAGING_E2E_HOST_USERNAME must be 'e2e-host' (is 'staging-host')" \
  STAGING_E2E_HOST_USERNAME staging-host
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
STAGING_HOST_PASSWORD=$HOST_PW
STAGING_E2E_HOST_PASSWORD=$E2E_PW
STAGING_R2_ACCESS_KEY_ID=0123456789abcdef0123456789abcdef
STAGING_R2_SECRET_ACCESS_KEY=fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210
STAGING_HARBOR_TEST_PASSWORD=$TEST_PW
STAGING_HARBOR_LIVE_PASSWORD=$LIVE_PW
STAGING_ICECAST_SOURCE_PASSWORD=sourcesourcesourcesource1
STAGING_ICECAST_ADMIN_PASSWORD=adminadminadminadminadmin1
STAGING_ICECAST_RELAY_PASSWORD=relayrelayrelayrelayrelay1
STAGING_SOUNDCLOUD_CLIENT_ID=stagingclientid
STAGING_SOUNDCLOUD_CLIENT_SECRET=stagingclientsecret
STAGING_TELEGRAM_BOT_TOKEN=8880403786:AAtest-token_x
STAGING_INSTAGRAM_ACCESS_TOKEN_DEV=testtoken
STAGING_INSTAGRAM_BUSINESS_ACCOUNT_ID_DEV=17841400000000000
EOF
}

install_env() { # secrets on stdin
  staging install-env
}

rm -f "$BOX/.env" "$BOX/stream.env"
expect_ok "install-env writes env files from the secrets" install_env < <(secrets)
expect_ok "installed env files pass check-env" check
if grep -q "^SUPERADMIN_PASSWORD_HASH='\$argon2id\\$" "$BOX/.env"; then pass "superadmin hash is single-quoted"; else fail "superadmin hash is single-quoted"; fi
if grep -rqF -e "$ADMIN_PW" -e "$HOST_PW" -e "$E2E_PW" "$BOX"; then fail "plaintext login passwords were written"; else pass "plaintext login passwords are not stored"; fi
if grep -q "^STAGING_HOST_PASSWORD_HASH='\$argon2id\\$" "$BOX/.env"; then pass "host login hash (password with spaces and symbols)"; else fail "host login hash (password with spaces and symbols)"; fi
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
expect_fact "seed-db adds the staging host" "$BOX/data/unheard.db" "user staging-host host $HOST_HASH"
expect_fact "seed-db adds the second staging host" "$BOX/data/unheard.db" "user staging-host-2 host $HOST_HASH"
expect_fact "seed-db adds the e2e host" "$BOX/data/unheard.db" "user e2e-host host $E2E_HASH"
expect_fact "seed-db keeps production users unusable" "$BOX/data/unheard.db" "user boss superadmin !"

make_prod_db "$TMP/raw.db"
expect_ok "seed-db scrubs a copy that was not scrubbed" seed_from_stdin "$TMP/raw.db"
expect_fact "seed-db empties sessions itself" "$BOX/data/unheard.db" "sessions 0"
expect_fact "seed-db drops stored tokens itself" "$BOX/data/unheard.db" "settings notifications_enabled"

python3 -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute(\"UPDATE users SET role='admin', password_hash='old' WHERE username='staging-host'\"); c.commit()" "$BOX/data/unheard.db"
expect_ok "users resets the staging logins in the running DB" staging users
expect_fact "users gives staging-host back its role and Bitwarden password" "$BOX/data/unheard.db" "user staging-host host $HOST_HASH"
expect_fact "users resets the second staging host too" "$BOX/data/unheard.db" "user staging-host-2 host $HOST_HASH"
set_key "$BOX/.env" STAGING_HOST_PASSWORD_HASH -
expect_refused "users refuses .env without the host hash" "a host login needs a username and an argon2 hash" staging users
write_good
set_key "$BOX/.env" STAGING_E2E_HOST_PASSWORD_HASH -
expect_refused "users refuses .env without the e2e host hash" "a host login needs a username and an argon2 hash" staging users
write_good

printf 'not a database' >"$TMP/broken.db"
expect_refused "seed-db refuses a damaged copy" "staging DB not changed" seed_from_stdin "$TMP/broken.db"
expect_fact "a refused seed leaves the staging DB alone" "$BOX/data/unheard.db" "artists 1"

echo "--- hold and the CI key"
DIGEST="sha256:$(printf '%064d' 7)"
write_good
rm -f "$BOX/hold" "$BOX/icecast.xml"

ci() { # what the CI asks for over ssh
  SSH_ORIGINAL_COMMAND="$1" staging ci-deploy
}

expect_refused "the CI key cannot run other commands" "the CI key may only run" ci "rm -rf /"
expect_refused "the CI key needs an exact digest" "the CI key may only run" ci "deploy sha-abc1234"
expect_refused "the CI key cannot add commands" "the CI key may only run" ci "deploy sha-abc1234 $DIGEST; reboot"
expect_refused "the CI key cannot use a short digest" "the CI key may only run" ci "deploy sha-abc1234 sha256:abc"
expect_refused "a hold needs a reason" "usage: staging.sh hold" staging hold
echo sha-abc1234 >"$BOX/tag"
if [[ "$(ci status)" == $'tag: sha-abc1234\nhold: no' ]]; then pass "the CI key can read the tag and hold"; else fail "the CI key can read the tag and hold: $(ci status 2>&1)"; fi
expect_ok "hold pauses CI deploys" staging hold "R1 release checks"
set +e
out="$(ci "deploy sha-abc1234 $DIGEST" 2>&1)"
code=$?
set -e
if [[ "$code" == 3 && "$out" == *"on hold"*"R1 release checks"*"sha-abc1234 was not deployed"* ]]; then
  pass "a CI deploy during a hold changes nothing and exits 3"
else
  fail "a CI deploy during a hold changes nothing and exits 3: exit $code, $out"
fi
if [[ "$(ci status)" == *"hold: "*"R1 release checks"* ]]; then pass "status shows the hold"; else fail "status shows the hold: $(ci status 2>&1)"; fi
: >"$CALLS"
expect_refused "up by hand refuses during a hold" "R1 release checks): up refused" staging up sha-abc1234
expect_refused "down by hand refuses during a hold" "down refused" staging down
if [[ ! -s "$CALLS" ]]; then pass "a refused up or down runs no docker"; else fail "a refused up or down runs no docker: $(cat "$CALLS")"; fi
expect_refused "STAGING_IGNORE_HOLD=1 lets up go on" "run staging.sh setup first" env STAGING_IGNORE_HOLD=1 \
  PATH="$TMP/bin:$PATH" STAGING_DIR="$BOX" bash "$HERE/staging.sh" up sha-abc1234
expect_ok "hold replaces a release hold" staging hold "R1 release checks"
expect_ok "unhold resumes CI deploys" staging unhold
expect_refused "after unhold a CI deploy reaches up" "run staging.sh setup first" ci "deploy sha-abc1234 $DIGEST"

echo "--- the chaos test show through the CI key"
rm -f "$BOX/hold"
expect_ok "e2e-hold holds an idle staging" ci "e2e-hold 42"
if [[ "$(cat "$BOX/hold")" == *" | e2e run 42" ]]; then pass "the hold names the run"; else fail "the hold names the run: $(cat "$BOX/hold")"; fi
expect_refused "unhold by hand refuses a test run's hold" "e2e run 42): unhold refused" staging unhold
expect_refused "hold by hand refuses to replace a test run's hold" "hold refused" staging hold "R1 release checks"
if [[ "$(cat "$BOX/hold")" == *" | e2e run 42" ]]; then pass "the test run keeps its hold"; else fail "the test run keeps its hold: $(cat "$BOX/hold")"; fi
expect_ok "e2e-unhold of another run" ci "e2e-unhold 43"
if [[ -f "$BOX/hold" ]]; then pass "leaves the run's hold"; else fail "leaves the run's hold"; fi
expect_ok "e2e-unhold removes its own hold" ci "e2e-unhold 42"
if [[ ! -f "$BOX/hold" ]]; then pass "the hold is gone"; else fail "the hold is gone"; fi
staging hold "R1 release checks" >/dev/null
if [[ "$(ci "e2e-hold 42")" == "already on hold: "*"R1 release checks" ]]; then pass "e2e-hold keeps a release hold"; else fail "e2e-hold keeps a release hold"; fi
ci "e2e-unhold 42" >/dev/null
if [[ "$(cat "$BOX/hold")" == *"| R1 release checks" ]]; then pass "e2e-unhold never removes a release hold"; else fail "e2e-unhold never removes a release hold"; fi
staging unhold >/dev/null
: >"$CALLS"
expect_ok "e2e-restart-liquidsoap" ci e2e-restart-liquidsoap
if grep -q "restart liquidsoap" "$CALLS"; then pass "it restarts only Liquidsoap"; else fail "it restarts only Liquidsoap: $(cat "$CALLS")"; fi
# Stub: the API answers /health and Icecast lists both mounts.
mkdir -p "$TMP/bin-up"
cat >"$TMP/bin-up/curl" <<'EOF'
#!/usr/bin/env bash
echo "curl ${*: -1}" >>"$CALLS"
[[ "${*: -1}" != */status-json.xsl ]] || echo '{"source":[{"listenurl":"http://x/live.mp3"},{"listenurl":"http://x/test.mp3"}]}'
EOF
chmod +x "$TMP/bin-up/curl"
ci_up() { # a CI request while the containers come back
  PATH="$TMP/bin-up:$PATH" ci "$1"
}
: >"$CALLS"
expect_ok "e2e-restart-api" ci_up e2e-restart-api
if [[ "$(cat "$CALLS")" == *"restart api"$'\n'"curl http://127.0.0.1:8000/health" ]]; then
  pass "it restarts only the API, then waits for /health"
else
  fail "it restarts only the API, then waits for /health: $(cat "$CALLS")"
fi
: >"$CALLS"
expect_ok "e2e-restart-icecast" ci_up e2e-restart-icecast
if [[ "$(cat "$CALLS")" == *"restart icecast"$'\n'"curl http://127.0.0.1:8010/status-json.xsl" ]]; then
  pass "it restarts only Icecast, then waits for both mounts"
else
  fail "it restarts only Icecast, then waits for both mounts: $(cat "$CALLS")"
fi
cut() { # e2e-cut, called from 203.0.113.7
  SSH_CLIENT="203.0.113.7 51000 22" SSH_ORIGINAL_COMMAND="e2e-cut $1" staging ci-deploy
}
: >"$CALLS"
expect_ok "e2e-cut cuts the caller" cut 20
rule="INPUT -s 203.0.113.7 -p tcp -m multiport --dports 80,443 -j REJECT --reject-with tcp-reset"
if [[ "$(cat "$CALLS")" == "systemd-run --quiet --on-active=20 --timer-property=AccuracySec=100ms iptables -D $rule"$'\n'"iptables -I $rule" ]]; then
  pass "the cut gets its timer first, then the rule, for the caller's IP only"
else
  fail "the cut gets its timer first, then the rule, for the caller's IP only: $(cat "$CALLS")"
fi
for bad in 0 61 "20 198.51.100.1" "20; reboot"; do
  expect_refused "e2e-cut refuses '$bad'" "the CI key may only run" cut "$bad"
done
expect_refused "e2e-cut needs the caller's address" "needs the caller's IPv4 address" ci "e2e-cut 20"
expect_refused "e2e-hold needs a run number" "the CI key may only run" ci 'e2e-hold $(reboot)'
python3 - "$BOX/data/unheard.db" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("CREATE TABLE IF NOT EXISTS shows (id INTEGER PRIMARY KEY, title TEXT, date TEXT)")
con.execute("CREATE TABLE IF NOT EXISTS recording_versions (id INTEGER PRIMARY KEY, show_id INTEGER, status TEXT, duration_ms INTEGER, archive_key TEXT)")
con.execute("INSERT INTO shows (id, title, date) VALUES (901, 'e2e-42 #1', '2026-11-16'), (902, 'Real show #3', '2026-11-16')")
con.execute("INSERT INTO recording_versions (show_id, status, duration_ms, archive_key) VALUES (901, 'finalized', 312000, 'shows/x.mp3'), (902, 'finalized', 1, NULL)")
con.commit()
PY
if [[ "$(ci "e2e-recordings 901")" == "finalized 312000 1" ]]; then pass "e2e-recordings lists a test episode's recordings"; else fail "e2e-recordings lists a test episode's recordings: $(ci "e2e-recordings 901" 2>&1)"; fi
expect_refused "e2e-recordings refuses other shows" "is not a test episode" ci "e2e-recordings 902"
expect_refused "e2e-recordings needs a show number" "the CI key may only run" ci "e2e-recordings 1 OR 1=1"
python3 - "$BOX/data/unheard.db" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.executescript("""
CREATE TABLE show_templates (id INTEGER PRIMARY KEY, owner_user_id INTEGER NOT NULL REFERENCES users(id), name TEXT NOT NULL);
ALTER TABLE shows ADD COLUMN template_id INTEGER REFERENCES show_templates(id) ON DELETE SET NULL;
CREATE TABLE series_uploads (session_id TEXT PRIMARY KEY, series_id INTEGER NOT NULL REFERENCES show_templates(id) ON DELETE CASCADE);
INSERT INTO show_templates (id, owner_user_id, name) VALUES
  (1, (SELECT id FROM users WHERE username = 'staging-host'), 'e2e-42-live'),
  (2, (SELECT id FROM users WHERE username = 'staging-host'), 'e2e-42-set'),
  (3, (SELECT id FROM users WHERE username = 'boss'), 'e2e-boss'),
  (4, (SELECT id FROM users WHERE username = 'staging-host'), 'Real show'),
  (5, (SELECT id FROM users WHERE username = 'e2e-host'), 'e2e-43-live');
INSERT INTO series_uploads VALUES ('upload-1', 1);
INSERT INTO shows (id, title, date, template_id) VALUES (903, 'e2e-42-set #1', '2026-11-17', 2);
""")
PY
out="$(ci e2e-cleanup 2>&1)"
if [[ "$out" == "deleted 2 test Show(s); kept, with episodes: e2e-42-set" ]]; then
  pass "e2e-cleanup deletes the test hosts' test Shows without episodes"
else
  fail "e2e-cleanup deletes the test hosts' test Shows without episodes: $out"
fi
left="$(python3 -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); print(','.join(r[0] for r in c.execute('SELECT name FROM show_templates ORDER BY id')), c.execute('SELECT COUNT(*) FROM series_uploads').fetchone()[0])" "$BOX/data/unheard.db")"
if [[ "$left" == "e2e-42-set,e2e-boss,Real show 0" ]]; then
  pass "e2e-cleanup keeps other Shows and drops the deleted Show's uploads"
else
  fail "e2e-cleanup keeps other Shows and drops the deleted Show's uploads: $left"
fi
expect_refused "e2e-cleanup takes no arguments" "the CI key may only run" ci "e2e-cleanup 1"

today="$(date -u +%F)"
python3 - "$BOX/data/unheard.db" "$today" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
day = sys.argv[2]
con.execute("ALTER TABLE shows ADD COLUMN start_time TEXT")
con.execute("ALTER TABLE shows ADD COLUMN end_time TEXT")
con.executemany(
    "INSERT INTO shows (id, title, date, start_time, end_time) VALUES (?, ?, ?, ?, ?)",
    [
        (910, "Copied show", day, "20:00", None),  # no end: a point inside the slot
        (911, "Own test show", day, "19:10", "21:07"),
        (912, "Earlier show", day, "17:00", "19:30"),  # ends when the slot starts
        (913, "No start time", day, None, None),
        (914, "Another day", "2026-01-01", "20:00", None),
    ],
)
con.commit()
PY
slot_days() { # the shows 910-914 as id:days from today
  python3 -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); print(' '.join(f'{i}:{d}' for i, d in c.execute(\"SELECT id, CAST(julianday(date) - julianday(?) AS INTEGER) FROM shows WHERE id BETWEEN 910 AND 913 ORDER BY id\", (sys.argv[2],))), c.execute('SELECT date FROM shows WHERE id = 914').fetchone()[0])" "$BOX/data/unheard.db" "$today"
}
out="$(ci "e2e-free-slot $today 19:30 21:59" 2>&1)"
if [[ "$out" == "slot $today 19:30-21:59: moved 2 show(s) to the day before: 910 Copied show, 911 Own test show" ]]; then
  pass "e2e-free-slot moves the shows in the slot to the day before"
else
  fail "e2e-free-slot moves the shows in the slot to the day before: $out"
fi
if [[ "$(slot_days)" == "910:-1 911:-1 912:0 913:0 2026-01-01" ]]; then
  pass "e2e-free-slot leaves shows outside the slot alone"
else
  fail "e2e-free-slot leaves shows outside the slot alone: $(slot_days)"
fi
if [[ "$(ci "e2e-free-slot $today 19:30 21:59")" == "slot $today 19:30-21:59: free" ]]; then
  pass "e2e-free-slot on a free slot changes nothing"
else
  fail "e2e-free-slot on a free slot changes nothing"
fi
expect_refused "e2e-free-slot refuses a day far from today" "is not between yesterday and two days ahead" ci "e2e-free-slot 2026-01-01 19:30 21:59"
# A test books a day ahead in Berlin time: up to two days ahead of the box's UTC date.
ahead="$(python3 -c "import datetime; print(datetime.datetime.now(datetime.timezone.utc).date() + datetime.timedelta(days=2))")"
if [[ "$(ci "e2e-free-slot $ahead 00:03 01:03")" == "slot $ahead 00:03-01:03: free" ]]; then
  pass "e2e-free-slot takes a slot two days ahead"
else
  fail "e2e-free-slot takes a slot two days ahead"
fi
far="$(python3 -c "import datetime; print(datetime.datetime.now(datetime.timezone.utc).date() + datetime.timedelta(days=3))")"
expect_refused "e2e-free-slot refuses three days ahead" "is not between yesterday and two days ahead" ci "e2e-free-slot $far 00:03 01:03"
expect_refused "e2e-free-slot needs a date and two times" "the CI key may only run" ci "e2e-free-slot $today 25:00 26:00"
expect_refused "e2e-free-slot cannot add commands" "the CI key may only run" ci "e2e-free-slot $today 19:30 21:59; reboot"

announce() { # the announce_* settings, sorted
  python3 -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); print(' '.join(f'{k}={v}' for k, v in c.execute(\"SELECT key, value FROM app_settings WHERE key GLOB 'announce_*' ORDER BY key\")))" "$BOX/data/unheard.db"
}
python3 - "$BOX/data/unheard.db" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT (datetime('now')))")
con.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('announce_feed_per_day', '1'), ('announce_slots', '12:00,18:00')")
con.commit()
PY
before="$(announce)"
ci "e2e-instagram on 14:05" >/dev/null
ci "e2e-instagram on 14:35" >/dev/null
on="$(announce)"
if [[ "$on" == *announce_approval_mode=never* && "$on" == *announce_feed_per_day=20* && "$on" == *announce_slots=14:35* ]]; then
  pass "e2e-instagram on sets the test limits and feed time"
else
  fail "e2e-instagram on sets the test limits and feed time: $on"
fi
ci "e2e-instagram off" >/dev/null
if [[ "$(announce)" == "$before" ]]; then
  pass "e2e-instagram off restores the settings from before the first on"
else
  fail "e2e-instagram off restores the settings from before the first on: $(announce)"
fi
if [[ "$(ci "e2e-instagram off")" == "no saved announcement settings: nothing to restore" ]]; then
  pass "e2e-instagram off twice changes nothing"
else
  fail "e2e-instagram off twice changes nothing"
fi
python3 -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute('ALTER TABLE shows ADD COLUMN soundcloud_public INTEGER DEFAULT 0'); c.commit()" "$BOX/data/unheard.db"
ci "e2e-instagram public 901" >/dev/null
public="$(python3 -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); print(c.execute('SELECT group_concat(soundcloud_public) FROM (SELECT soundcloud_public FROM shows WHERE id IN (901, 902) ORDER BY id)').fetchone()[0])" "$BOX/data/unheard.db")"
if [[ "$public" == "1,0" ]]; then pass "e2e-instagram public marks a test episode's track public"; else fail "e2e-instagram public marks a test episode's track public: $public"; fi
expect_refused "e2e-instagram public refuses other shows" "is not a test episode" ci "e2e-instagram public 902"
expect_refused "e2e-instagram public needs a show number" "the CI key may only run" ci "e2e-instagram public 1 OR 1=1"
expect_refused "e2e-instagram needs a valid time" "the CI key may only run" ci "e2e-instagram on 24:00"
expect_refused "e2e-instagram takes only on and off" "the CI key may only run" ci "e2e-instagram on 12:00; reboot"

AUTH="$TMP/authorized_keys"
cp "$HERE/ci-deploy.pub" "$BOX/ci-deploy.pub"
ci_key="$(cat "$HERE/ci-deploy.pub")"
printf 'ssh-ed25519 AAAAotherkey anton\n%s\n' "$ci_key" >"$AUTH"
install_key() {
  STAGING_AUTHORIZED_KEYS="$AUTH" staging install-ci-key
}
expect_ok "install-ci-key restricts the CI key" install_key
want="command=\"$BOX/staging.sh ci-deploy\",restrict $ci_key"
if [[ "$(grep -c "${ci_key#* }" "$AUTH")" == 1 ]] && grep -qxF "$want" "$AUTH" && grep -qx 'ssh-ed25519 AAAAotherkey anton' "$AUTH"; then
  pass "the CI key line is replaced, other keys stay"
else
  fail "the CI key line is replaced, other keys stay: $(cat "$AUTH")"
fi
before="$(cat "$AUTH")"
expect_ok "install-ci-key runs again" install_key
if [[ "$(cat "$AUTH")" == "$before" ]]; then pass "install-ci-key changes nothing the second time"; else fail "install-ci-key changes nothing the second time"; fi
echo "ssh-ed25519 AAAAsomeoneelse laptop" >"$BOX/ci-deploy.pub"
expect_refused "install-ci-key refuses a key that is not the CI key" "ci-deploy.pub is not the CI deploy key" install_key

echo "--- push-secrets"
# Stub bw with a JSON file as the vault, and an ssh that runs install-env here.
VAULT="$TMP/vault.json"
mkdir -p "$TMP/pushbin"
cat >"$TMP/pushbin/bw" <<EOF
#!/usr/bin/env bash
case "\$1 \$2" in
  "status "*) echo '{"status":"unlocked"}' ;;
  "sync "*) ;;
  "list folders") echo '[{"id":"f1","name":"Staging"}]' ;;
  "list items") jq --arg s "\$4" '[.[] | select(.name | contains(\$s))]' "$VAULT" ;;
  "get template") echo '{"type":1,"name":"Item name","folderId":null,"notes":null,"login":null}' ;;
  "generate "*) LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c "\$4" ;;
  "encode ") base64 ;;
  "create item") jq --argjson i "\$(base64 -d)" '. + [\$i]' "$VAULT" >"$VAULT.new" && mv "$VAULT.new" "$VAULT" ;;
  *) echo "stub bw: \$*" >&2; exit 1 ;;
esac
EOF
cat >"$TMP/pushbin/ssh" <<EOF
#!/usr/bin/env bash
PATH="$TMP/bin:\$PATH" STAGING_DIR="$BOX" bash "$HERE/staging.sh" install-env
EOF
SM="$TMP/sm.json"
cat >"$TMP/pushbin/bws" <<EOF
#!/usr/bin/env bash
[[ -n "\${BWS_ACCESS_TOKEN:-}" ]] || { echo "Missing access token" >&2; exit 1; }
[[ "\$*" == "secret list --output json" ]] || { echo "stub bws: \$*" >&2; exit 1; }
cat "$SM"
EOF
chmod +x "$TMP/pushbin/bw" "$TMP/pushbin/ssh" "$TMP/pushbin/bws"

# Secrets Manager with the station SoundCloud app, the e2e login (and an unrelated secret).
sm_secrets() {
  printf '[{"key":"SOUNDCLOUD_CLIENT_ID","value":"stationclientid"},
    {"key":"SOUNDCLOUD_CLIENT_SECRET","value":"%s"},
    {"key":"INSTAGRAM_ACCESS_TOKEN_DEV","value":"igdevtoken"},
    {"key":"INSTAGRAM_BUSINESS_ACCOUNT_ID_DEV","value":"17841400000000000"},
    {"key":"INSTAGRAM_ACCESS_TOKEN_PROD","value":"igprodtoken"},
    {"key":"STAGING_E2E_HOST_PASSWORD","value":"%s"},
    {"key":"STAGING_TELEGRAM_BOT_TOKEN","value":"8880403786:AApushed-token_x"},
    {"key":"TELEGRAM_BOT_TOKEN","value":"123:abc"}]' "$1" "${2-$E2E_PW}" >"$SM"
}
sm_secrets stationclientsecret

r2_items() {
  printf '[{"name":"Moafunk STAGING_R2_ACCESS_KEY_ID","login":{"password":"0123456789abcdef0123456789abcdef"}},
    {"name":"Moafunk STAGING_R2_SECRET_ACCESS_KEY","login":{"password":"%s"}}]' "$1" >"$VAULT"
}

push() {
  PATH="$TMP/pushbin:$PATH" BW_FOLDER=Staging BWS_ACCESS_TOKEN=sm-token bash "$HERE/push-secrets.sh" root@staging.test
}

push_without_sm_token() {
  PATH="$TMP/pushbin:$PATH" BW_FOLDER=Staging env -u BWS_ACCESS_TOKEN bash "$HERE/push-secrets.sh" root@staging.test
}

# Password of the vault item NAME (empty when it is not a string).
vault_pw() {
  jq -r --arg n "Moafunk $1" '.[] | select(.name==$n) | .login.password | strings' "$VAULT"
}

rm -f "$BOX/.env" "$BOX/stream.env"
r2_items fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210
expect_ok "push-secrets creates the items and installs the env" push
expect_ok "pushed env files pass check-env" check
if [[ "$(vault_pw STAGING_HARBOR_LIVE_PASSWORD)" =~ ^[A-Za-z0-9]{48}$ ]]; then
  pass "generated passwords are 48 letters and digits"
else
  fail "generated passwords are 48 letters and digits: '$(vault_pw STAGING_HARBOR_LIVE_PASSWORD)'"
fi
if [[ "$(jq -r '.[] | select(.name=="Moafunk STAGING_SUPERADMIN_PASSWORD") | "\(.login.username) \(.login.password | length) \(.folderId)"' "$VAULT")" == "staging-admin 24 f1" ]]; then
  pass "the superadmin item has the username, 24 characters and the folder"
else
  fail "the superadmin item has the username, 24 characters and the folder: $(jq -c '.[2]' "$VAULT")"
fi
if grep -q "ICECAST_URL=icecast://source:$(vault_pw STAGING_HARBOR_LIVE_PASSWORD)@" "$BOX/.env"; then
  pass "the box gets the Bitwarden values"
else
  fail "the box gets the Bitwarden values"
fi
if [[ "$(jq -r '.[] | select(.name=="Moafunk STAGING_HOST_PASSWORD") | "\(.login.username) \(.login.password | length)"' "$VAULT")" == "staging-host 24" ]]; then
  pass "the host item has the username and 24 characters"
else
  fail "the host item has the username and 24 characters"
fi
if out="$(push 2>&1)" && [[ "$out" != *created* ]]; then pass "a second run creates nothing"; else fail "a second run creates nothing: $out"; fi
if grep -qx "SOUNDCLOUD_CLIENT_ID=stationclientid" "$BOX/.env" && grep -qx "SOUNDCLOUD_CLIENT_SECRET=stationclientsecret" "$BOX/.env"; then
  pass "the box gets the SoundCloud app from Secrets Manager"
else
  fail "the box gets the SoundCloud app from Secrets Manager"
fi
if [[ "$out" != *stationclientsecret* ]]; then pass "push prints no Secrets Manager value"; else fail "push prints no Secrets Manager value"; fi
if grep -q "123:abc" "$BOX/.env"; then fail "an unrelated Secrets Manager secret reached the box"; else pass "only the listed secrets come from Secrets Manager"; fi
if grep -qx "TELEGRAM_BOT_TOKEN=8880403786:AApushed-token_x" "$BOX/.env"; then
  pass "the box gets the Telegram test bot token"
else
  fail "the box gets the Telegram test bot token"
fi
if grep -qx "INSTAGRAM_ACCESS_TOKEN_DEV=igdevtoken" "$BOX/.env" && grep -qx "INSTAGRAM_ACCOUNT=dev" "$BOX/.env"; then
  pass "the box gets the Instagram test account"
else
  fail "the box gets the Instagram test account"
fi
if grep -q "igprodtoken" "$BOX/.env"; then fail "the Instagram station token reached the box"; else pass "the Instagram station token stays off the box"; fi
if grep -q "^STAGING_E2E_HOST_PASSWORD_HASH='\$argon2id\\$" "$BOX/.env" && ! grep -qF "$E2E_PW" "$BOX/.env"; then
  pass "the box gets the e2e login from Secrets Manager, only as a hash"
else
  fail "the box gets the e2e login from Secrets Manager, only as a hash"
fi

expect_refused "push without a Secrets Manager token stops" "export BWS_ACCESS_TOKEN" push_without_sm_token
sm_secrets ""
expect_refused "an empty SoundCloud secret stops the push" "Secrets Manager secret SOUNDCLOUD_CLIENT_SECRET is empty" push
printf '[{"key":"SOUNDCLOUD_CLIENT_ID","value":"stationclientid"}]' >"$SM"
expect_refused "a missing SoundCloud secret stops the push" "0 Secrets Manager secrets named SOUNDCLOUD_CLIENT_SECRET" push
sm_secrets stationclientsecret ""
expect_refused "an empty e2e login stops the push" "Secrets Manager secret STAGING_E2E_HOST_PASSWORD is empty" push
sm_secrets stationclientsecret
jq 'map(select(.key != "STAGING_TELEGRAM_BOT_TOKEN"))' "$SM" >"$SM.new" && mv "$SM.new" "$SM"
expect_refused "a missing Telegram test bot token stops the push" "0 Secrets Manager secrets named STAGING_TELEGRAM_BOT_TOKEN" push
sm_secrets stationclientsecret

r2_items fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210
jq '.[1:]' "$VAULT" >"$VAULT.new" && mv "$VAULT.new" "$VAULT"
expect_refused "a missing R2 item stops the push" "add exactly one Bitwarden item 'Moafunk STAGING_R2_ACCESS_KEY_ID'" push

r2_items fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210
jq '. + [{"name":"Moafunk STAGING_SECRET_KEY","login":{"password":{"type":1}}}]' "$VAULT" >"$VAULT.new" && mv "$VAULT.new" "$VAULT"
expect_refused "an item whose password is not a string stops the push" "Moafunk STAGING_SECRET_KEY has no password" push

if ((failures > 0)); then
  echo "$failures staging test(s) failed"
  exit 1
fi
echo "all staging tests passed"
