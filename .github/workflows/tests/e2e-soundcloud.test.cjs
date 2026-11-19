const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { createRequire } = require('node:module');
const path = require('node:path');
const { test } = require('node:test');

const frontendRequire = createRequire(path.resolve(__dirname, '../../../frontend/package.json'));
const yaml = frontendRequire('js-yaml');
const load = (file) => yaml.load(readFileSync(path.resolve(__dirname, '..', file), 'utf8'));
const soundcloud = load('e2e-soundcloud.yml');
const job = soundcloud.jobs.soundcloud;
const chaos = load('e2e-staging.yml').jobs['chaos-show'];
const step = (name) => job.steps.find((s) => s.name === name);

test('the SoundCloud test runs nightly before the chaos show, and by hand', () => {
  assert.deepEqual(Object.keys(soundcloud.on).sort(), ['schedule', 'workflow_dispatch']);
  assert.deepEqual(soundcloud.on.schedule, [{ cron: '0 2 * * *' }]);
  assert.deepEqual(load('e2e-staging.yml').on.schedule, [{ cron: '30 2 * * *' }]);
  assert.equal(job.steps[0].with.ref, "${{ github.event_name == 'schedule' && 'staging' || '' }}");
});

test('the SoundCloud test runs only its own spec', () => {
  const run = step('Run the SoundCloud test');
  assert.equal(run.run, 'npx playwright test tests/soundcloud.spec.ts');
  assert.equal(run['working-directory'], 'e2e');
  assert.equal(run.env.STAGING_E2E_HOST_PASSWORD, '${{ steps.secrets.outputs.STAGING_E2E_HOST_PASSWORD }}');
  assert.equal(job.env.E2E_RUN, '${{ github.run_id }}');
});

test('the SoundCloud test never runs during a chaos show or a deploy', () => {
  // Its start deletes every leftover e2e-* Show, the chaos show's too.
  assert.deepEqual(job.concurrency, { group: 'staging-box', 'cancel-in-progress': false });
  assert.deepEqual(job.concurrency, chaos.concurrency);
  assert.deepEqual(load('backend.yml').jobs['deploy-staging'].concurrency, chaos.concurrency);
});

test('the SoundCloud test uses only the staging CI key and the e2e host login', () => {
  assert.deepEqual(soundcloud.permissions, { contents: 'read' });
  const text = JSON.stringify(soundcloud);
  assert.deepEqual(
    [...text.matchAll(/secrets\.([A-Z_]+)/g)].map((match) => match[1]),
    ['BW_ACCESS_TOKEN']
  );
  assert.doesNotMatch(text, /HETZNER|178\.104\.160\.103|admin\.live|deploy_hetzner|scp/i);
  const ids = (j) => j.steps.find((s) => s.id === 'secrets').with.secrets;
  assert.equal(ids(job), ids(chaos));
  const hostKey = (j) => j.steps.find((s) => s.env?.HOST_KEY).env.HOST_KEY;
  assert.equal(hostKey(job), hostKey(chaos));
  const unhold = step("Remove this run's hold (also after a failure)");
  assert.match(unhold.if, /always\(\)/, 'the hold is removed also after a failure');
  assert.match(unhold.run, /"e2e-unhold \$E2E_RUN"/);
});
