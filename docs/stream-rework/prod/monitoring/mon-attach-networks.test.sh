#!/usr/bin/env bash
# Tests for mon-attach-networks.sh, with a fake `docker` on PATH.
# Run: docs/stream-rework/prod/monitoring/mon-attach-networks.test.sh
set -euo pipefail

SCRIPT="$(cd "$(dirname "$0")" && pwd)/mon-attach-networks.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
fail=0

# Fake docker. State: $TMP/nets (attached nets, one per line), $TMP/calls (connect log).
mkdir -p "$TMP/bin"
cat >"$TMP/bin/docker" <<'FAKE'
#!/usr/bin/env bash
set -euo pipefail
case "$1" in
  ps) [[ -n "${FAKE_CID:-}" ]] && echo "$FAKE_CID"; exit 0 ;;
  inspect) tr '\n' ' ' <"$FAKE_DIR/nets"; echo; exit 0 ;;
  network)
    echo "$*" >>"$FAKE_DIR/calls"
    [[ "${FAKE_CONNECT_FAILS:-0}" == 1 ]] && { echo "connect failed" >&2; exit 1; }
    net="${*: -2:1}"
    echo "$net" >>"$FAKE_DIR/nets"
    exit 0 ;;
esac
echo "unexpected docker call: $*" >&2
exit 2
FAKE
chmod +x "$TMP/bin/docker"

run() { # attached-nets... ; sets $out, $code
  printf '%s\n' "$@" | grep -v '^$' >"$TMP/nets" || true
  : >"$TMP/calls"
  code=0
  out=$(PATH="$TMP/bin:$PATH" FAKE_DIR="$TMP" FAKE_CID="${CID-abc123}" \
    "$SCRIPT" 2>&1) || code=$?
}
result() { # description ok
  if [[ "$2" == 1 ]]; then echo "ok   $1"; else echo "FAIL $1"; fail=1; fi
}
calls() { cat "$TMP/calls"; }
is() { result "$1" "$([[ "$2" == "$3" ]] && echo 1 || echo 0)"; }

# 2026-09-25 case: compose kept only the backend net.
run unheard-backend_default
is "missing default net: exit 0" "$code" 0
is "missing default net: connects it with the alias" "$(calls)" \
  "network connect --alias prometheus moafunk-monitoring_default abc123"

# June case: compose kept only the default net.
run moafunk-monitoring_default
is "missing backend net: exit 0" "$code" 0
is "missing backend net: connects it" "$(calls)" \
  "network connect --alias prometheus unheard-backend_default abc123"

run moafunk-monitoring_default unheard-backend_default
is "both attached: exit 0" "$code" 0
is "both attached: no connect" "$(calls)" ""

run
is "no nets: connects both" "$(calls | wc -l | tr -d ' ')" 2

BACKEND_NETWORK=other_default run moafunk-monitoring_default
is "BACKEND_NETWORK override is used" "$(calls)" \
  "network connect --alias prometheus other_default abc123"

# A net whose name only contains the wanted one must not count as attached.
run moafunk-monitoring_default_old unheard-backend_default
is "prefix name is not a match" "$(calls)" \
  "network connect --alias prometheus moafunk-monitoring_default abc123"

FAKE_CONNECT_FAILS=1 run unheard-backend_default
is "failed connect: exit non-zero" "$([[ "$code" != 0 ]] && echo y)" y

CID="" run unheard-backend_default
is "no prometheus container: exit non-zero" "$([[ "$code" != 0 ]] && echo y)" y
is "no prometheus container: says so" "$(grep -c 'no running prometheus' <<<"$out")" 1

exit "$fail"
