#!/usr/bin/env bash
set -euo pipefail

# Test R2 content accessibility using the same flow as the frontend.
# This script:
# 1. Logs in to the backend API
# 2. Fetches artist data (includes presigned URLs)
# 3. Attempts to fetch content from presigned URLs
# 4. Reports success/failure for each

BACKEND_URL="${BACKEND_URL:-http://localhost:8000}"
USERNAME="${USERNAME:-superadmin}"
PASSWORD="${PASSWORD:-}"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "=========================================="
echo "R2 Content Access Test"
echo "=========================================="
echo "Backend URL: $BACKEND_URL"
echo ""

# Check if password is provided
if [ -z "$PASSWORD" ]; then
    echo -e "${RED}Error: PASSWORD environment variable is required${NC}"
    echo "Usage: PASSWORD=your-password ./scripts/test_r2_access.sh"
    exit 1
fi

# Check required tools
for cmd in curl jq; do
    if ! command -v $cmd &> /dev/null; then
        echo -e "${RED}Error: $cmd is required but not installed${NC}"
        exit 1
    fi
done

# Create temp file for cookies
COOKIE_FILE=$(mktemp)
trap "rm -f $COOKIE_FILE" EXIT

echo "1. Logging in as $USERNAME..."
LOGIN_RESPONSE=$(curl -s -c "$COOKIE_FILE" -b "$COOKIE_FILE" \
    -X POST "$BACKEND_URL/api/auth/login" \
    -H "Content-Type: application/json" \
    -d "{\"username\": \"$USERNAME\", \"password\": \"$PASSWORD\"}" \
    -w "\n%{http_code}")

HTTP_CODE=$(echo "$LOGIN_RESPONSE" | tail -n1)
BODY=$(echo "$LOGIN_RESPONSE" | sed '$d')

if [ "$HTTP_CODE" != "200" ]; then
    echo -e "${RED}✗ Login failed (HTTP $HTTP_CODE)${NC}"
    echo "$BODY"
    exit 1
fi
echo -e "${GREEN}✓ Login successful${NC}"
echo ""

echo "2. Fetching artists list..."
ARTISTS_RESPONSE=$(curl -s -b "$COOKIE_FILE" "$BACKEND_URL/api/artists" -w "\n%{http_code}")
HTTP_CODE=$(echo "$ARTISTS_RESPONSE" | tail -n1)
BODY=$(echo "$ARTISTS_RESPONSE" | sed '$d')

if [ "$HTTP_CODE" != "200" ]; then
    echo -e "${RED}✗ Failed to fetch artists (HTTP $HTTP_CODE)${NC}"
    echo "$BODY"
    exit 1
fi

ARTIST_COUNT=$(echo "$BODY" | jq '.artists | length')
echo -e "${GREEN}✓ Found $ARTIST_COUNT artists${NC}"
echo ""

if [ "$ARTIST_COUNT" -eq 0 ]; then
    echo -e "${YELLOW}⚠ No artists in database - nothing to test${NC}"
    exit 0
fi

# Get first artist ID
FIRST_ARTIST_ID=$(echo "$BODY" | jq '.artists[0].id')
FIRST_ARTIST_NAME=$(echo "$BODY" | jq -r '.artists[0].name')

echo "3. Fetching artist detail for: $FIRST_ARTIST_NAME (ID: $FIRST_ARTIST_ID)..."
DETAIL_RESPONSE=$(curl -s -b "$COOKIE_FILE" "$BACKEND_URL/api/artists/$FIRST_ARTIST_ID" -w "\n%{http_code}")
HTTP_CODE=$(echo "$DETAIL_RESPONSE" | tail -n1)
BODY=$(echo "$DETAIL_RESPONSE" | sed '$d')

if [ "$HTTP_CODE" != "200" ]; then
    echo -e "${RED}✗ Failed to fetch artist detail (HTTP $HTTP_CODE)${NC}"
    echo "$BODY"
    exit 1
fi
echo -e "${GREEN}✓ Artist detail fetched${NC}"
echo ""

# Extract file URLs
echo "4. Checking presigned URLs..."
FILE_URLS=$(echo "$BODY" | jq -r '.file_urls')

if [ "$FILE_URLS" == "null" ] || [ "$FILE_URLS" == "{}" ]; then
    echo -e "${YELLOW}⚠ No file URLs found for this artist${NC}"
    exit 0
fi

echo "   Found file URLs:"
echo "$FILE_URLS" | jq -r 'keys[]' | while read KEY; do
    echo "   - $KEY"
done
echo ""

echo "5. Testing content access for each URL..."
echo ""

TOTAL=0
SUCCESS=0
FAILED=0

# Test each URL
for KEY in $(echo "$FILE_URLS" | jq -r 'keys[]'); do
    URL=$(echo "$FILE_URLS" | jq -r ".[\"$KEY\"]")
    TOTAL=$((TOTAL + 1))
    
    echo "   Testing: $KEY"
    echo "   URL: ${URL:0:80}..."
    
    # Make HEAD request to check accessibility
    CONTENT_RESPONSE=$(curl -s -I "$URL" -o /dev/null -w "%{http_code}|%{content_type}|%{size_download}")
    
    IFS='|' read -r HTTP_CODE CONTENT_TYPE SIZE <<< "$CONTENT_RESPONSE"
    
    if [ "$HTTP_CODE" == "200" ]; then
        echo -e "   ${GREEN}✓ Accessible (HTTP $HTTP_CODE, Type: $CONTENT_TYPE)${NC}"
        SUCCESS=$((SUCCESS + 1))
    else
        echo -e "   ${RED}✗ Failed (HTTP $HTTP_CODE)${NC}"
        
        # Try to get more info with GET request
        ERROR_BODY=$(curl -s "$URL" 2>&1 | head -c 500)
        echo "   Error details: $ERROR_BODY"
        FAILED=$((FAILED + 1))
    fi
    echo ""
done

echo "=========================================="
echo "Summary"
echo "=========================================="
echo "Total URLs tested: $TOTAL"
echo -e "Successful: ${GREEN}$SUCCESS${NC}"
echo -e "Failed: ${RED}$FAILED${NC}"

if [ "$FAILED" -gt 0 ]; then
    echo ""
    echo -e "${YELLOW}Troubleshooting tips:${NC}"
    echo "1. Check CORS is configured: BUCKET=unheard-artists-dev ./scripts/configure_r2_cors.sh"
    echo "2. Verify bucket exists: wrangler r2 bucket list"
    echo "3. Check R2_BUCKET_NAME in .env matches where files are stored"
    echo "4. Verify files exist: wrangler r2 object get <bucket>/<key>"
    exit 1
fi

echo ""
echo -e "${GREEN}All content is accessible!${NC}"
