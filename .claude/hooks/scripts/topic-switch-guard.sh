#!/usr/bin/env bash
# UserPromptSubmit hook.
#
# Detects a topic switch + parking-required state and injects a directive
# that tells Claude to run a commit / PR / merge protocol BEFORE
# answering the new prompt.
#
# stdout → injected as additional context to Claude.
# stderr → shown to the user as feedback.
# exit 0 → continue, exit 2 → block prompt (we don't block).

set -uo pipefail

INPUT="$(cat)"

PROMPT=$(printf '%s' "$INPUT" | jq -r '.prompt // .user_prompt // empty' 2>/dev/null)
CWD=$(printf '%s' "$INPUT" | jq -r '.cwd // .workingDirectory // .workspace.current_dir // empty' 2>/dev/null)
[ -z "$CWD" ] && CWD="$(pwd)"

# Guard: must be inside a git repo
git -C "$CWD" rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0

# Guard: must have at least one commit
git -C "$CWD" rev-parse --verify HEAD >/dev/null 2>&1 || exit 0

# Guard: not detached HEAD
BRANCH=$(git -C "$CWD" rev-parse --abbrev-ref HEAD 2>/dev/null)
[ "$BRANCH" = "HEAD" ] && exit 0

# --- Heuristic: does this prompt look like a topic switch? -----------------
P_LOWER=$(printf '%s' "$PROMPT" | tr '\n' ' ' | tr '[:upper:]' '[:lower:]')

is_switch=0

# Strong signals: explicit slash commands
if printf '%s' "$P_LOWER" | grep -E -q -- '(^|[[:space:]])/(plan|clear|new|reset|topic)\b'; then
  is_switch=1
fi

# Phrasal signals — English + German (Anton works bilingually)
if [ "$is_switch" -eq 0 ] && printf '%s' "$P_LOWER" | grep -E -q -- '(^|[[:space:]])(now (let'\''?s|i (want|would|need)|switch)|let'\''?s (start|switch|move|tackle|do)|next (task|topic|thing|up)|new (task|topic|feature|issue|ticket)|switch to|change topic|different (task|topic|feature)|moving on|moving to|on to|forget (that|about)|instead (of|let'\''?s)|actually,? (let'\''?s|i (want|need))|wait,? instead|jetzt (lass|m(ö|oe)chte|will|brauche)|n(ä|ae)chst(es|er) (thema|aufgabe|task)|neue(s|r)? (aufgabe|task|thema|feature|ticket)|wechsle (zu|nach)|anderes thema|stattdessen)\b'; then
  is_switch=1
fi

[ "$is_switch" -eq 0 ] && exit 0

# --- Is anything actually waiting to be parked? ---------------------------
DIRTY_OUTPUT=$(git -C "$CWD" status --porcelain 2>/dev/null)

UNPUSHED_COUNT=0
HAS_UPSTREAM=0
if git -C "$CWD" rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
  HAS_UPSTREAM=1
  UPSTREAM=$(git -C "$CWD" rev-parse --abbrev-ref --symbolic-full-name '@{u}')
  UNPUSHED_COUNT=$(git -C "$CWD" rev-list --count "$UPSTREAM..HEAD" 2>/dev/null || echo 0)
fi

# Nothing to do? exit silently.
DIRTY_FILE_COUNT=$(printf '%s' "$DIRTY_OUTPUT" | grep -c . || true)
if [ "$DIRTY_FILE_COUNT" -eq 0 ] && [ "$UNPUSHED_COUNT" -eq 0 ]; then
  exit 0
fi

# --- Compose directive ----------------------------------------------------
TRUNK_RAW=$(git -C "$CWD" rev-parse --abbrev-ref origin/HEAD 2>/dev/null || true)
TRUNK=$(printf '%s' "$TRUNK_RAW" | sed 's|^origin/||')
[ -z "$TRUNK" ] && TRUNK="main"

if [ "$HAS_UPSTREAM" -eq 0 ]; then
  UPSTREAM_LINE="Upstream: <none — branch not yet pushed>"
else
  UPSTREAM_LINE="Upstream: $UPSTREAM ($UNPUSHED_COUNT unpushed commit(s))"
fi

cat <<DIRECTIVE
[topic-switch-guard]
The user's new prompt looks like a topic switch, and there is work that should be parked first.

Branch:   $BRANCH
Trunk:    $TRUNK
Status:   $DIRTY_FILE_COUNT file(s) with uncommitted changes
$UPSTREAM_LINE

BEFORE you start working on the new prompt, follow this protocol:

1. Briefly summarise what's currently in flight (1–2 sentences from \`git status\` + last \`git log\`).
2. ASK the user (one combined question, not three):
   a. Should I commit the current changes? (yes / no / show diff)
   b. If yes, should I also open a PR and merge it to "$TRUNK" before switching topic? (yes / no / draft only)
3. Wait for the user's answer. Do not start the new task before they respond.
4. Execute their choice using the existing slash commands when possible:
   - commit only       → /git.commit
   - commit + draft PR → /git.commit then /git.pr (draft)
   - commit + PR + merge to trunk → /git.commit, /git.pr, then \`gh pr merge --squash --delete-branch\` (with confirmation)
   - skip              → just acknowledge and proceed
5. After the user's chosen step completes (or they skip), THEN start work on the new prompt.

If the user already addressed this in the same prompt (e.g. "commit and then..."), skip the question and just execute.
If the user explicitly says "ignore the guard" or "just do X", honour that.
DIRECTIVE

exit 0
