#!/usr/bin/env bash
# Sets the deployed::* label on the issues behind a git range (docs/deployment.md).
# CI runs it; DRY_RUN=1 only prints what it would change.
#
#   scripts/deployed-labels.sh staging <before>..<after>   after a merge into staging
#   scripts/deployed-labels.sh production <rN>..<sha>      after a production deploy
#
# For each commit in the range it reads the merged PR (REST commits/<sha>/pulls).
# An issue counts when a line of the PR body starts with Refs:, Closes:, Fixes:,
# Issue: or Ticket: and has the full issue URL. The issue gets deployed::<stage>
# and loses the other deployed::* label, so it has one at a time.
# Needs REPO (owner/name) and GH_TOKEN. Uses only the REST API.
set -euo pipefail

usage() { echo "usage: $0 staging|production <git range>" >&2; exit 2; }
[[ $# -eq 2 && -n "$2" ]] || usage
case "$1" in
  staging) other=production ;;
  production) other=staging ;;
  *) usage ;;
esac
: "${REPO:?set REPO to owner/name}"
add="deployed::$1"
drop="deployed::$other"

commits="$(git rev-list "$2")" # a bad range stops here, not in the loop
issues=""
for sha in $commits; do
  issues+="$(gh api "repos/$REPO/commits/$sha/pulls" | jq -r --arg repo "$REPO" '
    .[] | select(.merged_at != null) | (.body // "") | split("\n")[]
    | select(test("^\\s*(Refs|Closes|Fixes|Issue|Ticket):"; "i"))
    | scan("https://github\\.com/" + ($repo | gsub("\\."; "\\.")) + "/issues/([0-9]+)")[0]')"$'\n'
done
issues="$(sort -nu <<<"$issues" | sed '/^$/d')"
[[ -n "$issues" ]] || { echo "No linked issues in $2"; exit 0; }

failed=0
fail() { echo "::error::$*"; failed=1; }
for n in $issues; do
  url="https://github.com/$REPO/issues/$n"
  # First line: pr or issue. Then one label per line.
  info="$(gh api "repos/$REPO/issues/$n" --jq '(if .pull_request then "pr" else "issue" end), .labels[].name')" ||
    { fail "$url: cannot read it"; continue; }
  [[ "${info%%$'\n'*}" == issue ]] || { echo "$url is a pull request: skipped"; continue; }
  need_add=yes
  need_drop=no
  if grep -qxF "$add" <<<"$info"; then need_add=no; fi
  if grep -qxF "$drop" <<<"$info"; then need_drop=yes; fi
  if [[ $need_add == no && $need_drop == no ]]; then echo "$url: has $add"; continue; fi
  echo "$url: set $add$([[ $need_drop == no ]] || echo ", remove $drop")"
  [[ "${DRY_RUN:-0}" != 1 ]] || continue
  # Add first, so the issue is never without a deployed::* label.
  if [[ $need_add == yes ]] && ! gh api -X POST "repos/$REPO/issues/$n/labels" -f "labels[]=$add" >/dev/null; then
    fail "$url: cannot add $add"
    continue
  fi
  if [[ $need_drop == yes ]] && ! gh api -X DELETE "repos/$REPO/issues/$n/labels/${drop//:/%3A}" >/dev/null; then
    fail "$url: cannot remove $drop"
  fi
done
exit "$failed"
