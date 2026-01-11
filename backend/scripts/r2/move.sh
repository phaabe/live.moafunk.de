#!/bin/bash
# Move/rename objects in R2 bucket (copy + delete)
# Usage: ./move.sh <source-key> <dest-key>
#        ./move.sh --prefix <old-prefix> <new-prefix>  # Bulk rename prefix
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
TEMP_DIR="/tmp/r2-move-$$"

cleanup() {
    rm -rf "$TEMP_DIR"
}
trap cleanup EXIT

move_object() {
    local src="$1"
    local dst="$2"
    
    echo "Moving: $src -> $dst"
    
    mkdir -p "$TEMP_DIR"
    local temp_file="$TEMP_DIR/$(basename "$src")"
    
    # Download
    if ! wrangler r2 object get "$BUCKET/$src" --file "$temp_file" 2>/dev/null; then
        echo "  ✗ Failed to download source"
        return 1
    fi
    
    # Upload to new location
    if ! wrangler r2 object put "$BUCKET/$dst" --file "$temp_file" 2>/dev/null; then
        echo "  ✗ Failed to upload to destination"
        return 1
    fi
    
    # Delete original
    if ! wrangler r2 object delete "$BUCKET/$src" 2>/dev/null; then
        echo "  ⚠ Copied but failed to delete source (duplicate exists)"
        return 1
    fi
    
    rm -f "$temp_file"
    echo "  ✓ Moved"
    return 0
}

if [[ $# -lt 2 ]]; then
    echo "Usage: $0 <source-key> <dest-key>"
    echo "       $0 --prefix <old-prefix> <new-prefix>"
    exit 1
fi

echo "Bucket: $BUCKET"
echo "---"

if [[ "$1" == "--prefix" ]]; then
    OLD_PREFIX="${2:-}"
    NEW_PREFIX="${3:-}"
    
    if [[ -z "$OLD_PREFIX" || -z "$NEW_PREFIX" ]]; then
        echo "Error: --prefix requires old-prefix and new-prefix arguments"
        exit 1
    fi
    
    echo "Renaming prefix: $OLD_PREFIX -> $NEW_PREFIX"
    echo "Fetching object list..."
    
    KEYS=$(wrangler r2 object list "$BUCKET" --prefix "$OLD_PREFIX" --json 2>/dev/null | jq -r '.[].key // empty')
    
    if [[ -z "$KEYS" ]]; then
        echo "No objects found with prefix: $OLD_PREFIX"
        exit 0
    fi
    
    COUNT=0
    FAILED=0
    while IFS= read -r key; do
        new_key="${NEW_PREFIX}${key#$OLD_PREFIX}"
        if move_object "$key" "$new_key"; then
            ((COUNT++))
        else
            ((FAILED++))
        fi
    done <<< "$KEYS"
    
    echo "---"
    echo "Moved: $COUNT, Failed: $FAILED"

else
    SRC="$1"
    DST="$2"
    
    if move_object "$SRC" "$DST"; then
        echo "---"
        echo "Success"
    else
        echo "---"
        echo "Failed"
        exit 1
    fi
fi
