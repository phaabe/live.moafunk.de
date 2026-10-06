#!/bin/bash
# Codex PreToolUse adapter. Bash 3.2 and Python 3.10+ are required.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HOOK_PYTHON=python3
if [[ -n "${EPIC_RUNTIME_ROOT:-}" ]]; then
  CODE_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
  if ! HOOK_PYTHON="$(python3 -I "$CODE_ROOT/.codex/protected_home.py" hook-python --code-root "$CODE_ROOT")"; then
    printf '%s\n' 'BLOCKED by Codex epic guard: protected interpreter unavailable.' >&2
    exit 2
  fi
fi
if "$HOOK_PYTHON" -I "$SCRIPT_DIR/epic_guard.py"; then
  exit 0
else
  # Codex treats exit 2 as a block, including dependency and parser failures.
  exit 2
fi
