#!/bin/bash
# Run all backups (database + R2)
# Usage: ./backup-all.sh [--full]  # --full creates dated R2 snapshot
#
# This script is designed to be called:
# - Manually for ad-hoc backups
# - By cron for scheduled backups
# - By GitHub Actions for CI/CD triggered backups

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FULL_BACKUP="${1:-}"

echo "========================================"
echo "  UNHEARD BACKUP - $(date)"
echo "========================================"
echo ""

# Track errors
ERRORS=0

# Backup database
echo ">>> Step 1/2: Database Backup"
echo ""
if ! "$SCRIPT_DIR/backup-db.sh"; then
    echo ""
    echo "⚠ Database backup failed!"
    ERRORS=$((ERRORS + 1))
fi

echo ""
echo ">>> Step 2/2: R2 Media Backup"
echo ""
if ! "$SCRIPT_DIR/backup-r2.sh" $FULL_BACKUP; then
    echo ""
    echo "⚠ R2 backup failed!"
    ERRORS=$((ERRORS + 1))
fi

echo ""
echo "========================================"
if [[ $ERRORS -eq 0 ]]; then
    echo "  ✓ ALL BACKUPS COMPLETED SUCCESSFULLY"
else
    echo "  ⚠ BACKUP COMPLETED WITH $ERRORS ERROR(S)"
fi
echo "========================================"

exit $ERRORS
