# Deployment

## Branches and servers

| Branch | Server | How code gets there |
|--------|--------|---------------------|
| feature branch | none | CI only, through a PR into `staging` |
| `staging` | https://staging.live.moafunk.de (Hetzner `cx23` `moafunk-staging`) | automatically, on every push (`backend.yml`, job `deploy-staging`) |
| `main` | https://admin.live.moafunk.de (production) | by hand, with the workflow **DEPLOY - Release to production** |

The admin SPA ships inside the backend image, so one image covers API and admin. The public site on GitHub Pages deploys from `main` (`frontend.yml`).

## Rules

1. Every change is a PR into `staging`, squash merged. Each PR has exactly one label `release::feature`, `release::fix` or `release::internal` (check `release-label`). The label sorts the PR into the release notes; `release::internal` is left out. Nobody commits to `staging` or `main` directly. Only release PRs (`release/rN`) and hotfix PRs (`hotfix/<slug>`) go into `main` (check `release-gate`).
2. Every merge into `staging` builds an image `sha-<7>` and puts exactly that image on staging (`backend.yml`, job `deploy-staging`). On hold, the deploy does not happen. A merge never waits for the chaos test show: it runs every night on what staging runs, and on each release PR (rule 3). The nightly runs start from `nightly.yml` (02:00 UTC the SoundCloud test, 02:30 the chaos test show), which runs `e2e-soundcloud.yml` and `e2e-staging.yml` on the `staging` branch, so they use the workflow files and tests from `staging`. GitHub runs a schedule, and **Run workflow**, only for a workflow file that is on `main`; the run then uses the file from the branch it names. A CI-only change (for example a new workflow file) reaches `main` with the next release, or at once with a hotfix PR (rule 5). PRs that change `e2e/` run only its typecheck and unit tests (`e2e-unit.yml`). On a PR into `staging`, `backend-ci` is skipped when the PR changes nothing it checks, and `stream-system` when it changes nothing in the stream chain (`scripts/pr-touches.sh`). A skipped check counts as passed. Both always run on PRs into `main`. After a merge, `backend-ci` always runs on `staging`, and `stream-system` runs when backend code changed.
3. A release goes like this. `rN` is the next number after the last `r*` tag (`r0` was the first).
   1. On the staging box: `/opt/unheard-staging/staging.sh hold "rN release checks"`. Builds go on, but staging keeps the release candidate. Until `unhold`, nobody deploys, stops or restarts staging by hand: `staging.sh up` and `down` refuse during a hold (`STAGING_IGNORE_HOLD=1` overrides), and plain `docker compose` is not allowed. `staging.sh status` prints the tag it runs (`sha-<7>`): that commit is the release.
   2. The restore drill (below): put the newest production backup into staging.
   3. `scripts/release.sh pr rN sha-<7>`. It cuts `release/rN` from that commit and opens the release PR `release/rN` → `main`. The PR body holds the release notes since the last tag, grouped by the `release::*` labels. Note the backup's name in a comment on the PR.
   4. The release PR runs the chaos test show as a 90-minute soak on staging (**TEST - Release soak**, `e2e-release.yml`). It first checks that staging runs exactly the image of the release commit, so keep the hold until the merge. `release-gate` fails when the PR opens. After a green soak, `e2e-release.yml` re-runs it, and it passes. A run by hand or the nightly run does not count. If the soak failed, fix the cause and re-run its failed jobs (the PR's **TEST - Release soak** run → Re-run failed jobs). The soak waits while a nightly run is on staging; a merge into `staging` in that time cancels the waiting soak (one waiting job per concurrency group), so re-run it then. When every check is green, merge it with a **merge commit**, never a squash: `gh pr merge <n> --merge --delete-branch --match-head-commit <sha>`. A squash would put a commit on `main` that `staging` never has, so the next release would conflict and its notes would miss the PRs.
   5. `scripts/release.sh tag rN`. It tags `rN` on the merge commit and creates the GitHub Release with the same notes.
   6. When the `main` image build is green, run **DEPLOY - Release to production** with `dry_run` on, then again with `dry_run` off.
   7. `/opt/unheard-staging/staging.sh unhold`.
4. Deploy to production only release commits on `main` that passed the checks on staging, or a hotfix (rule 5). Never during a show or in the 2 hours before one. The best time is soon after a show, so there is a week to fix problems.
5. A hotfix skips the chaos test and goes to `main` at once:
   1. Branch `hotfix/<slug>` from `main`. Open a PR into `main` with the label `hotfix`. The unit tests must pass; the chaos test does not run (`release-gate` checks only the branch name and the label).
   2. Squash merge it: `gh pr merge <n> --squash --delete-branch --match-head-commit <sha>`.
   3. When the `main` image build is green, run **DEPLOY - Release to production** with `hotfix` on, first with `dry_run` on. It skips only the staging check, and only while `main`'s head commit is that hotfix merge.
   4. Right after: the same fix as a normal PR into `staging`, label `release::fix`. Cherry-pick the commit from `main` onto a branch from `origin/staging` (`git cherry-pick <commit>`). Without it, the next release conflicts with the fix or removes it.
6. Issue labels show where a ticket's change runs (`scripts/deployed-labels.sh`). An issue counts when a merged PR links it with its full URL on a line that starts with `Refs:`, `Closes:`, `Fixes:`, `Issue:` or `Ticket:`. After each merge into `staging`, its issues get `deployed::staging` (**OPS - Label issues on staging**). After a real production deploy (not a dry run, not a rollback), the job `label-issues` gives the issues of the commits since the `r*` tag before `main`'s head `deployed::production`. Each issue has one `deployed::*` label at a time. A release that was never deployed keeps `deployed::staging`; then run `REPO=phaabe/live.moafunk.de scripts/deployed-labels.sh production rM..rN` by hand (`DRY_RUN=1` first).
7. Rollback: run **DEPLOY - Release to production** with `rollback_tag` set to the earlier tag (the deploy log prints it). This skips the staging check but keeps all other checks. Restore the DB backup only if a migration broke data; migrations only go forward.

## What the production deploy checks

It stops, and changes nothing, when:

- it does not run from `main`, or someone other than Anton (`anneoneone`) or phaabe starts it, or the confirmation is not `deploy production`
- the code on `main` is not exactly the code that staging runs (it asks the staging box which tag it runs and compares the git trees). With `hotfix` on, it skips only this check, and only when `main`'s head commit is the merge of a PR with label `hotfix`. Otherwise the run stops.
- the image for the commit is not built yet, or the pulled image does not have the expected digest
- a show is on air, or a show starts or runs within the next 2 hours
- another deploy is running

Then it backs up the DB (SQLite backup API), starts the new image, waits until it is healthy, checks `/health` and `/api/stream/status`, and prints the rollback. The host part is `backend/deploy/deploy-production.sh`; it also runs by hand over ssh (see its header).

After a real deploy (not a dry run) the run ends with a read-only smoke test from outside, and fails if one check fails:

- `https://admin.live.moafunk.de/health` says healthy, and `/api/stream/status` answers
- the admin login page and its app script load
- `unheard-api` runs exactly the image digest this run deployed. That image is `main`'s build of the code that staging ran (checked before the deploy). Staging ran its own build of the same code, so its digest differs.

## Show-day check

`backend/deploy/showday-check.sh` runs on the production box every 15 minutes from a systemd timer. The day before a show (from 18:00 Berlin) and in the hour before it, it checks the box and posts the result to the MoafunkBot topic of the Telegram group, with the backend's own bot settings (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_ADMIN_CHAT_ID`, `TELEGRAM_TOPIC_ID` in `/opt/unheard-backend/.env`). Each show gets each post once. It needs no CI key and changes nothing.

It checks: the API is healthy, `https://admin.live.moafunk.de` answers, the TLS certificate is valid for 14 more days, Icecast has `/live.mp3` and `/test.mp3`, Liquidsoap runs and takes audio on both harbor mounts `/test` and `/live` (an upload without a password must get 401; 404 means the mount is missing, and `systemctl restart liquidsoap` brings it back), 5 GB are free, and the newest DB backup is at most 30 hours old. The post also says if a pre-recorded set is not confirmed.

Install it once (Anton, from the repo root on your Mac):

```bash
scp backend/deploy/showday-check.sh root@178.104.160.103:/usr/local/sbin/showday-check.sh
ssh root@178.104.160.103 'bash -s' <<'EOF'
set -euo pipefail
chmod 755 /usr/local/sbin/showday-check.sh
cat >/etc/systemd/system/showday-check.service <<'UNIT'
[Unit]
Description=Moafunk show-day check (posts to the MoafunkBot topic)
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/showday-check.sh
UNIT
cat >/etc/systemd/system/showday-check.timer <<'UNIT'
[Unit]
Description=Run the Moafunk show-day check every 15 minutes

[Timer]
OnCalendar=*:0/15
Persistent=true

[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now showday-check.timer
systemctl list-timers showday-check.timer --no-pager
EOF
```

Try it: `ssh root@178.104.160.103 'SHOWDAY_FORCE=1 /usr/local/sbin/showday-check.sh'` checks now and posts once for every show of today and tomorrow (nothing when there is none). A failed check or post shows in `systemctl status showday-check`. After a change to the script, copy it again (the first line above).

## Restore drill

Before each release, prove that the latest production backup (`backup.yml`, nightly to `r2-backup:unheard-backups/db/`) restores and works: put it into staging before you open the release PR, so the release soak runs on it. Staging is on hold for the release checks, so no deploy replaces the data meanwhile. The backup is scrubbed on the production box before it leaves, as for any copy of production data (`backend/staging/README.md`).

```bash
# 1. The newest backup, unpacked on the production box
ssh root@178.104.160.103 'set -e; f="$(rclone lsf r2-backup:unheard-backups/db/ --files-only | sort | tail -1)"; echo "backup: $f" >&2; rclone cat "r2-backup:unheard-backups/db/$f" | gunzip >/tmp/restore-drill.db'
# 2. Scrubbed there, then put into staging
ssh root@178.104.160.103 'PROD_DB=/tmp/restore-drill.db bash -s -- export-db' < backend/staging/staging.sh \
  | ssh -F ~/.config/moafunk-staging/ssh_config staging '/opt/unheard-staging/staging.sh seed-db -'
ssh root@178.104.160.103 'rm -f /tmp/restore-drill.db'
```

3. Open the release PR (rule 3, step 3). Its 90-minute soak runs on this data and must pass. Log in as `staging-admin` and look at a few episodes and Shows from production.
4. Note the backup's name in the release PR.

## Keys

| Key | Can do | Stored |
|-----|--------|--------|
| staging CI key (`backend/staging/ci-deploy.pub`) | only `staging.sh ci-deploy` on the staging box: read the tag and hold, or deploy one tag with its digest | Bitwarden Secrets Manager `STAGING_DEPLOY_SSH_KEY` |
| production key | root on the production box | Bitwarden Secrets Manager `HETZNER_SSH_KEY` |

The staging box has no key for production, and the staging key has no access to production.

## Not set up yet

These need a repository admin (phaabe), see https://github.com/phaabe/live.moafunk.de/issues/890:

- ruleset on `staging`: PR required, squash only, required checks `backend-ci`, `frontend-ci`, `stream-system` and `release-label`
- ruleset on `main`: PR required, merge commit and squash allowed, required checks `backend-ci`, `frontend-ci`, `stream-system` and `release-gate`, no direct push, no bypass
- required approvers on the environment `api-production`

**DEPLOY - Release to production** can be started only after its workflow file is on `main`, so the first release that contains it also brings it.
