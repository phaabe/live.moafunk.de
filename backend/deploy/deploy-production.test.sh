#!/usr/bin/env bash
# Tests for deploy-production.sh with a fake docker and curl and a fixture DB.
# Runs anywhere with bash 3.2+ and python3; no Docker or server needed:
#   bash backend/deploy/deploy-production.test.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
APP="$TMP/app"
BIN="$TMP/bin"
IMAGE=ghcr.io/phaabe/live.moafunk.de-backend
DIGEST="sha256:$(printf '%064d' 5)"
failures=0
mkdir -p "$APP/data" "$BIN"

# Fake docker. State lives in files: the running image, and a call log.
cat >"$BIN/docker" <<'EOF'
#!/usr/bin/env bash
echo "docker $*" >>"$FAKE/calls"
case "$*" in
  "compose version") exit 0 ;;
  *"{{.Config.Image}}"*) cat "$FAKE/running" ;;
  *"com.docker.compose.project.working_dir"*) echo "$FAKE_APP" ;;
  *"com.docker.compose.project.config_files"*) echo "$FAKE_APP/docker-compose.prod.yml" ;;
  *"com.docker.compose.project\""*) echo unheard-backend ;;
  *"{{.State.Health.Status}}"*) echo healthy ;;
  "image inspect"*) echo "$FAKE_REPO_DIGEST" ;;
  "compose "*" up -d unheard-api") echo "ghcr.io/phaabe/live.moafunk.de-backend:$UNHEARD_TAG" >"$FAKE/running" ;;
  "compose "*" pull unheard-api") ;;
  "logs "*) echo "Starting server on 0.0.0.0:8000" ;;
  *) echo "fake docker: unexpected: $*" >&2; exit 1 ;;
esac
EOF
cat >"$BIN/curl" <<'EOF'
#!/usr/bin/env bash
case "$*" in
  *127.0.0.1:8000/api/stream/status*) echo "$FAKE_STATUS" ;;
  *"%{http_code}"*) printf 200 ;;
  *) echo "fake curl: unexpected: $*" >&2; exit 1 ;;
esac
EOF
chmod +x "$BIN/docker" "$BIN/curl"

# Fixture DB: shows relative to now in Berlin. $1 = hours until a show starts
# today ("none" for no show).
make_db() {
  rm -f "$APP/data/unheard.db"
  python3 - "$APP/data/unheard.db" "$1" <<'PY'
import datetime, sqlite3, sys
from zoneinfo import ZoneInfo

con = sqlite3.connect(sys.argv[1])
con.execute("CREATE TABLE shows (id INTEGER PRIMARY KEY, title TEXT, date TEXT, start_time TEXT, end_time TEXT)")
con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT)")
con.execute("INSERT INTO users (username) VALUES ('kept')")
if sys.argv[2] != "none":
    start = datetime.datetime.now(ZoneInfo("Europe/Berlin")) + datetime.timedelta(hours=float(sys.argv[2]))
    end = start + datetime.timedelta(hours=1)
    con.execute(
        "INSERT INTO shows (title, date, start_time, end_time) VALUES ('Test show', ?, ?, ?)",
        (start.date().isoformat(), start.strftime("%H:%M"), end.strftime("%H:%M")),
    )
con.commit()
PY
}

# Fresh box: sha-0000000 runs, nothing on air, no show soon, the image has DIGEST.
reset() {
  rm -rf "$TMP/fake" "$APP/data"/unheard.db.pre-* "$APP/data"/image-before-*
  mkdir -p "$TMP/fake"
  echo "$IMAGE:sha-0000000" >"$TMP/fake/running"
  : >"$TMP/fake/calls"
  make_db none
}

deploy() { # env assignments for the script, e.g. TAG=... DIGEST=...
  env PATH="$BIN:$PATH" FAKE="$TMP/fake" FAKE_APP="$APP" FAKE_STATUS="${FAKE_STATUS:-{\"active\":false}}" \
    FAKE_REPO_DIGEST="${FAKE_REPO_DIGEST:-$IMAGE@$DIGEST}" APP_DIR="$APP" DEPLOY_NO_LOCK=1 \
    PUBLIC=https://prod.test "$@" bash "$HERE/deploy-production.sh"
}

pass() { echo "ok   $1"; }
fail() {
  echo "FAIL $1"
  failures=$((failures + 1))
}
called() { grep -q "$1" "$TMP/fake/calls"; }
backups() { ls "$APP/data" | grep -c '^unheard.db.pre-' || true; }

check() { # name want-exit want-text [env...]
  local name="$1" want="$2" text="$3" out code=0
  shift 3
  out="$(deploy "$@" 2>&1)" || code=$?
  if [[ "$code" == "$want" && "$out" == *"$text"* ]]; then pass "$name"; else fail "$name: exit $code, $out"; fi
}

reset
check "a bad tag is refused" 1 "TAG must look like sha-1234567" TAG=latest DIGEST="$DIGEST"
check "a missing digest is refused" 1 "DIGEST must be sha256" TAG=sha-1234567 DIGEST=

reset
echo "$IMAGE:sha-1234567" >"$TMP/fake/running"
check "the running tag again does nothing" 0 "Nothing to do: no backup, no restart" TAG=sha-1234567 DIGEST="$DIGEST"
if called pull; then fail "no pull when the tag already runs"; else pass "no pull when the tag already runs"; fi

reset
FAKE_STATUS='{"active":true}' check "a show on air stops the deploy" 1 "FAIL  stream status" TAG=sha-1234567 DIGEST="$DIGEST"
if called pull || [[ "$(backups)" != 0 ]]; then fail "nothing pulled or backed up while on air"; else pass "nothing pulled or backed up while on air"; fi

reset
make_db 1
check "a show due in 1 hour stops the deploy" 1 "a show is on or due within 2 hours" TAG=sha-1234567 DIGEST="$DIGEST"
reset
make_db -0.5
check "a show that started 30 minutes ago stops the deploy" 1 "a show is on or due within 2 hours" TAG=sha-1234567 DIGEST="$DIGEST"
reset
make_db 3
check "a show in 3 hours does not stop the deploy" 0 "OK    no show now or in the next 2 hours" TAG=sha-1234567 DIGEST="$DIGEST" DRY_RUN=1

reset
make_db 1
check "a dry run reports a blocked window" 0 "DRY   a real deploy would stop here" TAG=sha-1234567 DIGEST="$DIGEST" DRY_RUN=1

reset
FAKE_REPO_DIGEST="$IMAGE@sha256:$(printf '%064d' 9)" check "a different image digest stops the deploy" 1 "is not $DIGEST; nothing was changed" \
  TAG=sha-1234567 DIGEST="$DIGEST"
if called "up -d" || [[ "$(backups)" != 0 ]]; then fail "nothing switched or backed up on a digest mismatch"; else pass "nothing switched or backed up on a digest mismatch"; fi

reset
check "a dry run changes nothing" 0 "DRY   done" TAG=sha-1234567 DIGEST="$DIGEST" DRY_RUN=1
if called "up -d" || [[ "$(backups)" != 0 || "$(cat "$TMP/fake/running")" != "$IMAGE:sha-0000000" ]]; then
  fail "dry run: no switch, no backup"
else
  pass "dry run: no switch, no backup"
fi

reset
check "a real deploy switches and reports healthy" 0 "OK    unheard-api healthy on $IMAGE:sha-1234567" TAG=sha-1234567 DIGEST="$DIGEST"
backup="$(ls "$APP/data" | grep '^unheard.db.pre-sha-1234567-' || true)"
if [[ -n "$backup" ]] && python3 -c "import sqlite3,sys; assert sqlite3.connect(sys.argv[1]).execute('SELECT username FROM users').fetchone()[0] == 'kept'" "$APP/data/$backup"; then
  pass "the DB backup is a working copy"
else
  fail "the DB backup is a working copy: '$backup'"
fi
if grep -qx "$IMAGE:sha-0000000" "$APP/data"/image-before-sha-1234567-*; then pass "the previous image is noted for rollback"; else fail "the previous image is noted for rollback"; fi
check "a second run after a deploy does nothing" 0 "Nothing to do" TAG=sha-1234567 DIGEST="$DIGEST"

if ((failures > 0)); then
  echo "$failures deploy test(s) failed"
  exit 1
fi
echo "all deploy tests passed"
