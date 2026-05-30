#!/usr/bin/env bash
# PostToolUse hook for Write|Edit|MultiEdit.
# Runs the linter on the changed file. Stays silent on success;
# writes findings to stderr so Claude sees and can react.
set -uo pipefail

INPUT="$(cat)"
FILE=$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // .toolInput.file_path // .tool_input.path // .toolInput.path // empty' 2>/dev/null)

[ -z "$FILE" ] && exit 0
[ ! -f "$FILE" ] && exit 0

# Skip lint for huge files (likely generated)
LINES=$(wc -l < "$FILE" 2>/dev/null || echo 0)
[ "$LINES" -gt 5000 ] && exit 0

lint() {
  local out
  out=$("$@" 2>&1) || {
    rc=$?
    if [ -n "$out" ]; then
      {
        echo "Linter findings on $FILE:"
        printf '%s\n' "$out" | head -80
      } >&2
    fi
    return 0  # never block — informational only
  }
}

case "$FILE" in
  *.ts|*.tsx|*.js|*.jsx|*.mjs|*.cjs)
    if [ -f "package.json" ] && grep -q '"eslint"' package.json 2>/dev/null && command -v npx >/dev/null 2>&1; then
      lint npx eslint --max-warnings 0 "$FILE"
    elif command -v biome >/dev/null 2>&1; then
      lint biome lint "$FILE"
    fi
    ;;
  *.py)
    if command -v ruff >/dev/null 2>&1; then
      lint ruff check "$FILE"
    fi
    ;;
  *.go)
    if command -v golangci-lint >/dev/null 2>&1; then
      lint golangci-lint run --no-config --disable-all --enable govet,staticcheck "$FILE"
    fi
    ;;
  *.rs)
    # clippy is per-crate, not per-file; skip in hook.
    :
    ;;
  *.sh|*.bash)
    if command -v shellcheck >/dev/null 2>&1; then
      lint shellcheck "$FILE"
    fi
    ;;
  *.tf)
    if command -v terraform >/dev/null 2>&1; then
      lint terraform fmt -check "$FILE"
    fi
    ;;
esac

exit 0
