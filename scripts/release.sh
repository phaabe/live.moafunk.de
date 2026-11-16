#!/usr/bin/env bash
# Release helper (docs/deployment.md). Run from the repo root on your Mac, with
# git and gh logged in as someone with write access.
#
#   scripts/release.sh pr rN <commit>   cut release/rN from the commit staging runs
#                                       (staging.sh status) and open the release PR
#                                       into main, with the notes since the last r* tag
#   scripts/release.sh tag rN           after the release PR is merged with a merge
#                                       commit: tag rN on it and create the GitHub Release
set -euo pipefail

REPO="${RELEASE_REPO:-phaabe/live.moafunk.de}"
die() { echo "release: $*" >&2; exit 1; }
usage() { die "usage: $0 pr rN <commit> | $0 tag rN"; }

cmd="${1:-}"
name="${2:-}"
[[ "$cmd" == pr || "$cmd" == tag ]] || usage
[[ "$name" =~ ^r[0-9]+$ ]] || usage
branch="release/$name"

git fetch -q --tags origin main staging

# The last release before this one: the highest rM tag with M < N.
prev="$(git tag -l 'r*' | sed -n 's/^r\([0-9][0-9]*\)$/\1/p' |
  awk -v n="${name#r}" '$1 + 0 < n + 0' | sort -n | tail -1)"
[[ -n "$prev" ]] || die "no release tag before $name"
prev="r$prev"

notes="$(mktemp)"
trap 'rm -f "$notes"' EXIT

# Notes for prev..target, grouped by the release::* labels (.github/release.yml).
write_notes() {
  gh api "repos/$REPO/releases/generate-notes" -f tag_name="$name" -f target_commitish="$1" \
    -f previous_tag_name="$prev" | jq -er .body >"$notes"
}

case "$cmd" in
  pr)
    want="${3:-}"
    want="${want#sha-}" # staging.sh status prints the tag sha-<commit>
    [[ "$want" =~ ^[0-9a-f]{7,40}$ && $# -eq 3 ]] || usage
    commit="$(git rev-parse --verify --quiet "$want^{commit}")" || die "$want is not a commit here"
    git merge-base --is-ancestor "$commit" origin/staging || die "$commit is not on staging"
    git rev-parse --verify --quiet "refs/tags/$name" >/dev/null && die "tag $name exists already"
    [[ -z "$(git ls-remote --heads origin "$branch")" ]] || die "$branch exists already"
    git push -q origin "$commit:refs/heads/$branch"
    write_notes "$branch"
    gh pr create --repo "$REPO" --base main --head "$branch" --title "Release $name" --body-file "$notes"
    echo "Merge it with a merge commit (never squash) when every check is green, then: $0 tag $name"
    ;;
  tag)
    [[ $# -eq 2 ]] || usage
    pr="$(gh api "repos/$REPO/pulls?state=closed&base=main&head=${REPO%%/*}:$branch" |
      jq -r '[.[] | select(.merged_at != null)][0] // empty | "\(.number) \(.merge_commit_sha) \(.head.sha)"')"
    read -r number merge release_head <<<"$pr" || true
    [[ "${number:-}" =~ ^[0-9]+$ ]] || die "no merged PR from $branch into main"
    git merge-base --is-ancestor "$merge" origin/main || die "$merge is not on main"
    # A merge commit has the release head as its second parent. A squash has one parent.
    read -r _ _ second extra <<<"$(git rev-list --parents -n 1 "$merge")" || true
    [[ "${second:-}" == "$release_head" && -z "${extra:-}" ]] ||
      die "PR $number was not merged with a merge commit; see docs/deployment.md"
    write_notes "$release_head"
    git tag -a "$name" "$merge" -m "Release $name"
    git push -q origin "refs/tags/$name"
    gh release create "$name" --repo "$REPO" --verify-tag --title "Release $name" --notes-file "$notes"
    ;;
esac
