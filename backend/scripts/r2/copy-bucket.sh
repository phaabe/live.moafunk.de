#!/bin/bash
# Copy objects from one R2 bucket to another (server-side when possible)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"

usage() {
    cat <<EOF
Usage: $(basename "$0") [options]

Copy all objects from a source bucket to a destination bucket.

Options:
  -e, --env <path>      Path to .env file (default: backend/.env)
  --src <bucket>        Source bucket name (required)
  --dst <bucket>        Destination bucket name (required)
  --prefix <prefix>     Only copy objects under this prefix
  --dry-run             Show what would be copied (rclone/aws dry-run)
  -h, --help            Show this help message

Environment variables (used if not passed via flags):
  R2_ACCOUNT_ID         Cloudflare account ID
  R2_ACCESS_KEY_ID      R2 access key
  R2_SECRET_ACCESS_KEY  R2 secret key

Examples:
  $(basename "$0") --src unheard-artists-dev --dst unheard-artists-prod
  $(basename "$0") --src unheard-artists-dev --dst unheard-backups --prefix artists/
  $(basename "$0") -e ./.env --src a --dst b --dry-run
EOF
    exit 0
}

# Parse arguments
ENV_FILE="$BACKEND_DIR/.env"
SRC_BUCKET=""
DST_BUCKET=""
PREFIX=""
DRY_RUN=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            usage
            ;;
        -e|--env)
            ENV_FILE="${2:-}"
            shift 2
            ;;
        --src)
            SRC_BUCKET="${2:-}"
            shift 2
            ;;
        --dst)
            DST_BUCKET="${2:-}"
            shift 2
            ;;
        --prefix)
            PREFIX="${2:-}"
            shift 2
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        -*)
            echo "Unknown option: $1" >&2
            echo "Use --help for usage information" >&2
            exit 1
            ;;
        *)
            echo "Unknown argument: $1" >&2
            echo "Use --help for usage information" >&2
            exit 1
            ;;
    esac
done

# Load .env if exists
if [[ -f "$ENV_FILE" ]]; then
    set -a
    source "$ENV_FILE"
    set +a
fi

# Validate
if [[ -z "$SRC_BUCKET" || -z "$DST_BUCKET" ]]; then
    echo "Error: --src and --dst are required" >&2
    exit 1
fi

if [[ "$SRC_BUCKET" == "$DST_BUCKET" ]]; then
    echo "Error: source and destination buckets must differ" >&2
    exit 1
fi

R2_ENDPOINT="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
REMOTE="r2-prod"

# Export R2 credentials for AWS CLI
export AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID"
export AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY"
export AWS_DEFAULT_REGION="auto"

# Prefer rclone if configured
if command -v rclone &> /dev/null && [[ -f "$HOME/.config/rclone/rclone.conf" ]]; then
    SRC_URI="$REMOTE:$SRC_BUCKET/${PREFIX}"
    DST_URI="$REMOTE:$DST_BUCKET/${PREFIX}"
    echo "Copying via rclone: $SRC_URI -> $DST_URI"
    if $DRY_RUN; then
        rclone sync "$SRC_URI" "$DST_URI" --dry-run --progress
    else
        rclone sync "$SRC_URI" "$DST_URI" --progress
    fi
    exit 0
fi

# Fallback to AWS CLI sync
if command -v aws &> /dev/null; then
    SRC_URI="s3://$SRC_BUCKET/${PREFIX}"
    DST_URI="s3://$DST_BUCKET/${PREFIX}"
    echo "Copying via aws s3 sync: $SRC_URI -> $DST_URI"
    DRY_FLAG=""
    $DRY_RUN && DRY_FLAG="--dryrun"
    aws s3 sync "$SRC_URI" "$DST_URI" --endpoint-url "$R2_ENDPOINT" $DRY_FLAG
    exit 0
fi

echo "Error: Neither rclone nor aws CLI is available"
echo "Install rclone: curl https://rclone.org/install.sh | sudo bash"
exit 1
