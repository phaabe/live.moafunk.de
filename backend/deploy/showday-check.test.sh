#!/usr/bin/env bash
# Tests for showday-check.sh with stubbed curl, openssl, docker, df and
# rclone, a fake DB and a fixed clock. Needs bash 3.2+ and python3:
#   bash backend/deploy/showday-check.test.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
APP="$TMP/app"
FAKE="$TMP/fake"
failures=0
mkdir -p "$APP/data" "$FAKE/bin"

pass() { echo "ok   $1"; }
fail() {
  echo "FAIL $1"
  failures=$((failures + 1))
}

cat >"$FAKE/bin/curl" <<'EOF'
#!/usr/bin/env bash
# Telegram: log the arguments and the config read from stdin.
if [[ " $* " == *" -K - "* ]]; then
  { echo "ARGS $*"; echo "STDIN $(cat)"; } >>"$FAKE/posts"
  exit "${FAKE_POST_EXIT:-0}"
fi
case "$*" in
  *127.0.0.1:8000/health*) echo '{"app":"unheard-backend","status":"healthy"}' ;;
  *status-json.xsl*) echo "${FAKE_MOUNTS-"listenurl /live.mp3 /test.mp3"}" ;;
  *) echo ok ;;
esac
EOF
cat >"$FAKE/bin/openssl" <<'EOF'
#!/usr/bin/env bash
cat >/dev/null
[[ "$1" == s_client ]] && echo "-----BEGIN CERTIFICATE-----"
exit 0
EOF
cat >"$FAKE/bin/docker" <<'EOF'
#!/usr/bin/env bash
echo "${FAKE_LIQUIDSOAP:-true}"
EOF
cat >"$FAKE/bin/df" <<'EOF'
#!/usr/bin/env bash
printf 'Filesystem 1024-blocks Used Available Capacity Mounted\n/dev/sda1 80000000 1 %s 10%% /\n' "${FAKE_FREE_KB:-20000000}"
EOF
cat >"$FAKE/bin/rclone" <<'EOF'
#!/usr/bin/env bash
printf 'unheard-db-2026-11-15_03-00-00.db.gz\nunheard-db-2026-11-16_03-00-00.db.gz\n'
EOF
chmod +x "$FAKE/bin/"*

cat >"$APP/.env" <<'EOF'
TELEGRAM_BOT_TOKEN="123:secret-token"
TELEGRAM_ADMIN_CHAT_ID=-100777
EOF
python3 - "$APP/data/unheard.db" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("CREATE TABLE shows (id INTEGER PRIMARY KEY, title TEXT, date TEXT, start_time TEXT, prerecorded_key TEXT, prerecorded_confirmed_at TEXT)")
con.executemany("INSERT INTO shows VALUES (?, ?, ?, ?, ?, ?)", [
    (1, "Brunchtime #31", "2026-11-17", "20:00", None, None),
    (2, "Night Set #2", "2026-11-16", "19:00", "sets/x.mp3", "2026-11-10T10:00:00Z"),
    (3, "Later #3", "2026-11-16", "21:30", None, None),
    (4, "Done #4", "2026-11-16", "10:00", None, None),
])
con.commit()
PY

# Run the check at Berlin time $1 (with more env after it).
check() {
  local at="$1"
  shift
  env PATH="$FAKE/bin:$PATH" FAKE="$FAKE" APP_DIR="$APP" STATE_DIR="$TMP/state" \
    SHOWDAY_NOW="$at" "$@" bash "$HERE/showday-check.sh"
}
posts() { grep -c '^ARGS' "$FAKE/posts" 2>/dev/null || true; }

: >"$FAKE/posts"
if out="$(check 2026-11-16T12:00:00+01:00)" && [[ "$(posts)" == 0 ]]; then
  pass "before 18:00 and more than an hour before a show: no post"
else
  fail "before 18:00 and more than an hour before a show: no post ($out)"
fi

if out="$(check 2026-11-16T18:05:00+01:00)"; then pass "all checks pass: exit 0"; else fail "all checks pass: exit 0 ($out)"; fi
if [[ "$(posts)" == 2 && "$out" == *"Tomorrow: Brunchtime #31 · 2026-11-17 20:00 · live"* &&
  "$out" == *"In 1 hour: Night Set #2 · 2026-11-16 19:00 · pre-recorded, set confirmed"* ]]; then
  pass "the day before (from 18:00) and the hour before each get a post"
else
  fail "the day before (from 18:00) and the hour before each get a post: $(posts) posts, $out"
fi
if [[ "$out" != *"Later #3"* && "$out" != *"Done #4"* ]]; then pass "later and past shows get none"; else fail "later and past shows get none"; fi
if [[ "$out" == *"all checks passed"* && "$out" == *"DB backup 14 h old"* ]]; then pass "the post has the checks"; else fail "the post has the checks: $out"; fi
if grep -q 'message_thread_id=26' "$FAKE/posts" && grep -q 'chat_id=-100777' "$FAKE/posts"; then
  pass "posts go to the MoafunkBot topic 26 of the admin chat"
else
  fail "posts go to the MoafunkBot topic 26 of the admin chat: $(cat "$FAKE/posts")"
fi
if grep '^ARGS' "$FAKE/posts" | grep -q secret-token; then
  fail "the bot token is not on curl's command line"
elif grep '^STDIN' "$FAKE/posts" | grep -q 'bot123:secret-token/sendMessage'; then
  pass "the bot token is not on curl's command line"
else
  fail "the bot token reaches curl on stdin: $(cat "$FAKE/posts")"
fi

: >"$FAKE/posts"
check 2026-11-16T18:20:00+01:00 >/dev/null
if [[ "$(posts)" == 0 ]]; then pass "a second run posts nothing"; else fail "a second run posts nothing: $(posts)"; fi

: >"$FAKE/posts"
set +e
out="$(check 2026-11-16T20:35:00+01:00 FAKE_LIQUIDSOAP=false FAKE_FREE_KB=1000)"
code=$?
set -e
if [[ "$code" == 1 && "$(posts)" == 1 && "$out" == *"SOMETHING IS WRONG"* && "$out" == *"❌ Liquidsoap"* && "$out" == *"❌ only 0 GB free"* ]]; then
  pass "a failed check is posted and exits 1"
else
  fail "a failed check is posted and exits 1: exit $code, $(posts) posts, $out"
fi

rm -rf "$TMP/state"
: >"$FAKE/posts"
set +e
out="$(check 2026-11-16T20:35:00+01:00 FAKE_POST_EXIT=22)"
code=$?
set -e
if [[ "$code" == 1 && ! -e "$TMP/state/3-hour" ]]; then
  pass "a post that fails exits 1 and is tried again next time"
else
  fail "a post that fails exits 1 and is tried again next time: exit $code"
fi

if ((failures > 0)); then
  echo "$failures show-day check test(s) failed"
  exit 1
fi
echo "all show-day check tests passed"
