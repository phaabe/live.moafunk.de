const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } = require('node:fs');
const { createRequire } = require('node:module');
const { tmpdir } = require('node:os');
const path = require('node:path');
const { test } = require('node:test');

const frontendRequire = createRequire(path.resolve(__dirname, '../../../frontend/package.json'));
const yaml = frontendRequire('js-yaml');
const load = (file) => yaml.load(readFileSync(path.resolve(__dirname, '..', file), 'utf8'));
const chaos = load('e2e-staging.yml');
const job = chaos.jobs['chaos-show'];
const release = load('e2e-release.yml');
const sha = `abc1234${'d'.repeat(33)}`;

// Runs a step's script with fake commands in bin ({ name: script }).
function runner(fakes) {
  const dir = mkdtempSync(path.join(tmpdir(), 'e2e-staging-'));
  const bin = path.join(dir, 'bin');
  mkdirSync(bin);
  for (const [name, script] of Object.entries(fakes)) {
    writeFileSync(path.join(bin, name), script);
    chmodSync(path.join(bin, name), 0o755);
  }
  const run = (script, env) =>
    execFileSync('/bin/bash', ['--noprofile', '--norc', '-e', '-c', script], {
      env: { PATH: `${bin}:${path.dirname(process.execPath)}:/usr/bin:/bin`, ...env },
      stdio: 'pipe',
    }).toString();
  return { dir, run, done: () => rmSync(dir, { recursive: true, force: true }) };
}

test('the chaos test show runs nightly, by hand and on release PRs, never on PRs into staging', () => {
  assert.deepEqual(Object.keys(chaos.on).sort(), ['schedule', 'workflow_call', 'workflow_dispatch']);
  assert.deepEqual(chaos.on.schedule, [{ cron: '30 2 * * *' }]);
  assert.deepEqual(chaos.on.workflow_call.inputs, {
    release_sha: { description: 'The release commit; staging must run exactly its image', type: 'string', required: true },
    duration: { description: 'Extra minutes on air', type: 'string', default: '0' },
  });
  assert.equal(job.env.DURATION, "${{ inputs.duration || '0' }}");
  // Only the release PR calls it, always with its head commit.
  const callers = readdirSync(path.resolve(__dirname, '..'))
    .filter((file) => /\.ya?ml$/.test(file))
    .flatMap((file) => Object.entries(load(file).jobs ?? {}).map(([id, j]) => [file, id, j]))
    .filter(([, , j]) => j.uses === './.github/workflows/e2e-staging.yml');
  assert.deepEqual(callers.map(([file, id]) => `${file}:${id}`), ['e2e-release.yml:e2e-staging']);
  // The nightly test comes from staging, a release run's from the release commit.
  assert.equal(
    job.steps[0].with.ref,
    "${{ github.event_name == 'schedule' && 'staging' || inputs.release_sha || '' }}"
  );
});

test('the chaos test show uses only the staging CI key and the e2e host login', () => {
  assert.deepEqual(chaos.permissions, { contents: 'read' });
  assert.deepEqual(job.concurrency, { group: 'staging-box', 'cancel-in-progress': false });
  const text = JSON.stringify(chaos);
  assert.deepEqual(
    [...text.matchAll(/secrets\.([A-Z_]+)/g)].map((match) => match[1]),
    ['BW_ACCESS_TOKEN']
  );
  assert.doesNotMatch(text, /HETZNER|178\.104\.160\.103|admin\.live|deploy_hetzner|scp/i);
  const keys = job.steps.find((step) => step.id === 'secrets');
  assert.deepEqual(
    keys.with.secrets.trim().split('\n').map((line) => line.split(' > ')[1]),
    ['STAGING_DEPLOY_SSH_KEY', 'STAGING_E2E_HOST_PASSWORD']
  );
  const hostKey = job.steps.find((step) => step.env?.HOST_KEY).env.HOST_KEY;
  const deploy = load('backend.yml').jobs['deploy-staging'].steps.find((step) => step.id === 'deploy');
  assert.equal(hostKey, deploy.env.HOST_KEY);
  const unhold = job.steps.find((step) => /e2e-unhold/.test(step.run ?? ''));
  assert.match(unhold.if, /always\(\)/, 'the hold is removed also after a failure');
});

test('a release run first checks that staging runs exactly the release image', () => {
  const names = job.steps.map((step) => step.name ?? step.uses);
  const check = job.steps.find((step) => step.name === 'Check staging runs the release commit');
  assert.ok(names.indexOf(check.name) > names.indexOf('Write the CI key and the pinned host key'));
  assert.ok(names.indexOf(check.name) < names.indexOf('actions/setup-node@v4'), 'fail fast, before the install');
  assert.equal(check.if, "inputs.release_sha != ''", 'nightly and manual runs test whatever staging runs');
  assert.deepEqual(check.env, { RELEASE_SHA: '${{ inputs.release_sha }}' });
  assert.match(check.run, /StrictHostKeyChecking=yes/);

  // Fake ssh: records its arguments, prints FAKE_STATUS, exits with FAKE_EXIT.
  const { dir, run, done } = runner({
    ssh: '#!/bin/bash\nprintf \'%s\\n\' "$@" >"$OUT/ssh-args"\nprintf \'%b\\n\' "$FAKE_STATUS"\nexit "${FAKE_EXIT:-0}"\n',
  });
  const check_ = (tag, env = {}) =>
    run(check.run, {
      OUT: dir,
      STAGING_SSH_KEY: '/k',
      STAGING_KNOWN_HOSTS: '/h',
      RELEASE_SHA: sha,
      FAKE_STATUS: `tag: ${tag}\\nhold: 2026-11-18T10:00Z | ${tag} | r2 release checks`,
      ...env,
    });
  const refuses = (tag, env, message = /staging runs .*, not the release sha-abc1234/) =>
    assert.throws(() => check_(tag, env), (err) => message.test(err.stdout.toString()));
  try {
    assert.match(check_('sha-abc1234'), /Staging runs the release: sha-abc1234/);
    const args = readFileSync(path.join(dir, 'ssh-args'), 'utf8').trim().split('\n');
    assert.deepEqual(args.slice(-2), ['root@staging.live.moafunk.de', 'status']);
    assert.ok(args.includes('/k') && args.includes('UserKnownHostsFile=/h'));
    check_(`sha-${sha}`);
    refuses('sha-abc1235');
    refuses(`sha-abc1234${'e'.repeat(33)}`);
    refuses('sha-abc123');
    refuses('none', {}, /staging runs none, not the release/);
    refuses('', { FAKE_STATUS: 'hold: no' }, /staging runs no image, not the release/);
    refuses('sha-abc1234', { RELEASE_SHA: 'abc1234' }, /release_sha must be a full commit hash/);
    assert.throws(() => check_('sha-abc1234', { FAKE_EXIT: '255' }), 'no answer from the box fails');
  } finally {
    done();
  }
});

test('a release PR into main runs the chaos test show as a 90-minute soak of its head commit', () => {
  assert.deepEqual(release.on, { pull_request: { branches: ['main'] } });
  assert.deepEqual(release.permissions, { contents: 'read' });
  assert.deepEqual(Object.keys(release.jobs), ['e2e-staging', 'rerun-release-gate']);
  const e2e = release.jobs['e2e-staging'];
  assert.equal(e2e.if, "startsWith(github.head_ref, 'release/')", 'hotfix PRs skip it');
  assert.equal(e2e.uses, './.github/workflows/e2e-staging.yml');
  assert.deepEqual(e2e.with, { release_sha: '${{ github.event.pull_request.head.sha }}', duration: '90' });
  assert.equal(e2e.secrets, 'inherit');
  // release-gate trusts exactly this check name.
  assert.match(load('release-gate.yml').jobs['release-gate'].steps[0].run, /name="e2e-staging \/ chaos-show"/);
});

test('a green chaos test show re-runs a failed release-gate, and nothing else', () => {
  const rerun = release.jobs['rerun-release-gate'];
  assert.equal(rerun.needs, 'e2e-staging');
  assert.equal(rerun.if, undefined, 'only after a green chaos test show');
  assert.deepEqual(rerun.permissions, { actions: 'write' });
  assert.doesNotMatch(JSON.stringify(rerun), /secrets\.|bitwarden|\bssh\b/i);
  assert.equal(rerun.steps.length, 1);
  const step = rerun.steps[0];
  assert.deepEqual(step.env, {
    GH_TOKEN: '${{ github.token }}',
    REPO: '${{ github.repository }}',
    HEAD_SHA: '${{ github.event.pull_request.head.sha }}',
  });

  // Fake gh: answers the runs list from FAKE_RUN (one run or none), records a re-run.
  const { dir, run, done } = runner({
    gh: `#!/usr/bin/env node
const fs = require('node:fs');
const args = process.argv.slice(2).join(' ');
const repo = 'repos/phaabe/live.moafunk.de/actions';
if (args === 'api -X GET ' + repo + '/workflows/release-gate.yml/runs -f head_sha=' + process.env.HEAD_SHA + ' -f event=pull_request -f per_page=1') {
  const runs = process.env.FAKE_RUN ? [JSON.parse(process.env.FAKE_RUN)] : [];
  console.log(JSON.stringify({ total_count: runs.length, workflow_runs: runs }));
} else if (args === 'api -X POST ' + repo + '/runs/77/rerun') {
  fs.writeFileSync(process.env.OUT + '/rerun', 'yes');
} else process.exit(9);
`,
  });
  const gate = (status, conclusion) =>
    run(step.run, {
      OUT: dir,
      REPO: 'phaabe/live.moafunk.de',
      HEAD_SHA: sha,
      FAKE_RUN: status ? JSON.stringify({ id: 77, status, conclusion }) : '',
    });
  const rerunDone = () => {
    const file = path.join(dir, 'rerun');
    const yes = existsSync(file);
    rmSync(file, { force: true });
    return yes;
  };
  try {
    assert.match(gate('completed', 'failure'), /release-gate re-run: https:\/\/github\.com\/phaabe\/live\.moafunk\.de\/actions\/runs\/77/);
    assert.equal(rerunDone(), true);
    assert.match(gate('completed', 'success'), /passed already/);
    assert.equal(rerunDone(), false);
    assert.match(gate('in_progress', null), /release-gate is in_progress/);
    assert.equal(rerunDone(), false);
    assert.throws(() => gate(null), (err) => /no release-gate run on abc1234d{33}/.test(err.stdout.toString()));
  } finally {
    done();
  }
});

test('PRs that change e2e/ or the frontend get the e2e checks and the local Studio soundcheck without staging or secrets', () => {
  const unit = load('e2e-unit.yml');
  assert.deepEqual(unit.on, {
    pull_request: {
      branches: ['staging', 'main'],
      paths: ['e2e/**', 'frontend/**', '.github/workflows/e2e-unit.yml'],
    },
  });
  assert.deepEqual(unit.permissions, { contents: 'read' });
  assert.deepEqual(Object.keys(unit.jobs), ['e2e-unit', 'studio-soundcheck']);
  // The soundcheck runs against the local mock, never the staging show.
  const soundcheck = unit.jobs['studio-soundcheck'].steps;
  assert.equal(soundcheck[0].with['persist-credentials'], false);
  assert.match(soundcheck.at(-2).run, /^npm run local$/m);
  const { steps, defaults } = unit.jobs['e2e-unit'];
  assert.deepEqual(defaults, { run: { 'working-directory': 'e2e' } });
  assert.equal(steps[0].with['persist-credentials'], false);
  assert.deepEqual(steps.filter((s) => s.run).map((s) => s.run), ['npm ci', 'npm run typecheck', 'npm run unit']);
  assert.doesNotMatch(JSON.stringify(unit), /secrets\.|bitwarden|\bssh\b|staging\.live|playwright test/i);
});
