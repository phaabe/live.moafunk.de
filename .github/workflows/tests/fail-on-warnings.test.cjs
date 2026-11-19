const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { createRequire } = require('node:module');
const path = require('node:path');
const { test } = require('node:test');

const root = path.resolve(__dirname, '../../..');
const yaml = createRequire(path.join(root, 'frontend/package.json'))('js-yaml');
const load = (file) => yaml.load(readFileSync(path.join(root, '.github/workflows', file), 'utf8'));
// Every step of the workflow whose command matches `pattern`.
const stepsRunning = (file, pattern) =>
  Object.values(load(file).jobs)
    .flatMap((job) => job.steps ?? [])
    .filter((step) => pattern.test(step.run ?? ''));

test('a Clippy warning fails backend-ci', () => {
  const clippy = stepsRunning('epic-ci.yml', /cargo clippy/);
  assert.deepEqual(
    clippy.map((step) => step.run),
    ['cargo clippy --locked --all-targets -- -D warnings']
  );
  assert.equal(clippy[0]['continue-on-error'], undefined);
});

test('an ESLint warning fails the frontend lint in both workflows', () => {
  const { scripts } = JSON.parse(readFileSync(path.join(root, 'frontend/package.json'), 'utf8'));
  assert.match(scripts.lint, /^eslint \. .*--max-warnings 0( |$)/);
  // Both workflows lint through the script, so the flag always applies.
  for (const file of ['epic-ci.yml', 'frontend.yml']) {
    const lint = stepsRunning(file, /eslint|npm run lint/);
    assert.deepEqual(lint.map((step) => step.run), ['npm run lint'], file);
    assert.equal(lint[0]['continue-on-error'], undefined, file);
  }
});
