#!/bin/bash
# Codex PreToolUse adapter. Bash 3.2 and Python 3.10+ are required.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if python3 "$SCRIPT_DIR/epic_guard.py"; then
  exit 0
else
  # Codex treats exit 2 as a block, including dependency and parser failures.
  exit 2
fi
