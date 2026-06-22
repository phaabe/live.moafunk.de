# Feedback widget (dev/review)

A drop-in **feedback layer** for the dev/review process, mounted on the **admin
dashboard only**. Reviewers click a bug button, annotate a screenshot, and submit —
it becomes a **GitHub issue** with the screenshot plus page URL and browser/viewport
context. The public pages are intentionally never touched.

- **Tool:** [BugDrop](https://github.com/mean-weasel/bugdrop) — MIT, open source.
- **Where feedback lands:** GitHub Issues in a target repo (default: `phaabe/live.moafunk.de`).
- **Hosting:** the free hosted Cloudflare Worker (`bugdrop.neonwatty.workers.dev`).
  No GitHub token lives in the browser — a GitHub App does the token exchange
  server-side. Self-hosting the Worker is possible later (see below).
- **Scope:** dev/review **only**. Never injected into production.

## One-time setup (org admin)

1. Install the **BugDrop GitHub App** on the target repo from the
   [Marketplace listing](https://github.com/marketplace/bugdrop-in-app-feedback-to-github-issues).
   Works with private repos and branch-protection rules. On first feedback it
   auto-creates a `bugdrop-screenshots` branch to hold screenshot images.

## How it's wired

A conditional Vite plugin (`bugdropFeedbackWidget` in `frontend/vite.config.ts`)
injects the widget's `<script>` tag into the **admin SPA** (`src/admin/index.html`)
only — never the public pages — and only when `VITE_FEEDBACK_WIDGET` is set to a
GitHub `owner/repo`. Production builds (GitHub Pages, admin Docker) never set the
var, so the tag is absent from those bundles.

## Enabling it

**Local dev** — add to `frontend/.env.local` (gitignored):

```
VITE_FEEDBACK_WIDGET=phaabe/live.moafunk.de
```

Then `npm run dev` (or `npm run build` for a static review build). Unset it to turn
the widget off.

**Review/preview build** — set the var for that build only, e.g.:

```
VITE_FEEDBACK_WIDGET=phaabe/live.moafunk.de npm run build
```

## Triaging incoming feedback

Issues arrive in the target repo. Apply this repo's taxonomy when triaging:
one or more `type::*` (`backend` / `frontend` / `ci`) plus the most specific
`project::*` area (e.g. `public-pages`, `admin_dashboard`, `Stream`). Use `later`
for backlog items.

## Privacy

Add `data-bugdrop-mask` to any element that could expose sensitive data in a
screenshot — password/credit-card inputs are masked automatically, but stream keys
and user PII are not. Candidate spots in the admin SPA: `UserEditPage.vue`,
`UsersPage.vue`, `ChangePasswordPage.vue`, and any stream-key field in the show
wizard (`WizardHost.vue`). Apply masks as those become reachable in review builds.

## Verifying

- Local: `npm run dev` with the var set → open `/admin/`, the bug button appears (and is absent on the public `/`); submit → issue lands in the repo with screenshot + context.
- Prod guard: `npm run build` **without** the var, then `grep -r bugdrop dist` → no matches.

## Self-host upgrade path (optional)

Self-host the BugDrop Cloudflare Worker for full data ownership. Configure via env
(`GITHUB_APP_NAME`, `ALLOWED_ORIGINS`, `MAX_SCREENSHOT_SIZE_MB`, optional
short-lived `AUTH_TOKEN_*` to restrict who can file), then point the plugin's script
`src` at your Worker. No widget-side code change.
