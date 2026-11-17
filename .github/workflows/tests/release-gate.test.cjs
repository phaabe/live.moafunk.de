const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const { mkdtempSync, readFileSync, rmSync, writeFileSync, chmodSync, mkdirSync } = require('node:fs');
const { createRequire } = require('node:module');
const { tmpdir } = require('node:os');
const path = require('node:path');
const { test } = require('node:test');

const frontendRequire = createRequire(path.resolve(__dirname, '../../../frontend/package.json'));
const yaml = frontendRequire('js-yaml');
const workflow = yaml.load(readFileSync(path.resolve(__dirname, '../release-gate.yml'), 'utf8'));
const job = workflow.jobs['release-gate'];
const release = yaml.load(readFileSync(path.resolve(__dirname, '../e2e-release.yml'), 'utf8'));
const chaos = yaml.load(readFileSync(path.resolve(__dirname, '../e2e-staging.yml'), 'utf8'));
const sha = 'c'.repeat(40);

test('release gate runs on every PR into main and on label changes', () => {
  assert.equal(workflow.name, 'Release gate');
  assert.deepEqual(Object.keys(workflow.on), ['pull_request']);
  assert.deepEqual(workflow.on.pull_request.branches, ['main']);
  assert.deepEqual(
    [...workflow.on.pull_request.types].sort(),
    ['edited', 'labeled', 'opened', 'reopened', 'synchronize', 'unlabeled']
  );
  assert.equal(workflow.on.pull_request.paths, undefined, 'a required check must always run');
  assert.deepEqual(workflow.permissions, { checks: 'read' });
  assert.deepEqual(Object.keys(workflow.jobs), ['release-gate']);
  assert.equal(job.if, undefined, 'a skipped job would count as passed');
  assert.equal(job.steps.length, 1);
  assert.deepEqual(job.steps[0].env, {
    GH_TOKEN: '${{ github.token }}',
    REPO: '${{ github.repository }}',
    HEAD_REF: '${{ github.event.pull_request.head.ref }}',
    HEAD_REPO: '${{ github.event.pull_request.head.repo.full_name }}',
    HEAD_SHA: '${{ github.event.pull_request.head.sha }}',
    LABELS: '${{ toJSON(github.event.pull_request.labels.*.name) }}',
  });
});

test('release gate: release/* needs a green chaos-show of its release PR run on the head commit, hotfix/* the label', () => {
  const dir = mkdtempSync(path.join(tmpdir(), 'release-gate-'));
  const bin = path.join(dir, 'bin');
  mkdirSync(bin);
  // Fake gh: answers the check-runs call for one check name from FAKE_RUNS
  // ({ name: [check runs] }); any other call fails.
  writeFileSync(
    path.join(bin, 'gh'),
    `#!/usr/bin/env node
const args = process.argv.slice(2);
const want = ['api', '-X', 'GET', 'repos/phaabe/live.moafunk.de/commits/' + process.env.HEAD_SHA + '/check-runs', '-f'];
if (JSON.stringify(args.slice(0, 5)) !== JSON.stringify(want) || !args[5].startsWith('check_name=') || args.length !== 6) process.exit(9);
const runs = JSON.parse(process.env.FAKE_RUNS)[args[5].slice('check_name='.length)] || [];
console.log(JSON.stringify({ total_count: runs.length, check_runs: runs }));
`
  );
  chmodSync(path.join(bin, 'gh'), 0o755);
  const runOf = (conclusion, extra = {}) => ({
    status: conclusion ? 'completed' : 'in_progress',
    conclusion,
    html_url: 'https://github.com/phaabe/live.moafunk.de/actions/runs/1/job/2',
    app: { slug: 'github-actions' },
    ...extra,
  });
  const gate = (ref, { labels = [], runs = {}, repo = 'phaabe/live.moafunk.de' } = {}) =>
    execFileSync('/bin/bash', ['--noprofile', '--norc', '-e', '-c', job.steps[0].run], {
      env: {
        PATH: `${bin}:${path.dirname(process.execPath)}:/usr/bin:/bin`,
        REPO: 'phaabe/live.moafunk.de',
        HEAD_REF: ref,
        HEAD_REPO: repo,
        HEAD_SHA: sha,
        LABELS: JSON.stringify(labels),
        FAKE_RUNS: JSON.stringify(runs),
      },
      stdio: 'pipe',
    }).toString();
  const refuses = (ref, opts, message) =>
    assert.throws(() => gate(ref, opts), (err) => message.test(err.stdout.toString()));
  try {
    // Release: only the release PR's run (e2e-release.yml, job e2e-staging) counts.
    const releaseRun = `${Object.keys(release.jobs)[0]} / ${Object.keys(chaos.jobs)[0]}`;
    assert.equal(releaseRun, 'e2e-staging / chaos-show');
    assert.match(gate('release/r1', { runs: { [releaseRun]: [runOf('success')] } }), /passed on c{40}/);
    // A re-run after a failure: one of the runs is green.
    gate('release/r2', { runs: { [releaseRun]: [runOf('failure'), runOf('success')] } });
    const noGreen = /no green chaos test show on the release commit c{40} yet/;
    refuses('release/r1', {}, noGreen);
    refuses('release/r1', { runs: { [releaseRun]: [runOf('failure')] } }, noGreen);
    refuses('release/r1', { runs: { [releaseRun]: [runOf(null)] } }, noGreen);
    refuses('release/r1', { runs: { [releaseRun]: [runOf('skipped')] } }, noGreen);
    refuses('release/r1', { runs: { [releaseRun]: [runOf('success', { app: { slug: 'someone-else' } })] } }, noGreen);
    // Runs by hand or nightly test what staging runs, not this commit.
    refuses('release/r1', { runs: { 'chaos-show': [runOf('success')] } }, noGreen);
    refuses('release/r1', { runs: { 'chaos-show-x': [runOf('success')] } }, noGreen);
    // The hotfix label alone does not let a release skip the chaos test.
    refuses('release/r1', { labels: ['hotfix'] }, noGreen);
    // A fork's PR runs the fork's workflow files, which could fake the check.
    refuses('release/r1', { repo: 'someone/live.moafunk.de', runs: { [releaseRun]: [runOf('success')] } }, /must come from a branch of phaabe\/live\.moafunk\.de/);

    // Hotfix: the branch and the label.
    assert.match(gate('hotfix/fix-login', { labels: ['type::backend', 'hotfix'] }), /chaos test is skipped/);
    refuses('hotfix/fix-login', { labels: ['release::fix'] }, /needs the label hotfix/);
    refuses('hotfix/fix-login', {}, /needs the label hotfix/);

    // Anything else.
    refuses('staging', { runs: { [releaseRun]: [runOf('success')] } }, /only release\/\* and hotfix\/\*/);
    refuses('feat/x', { labels: ['hotfix'] }, /only release\/\* and hotfix\/\*/);
    refuses('fix/release/r1', { labels: ['hotfix'] }, /only release\/\* and hotfix\/\*/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
