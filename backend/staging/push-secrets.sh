#!/usr/bin/env bash
# Run on your Mac, not on a server. Puts the staging secrets from Bitwarden on
# the staging box:
#   1. creates every missing generated "Moafunk STAGING_*" item in Bitwarden,
#   2. reads all values by exact name,
#   3. pipes NAME=value lines over ssh into `staging.sh install-env`.
# No value is printed, written to a file or passed as a program argument.
#
#   export BW_SESSION="$(bw unlock --raw)"
#   BW_FOLDER="<folder>" backend/staging/push-secrets.sh root@<staging-ip> -i ~/.ssh/<key>
#
# The two R2 items (STAGING_R2_ACCESS_KEY_ID, STAGING_R2_SECRET_ACCESS_KEY) are
# not generated: Cloudflare shows them once, so add them in the Bitwarden app
# (password field) before running this.
set -euo pipefail

PREFIX="Moafunk"
ADMIN_USER="staging-admin"
GENERATED="STAGING_SECRET_KEY STAGING_SUPERADMIN_PASSWORD STAGING_HARBOR_TEST_PASSWORD
STAGING_HARBOR_LIVE_PASSWORD STAGING_ICECAST_SOURCE_PASSWORD STAGING_ICECAST_ADMIN_PASSWORD
STAGING_ICECAST_RELAY_PASSWORD"
MANUAL="STAGING_R2_ACCESS_KEY_ID STAGING_R2_SECRET_ACCESS_KEY"

die() {
  echo "push-secrets: $*" >&2
  exit 1
}

[[ $# -ge 1 ]] || die "usage: BW_FOLDER=<folder> push-secrets.sh <user@staging-host> [ssh options]"
target="$1"
shift
[[ -n "${BW_FOLDER:-}" ]] || die "set BW_FOLDER to the Bitwarden folder for these items"
[[ "$(bw status | jq -r .status)" == unlocked ]] || die 'run: export BW_SESSION="$(bw unlock --raw)"'

bw sync >/dev/null
folder="$(bw list folders | jq -r --arg n "$BW_FOLDER" '.[] | select(.name==$n) | .id')"
[[ -n "$folder" ]] || die "Bitwarden folder '$BW_FOLDER' not found; folders: $(bw list folders | jq -r '[.[].name] | join(", ")')"

# Number of items with exactly this name.
items_named() {
  bw list items --search "$1" | jq --arg n "$1" 'map(select(.name==$n)) | length'
}

# Password of the one item with exactly this name.
bwpw() {
  bw list items --search "$1" |
    jq -er --arg n "$1" 'map(select(.name==$n)) | if length==1 then .[0].login.password else error("\(length) items named \($n)") end'
}

template="$(bw get template item)"
for name in $GENERATED; do
  item="$PREFIX $name"
  case "$(items_named "$item")" in
    1) continue ;;
    0) ;;
    *) die "more than one item named '$item'; delete the extra ones" ;;
  esac
  user=""
  [[ "$name" != STAGING_SUPERADMIN_PASSWORD ]] || user="$ADMIN_USER"
  # Letters and digits only: the harbor passwords sit unescaped in a URL, and
  # all values sit unquoted in .env. The superadmin password is typed by hand.
  length=48
  [[ "$name" != STAGING_SUPERADMIN_PASSWORD ]] || length=24
  bw generate -uln --length "$length" |
    jq -Rs --arg n "$item" --arg f "$folder" --arg u "$user" --argjson t "$template" \
      '$t | .type=1 | .name=$n | .folderId=$f | .notes="live.moafunk.de staging (backend/staging)" | .login={username:$u, password:rtrimstr("\n")}' |
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
unset value

printf '%s' "$payload" | ssh "$@" "$target" /opt/unheard-staging/staging.sh install-env
unset payload
echo "secrets installed on $target"
