#!/usr/bin/env bash
# Resolve Docker Compose and exec it with the given args.
#
# The Hetzner box may have EITHER the v2 plugin (`docker compose`) OR only the
# standalone `docker-compose` binary (deploy_hetzner.sh installs whichever it
# can; this box has only the standalone v2.24.6 binary). monitoring.service
# calls this wrapper instead of hardcoding one form, so the unit works on either.
set -euo pipefail

if docker compose version >/dev/null 2>&1; then
  exec docker compose "$@"
elif command -v docker-compose >/dev/null 2>&1; then
  exec docker-compose "$@"
else
  echo "mon-compose: no Docker Compose found (neither 'docker compose' plugin nor 'docker-compose')" >&2
  exit 1
fi
