#!/usr/bin/env bash
# Run on your Mac, not on a server. Puts the staging secrets from Bitwarden on
# the staging box:
#   1. creates every missing generated "Moafunk STAGING_*" item in Bitwarden,
#   2. reads all values by exact name,
#   3. reads the SoundCloud app and the e2e login from Bitwarden Secrets Manager,
#   4. pipes NAME=value lines over ssh into `staging.sh install-env`.
# No value is printed, written to a file or passed as a program argument.
#
#   export BW_SESSION="$(bw unlock --raw)"
#   export BWS_ACCESS_TOKEN=<Secrets Manager token>
#   BW_FOLDER="<folder>" backend/staging/push-secrets.sh root@<staging-ip> -i ~/.ssh/<key>
#
# The two R2 items (STAGING_R2_ACCESS_KEY_ID, STAGING_R2_SECRET_ACCESS_KEY) are
# not generated: Cloudflare shows them once, so add them in the Bitwarden app
# (password field) before running this.
set -euo pipefail

PREFIX="Moafunk"
ADMIN_USER="staging-admin"
HOST_USER="staging-host"
GENERATED="STAGING_SECRET_KEY STAGING_SUPERADMIN_PASSWORD STAGING_HOST_PASSWORD STAGING_HARBOR_TEST_PASSWORD
STAGING_HARBOR_LIVE_PASSWORD STAGING_ICECAST_SOURCE_PASSWORD STAGING_ICECAST_ADMIN_PASSWORD
STAGING_ICECAST_RELAY_PASSWORD"
MANUAL="STAGING_R2_ACCESS_KEY_ID STAGING_R2_SECRET_ACCESS_KEY"
# Secrets Manager key=staging name. Staging shares the station's SoundCloud app
# (SoundCloud allows one app per account), so its values are not copied. The
# e2e login lives only in Secrets Manager: CI (e2e-staging.yml) reads it there.
SM_KEYS="SOUNDCLOUD_CLIENT_ID=STAGING_SOUNDCLOUD_CLIENT_ID SOUNDCLOUD_CLIENT_SECRET=STAGING_SOUNDCLOUD_CLIENT_SECRET
STAGING_E2E_HOST_PASSWORD=STAGING_E2E_HOST_PASSWORD"

die() {
  echo "push-secrets: $*" >&2
  exit 1
}

[[ $# -ge 1 ]] || die "usage: BW_FOLDER=<folder> push-secrets.sh <user@staging-host> [ssh options]"
target="$1"
shift
[[ -n "${BW_FOLDER:-}" ]] || die "set BW_FOLDER to the Bitwarden folder for these items"
[[ "$(bw status | jq -r .status)" == unlocked ]] || die 'run: export BW_SESSION="$(bw unlock --raw)"'
command -v bws >/dev/null || die "bws (Bitwarden Secrets Manager CLI) is not installed"
[[ -n "${BWS_ACCESS_TOKEN:-}" ]] || die "run: export BWS_ACCESS_TOKEN=<Secrets Manager token>"
# bws reads both from the environment only. US cloud unless set (as dev-secrets.sh).
export BWS_ACCESS_TOKEN
export BWS_SERVER_URL="${BWS_SERVER_URL:-https://vault.bitwarden.com}"
sm_json="$(bws secret list --output json)" || die "'bws secret list' failed; check BWS_ACCESS_TOKEN and BWS_SERVER_URL"

bw sync >/dev/null
folder="$(bw list folders | jq -r --arg n "$BW_FOLDER" '.[] | select(.name==$n) | .id')"
[[ -n "$folder" ]] || die "Bitwarden folder '$BW_FOLDER' not found; folders: $(bw list folders | jq -r '[.[].name] | join(", ")')"

# Number of items with exactly this name.
items_named() {
  bw list items --search "$1" | jq --arg n "$1" 'map(select(.name==$n)) | length'
}

# Password of the one item with exactly this name; fails unless it is a non-empty string.
bwpw() {
  bw list items --search "$1" |
    jq -er --arg n "$1" 'map(select(.name==$n))
      | if length != 1 then error("\(length) items named \($n)")
        elif (.[0].login.password | type) != "string" or .[0].login.password == "" then error("\($n) has no password")
        else .[0].login.password end'
}

# Value of the one Secrets Manager secret with this key; fails unless it is non-empty.
smval() {
  printf '%s' "$sm_json" |
    jq -er --arg k "$1" 'map(select(.key==$k))
      | if length != 1 then error("\(length) Secrets Manager secrets named \($k)")
        elif (.[0].value | type) != "string" or .[0].value == "" then error("Secrets Manager secret \($k) is empty")
        else .[0].value end'
}

template="$(bw get template item)"
for name in $GENERATED; do
  item="$PREFIX $name"
  case "$(items_named "$item")" in
    1) continue ;;
    0) ;;
    *) die "more than one item named '$item'; delete the extra ones" ;;
  esac
  # Letters and digits only: the harbor passwords sit unescaped in a URL, and
  # all values sit unquoted in .env. Login passwords are typed by hand.
  user=""
  length=48
  case "$name" in
    STAGING_SUPERADMIN_PASSWORD) user="$ADMIN_USER" length=24 ;;
    STAGING_HOST_PASSWORD) user="$HOST_USER" length=24 ;;
  esac
  bw generate -uln --length "$length" |
    jq -Rs --arg n "$item" --arg f "$folder" --arg u "$user" --argjson t "$template" \
      'rtrimstr("\n") as $p | $t | .type=1 | .name=$n | .folderId=$f | .notes="live.moafunk.de staging (backend/staging)" | .login={username:$u, password:$p}' |
    bw encode | bw create item >/dev/null
  echo "created Bitwarden item '$item'"
done
bw sync >/dev/null

for name in $MANUAL; do
  [[ "$(items_named "$PREFIX $name")" == 1 ]] ||
    die "add exactly one Bitwarden item '$PREFIX $name' (value in the password field) first"
done

payload=""
for name in $GENERATED $MANUAL; do
  value="$(bwpw "$PREFIX $name")" || die "cannot read '$PREFIX $name'"
  payload="$payload$name=$value"$'\n'
done
for pair in $SM_KEYS; do
  value="$(smval "${pair%%=*}")" || die "cannot read Secrets Manager secret '${pair%%=*}'"
  payload="$payload${pair#*=}=$value"$'\n'
done
unset value sm_json

printf '%s' "$payload" | ssh "$@" "$target" /opt/unheard-staging/staging.sh install-env
unset payload
echo "secrets installed on $target"
