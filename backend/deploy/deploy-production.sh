#!/usr/bin/env bash
# Deploy one API image to production. Runs ON the production box: the
# "Deploy production" workflow pipes it in over ssh, and it also runs by hand:
#   ssh root@<prod> 'TAG=sha-1234567 DIGEST=sha256:<64 hex> DRY_RUN=1 bash -s' < backend/deploy/deploy-production.sh
#
# Refuses when a show is on air or due within 2 hours, or when the pulled
# image is not DIGEST. Then backs up the DB (SQLite backup API), starts the
# image, waits for health and prints the rollback. DRY_RUN=1 pulls and checks
# the image but backs up and switches nothing. Never edits .env: the tag goes
# to this one compose call as UNHEARD_TAG.
set -euo pipefail

TAG="${TAG:-}"
DIGEST="${DIGEST:-}"
DRY_RUN="${DRY_RUN:-0}"
APP_DIR="${APP_DIR:-/opt/unheard-backend}"
SERVICE="${SERVICE:-unheard-api}"
IMAGE=ghcr.io/phaabe/live.moafunk.de-backend
PUBLIC="${PUBLIC:-https://admin.live.moafunk.de}"
SHOW_GUARD_HOURS=2

fail() {
  echo "FAIL  $*"
  exit 1
}
dry() { [[ "$DRY_RUN" == 1 ]]; }
run() {
  if dry; then echo "DRY   would run: $*"; else echo "RUN   $*"; "$@"; fi
}

[[ "$TAG" =~ ^sha-[0-9a-f]{7}$ ]] || fail "TAG must look like sha-1234567, got '$TAG'"
[[ "$DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || fail "DIGEST must be sha256:<64 hex>, got '$DIGEST'"
cd "$APP_DIR"

# One deploy at a time. DEPLOY_NO_LOCK=1 is for the tests only.
if [[ "${DEPLOY_NO_LOCK:-0}" != 1 ]]; then
  exec 9>"$APP_DIR/.deploy.lock"
  flock -n 9 || fail "another deploy is running"
fi

stamp="$(date -u +%Y%m%d_%H%M%S)"
echo "Production deploy · Berlin $(TZ=Europe/Berlin date '+%a %F %H:%M') · $TAG$(dry && echo ' · DRY RUN')"

# ── Compose setup, read from the running container ───────────────────────────
label() { docker inspect "$SERVICE" --format "{{ index .Config.Labels \"$1\" }}"; }
before="$(docker inspect "$SERVICE" --format '{{.Config.Image}}')"
workdir="$(label com.docker.compose.project.working_dir)"
files="$(label com.docker.compose.project.config_files)"
project="$(label com.docker.compose.project)"
if docker compose version >/dev/null 2>&1; then
  compose=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  compose=(docker-compose)
else
  fail "neither 'docker compose' nor 'docker-compose' found"
fi
cfiles=()
IFS=',' read -r -a parts <<<"$files"
for f in ${parts[@]+"${parts[@]}"}; do cfiles+=(-f "$f"); done
echo "OK    now running $before"
if [[ "$before" == *":$TAG" ]]; then
  # A second run must not write a backup: it would be taken after the
  # migration and look like a rollback point.
  echo "OK    $TAG is already running. Nothing to do: no backup, no restart."
  exit 0
fi
echo "OK    compose: ${compose[*]} · project $project · dir $workdir · files $files"
[[ "$workdir" == "$APP_DIR" ]] || echo "WARN  compose dir $workdir is not $APP_DIR"

# ── Show-free window ─────────────────────────────────────────────────────────
blocked=0
status="$(curl -fsS -m 5 http://127.0.0.1:8000/api/stream/status || echo unreachable)"
case "$status" in
  *'"active":false'*) echo "OK    nothing on air" ;;
  *) echo "FAIL  stream status: $status"; blocked=1 ;;
esac
if ! SHOW_GUARD_HOURS="$SHOW_GUARD_HOURS" python3 - <<'PY'; then blocked=1; fi
import datetime, os, sqlite3, sys
from zoneinfo import ZoneInfo

hours = int(os.environ["SHOW_GUARD_HOURS"])
berlin = ZoneInfo("Europe/Berlin")
now = datetime.datetime.now(berlin)
until = now + datetime.timedelta(hours=hours)
db = sqlite3.connect("file:data/unheard.db?mode=ro", uri=True)
busy = []
days = [(now.date() + datetime.timedelta(days=d)).isoformat() for d in (-1, 0, 1)]
for sid, title, date, start, end in db.execute(
    "SELECT id, title, date, start_time, end_time FROM shows WHERE date IN (?, ?, ?)", days
):
    if not start:
        continue
    begin = datetime.datetime.fromisoformat(f"{date}T{start}").replace(tzinfo=berlin)
    finish = begin + datetime.timedelta(hours=2)
    if end:
        finish = datetime.datetime.fromisoformat(f"{date}T{end}").replace(tzinfo=berlin)
        if finish <= begin:  # runs past midnight
            finish += datetime.timedelta(days=1)
    if begin < until and finish > now:
        busy.append(f"show {sid} '{title}' {date} {start}-{end or '?'}")
if busy:
    print(f"FAIL  a show is on or due within {hours} hours: " + "; ".join(busy))
    sys.exit(1)
print(f"OK    no show now or in the next {hours} hours")
PY
if ((blocked)); then
  dry && echo "DRY   a real deploy would stop here"
  dry || exit 1
fi

# ── Pull and check the exact image ───────────────────────────────────────────
echo "RUN   pull $IMAGE:$TAG"
UNHEARD_TAG="$TAG" "${compose[@]}" ${cfiles[@]+"${cfiles[@]}"} pull "$SERVICE"
docker image inspect --format '{{join .RepoDigests "\n"}}' "$IMAGE:$TAG" | grep -qxF "$IMAGE@$DIGEST" ||
  fail "$IMAGE:$TAG is not $DIGEST; nothing was changed"
echo "OK    $IMAGE:$TAG is $DIGEST"

# ── Back up, switch ──────────────────────────────────────────────────────────
backup="data/unheard.db.pre-$TAG-$stamp"
if dry; then
  echo "DRY   would back up data/unheard.db to $backup"
else
  python3 - "$backup" <<'PY'
import sqlite3, sys

src = sqlite3.connect("file:data/unheard.db?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[1])
src.backup(dst)
ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
dst.close()
src.close()
if ok != "ok":
    sys.exit(f"FAIL  backup integrity_check: {ok}")
PY
  echo "$before" >"data/image-before-$TAG-$stamp.txt"
  echo "OK    backup $backup ($(du -h "$backup" | cut -f1)), previous image noted in data/image-before-$TAG-$stamp.txt"
fi

run env UNHEARD_TAG="$TAG" "${compose[@]}" ${cfiles[@]+"${cfiles[@]}"} up -d "$SERVICE"

if dry; then
  echo "DRY   done. The image is pulled and checked; nothing else was changed."
  exit 0
fi

# ── Healthy? ─────────────────────────────────────────────────────────────────
health=""
for _ in $(seq 1 36); do
  health="$(docker inspect "$SERVICE" --format '{{.State.Health.Status}}')"
  [[ "$health" == healthy ]] && break
  sleep 5
done
now_image="$(docker inspect "$SERVICE" --format '{{.Config.Image}}')"
echo
docker logs --since 10m "$SERVICE" 2>&1 | sed $'s/\x1b\\[[0-9;]*m//g' |
  grep -E "migration|Starting server|ERROR|panicked" || echo "(no migration lines in the log)"
echo
failed=0
if [[ "$health" == healthy && "$now_image" == *":$TAG" ]]; then
  echo "OK    $SERVICE healthy on $now_image"
else
  echo "FAIL  $SERVICE is '$health' on $now_image"
  failed=1
fi
for path in /health /api/stream/status; do
  code="$(curl -s -m 10 -o /dev/null -w '%{http_code}' "$PUBLIC$path" || true)"
  echo "CHECK $PUBLIC$path → $code"
  [[ "$code" == 200 ]] || failed=1
done
echo
echo "Rollback, if needed: run the workflow again with rollback_tag=${before##*:}, or on the box:"
echo "  cd $APP_DIR && UNHEARD_TAG=${before##*:} ${compose[*]} ${cfiles[*]-} up -d $SERVICE"
echo "  DB (only if a migration broke it): stop the API, copy $backup over data/unheard.db, start the API."
exit "$failed"
