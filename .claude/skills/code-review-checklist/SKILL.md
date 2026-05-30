---
name: code-review-checklist
description: Use during any code review or self-review of a diff. Comprehensive checklist for security, performance, readability, and design.
---

# Code Review Checklist Skill

Apply in this order. Stop at the first severity that matters and fix before continuing.

## 1. Correctness  (must)

- [ ] Off-by-one / loop bounds correct.
- [ ] Null / undefined / None / nil paths handled.
- [ ] Errors are caught at the **right level** — not too broad, not swallowed.
- [ ] Race conditions: any shared mutable state? any double-fetch? any unsynchronised init?
- [ ] Resource lifecycle: files / connections / contexts closed (use `with` / `defer` / `using`).
- [ ] State machines: every transition reachable; no impossible states representable.

## 2. Security  (must)

- [ ] No secrets in diff (check `.env*`, hardcoded keys, default passwords).
- [ ] User input → never directly into SQL, shell, eval, template engine HTML, file paths.
- [ ] Authn: every protected endpoint actually wraps `requireAuth()`.
- [ ] Authz: just because a user is logged in does not mean they can see / edit *this* resource. Check object-level permissions.
- [ ] Crypto: no MD5 / SHA1 for security; CSPRNG (`crypto.randomBytes`, `secrets.token_bytes`) for tokens; bcrypt / argon2 for passwords.
- [ ] Dependency CVEs: any new / bumped dep — `npm audit` / `pip-audit` / `cargo audit`.

## 3. Tests  (must for new behaviour)

- [ ] Happy path covered.
- [ ] At least one error / edge case covered.
- [ ] Bug fix → regression test that fails without the fix and passes with it.
- [ ] No `skip` / `xfail` added without justification.
- [ ] Tests are deterministic — no `time.sleep`, no real network, no random without seed.

## 4. API design  (when changing public surface)

- [ ] Names are precise and consistent with siblings.
- [ ] Backward compatible (or major version bump justified).
- [ ] Errors have a typed contract — consumers can branch on them.
- [ ] Optionals have sensible defaults.
- [ ] Async functions actually need to be async.

## 5. Performance  (when in hot path)

- [ ] No N+1 queries (loops doing per-item DB hits).
- [ ] No quadratic loops where linear suffices.
- [ ] No unbounded memory growth (streaming where possible).
- [ ] No sync I/O in async / event-loop code.
- [ ] Caching with explicit TTL / invalidation.
- [ ] Indexes match query patterns.

## 6. Readability  (should)

- [ ] Function ≤ ~40 lines. If longer, justified.
- [ ] Cyclomatic complexity ≤ ~10.
- [ ] Names: precise > generic. `userId` > `id`. `parsedSchema` > `data`.
- [ ] Magic numbers → named constants.
- [ ] Comments explain *why*, not *what*.
- [ ] Dead code removed (commented-out, unused imports, unreachable branches).

## 7. Style  (must match project — never your taste)

- [ ] Formatter clean (`prettier --check`, `ruff format --check`, `gofmt -l`).
- [ ] Linter clean (`eslint`, `ruff check`, `golangci-lint`, `clippy -- -D warnings`).
- [ ] Imports ordered consistently with the rest of the project.

## Severity guide

- **Must fix** → blocks merge: any correctness, security, or test gap.
- **Should fix** → raise before merge: API consistency, perf in hot path, naming.
- **Nit** → optional, mention but don't block: style, minor cleanups.
