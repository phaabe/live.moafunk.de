const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const { readFileSync } = require('node:fs');
const { createRequire } = require('node:module');
const path = require('node:path');
const { test } = require('node:test');

const frontendRequire = createRequire(path.resolve(__dirname, '../../../frontend/package.json'));
const yaml = frontendRequire('js-yaml');
const workflow = yaml.load(readFileSync(path.resolve(__dirname, '../release-label.yml'), 'utf8'));
const notes = yaml.load(readFileSync(path.resolve(__dirname, '../../release.yml'), 'utf8'));

function check(labels) {
  const { run, env } = workflow.jobs['release-label'].steps[0];
  assert.equal(env.LABELS, '${{ toJSON(github.event.pull_request.labels.*.name) }}');
  return execFileSync('/bin/bash', ['--noprofile', '--norc', '-e', '-c', run], {
    env: { PATH: '/usr/bin:/bin', LABELS: JSON.stringify(labels) },
    stdio: 'pipe',
  }).toString();
}

test('release label check runs on every PR into staging and on label changes', () => {
  assert.deepEqual(Object.keys(workflow.on), ['pull_request']);
  assert.deepEqual(workflow.on.pull_request.branches, ['staging']);
  assert.deepEqual(
    [...workflow.on.pull_request.types].sort(),
    ['edited', 'labeled', 'opened', 'reopened', 'synchronize', 'unlabeled']
  );
  assert.equal(workflow.on.pull_request.paths, undefined, 'a required check must always run');
  assert.deepEqual(workflow.permissions, {});
  assert.deepEqual(Object.keys(workflow.jobs), ['release-label']);
  assert.equal(workflow.jobs['release-label'].if, undefined, 'a skipped job would count as passed');
});

test('release label check passes only with exactly one known release label', () => {
  assert.match(check(['release::feature']), /Release label: release::feature/);
  check(['release::fix', 'type::backend']);
  check(['project::Stream', 'release::internal']);
  assert.throws(() => check([]), 'no label');
  assert.throws(() => check(['type::ci', 'hotfix']), 'no release label');
  assert.throws(() => check(['release::feature', 'release::fix']), 'two release labels');
  assert.throws(() => check(['release::feature', 'release::feature-x']), 'an unknown second label');
  assert.throws(() => check(['release::features']), 'a typo');
});

test('release notes are grouped by the release labels', () => {
  const { exclude, categories } = notes.changelog;
  assert.deepEqual(exclude.labels, ['release::internal']);
  assert.deepEqual(
    categories.map((c) => [c.title, c.labels]),
    [
      ['Features', ['release::feature']],
      ['Fixes', ['release::fix', 'hotfix']],
      ['Other changes', ['*']],
    ]
  );
});
