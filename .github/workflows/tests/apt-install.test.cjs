const assert = require('node:assert/strict');
const { readdirSync, readFileSync } = require('node:fs');
const { createRequire } = require('node:module');
const path = require('node:path');
const { test } = require('node:test');

const root = path.resolve(__dirname, '../../..');
const yaml = createRequire(path.join(root, 'frontend/package.json'))('js-yaml');
const read = (file) => readFileSync(path.join(root, file), 'utf8');
const load = (file) => yaml.load(read(file));

const action = './.github/actions/apt-install';
const saveOnPush =
  "${{ github.event_name == 'push' && (github.ref == 'refs/heads/staging' || github.ref == 'refs/heads/main') }}";
// Every job that installs apt packages, and the one that saves the cache.
const jobs = [
  { file: 'epic-ci.yml', job: 'backend', packages: 'pkg-config libssl-dev ffmpeg', save: saveOnPush },
  { file: 'stream-system.yml', job: 'system', packages: 'ffmpeg', options: '--no-install-recommends' },
  { file: 'e2e-unit.yml', job: 'studio-soundcheck', packages: 'ffmpeg pulseaudio' },
  { file: 'e2e-staging.yml', job: 'chaos-show', packages: 'ffmpeg' },
];

test('workflows install apt packages only through the cached action', () => {
  for (const file of readdirSync(path.join(root, '.github/workflows')).filter((f) => /\.ya?ml$/.test(f))) {
    assert.doesNotMatch(read(`.github/workflows/${file}`), /apt-get install|apt install/, `${file}: use ${action}`);
  }
  for (const { file, job, ...want } of jobs) {
    const steps = load(`.github/workflows/${file}`).jobs[job].steps;
    const install = steps.find((s) => s.uses === action);
    assert.ok(install, `${file}: ${job} uses ${action}`);
    assert.equal(steps[0].uses, 'actions/checkout@v4', `${file}: a local action needs the checkout first`);
    assert.deepEqual(install.with, want, `${file}: ${job}`);
  }
});

test('the action restores and saves the same cache, and saves only when asked', () => {
  const steps = load('.github/actions/apt-install/action.yml').runs.steps;
  const restore = steps.find((s) => s.uses === 'actions/cache/restore@v4');
  const save = steps.find((s) => s.uses === 'actions/cache/save@v4');
  // Path and key must match, or a restore never finds the saved cache.
  assert.equal(save.with.path, restore.with.path);
  assert.equal(save.with.key, restore.with.key);
  const install = steps.find((s) => /apt-get install/.test(s.run ?? ''));
  assert.match(install.run, /Dir::Cache::Archives "%s\/apt-debs\/"/);
  assert.equal(restore.with.path, '~/apt-debs/*.deb');
  for (const step of steps.slice(steps.indexOf(install) + 1)) {
    assert.equal(step.if, "inputs.save == 'true' && steps.restore.outputs.cache-hit != 'true'", step.name);
  }
});
