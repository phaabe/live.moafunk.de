---
description: Update CHANGELOG.md from commits since the last release tag.
argument-hint: [next-version, e.g. 1.4.0]
allowed-tools: Bash(git:*), Read, Edit
model: opus
---

# /changelog

## Workflow

1. Locate `CHANGELOG.md` (create from scratch if missing, using Keep a Changelog format).
2. Find the last release: parse the most recent `## [version]` heading, or fall back to `git describe --tags --abbrev=0`.
3. Get commits since: `git log <last-tag>..HEAD --pretty=format:'%H|%s|%b' --no-merges`.
4. Group commits by Conventional Commit type:
   - `feat` → **Added**
   - `fix` → **Fixed**
   - `perf` → **Performance**
   - `refactor` → **Changed**
   - `docs` / `chore` / `ci` / `build` → omit unless user-visible
   - `BREAKING CHANGE:` → **Breaking changes**
5. Determine next version from $ARGUMENTS or, if absent, infer (semver: breaking → major, feat → minor, fix → patch).
6. Insert a new `## [<version>] - YYYY-MM-DD` section above the previous one.
7. Diff and confirm with the user before writing.

Next version: $ARGUMENTS
