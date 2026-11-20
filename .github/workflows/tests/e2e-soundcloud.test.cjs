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

test('the SoundCloud test runs by hand; nightly.yml starts it each night on staging', () => {
  assert.deepEqual(soundcloud.on, { workflow_dispatch: null });
  // The test comes from the run's branch: staging for the nightly run.
  assert.deepEqual(job.steps[0], { uses: 'actions/checkout@v4' });
});

test('the SoundCloud test runs only its own spec', () => {
  const run = step('Run the SoundCloud test');
  assert.equal(run.run, 'npx playwright test tests/soundcloud.spec.ts');
  assert.equal(run['working-directory'], 'e2e');
  assert.equal(run.env.STAGING_E2E_HOST_PASSWORD, '${{ steps.secrets.outputs.STAGING_E2E_HOST_PASSWORD }}');
  assert.equal(job.env.E2E_RUN, '${{ github.run_id }}');
});

test('the SoundCloud test never runs during a chaos show, and holds staging against deploys', () => {
  // Its start deletes every leftover e2e-* Show, the chaos show's too.
  assert.deepEqual(job.concurrency, { group: 'staging-box', 'cancel-in-progress': false });
  assert.deepEqual(job.concurrency, chaos.concurrency);
  // A deploy waits for the hold (backend-build.test.cjs), not in this group.
  assert.notDeepEqual(load('backend.yml').jobs['deploy-staging'].concurrency, chaos.concurrency);
  assert.match(step('Hold staging for this run').run, /"e2e-hold \$E2E_RUN"/);
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
