#!/bin/bash
# Move/rename objects in R2 bucket (copy + delete)
# Usage: ./move.sh <source-key> <dest-key>
#        ./move.sh --prefix <old-prefix> <new-prefix>  # Bulk rename prefix
#        ./move.sh --env /path/to/.env <source-key> <dest-key>
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
TEMP_DIR="/tmp/r2-move-$$"

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
fi

cleanup() {
    rm -rf "$TEMP_DIR"
}
trap cleanup EXIT

move_object() {
    local src="$1"
    local dst="$2"
    
    echo "Moving: $src -> $dst"
    
    if $USE_RCLONE; then
        # rclone can do server-side copy
        if rclone copyto "$RCLONE_REMOTE:$BUCKET/$src" "$RCLONE_REMOTE:$BUCKET/$dst" 2>/dev/null; then
            if rclone delete "$RCLONE_REMOTE:$BUCKET/$src" 2>/dev/null; then
                echo "  ✓ Moved"
                return 0
            else
                echo "  ⚠ Copied but failed to delete source"
                return 1
            fi
        else
            echo "  ✗ Failed to copy"
            return 1
        fi
    elif $USE_AWS; then
        if aws s3 mv "s3://$BUCKET/$src" "s3://$BUCKET/$dst" --endpoint-url "$R2_ENDPOINT" 2>/dev/null; then
            echo "  ✓ Moved"
            return 0
        else
            echo "  ✗ Failed to move"
            return 1
        fi
    else
        # Fallback to wrangler (download + upload + delete)
        mkdir -p "$TEMP_DIR"
        local temp_file="$TEMP_DIR/$(basename "$src")"
        
        if ! wrangler r2 object get "$BUCKET/$src" --file "$temp_file" 2>/dev/null; then
            echo "  ✗ Failed to download source"
            return 1
        fi
        
        if ! wrangler r2 object put "$BUCKET/$dst" --file "$temp_file" 2>/dev/null; then
            echo "  ✗ Failed to upload to destination"
            return 1
        fi
        
        if ! wrangler r2 object delete "$BUCKET/$src" 2>/dev/null; then
            echo "  ⚠ Copied but failed to delete source"
            return 1
        fi
        
        rm -f "$temp_file"
        echo "  ✓ Moved"
        return 0
    fi
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
    
    if $USE_RCLONE; then
        KEYS=$(rclone lsf "$RCLONE_REMOTE:$BUCKET/$OLD_PREFIX" --files-only -R 2>/dev/null | sed "s|^|$OLD_PREFIX|")
    elif $USE_AWS; then
        KEYS=$(aws s3api list-objects-v2 --endpoint-url "$R2_ENDPOINT" --bucket "$BUCKET" --prefix "$OLD_PREFIX" --query 'Contents[].Key' --output text 2>/dev/null | tr '\t' '\n')
    else
        KEYS=$(wrangler r2 object list "$BUCKET" --prefix "$OLD_PREFIX" --json 2>/dev/null | jq -r '.[].key // empty')
    fi
    
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
