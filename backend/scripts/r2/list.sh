#!/bin/bash
# List objects in R2 bucket
# Usage: ./list.sh [prefix] [--json]
#
# Environment variables:
#   R2_BUCKET_NAME - Bucket name (default: from .env or 'unheard-artists-prod')
#   R2_ACCOUNT_ID  - Cloudflare account ID

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"

# Load .env if exists
if [[ -f "$BACKEND_DIR/.env" ]]; then
    set -a
    source "$BACKEND_DIR/.env"
    set +a
fi

BUCKET="${R2_BUCKET_NAME:-unheard-artists-prod}"
PREFIX="${1:-}"
OUTPUT_FORMAT="${2:-}"

echo "Listing objects in bucket: $BUCKET"
[[ -n "$PREFIX" ]] && echo "Prefix filter: $PREFIX"
echo "---"

if [[ "$OUTPUT_FORMAT" == "--json" ]]; then
    wrangler r2 object list "$BUCKET" ${PREFIX:+--prefix "$PREFIX"} --json
else
    wrangler r2 object list "$BUCKET" ${PREFIX:+--prefix "$PREFIX"}
fi
