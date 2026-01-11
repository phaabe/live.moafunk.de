#!/bin/bash
# Delete objects from R2 bucket
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"

usage() {
    cat <<EOF
Usage: $(basename "$0") [options] <key> [key2] [key3] ...
       $(basename "$0") [options] --prefix <prefix>
       $(basename "$0") [options] --stdin

Delete objects from an R2 bucket.

Options:
  -e, --env <path>      Path to .env file (default: backend/.env)
  -b, --bucket <name>   Bucket name (overrides R2_BUCKET_NAME env var)
  -y, --yes             Skip confirmation prompt
  -h, --help            Show this help message

Modes:
  --prefix <prefix>     Delete all objects with the given prefix
  --stdin               Read keys from stdin (one per line)

Environment variables:
  R2_BUCKET_NAME        Bucket name (default: 'unheard-artists-prod')
  R2_ACCOUNT_ID         Cloudflare account ID
  R2_ACCESS_KEY_ID      R2 access key
  R2_SECRET_ACCESS_KEY  R2 secret key

Examples:
  $(basename "$0") shows/file.mp3                    # Delete single object
  $(basename "$0") -b unheard-artists-dev file.mp3   # Use specific bucket
  $(basename "$0") --prefix shows/old-                # Delete by prefix
  echo "file.mp3" | $(basename "$0") --stdin          # Delete from stdin
EOF
    exit 0
}

# Parse arguments
ENV_FILE="$BACKEND_DIR/.env"
BUCKET_ARG=""
SKIP_CONFIRM=false
POSITIONAL_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            usage
            ;;
        -e|--env)
            ENV_FILE="${2:-}"
            shift 2
            ;;
        -b|--bucket)
            BUCKET_ARG="${2:-}"
            shift 2
            ;;
        -y|--yes)
            SKIP_CONFIRM=true
            shift
            ;;
        -*)
            if [[ "$1" != "--prefix" && "$1" != "--stdin" ]]; then
                echo "Unknown option: $1" >&2
                echo "Use --help for usage information" >&2
                exit 1
            fi
            POSITIONAL_ARGS+=("$1")
            shift
            ;;
        *)
            POSITIONAL_ARGS+=("$1")
            shift
            ;;
    esac
done
set -- "${POSITIONAL_ARGS[@]:-}"

# Load .env if exists
if [[ -f "$ENV_FILE" ]]; then
    set -a
    source "$ENV_FILE"
    set +a
fi

# Bucket priority: CLI arg > env var > default
BUCKET="${BUCKET_ARG:-${R2_BUCKET_NAME:-unheard-artists-prod}}"
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
    echo "Error: No keys specified" >&2
    echo "Use --help for usage information" >&2
    exit 1
fi

echo "Bucket: $BUCKET"
echo "---"

DELETED_KEYS=""
FAILED_KEYS=""

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
            DELETED_KEYS+="$key\n"
        else
            ((FAILED++))
            FAILED_KEYS+="$key\n"
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
            DELETED_KEYS+="$key\n"
        else
            ((FAILED++))
            FAILED_KEYS+="$key\n"
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
            DELETED_KEYS+="$key\n"
        else
            ((FAILED++))
            FAILED_KEYS+="$key\n"
        fi
    done
    
    echo "---"
    echo "Deleted: $COUNT, Failed: $FAILED"

    if [[ $COUNT -gt 0 ]]; then
        echo "Deleted keys:"
        printf "%s" "$DELETED_KEYS"
    fi

    if [[ $FAILED -gt 0 ]]; then
        echo "Failed keys:"
        printf "%s" "$FAILED_KEYS"
    fi
fi
