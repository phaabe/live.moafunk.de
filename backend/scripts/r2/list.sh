#!/bin/bash
# List objects in R2 bucket
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"

usage() {
    cat <<EOF
Usage: $(basename "$0") [options] [prefix]

List objects in an R2 bucket.

Options:
  -e, --env <path>      Path to .env file (default: backend/.env)
  -b, --bucket <name>   Bucket name (overrides R2_BUCKET_NAME env var)
  --json                Output in JSON format
  -h, --help            Show this help message

Environment variables:
  R2_BUCKET_NAME        Bucket name (default: 'unheard-artists-prod')
  R2_ACCOUNT_ID         Cloudflare account ID
  R2_ACCESS_KEY_ID      R2 access key
  R2_SECRET_ACCESS_KEY  R2 secret key

Examples:
  $(basename "$0")                          # List all objects
  $(basename "$0") shows/                   # List objects with prefix
  $(basename "$0") -b unheard-artists-dev   # Use specific bucket
  $(basename "$0") -e ./.env --json         # JSON output with custom env
EOF
    exit 0
}

# Parse arguments
ENV_FILE="$BACKEND_DIR/.env"
BUCKET_ARG=""
PREFIX=""
OUTPUT_FORMAT=""

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
        --json)
            OUTPUT_FORMAT="--json"
            shift
            ;;
        -*)
            echo "Unknown option: $1" >&2
            echo "Use --help for usage information" >&2
            exit 1
            ;;
        *)
            PREFIX="$1"
            shift
            ;;
    esac
done

# Load .env if exists
if [[ -f "$ENV_FILE" ]]; then
    set -a
    source "$ENV_FILE"
    set +a
fi

# Bucket priority: CLI arg > env var > default
BUCKET="${BUCKET_ARG:-${R2_BUCKET_NAME:-unheard-artists-prod}}"

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
