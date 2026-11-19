const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { createRequire } = require('node:module');
const path = require('node:path');
const { test } = require('node:test');

const root = path.resolve(__dirname, '../../..');
const yaml = createRequire(path.join(root, 'frontend/package.json'))('js-yaml');
const load = (file) => yaml.load(readFileSync(path.join(root, '.github/workflows', file), 'utf8'));

test('PRs check the website build in frontend-ci; the Pages workflow runs only on main', () => {
  const steps = load('epic-ci.yml').jobs.frontend.steps;
  const build = steps.find((step) => step.run === 'npm run build');
  assert.ok(build, 'frontend-ci builds the website');
  // A failed lint or test does not hide a broken build.
  assert.equal(build.if, "${{ !cancelled() && steps.dependencies.outcome == 'success' }}");
  assert.equal(build['continue-on-error'], undefined);

  // The Pages workflow reads the SoundCloud secrets, so it never runs on a PR.
  const pages = load('frontend.yml');
  assert.deepEqual(Object.keys(pages.on).sort(), ['push', 'workflow_dispatch']);
  assert.deepEqual(pages.on.push.branches, ['main']);
});
