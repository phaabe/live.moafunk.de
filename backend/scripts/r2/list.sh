#!/bin/bash
# List objects in R2 bucket
# Usage: ./list.sh [prefix] [--json]
#        ./list.sh --env /path/to/.env [prefix] [--json]
#
# Environment variables:
#   R2_BUCKET_NAME - Bucket name (default: from .env or 'unheard-artists-prod')
#   R2_ACCOUNT_ID  - Cloudflare account ID

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
PREFIX="${1:-}"
OUTPUT_FORMAT="${2:-}"

echo "Listing objects in bucket: $BUCKET"
[[ -n "$PREFIX" ]] && echo "Prefix filter: $PREFIX"
echo "---"

# Check if rclone is available and configured
if command -v rclone &> /dev/null && [[ -f "$HOME/.config/rclone/rclone.conf" ]]; then
    REMOTE="r2-prod"
    if [[ "$OUTPUT_FORMAT" == "--json" ]]; then
        rclone lsjson "$REMOTE:$BUCKET" ${PREFIX:+--include "$PREFIX**"}
    else
        rclone ls "$REMOTE:$BUCKET/$PREFIX"
    fi
else
    # Use AWS CLI with R2 credentials from env
    if ! command -v aws &> /dev/null; then
        echo "Error: Neither rclone nor aws CLI is available"
        echo "Install rclone: curl https://rclone.org/install.sh | sudo bash"
        echo "Or run: ./scripts/backup/setup_rclone.sh"
        exit 1
    fi
    
    R2_ENDPOINT="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
    
    # Export R2 credentials for AWS CLI
    export AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID"
    export AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY"
    export AWS_DEFAULT_REGION="auto"
    
    if [[ "$OUTPUT_FORMAT" == "--json" ]]; then
        aws s3api list-objects-v2 \
            --endpoint-url "$R2_ENDPOINT" \
            --bucket "$BUCKET" \
            ${PREFIX:+--prefix "$PREFIX"} \
            --output json
    else
        aws s3 ls "s3://$BUCKET/$PREFIX" \
            --endpoint-url "$R2_ENDPOINT" \
            --recursive
    fi
fi
