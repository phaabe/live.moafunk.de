#!/usr/bin/env bash
# PreToolUse hook: Claude's local guard for the architecture epic rules
# (docs/implementation/epic-rules.md). The logic lives in epic_guard.py,
# which parses each shell command and its real arguments.
#
# stdin → JSON, stderr → message back to Claude, exit 2 → block, exit 0 → allow.

set -euo pipefail

exec python3 "$(cd "$(dirname "$0")" && pwd)/epic_guard.py"
