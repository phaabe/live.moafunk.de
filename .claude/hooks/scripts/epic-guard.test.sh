#!/usr/bin/env bash
# Tests for epic-guard.sh. Run: .claude/hooks/scripts/epic-guard.test.sh
set -euo pipefail

HOOK="$(cd "$(dirname "$0")" && pwd)/epic-guard.sh"
SHA="0123456789abcdef0123456789abcdef01234567"
fail=0

check() { # expected_exit description json [env]
  local expected="$1" desc="$2" json="$3" env="${4:-}"
  local got=0
  if [[ -n "$env" ]]; then
    env "$env" "$HOOK" <<<"$json" >/dev/null 2>&1 || got=$?
  else
    "$HOOK" <<<"$json" >/dev/null 2>&1 || got=$?
  fi
  if [[ "$got" == "$expected" ]]; then
    echo "ok   $desc"
  else
    echo "FAIL $desc (exit $got, want $expected)"
    fail=1
  fi
}

bash_cmd() { jq -cn --arg c "$1" '{tool_name:"Bash", tool_input:{command:$c}, cwd:"/tmp"}'; }

check 2 "verdict in Codex's name via gh" "$(bash_cmd "gh pr comment 5 --body 'Review: APPROVED by Codex at $SHA'")"
check 2 "changes-requested in Codex's name" "$(bash_cmd "gh pr comment 5 --body 'Review: CHANGES REQUESTED by Codex at $SHA'")"
check 2 "verdict in Codex's name via MCP tool" \
  "$(jq -cn --arg b "Review: APPROVED by Codex at $SHA" '{tool_name:"mcp__github__add_issue_comment", tool_input:{body:$b}}')"
check 0 "format explanation without a real SHA" "$(bash_cmd "gh issue comment 1 --body 'Format: Review: APPROVED by Codex at <40-char head SHA>'")"
check 0 "Claude's own verdict" "$(bash_cmd "gh pr comment 5 --body 'Review: APPROVED by Claude at $SHA'")"

check 2 "pr create without base" "$(bash_cmd "gh pr create --fill")"
check 0 "pr create into dev branch" "$(bash_cmd "gh pr create --base dev/streaming-architecture --fill")"
check 2 "feature pr into main" "$(bash_cmd "gh pr create --base main --head feat/1-x --fill")"
check 2 "feature pr into main, -B form" "$(bash_cmd "gh pr create -B main -H fix/2-y --fill")"
check 0 "release pr into main" "$(bash_cmd "gh pr create --base main --head dev/streaming-architecture --fill")"
check 0 "approved hotfix override" "$(bash_cmd "gh pr create --base main --head fix/3-z --fill")" CLAUDE_ALLOW_MAIN_PR=1

check 2 "merge without expected head" "$(bash_cmd "gh pr merge 5 --squash")"
check 2 "merge with short sha" "$(bash_cmd "gh pr merge 5 --squash --match-head-commit abc123")"
check 0 "merge with expected head" "$(bash_cmd "gh pr merge 5 --squash --match-head-commit $SHA")"

check 0 "unrelated command" "$(bash_cmd "git status")"

# Codex review of https://github.com/phaabe/live.moafunk.de/pull/402
check 2 "base in body text does not hide base main" \
  "$(bash_cmd "gh pr create --base main --head feat/1-x --body 'Follow-up uses --base dev/streaming-architecture'")"
check 2 "second merge without its own expected head" \
  "$(bash_cmd "gh pr merge 5 --squash --match-head-commit $SHA && gh pr merge 6 --squash")"
check 0 "quoted expected head" "$(bash_cmd "gh pr merge 5 --squash --match-head-commit '$SHA'")"
check 0 "--match-head-commit=sha form" "$(bash_cmd "gh pr merge 5 --squash --match-head-commit=$SHA")"
check 0 "approved setup PR to main" "$(bash_cmd "gh pr create --base main --head ci/312-epic-guard --fill")"
check 2 "inline override does not reach the hook" \
  "$(bash_cmd "CLAUDE_ALLOW_MAIN_PR=1 gh pr create --base main --head fix/3-z --fill")"
check 2 "base=main form" "$(bash_cmd "gh pr create --base=main --head feat/1-x --fill")"
check 0 "heredoc body with quotes" "$(bash_cmd "gh pr create --base dev/streaming-architecture --body-file - <<'EOF'
It's a body with --base main and Review: APPROVED by Claude
EOF")"
check 2 "gh pr merge inside command substitution" "$(bash_cmd "echo \$(gh pr merge 5 --squash)")"
check 2 "MCP PR into main" "$(jq -cn '{tool_name:"mcp__github__create_pull_request", tool_input:{base:"main", head:"feat/1-x"}}')"
check 0 "MCP PR into dev branch" \
  "$(jq -cn '{tool_name:"mcp__github__create_pull_request", tool_input:{base:"dev/streaming-architecture", head:"feat/1-x"}}')"
check 2 "MCP merge" "$(jq -cn '{tool_name:"mcp__github__merge_pull_request", tool_input:{pull_number:5}}')"

exit "$fail"
