#!/usr/bin/env bash
# Tests for gitnexus-consult-first.sh. Run: .claude/hooks/scripts/gitnexus-consult-first.test.sh
set -euo pipefail

HOOK="$(cd "$(dirname "$0")" && pwd)/gitnexus-consult-first.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
TMP="$(cd "$TMP" && pwd -P)" # git reports the real path (/var is /private/var on macOS)
fail=0

# A fake indexed checkout whose folder name is not its registry name.
REPO="$TMP/live.moafunk.de-claude-runner"
mkdir -p "$REPO/.gitnexus" "$REPO/sub"
git -C "$REPO" init -q
ln -s "$REPO" "$TMP/link"
PLAIN="$TMP/plain" # a checkout without an index
mkdir -p "$PLAIN"
git -C "$PLAIN" init -q

REG="$TMP/registry.json"
jq -n --arg p "$REPO" '[{name: "other", path: "/elsewhere"}, {name: "live.moafunk.de", path: $p}]' >"$REG"
jq -n '[{name: "other", path: "/elsewhere"}]' >"$TMP/other.json"
echo '{not json' >"$TMP/broken.json"

REG_URI='gitnexus://repo/live.moafunk.de/context'
DIR_URI='gitnexus://repo/live.moafunk.de-claude-runner/context'

run() { # cwd registry [prompt]
  jq -cn --arg c "$1" --arg p "${3:-how does the stream handler work}" '{prompt: $p, cwd: $c}' |
    GITNEXUS_REGISTRY="$2" "$HOOK"
}

result() { # description ok
  if [[ "$2" == 1 ]]; then echo "ok   $1"; else echo "FAIL $1"; fail=1; fi
}
has() { # description output needle
  local ok=0
  grep -qF -- "$3" <<<"$2" && ok=1
  result "$1" "$ok"
}
lacks() { # description output needle
  local ok=1
  grep -qF -- "$3" <<<"$2" && ok=0
  result "$1" "$ok"
}

out=$(run "$REPO" "$REG")
has "registry name in the context resource" "$out" "$REG_URI"
has "registry name in the intro line" "$out" 'The repo "live.moafunk.de"'
lacks "no folder name" "$out" 'live.moafunk.de-claude-runner'
has "stale advice uses --skip-agents-md" "$out" '`npx gitnexus analyze --skip-agents-md`'
bare=$(printf '%s\n' "$out" | grep -oE 'gitnexus analyze[^`]*' | grep -vF -- '--skip-agents-md' || true)
result "no bare analyze advice" "$([[ -z "$bare" ]] && echo 1 || echo 0)"

has "subdirectory uses the repo's registry name" "$(run "$REPO/sub" "$REG")" "$REG_URI"
has "symlinked cwd matches the real registry path" "$(run "$TMP/link" "$REG")" "$REG_URI"
has "unregistered path falls back to the folder name" "$(run "$REPO" "$TMP/other.json")" "$DIR_URI"
has "missing registry falls back to the folder name" "$(run "$REPO" "$TMP/missing.json")" "$DIR_URI"
has "broken registry falls back to the folder name" "$(run "$REPO" "$TMP/broken.json")" "$DIR_URI"

result "no index, no directive" "$([[ -z "$(run "$PLAIN" "$REG")" ]] && echo 1 || echo 0)"
result "short ack, no directive" "$([[ -z "$(run "$REPO" "$REG" "ok thanks")" ]] && echo 1 || echo 0)"

exit "$fail"
