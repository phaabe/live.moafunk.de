const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const { mkdtempSync, readFileSync, writeFileSync } = require('node:fs');
const { createRequire } = require('node:module');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');

const frontendRequire = createRequire(path.resolve(__dirname, '../../../frontend/package.json'));
const yaml = frontendRequire('js-yaml');
const workflow = yaml.load(readFileSync(path.resolve(__dirname, '../release-label.yml'), 'utf8'));
const notes = yaml.load(readFileSync(path.resolve(__dirname, '../../release.yml'), 'utf8'));

// Fake gh and sleep: gh answers only the PR endpoint, sleep logs its argument.
const bin = mkdtempSync(path.join(os.tmpdir(), 'release-label-'));
writeFileSync(
  path.join(bin, 'gh'),
  '#!/bin/bash\n[[ "$1 $2 $3" == "api repos/o/r/pulls/7 --jq" ]] || { echo "unexpected: $*" >&2; exit 2; }\nprintf "%s" "$PR_JSON" | jq -r "$4"\n',
  { mode: 0o755 }
);
writeFileSync(path.join(bin, 'sleep'), '#!/bin/bash\necho "$1" >> "$SLEEP_LOG"\n', { mode: 0o755 });

function check(labels, action = 'labeled') {
  const { run, env } = workflow.jobs['release-label'].steps[0];
  assert.deepEqual(env, {
    GH_TOKEN: '${{ github.token }}',
    GH_REPO: '${{ github.repository }}',
    PR_NUMBER: '${{ github.event.pull_request.number }}',
    ACTION: '${{ github.event.action }}',
  });
  const sleepLog = path.join(mkdtempSync(path.join(bin, 'run-')), 'sleep.log');
  writeFileSync(sleepLog, '');
  const out = execFileSync('/bin/bash', ['--noprofile', '--norc', '-e', '-c', run], {
    env: {
      PATH: `${bin}:/usr/bin:/bin`,
      GH_REPO: 'o/r',
      PR_NUMBER: '7',
      ACTION: action,
      PR_JSON: JSON.stringify({ labels: labels.map((name) => ({ name })) }),
      SLEEP_LOG: sleepLog,
    },
    stdio: 'pipe',
  }).toString();
  return { out, slept: readFileSync(sleepLog, 'utf8') };
}

test('release label check runs on every PR into staging and on label changes', () => {
  assert.deepEqual(Object.keys(workflow.on), ['pull_request']);
  assert.deepEqual(workflow.on.pull_request.branches, ['staging']);
  assert.deepEqual(
    [...workflow.on.pull_request.types].sort(),
    ['edited', 'labeled', 'opened', 'reopened', 'synchronize', 'unlabeled']
  );
  assert.equal(workflow.on.pull_request.paths, undefined, 'a required check must always run');
  assert.deepEqual(workflow.permissions, { 'pull-requests': 'read' });
  assert.deepEqual(Object.keys(workflow.jobs), ['release-label']);
  assert.equal(workflow.jobs['release-label'].if, undefined, 'a skipped job would count as passed');
});

test('release label check passes only with exactly one known release label', () => {
  assert.match(check(['release::feature']).out, /Release label: release::feature/);
  check(['release::fix', 'type::backend']);
  check(['project::Stream', 'release::internal']);
  assert.throws(() => check([]), 'no label');
  assert.throws(() => check(['type::ci', 'hotfix']), 'no release label');
  assert.throws(() => check(['release::feature', 'release::fix']), 'two release labels');
  assert.throws(() => check(['release::feature', 'release::feature-x']), 'an unknown second label');
  assert.throws(() => check(['release::features']), 'a typo');
});

test('release label check reads labels from the API and waits only on opened', () => {
  // Labels set at PR create arrive after the opened event, so the event has none.
  assert.equal(check(['release::fix'], 'opened').slept, '15\n');
  assert.equal(check(['release::fix'], 'labeled').slept, '');
  assert.equal(check(['release::fix'], 'synchronize').slept, '');
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
