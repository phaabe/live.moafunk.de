#!/bin/bash
# Delete objects from R2 bucket
# Usage: ./delete.sh <key> [key2] [key3] ...
#        ./delete.sh --prefix <prefix>  # Delete all objects with prefix
#        ./delete.sh --stdin            # Read keys from stdin (one per line)
#
# Environment variables:
#   R2_BUCKET_NAME - Bucket name (default: from .env or 'unheard-artists-prod')

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

delete_object() {
    local key="$1"
    echo "Deleting: $key"
    if wrangler r2 object delete "$BUCKET/$key" 2>/dev/null; then
        echo "  ✓ Deleted"
    else
        echo "  ✗ Failed to delete"
        return 1
    fi
}

if [[ $# -eq 0 ]]; then
    echo "Usage: $0 <key> [key2] [key3] ..."
    echo "       $0 --prefix <prefix>"
    echo "       $0 --stdin"
    exit 1
fi

echo "Bucket: $BUCKET"
echo "---"

if [[ "$1" == "--prefix" ]]; then
    PREFIX="${2:-}"
    if [[ -z "$PREFIX" ]]; then
        echo "Error: --prefix requires a prefix argument"
        exit 1
    fi
    
    echo "Deleting all objects with prefix: $PREFIX"
    echo "Fetching object list..."
    
    # Get list of objects and delete each
    KEYS=$(wrangler r2 object list "$BUCKET" --prefix "$PREFIX" --json 2>/dev/null | jq -r '.[].key // empty')
    
    if [[ -z "$KEYS" ]]; then
        echo "No objects found with prefix: $PREFIX"
        exit 0
    fi
    
    COUNT=0
    FAILED=0
    while IFS= read -r key; do
        if delete_object "$key"; then
            ((COUNT++))
        else
            ((FAILED++))
        fi
    done <<< "$KEYS"
    
    echo "---"
    echo "Deleted: $COUNT, Failed: $FAILED"

elif [[ "$1" == "--stdin" ]]; then
    echo "Reading keys from stdin..."
    COUNT=0
    FAILED=0
    while IFS= read -r key; do
        [[ -z "$key" ]] && continue
        if delete_object "$key"; then
            ((COUNT++))
        else
            ((FAILED++))
        fi
    done
    
    echo "---"
    echo "Deleted: $COUNT, Failed: $FAILED"

else
    # Delete specified keys
    COUNT=0
    FAILED=0
    for key in "$@"; do
        if delete_object "$key"; then
            ((COUNT++))
        else
            ((FAILED++))
        fi
    done
    
    echo "---"
    echo "Deleted: $COUNT, Failed: $FAILED"
fi
