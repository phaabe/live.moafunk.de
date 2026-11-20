const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const { chmodSync, copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } = require('node:fs');
const { createRequire } = require('node:module');
const { tmpdir } = require('node:os');
const path = require('node:path');
const { test } = require('node:test');

const frontendRequire = createRequire(path.resolve(__dirname, '../../../frontend/package.json'));
const yaml = frontendRequire('js-yaml');
const load = (file) => yaml.load(readFileSync(path.resolve(__dirname, '..', file), 'utf8'));
const backup = load('backup.yml').jobs.backup;
const monitoring = load('backend.yml').jobs['deploy-monitoring'];
const prodHostKey = load('deploy-production.yml').jobs.deploy.steps.find((s) => s.name === 'Deploy').env.PROD_HOST_KEY;
const repo = path.resolve(__dirname, '../../..');

function bash(script, env) {
  return execFileSync('/bin/bash', ['--noprofile', '--norc', '-e', '-c', script], {
    env: { PATH: '/usr/bin:/bin', ...env },
    stdio: 'pipe',
  }).toString();
}

test('a manual run does only the parts whose box is checked', () => {
  const step = backup.steps.find((s) => s.id === 'backup-type');
  assert.doesNotMatch(step.run, /\$\{\{/, 'values come from env, not from expressions in the script');
  const dir = mkdtempSync(path.join(tmpdir(), 'backup-type-'));
  const run = (env) => {
    const out = path.join(dir, 'out');
    writeFileSync(out, '');
    bash(step.run, { GITHUB_OUTPUT: out, IN_DB: '', IN_R2: '', IN_FULL: '', ...env });
    return Object.fromEntries(readFileSync(out, 'utf8').trim().split('\n').map((l) => l.split('=')));
  };
  try {
    const manual = { EVENT: 'workflow_dispatch' };
    assert.deepEqual(run({ ...manual, IN_DB: 'false', IN_R2: 'true', IN_FULL: 'false' }), {
      backup_db: 'false', backup_r2: 'true', full_r2: 'false', trigger: 'manual',
    });
    assert.deepEqual(run({ ...manual, IN_DB: 'true', IN_R2: 'false', IN_FULL: 'true' }), {
      backup_db: 'true', backup_r2: 'false', full_r2: 'true', trigger: 'manual',
    });
    assert.deepEqual(run({ ...manual }), { backup_db: 'true', backup_r2: 'true', full_r2: 'false', trigger: 'manual' });
    assert.equal(run({ EVENT: 'schedule', IN_DB: 'false' }).backup_db, 'true');
    assert.equal(run({ EVENT: 'repository_dispatch' }).backup_r2, 'false');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
  assert.equal(step.env.IN_DB, '${{ inputs.backup_db }}');
  assert.equal(step.env.IN_R2, '${{ inputs.backup_r2 }}');
  assert.equal(step.env.IN_FULL, '${{ inputs.full_r2_backup }}');
  assert.equal(backup.steps.find((s) => s.name === 'Run database backup').if, "steps.backup-type.outputs.backup_db == 'true'");
  assert.equal(backup.steps.find((s) => s.name === 'Run R2 backup').if, "steps.backup-type.outputs.backup_r2 == 'true'");
});

test('backup and monitoring reach production only with the pinned host key', () => {
  assert.match(prodHostKey, /^ssh-ed25519 \S+$/);
  for (const [name, job] of [['backup', backup], ['deploy-monitoring', monitoring]]) {
    const text = JSON.stringify(job);
    assert.doesNotMatch(text, /ssh-keyscan|StrictHostKeyChecking=(no|accept-new)/, name);
    const setup = job.steps.find((s) => s.name === 'Setup SSH key');
    assert.equal(setup.env.PROD_HOST_KEY, prodHostKey, `${name}: same key as deploy-production.yml`);
    assert.match(setup.run, /printf '%s %s\\n' "\$HETZNER_IP" "\$PROD_HOST_KEY" > ~\/\.ssh\/prod_known_hosts/);
  }
  const sshSteps = backup.steps.filter((s) => /\bssh\b/.test(s.run ?? '') && s.name !== 'Setup SSH key');
  assert.equal(sshSteps.length, 3);
  for (const s of sshSteps) {
    assert.match(s.run, /-o UserKnownHostsFile="\$HOME\/\.ssh\/prod_known_hosts"\s+\\\s+-o StrictHostKeyChecking=yes/, s.name);
  }
  assert.match(monitoring.steps.find((s) => s.name === 'Deploy observability stack').run, /SSH_KNOWN_HOSTS=~\/\.ssh\/prod_known_hosts/);
  // ssh keeps the first value it gets for an option, so the script must not
  // also pass accept-new when a pinned file is given.
  const script = readFileSync(path.join(repo, 'backend/scripts/deploy_monitoring.sh'), 'utf8');
  const opts = bash(`${script.match(/^if \[\[ -n "\$\{SSH_KNOWN_HOSTS:-\}" \]\]; then[\s\S]*?^fi$/m)[0]}\necho "\${SSH_OPTS[*]}"`, {
    SSH_KEY: 'k',
    SSH_KNOWN_HOSTS: '/kh',
  });
  assert.equal(opts.trim(), '-i k -o UserKnownHostsFile=/kh -o StrictHostKeyChecking=yes');
});

// Runs a copy of backup-r2.sh (so no real backend/.env is loaded) with a fake
// rclone that logs its calls and answers size/lsf from env values.
function runBackupR2(env, args = ['--full']) {
  const dir = mkdtempSync(path.join(tmpdir(), 'backup-r2-'));
  const scriptDir = path.join(dir, 'backend/scripts/backup');
  const bin = path.join(dir, 'bin');
  mkdirSync(scriptDir, { recursive: true });
  mkdirSync(bin);
  copyFileSync(path.join(repo, 'backend/scripts/backup/backup-r2.sh'), path.join(scriptDir, 'backup-r2.sh'));
  writeFileSync(
    path.join(bin, 'rclone'),
    [
      '#!/bin/bash',
      'echo "$*" >>"$RCLONE_LOG"',
      'case "$1" in',
      '  size) [[ "$2" == *r2-snapshots* ]] && n="$FAKE_SNAPSHOT_COUNT" || n="$FAKE_SOURCE_COUNT"',
      '        echo "{\\"count\\":$n,\\"bytes\\":1000}" ;;',
      '  lsf) printf "%b" "$FAKE_SNAPSHOTS" ;;',
      'esac',
    ].join('\n')
  );
  chmodSync(path.join(bin, 'rclone'), 0o755);
  const log = path.join(dir, 'rclone.log');
  writeFileSync(log, '');
  let status = 0;
  try {
    execFileSync('/bin/bash', [path.join(scriptDir, 'backup-r2.sh'), ...args], {
      env: { PATH: `${bin}:${process.env.PATH}`, RCLONE_LOG: log, FAKE_SNAPSHOTS: '', ...env },
      stdio: 'pipe',
    });
  } catch (error) {
    status = error.status;
  }
  const calls = readFileSync(log, 'utf8').trim().split('\n');
  rmSync(dir, { recursive: true, force: true });
  return { status, calls, verbs: calls.map((c) => c.split(' ')[0]) };
}

test('backup-r2.sh refuses to sync an empty source, full or incremental', () => {
  for (const args of [['--full'], []]) {
    const { status, verbs } = runBackupR2({ FAKE_SOURCE_COUNT: '0', FAKE_SNAPSHOT_COUNT: '0' }, args);
    assert.notEqual(status, 0, `${args}: non-zero exit`);
    assert.ok(!verbs.includes('sync') && !verbs.includes('purge'), `${args}: ${verbs}`);
  }
});

test('backup-r2.sh keeps old snapshots when the new one is too small', () => {
  const { status, verbs } = runBackupR2({
    FAKE_SOURCE_COUNT: '100',
    FAKE_SNAPSHOT_COUNT: '89',
    FAKE_SNAPSHOTS: '2026-11-19/\\n2026-11-12/\\n2026-11-05/\\n',
  });
  assert.notEqual(status, 0);
  assert.ok(verbs.includes('sync'));
  assert.ok(!verbs.includes('purge'), `${verbs}`);
});

test('backup-r2.sh syncs a full snapshot and purges the oldest beyond retention', () => {
  const { status, calls } = runBackupR2({
    FAKE_SOURCE_COUNT: '100',
    FAKE_SNAPSHOT_COUNT: '90',
    FAKE_SNAPSHOTS: '2026-11-05/\\n2026-11-19/\\n2026-11-12/\\n',
  });
  assert.equal(status, 0);
  assert.ok(calls.some((c) => /^sync r2-prod:unheard-artists-prod r2-backup:unheard-backups\/r2-snapshots\/\d{4}-\d\d-\d\d /.test(c)));
  assert.deepEqual(
    calls.filter((c) => c.startsWith('purge')),
    ['purge r2-backup:unheard-backups/r2-snapshots/2026-11-05/']
  );
  const incremental = runBackupR2({ FAKE_SOURCE_COUNT: '5', FAKE_SNAPSHOT_COUNT: '0' }, []);
  assert.equal(incremental.status, 0);
  assert.ok(incremental.calls.some((c) => c.startsWith('sync r2-prod:unheard-artists-prod r2-backup:unheard-backups/r2-latest ')));
  assert.ok(!incremental.verbs.includes('purge'));
});
