#!/bin/bash
# Delete objects from R2 bucket
# Usage: ./delete.sh <key> [key2] [key3] ...
#        ./delete.sh --prefix <prefix>  # Delete all objects with prefix
#        ./delete.sh --stdin            # Read keys from stdin (one per line)
#        ./delete.sh --env /path/to/.env <key>
#
# Environment variables:
#   R2_BUCKET_NAME - Bucket name (default: from .env or 'unheard-artists-prod')

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"

# Parse --env flag
ENV_FILE="$BACKEND_DIR/.env"
if [[ "${1:-}" == "--env" || "${1:-}" == "-e" ]]; then
    ENV_FILE="${2:-}"
    shift 2
fi

# Load .env if exists
if [[ -f "$ENV_FILE" ]]; then
    set -a
    source "$ENV_FILE"
    set +a
fi

BUCKET="${R2_BUCKET_NAME:-unheard-artists-prod}"
R2_ENDPOINT="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
RCLONE_REMOTE="r2-prod"

# Export R2 credentials for AWS CLI
export AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID"
export AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY"
export AWS_DEFAULT_REGION="auto"

# Check for available tool
USE_RCLONE=false
USE_AWS=false
if command -v rclone &> /dev/null && [[ -f "$HOME/.config/rclone/rclone.conf" ]]; then
    USE_RCLONE=true
elif command -v aws &> /dev/null; then
    USE_AWS=true
else
    # Fallback to wrangler
    :
fi

delete_object() {
    local key="$1"
    echo "Deleting: $key"
    
    if $USE_RCLONE; then
        if rclone delete "$RCLONE_REMOTE:$BUCKET/$key" 2>/dev/null; then
            echo "  ✓ Deleted"
            return 0
        fi
    elif $USE_AWS; then
        if aws s3 rm "s3://$BUCKET/$key" --endpoint-url "$R2_ENDPOINT" 2>/dev/null; then
            echo "  ✓ Deleted"
            return 0
        fi
    else
        if wrangler r2 object delete "$BUCKET/$key" 2>/dev/null; then
            echo "  ✓ Deleted"
            return 0
        fi
    fi
    
    echo "  ✗ Failed to delete"
    return 1
}

list_objects_with_prefix() {
    local prefix="$1"
    if $USE_RCLONE; then
        rclone lsf "$RCLONE_REMOTE:$BUCKET/$prefix" --files-only -R 2>/dev/null | sed "s|^|$prefix|"
    elif $USE_AWS; then
        aws s3api list-objects-v2 --endpoint-url "$R2_ENDPOINT" --bucket "$BUCKET" --prefix "$prefix" --query 'Contents[].Key' --output text 2>/dev/null | tr '\t' '\n'
    else
        wrangler r2 object list "$BUCKET" --prefix "$prefix" --json 2>/dev/null | jq -r '.[].key // empty'
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
    KEYS=$(list_objects_with_prefix "$PREFIX")
    
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
