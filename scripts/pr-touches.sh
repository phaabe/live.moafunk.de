#!/usr/bin/env bash
# Prints true when a CI job must run: always, except on a PR into staging
# that changes no file matching the extended regex $1. Lets PRs into staging
# skip slow jobs they cannot affect. A job skipped by its `if` reports
# success, so its check can stay required.
#
# Env: EVENT (github.event_name), BASE_REF (github.base_ref).
# On a PR, HEAD must be the PR's merge commit with its parents fetched
# (actions/checkout with fetch-depth: 2). Its first parent is the base.
set -euo pipefail

pattern=${1:?usage: pr-touches.sh <extended-regex>}

if [[ "${EVENT:-}" != pull_request || "${BASE_REF:-}" != staging ]]; then
  echo true
  exit 0
fi

# No pipe into grep -q: with pipefail, an early grep exit fails git diff.
files="$(git diff --name-only HEAD^1 HEAD)"
if grep -qE -- "$pattern" <<<"$files"; then
  echo true
else
  echo false
fi
