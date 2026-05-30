#!/usr/bin/env bash
# PostToolUse hook for Write|Edit|MultiEdit.
# Runs the right formatter for the edited file. Quiet by default.
set -uo pipefail

INPUT="$(cat)"
FILE=$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // .toolInput.file_path // .tool_input.path // .toolInput.path // empty' 2>/dev/null)

[ -z "$FILE" ] && exit 0
[ ! -f "$FILE" ] && exit 0

format_with() {
  local cmd="$1"; shift
  command -v "$cmd" >/dev/null 2>&1 || return 0
  "$cmd" "$@" >/dev/null 2>&1 || true
}

case "$FILE" in
  *.ts|*.tsx|*.js|*.jsx|*.mjs|*.cjs|*.json|*.jsonc|*.json5|*.md|*.mdx|*.yml|*.yaml|*.css|*.scss|*.html|*.vue|*.svelte)
    if [ -f "package.json" ] && grep -q '"prettier"' package.json 2>/dev/null; then
      format_with npx prettier --write "$FILE"
    elif command -v prettier >/dev/null 2>&1; then
      format_with prettier --write "$FILE"
    elif command -v biome >/dev/null 2>&1; then
      format_with biome format --write "$FILE"
    fi
    ;;
  *.py)
    if command -v ruff >/dev/null 2>&1; then
      format_with ruff format "$FILE"
    elif command -v black >/dev/null 2>&1; then
      format_with black --quiet "$FILE"
    fi
    ;;
  *.go)
    format_with gofmt -w "$FILE"
    ;;
  *.rs)
    format_with rustfmt --edition 2021 "$FILE"
    ;;
  *.tf|*.tfvars)
    format_with terraform fmt "$FILE"
    ;;
  *.sh|*.bash)
    format_with shfmt -w -i 2 -ci "$FILE"
    ;;
  *.php)
    if [ -x "./vendor/bin/php-cs-fixer" ]; then
      format_with ./vendor/bin/php-cs-fixer fix --quiet "$FILE"
    fi
    ;;
esac

exit 0
