#!/usr/bin/env bash
# UserPromptSubmit hook.
# stdout → context for Claude, stderr → user feedback, exit 0 → continue.
set -uo pipefail

INPUT="$(cat)"
PROMPT=$(printf '%s' "$INPUT" | jq -r '.prompt // .user_prompt // empty' 2>/dev/null)
CWD=$(printf '%s' "$INPUT" | jq -r '.cwd // .workingDirectory // .workspace.current_dir // empty' 2>/dev/null)
[ -z "$CWD" ] && CWD="$(pwd)"

# Must be inside a git repo with an existing GitNexus index
REPO_ROOT=$(git -C "$CWD" rev-parse --show-toplevel 2>/dev/null || true)
[ -z "$REPO_ROOT" ] && exit 0
[ -d "$REPO_ROOT/.gitnexus" ] || exit 0

REPO_NAME=$(basename "$REPO_ROOT")
P_LOWER=$(printf '%s' "$PROMPT" | tr '\n' ' ' | tr '[:upper:]' '[:lower:]')

# ── Heuristic: does the prompt look like a code task? ────────────────────
# Strong signals — almost always code-related
is_code_task=0
if printf '%s' "$P_LOWER" | grep -E -q -- '(^|[[:space:]])/[a-z]*[.]?(plan|review|explain|scaffold|test|debug|refactor|impact|topic|ship|commit|pr|branch|sync|lint|format|typecheck|setup|clean|todo|changelog)\b'; then
  is_code_task=1
fi

# Phrasal signals
if [ "$is_code_task" -eq 0 ] && printf '%s' "$P_LOWER" | grep -E -q -- '(how does|how do|why does|why is|where is|where does|what calls|what uses|trace|debug|investigate|implement|refactor|rename|extract|move|split|fix|bug|error|crash|fail|broken|review|architecture|component|module|function|class|method|file|test|coverage|impact|safe to (change|edit|remove|delete|rename|refactor)|what (will|would|could) break|search for|find (all|the|every)|find me|look for|look at|show me|explore|understand|callers? of|callees? of|usages? of|references? to|depend(s|ence|encies) on|imports?|inherits?|extends|implements)\b'; then
  is_code_task=1
fi

# Deflate signals — short acks / meta stay silent
# Detect explicit slash-command intent so we DON'T deflate it for being short
is_slash=0
if printf '%s' "$P_LOWER" | grep -E -q -- '(^|[[:space:]])/[a-zA-Z][a-zA-Z.-]*'; then
  is_slash=1
fi
# Pure ack? deflate even if a keyword slipped in.
if printf '%s' "$P_LOWER" | grep -E -q -- '^(ok|okay|thanks|thank you|yes|y|no|n|sure|continue|go ahead|proceed)([[:space:]!.,]|$)'; then
  is_code_task=0
fi
# Very short prompts deflate — but ONLY if not a slash command and no strong code phrase
if [ "$is_slash" -eq 0 ] && [ "$(printf '%s' "$PROMPT" | wc -w)" -le 2 ]; then
  is_code_task=0
fi

[ "$is_code_task" -eq 0 ] && exit 0

# ── Compose the directive ────────────────────────────────────────────────
cat <<DIRECTIVE
[gitnexus-consult-first]
Code task detected. The repo "$REPO_NAME" has a GitNexus index — USE IT FIRST, before Grep / Read / Glob.

Before any code understanding, debugging, refactoring, or review:

1. **Read \`gitnexus://repo/$REPO_NAME/context\`** (~150 tokens) to confirm the index is fresh and see top-level stats.
2. **Call \`gitnexus_query({query: "..."})\`** with what you're investigating to find related execution flows.
3. For specific symbols, **call \`gitnexus_context({name: "<symbol>"})\`** to get callers / callees / processes.
4. For "what breaks if I change X?", **call \`gitnexus_impact({name: "<symbol>", depth: 2})\`**.
5. For "what do my current changes affect?", **call \`gitnexus_detect_changes({})\`**.
6. Only THEN fall back to Grep / Read / Glob for the parts the graph can't answer (string search, formatting, comments, raw text).

This is faster and far more accurate than greps for symbol-level questions. The graph already knows what calls what, who imports whom, and which functions belong to which execution flow.

If \`gitnexus://repo/$REPO_NAME/context\` says the index is stale, run \`!gitnexus analyze\` first (or note it; the SessionStart and PostToolUse hooks may already have a refresh in flight).

Skill files for deep workflows (read whichever matches the task):
  • gitnexus-exploring         — "how does X work?"
  • gitnexus-impact-analysis   — "what breaks if I change X?"
  • gitnexus-debugging         — "why is X failing?"
  • gitnexus-refactoring       — rename / extract / move / split
  • gitnexus-pr-review         — review a PR or diff
  • gitnexus-cli               — analyze / status / clean commands
  • gitnexus-guide             — full tools / resources / schema reference
DIRECTIVE

exit 0
