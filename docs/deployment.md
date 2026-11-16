# Deployment

## Branches and servers

| Branch | Server | How code gets there |
|--------|--------|---------------------|
| feature branch | none | CI only, through a PR into `staging` |
| `staging` | https://staging.live.moafunk.de (Hetzner `cx23` `moafunk-staging`) | automatically, on every push (`backend.yml`, job `deploy-staging`) |
| `main` | https://admin.live.moafunk.de (production) | by hand, with the workflow **Deploy production** |

The admin SPA ships inside the backend image, so one image covers API and admin. The public site on GitHub Pages deploys from `main` (`frontend.yml`).

## Rules

1. Every change is a PR into `staging`. Nobody commits to `staging` or `main` directly.
2. Every merge into `staging` builds an image `sha-<7>` and puts exactly that image on staging, unless staging is on hold.
3. A release goes like this:
   1. On the staging box: `/opt/unheard-staging/staging.sh hold "R1 release checks"`. Builds go on, but staging keeps the release candidate.
   2. Run the release checks on staging.
   3. Open the release PR `staging` → `main` (merge commit) and merge it.
   4. When the `main` image build is green, run **Deploy production** with `dry_run` on, then again with `dry_run` off.
   5. `/opt/unheard-staging/staging.sh unhold`.
4. Deploy to production only release commits on `main` that passed the checks on staging. Never during a show or in the 2 hours before one. The best time is soon after a show, so there is a week to fix problems.
5. Hotfixes take the same path, just faster: PR into `staging`, check on staging, release PR, production.
6. Rollback: run **Deploy production** with `rollback_tag` set to the earlier tag (the deploy log prints it). This skips the staging check but keeps all other checks. Restore the DB backup only if a migration broke data; migrations only go forward.

## What Deploy production checks

It stops, and changes nothing, when:

- it does not run from `main`, or someone other than Anton (`anneoneone`) or phaabe starts it, or the confirmation is not `deploy production`
- the code on `main` is not exactly the code that staging runs (it asks the staging box which tag it runs and compares the git trees)
- the image for the commit is not built yet, or the pulled image does not have the expected digest
- a show is on air, or a show starts or runs within the next 2 hours
- another deploy is running

Then it backs up the DB (SQLite backup API), starts the new image, waits until it is healthy, checks `/health` and `/api/stream/status`, and prints the rollback. The host part is `backend/deploy/deploy-production.sh`; it also runs by hand over ssh (see its header).

After a real deploy (not a dry run) the run ends with a read-only smoke test from outside, and fails if one check fails:

- `https://admin.live.moafunk.de/health` says healthy, and `/api/stream/status` answers
- the admin login page and its app script load
- `unheard-api` runs exactly the image digest this run deployed. That image is `main`'s build of the code that staging ran (checked before the deploy). Staging ran its own build of the same code, so its digest differs.

## Keys

| Key | Can do | Stored |
|-----|--------|--------|
| staging CI key (`backend/staging/ci-deploy.pub`) | only `staging.sh ci-deploy` on the staging box: read the tag and hold, or deploy one tag with its digest | Bitwarden Secrets Manager `STAGING_DEPLOY_SSH_KEY` |
| production key | root on the production box | Bitwarden Secrets Manager `HETZNER_SSH_KEY` |

The staging box has no key for production, and the staging key has no access to production.

## Not set up yet

These need a repository admin (phaabe):

- branch protection on `main` and `staging`: PR required, checks green
- required approvers on the environment `api-production`

**Deploy production** can be started only after its workflow file is on `main`, so the first release that contains it also brings it.
