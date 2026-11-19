const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const { chmodSync, mkdtempSync, readFileSync, rmSync, writeFileSync } = require('node:fs');
const { createRequire } = require('node:module');
const { tmpdir } = require('node:os');
const path = require('node:path');
const { test } = require('node:test');

const frontendRequire = createRequire(path.resolve(__dirname, '../../../frontend/package.json'));
const yaml = frontendRequire('js-yaml');
const load = (file) => yaml.load(readFileSync(path.resolve(__dirname, '..', file), 'utf8'));
const nightly = load('nightly.yml');
const job = nightly.jobs.start;
const step = job.steps[0];

// Runs the step with a fake gh that records its arguments; returns the gh calls.
function start(env) {
  const dir = mkdtempSync(path.join(tmpdir(), 'nightly-'));
  try {
    writeFileSync(path.join(dir, 'gh'), '#!/bin/bash\necho "$*" >>"$OUT/gh-calls"\n');
    chmodSync(path.join(dir, 'gh'), 0o755);
    writeFileSync(path.join(dir, 'gh-calls'), '');
    execFileSync('/bin/bash', ['--noprofile', '--norc', '-c', step.run], {
      env: { PATH: `${dir}:/usr/bin:/bin`, OUT: dir, SCHEDULE: '', TEST: '', ...env },
      stdio: 'pipe',
    });
    return readFileSync(path.join(dir, 'gh-calls'), 'utf8').trim().split('\n').filter(Boolean);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

test('nightly.yml owns the nightly schedules; the tests have none of their own', () => {
  assert.deepEqual(nightly.on.schedule, [{ cron: '0 2 * * *' }, { cron: '30 2 * * *' }]);
  assert.deepEqual(nightly.on.workflow_dispatch.inputs.test.options, ['both', 'soundcloud', 'chaos-show']);
  assert.equal(load('e2e-soundcloud.yml').on.schedule, undefined);
  assert.equal(load('e2e-staging.yml').on.schedule, undefined);
});

test('nightly.yml may only start workflows', () => {
  assert.deepEqual(nightly.permissions, { contents: 'read' });
  assert.deepEqual(job.permissions, { actions: 'write' });
  assert.deepEqual(step.env, {
    GH_TOKEN: '${{ github.token }}',
    GH_REPO: '${{ github.repository }}',
    SCHEDULE: '${{ github.event.schedule }}',
    TEST: '${{ inputs.test }}',
  });
  assert.match(step.run, /^set -euo pipefail/);
});

test('each schedule starts its test on the staging branch', () => {
  const soundcloud = 'workflow run e2e-soundcloud.yml --ref staging';
  const chaos = 'workflow run e2e-staging.yml --ref staging';
  assert.deepEqual(start({ SCHEDULE: '0 2 * * *' }), [soundcloud]);
  assert.deepEqual(start({ SCHEDULE: '30 2 * * *' }), [chaos]);
  // The schedule wins over the input, which is empty on a schedule anyway.
  assert.deepEqual(start({ SCHEDULE: '30 2 * * *', TEST: 'both' }), [chaos]);
  // By hand.
  assert.deepEqual(start({ TEST: 'both' }), [soundcloud, chaos]);
  assert.deepEqual(start({ TEST: 'soundcloud' }), [soundcloud]);
  assert.deepEqual(start({ TEST: 'chaos-show' }), [chaos]);
  // Never start nothing in silence.
  assert.throws(() => start({ SCHEDULE: '0 3 * * *' }), (err) => /no test for the schedule/.test(err.stdout));
  assert.throws(() => start({}), (err) => /unknown test ''/.test(err.stdout));
});
