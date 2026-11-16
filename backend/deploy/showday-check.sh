#!/usr/bin/env bash
# Show-day check. Runs ON the production box from a systemd timer every 15
# minutes (install: docs/deployment.md). The day before a show (from 18:00
# Berlin) and in the hour before it, it checks that the box is ready for the
# show and posts the result to the MoafunkBot topic of the Telegram group.
# Each show gets each post once. It changes nothing on the box.
#
#   showday-check.sh                    what the timer runs
#   SHOWDAY_FORCE=1 showday-check.sh    check and post now for every show of today and tomorrow
#
# Exits 1 when a check failed or the post did not go out, so the timer's unit
# shows the failure (systemctl status showday-check).
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/unheard-backend}"
STATE_DIR="${STATE_DIR:-/var/lib/showday-check}"
PUBLIC="${PUBLIC:-https://admin.live.moafunk.de}"
ICECAST="${ICECAST:-http://127.0.0.1:8010}"
LIQUIDSOAP="${LIQUIDSOAP:-moafunk-liquidsoap}"
BACKUPS="${BACKUPS:-r2-backup:unheard-backups/db/}"
MIN_FREE_GB=5
CERT_DAYS=14
BACKUP_HOURS=30 # daily backup; the name's time may be local or UTC

# A value from the backend's .env, without quotes. The file is read, never run.
env_value() {
  awk -v key="$1" 'index($0, key "=") == 1 { v = substr($0, length(key) + 2); gsub(/^["\047]|["\047]$/, "", v); print v; exit }' \
    "$APP_DIR/.env" 2>/dev/null || true
}

# Posts due now, one per line: "<state key><TAB><headline>".
due_posts() {
  python3 - "$APP_DIR/data/unheard.db" "$STATE_DIR" "${SHOWDAY_FORCE:-0}" "${SHOWDAY_NOW:-}" <<'PY'
import datetime, os, sqlite3, sys
from zoneinfo import ZoneInfo

db, state, force, fixed_now = sys.argv[1], sys.argv[2], sys.argv[3] == "1", sys.argv[4]
berlin = ZoneInfo("Europe/Berlin")
now = datetime.datetime.fromisoformat(fixed_now) if fixed_now else datetime.datetime.now(berlin)
now = now.astimezone(berlin)
today, tomorrow = now.date(), now.date() + datetime.timedelta(days=1)
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
rows = con.execute(
    "SELECT id, title, date, start_time, prerecorded_key, prerecorded_confirmed_at FROM shows"
    " WHERE date IN (?, ?) AND start_time IS NOT NULL ORDER BY date, start_time",
    (today.isoformat(), tomorrow.isoformat()),
)
for sid, title, date, start, set_key, confirmed in rows:
    begin = datetime.datetime.fromisoformat(f"{date}T{start}").replace(tzinfo=berlin)
    if begin <= now:
        continue
    if begin.date() == tomorrow and (force or now.hour >= 18):
        kind, when = "day", "Tomorrow"
    elif begin - now <= datetime.timedelta(hours=1) or force:
        kind, when = "hour", "Soon" if force else "In 1 hour"
    else:
        continue
    key = f"{sid}-{kind}"
    if not force and os.path.exists(os.path.join(state, key)):
        continue
    if confirmed:
        mode = "pre-recorded, set confirmed"
    elif set_key:
        mode = "pre-recorded, set NOT confirmed"
    else:
        mode = "live"
    print(f"{key}\t{when}: {title} · {date} {start} · {mode}")
PY
}

failed=0
ok() { echo "✅ $*"; }
bad() { echo "❌ $*"; }

checks() {
  if curl -fsS -m 10 http://127.0.0.1:8000/health 2>/dev/null | grep -q '"healthy"'; then
    ok "API healthy"
  else
    bad "API not healthy"
  fi
  if curl -fsS -m 10 "$PUBLIC/health" >/dev/null 2>&1; then ok "$PUBLIC answers"; else bad "$PUBLIC does not answer"; fi
  local host="${PUBLIC#https://}"
  if echo | openssl s_client -servername "$host" -connect "$host:443" 2>/dev/null |
    openssl x509 -noout -checkend $((CERT_DAYS * 86400)) >/dev/null 2>&1; then
    ok "TLS certificate valid for $CERT_DAYS more days"
  else
    bad "TLS certificate expires within $CERT_DAYS days (or cannot be read)"
  fi
  local mounts
  mounts="$(curl -fsS -m 10 "$ICECAST/status-json.xsl" 2>/dev/null || true)"
  if [[ "$mounts" == *"/live.mp3"* && "$mounts" == *"/test.mp3"* ]]; then
    ok "Icecast has /live.mp3 and /test.mp3"
  else
    bad "Icecast mounts missing (/live.mp3, /test.mp3)"
  fi
  if [[ "$(docker inspect -f '{{.State.Running}}' "$LIQUIDSOAP" 2>/dev/null)" == true ]]; then
    ok "Liquidsoap runs"
  else
    bad "Liquidsoap ($LIQUIDSOAP) is not running"
  fi
  local free_gb
  free_gb="$(df -Pk "$APP_DIR" | awk 'NR == 2 { print int($4 / 1048576) }')"
  if ((free_gb >= MIN_FREE_GB)); then ok "${free_gb} GB free"; else bad "only ${free_gb} GB free (want $MIN_FREE_GB)"; fi
  local newest age
  newest="$(rclone lsf "$BACKUPS" --files-only 2>/dev/null | sort | tail -1)"
  age="$(python3 - "$newest" "${SHOWDAY_NOW:-}" <<'PY'
import datetime, re, sys
m = re.search(r"(\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d)", sys.argv[1])
if not m:
    sys.exit(0)
taken = datetime.datetime.strptime(m.group(1), "%Y-%m-%d_%H-%M-%S").replace(tzinfo=datetime.timezone.utc)
now = datetime.datetime.fromisoformat(sys.argv[2]) if sys.argv[2] else datetime.datetime.now(datetime.timezone.utc)
print(int((now - taken).total_seconds() // 3600))
PY
)"
  if [[ -n "$age" ]] && ((age <= BACKUP_HOURS)); then
    ok "DB backup ${age} h old"
  else
    bad "no DB backup from the last $BACKUP_HOURS hours (newest: ${newest:-none})"
  fi
}

# Post TEXT to the MoafunkBot topic. The token goes to curl on stdin, not on
# its command line.
post() {
  local token chat topic
  token="$(env_value TELEGRAM_BOT_TOKEN)"
  chat="$(env_value TELEGRAM_ADMIN_CHAT_ID)"
  topic="$(env_value TELEGRAM_TOPIC_ID)"
  # The backend forces the MoafunkBot topic the same way (config.rs).
  [[ "$topic" =~ ^[1-9][0-9]*$ ]] || topic=26
  if [[ -z "$token" || -z "$chat" ]]; then
    echo "FAIL  TELEGRAM_BOT_TOKEN or TELEGRAM_ADMIN_CHAT_ID missing in $APP_DIR/.env"
    return 1
  fi
  printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$token" |
    curl -fsS -m 20 -o /dev/null -K - \
      --data-urlencode "chat_id=$chat" \
      --data-urlencode "message_thread_id=$topic" \
      --data-urlencode "text=$1"
}

mkdir -p "$STATE_DIR"
find "$STATE_DIR" -type f -mtime +3 -delete
posts="$(due_posts)"
[[ -n "$posts" ]] || exit 0
report="$(checks)"
[[ "$report" != *"❌"* ]] || failed=1
verdict="all checks passed"
((failed == 0)) || verdict="SOMETHING IS WRONG, check before the show"
while IFS=$'\t' read -r key headline; do
  text="Show-day check · $headline"$'\n'"$verdict"$'\n\n'"$report"
  echo "$text"
  if post "$text"; then
    touch "$STATE_DIR/$key"
  else
    failed=1
  fi
done <<<"$posts"
exit "$failed"
