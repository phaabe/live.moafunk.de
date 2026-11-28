const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const { chmodSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } = require('node:fs');
const { createRequire } = require('node:module');
const { tmpdir } = require('node:os');
const path = require('node:path');
const { test } = require('node:test');

const root = path.resolve(__dirname, '../../..');
const yaml = createRequire(path.join(root, 'frontend/package.json'))('js-yaml');
const load = (file) => yaml.load(readFileSync(path.join(root, '.github/workflows', file), 'utf8'));
const script = path.join(root, 'scripts/deployed-labels.sh');
const REPO = 'phaabe/live.moafunk.de';
const issue = (n) => `https://github.com/${REPO}/issues/${n}`;

// Fake gh: logs each call and answers from FAKE_STATE.
// commits/<sha>/pulls answers the pulls of the commit's subject; issues/<n>
// answers the issue, through real jq when --jq is given.
const fakeGh = `#!/usr/bin/env node
const fs = require('fs');
const { execFileSync } = require('child_process');
const args = process.argv.slice(2);
fs.appendFileSync(process.env.GH_LOG, JSON.stringify(args) + '\\n');
const state = JSON.parse(process.env.FAKE_STATE);
const write = args[1] === '-X';
const route = (write ? args[3] : args[1]).split('/');
const repo = route.slice(1, 3).join('/');
if (args[0] !== 'api' || route[0] !== 'repos' || repo !== process.env.REPO) process.exit(9);
if (!write && route[3] === 'commits' && route[5] === 'pulls' && route.length === 6) {
  const subject = execFileSync('git', ['log', '-1', '--format=%s', route[4]]).toString().trim();
  console.log(JSON.stringify(state.pulls[subject] || []));
} else if (!write && route[3] === 'issues' && route.length === 5) {
  // Like a 404: an issue the test does not know, or one it marks broken.
  if (!(route[4] in state.issues) || (state.broken || []).includes(Number(route[4]))) process.exit(1);
  const json = JSON.stringify(state.issues[route[4]]);
  const jq = args.indexOf('--jq');
  process.stdout.write(jq < 0 ? json : execFileSync('jq', ['-r', args[jq + 1]], { input: json }));
} else if (write && route[3] === 'issues' && route[5] === 'labels') {
  console.log('{}');
} else {
  process.exit(9);
}
`;

// A repo with commits "a", "b", "c" and a fake gh. Returns a runner.
function setup() {
  const dir = mkdtempSync(path.join(tmpdir(), 'deployed-labels-'));
  const bin = path.join(dir, 'bin');
  const work = path.join(dir, 'work');
  mkdirSync(bin);
  mkdirSync(work);
  writeFileSync(path.join(bin, 'gh'), fakeGh);
  chmodSync(path.join(bin, 'gh'), 0o755);
  const env = {
    PATH: `${bin}:${path.dirname(process.execPath)}:/usr/bin:/bin`,
    HOME: dir,
    GH_LOG: path.join(dir, 'gh.log'),
    REPO,
    GIT_AUTHOR_NAME: 't',
    GIT_AUTHOR_EMAIL: 't@t',
    GIT_COMMITTER_NAME: 't',
    GIT_COMMITTER_EMAIL: 't@t',
  };
  const git = (...args) => execFileSync('git', ['-C', work, ...args], { env, stdio: 'pipe' }).toString().trim();
  git('init', '-q', '-b', 'staging');
  git('commit', '-q', '--allow-empty', '-m', 'base');
  git('tag', 'base');
  for (const name of ['a', 'b', 'c']) git('commit', '-q', '--allow-empty', '-m', name);
  const run = (args, state, extra = {}) => {
    writeFileSync(env.GH_LOG, '');
    let out;
    let status = 0;
    try {
      out = execFileSync('/bin/bash', [script, ...args], {
        cwd: work,
        env: { ...env, FAKE_STATE: JSON.stringify(state), ...extra },
        stdio: 'pipe',
      }).toString();
    } catch (err) {
      out = err.stdout.toString() + err.stderr.toString();
      status = err.status;
    }
    const writes = readFileSync(env.GH_LOG, 'utf8')
      .trim()
      .split('\n')
      .filter(Boolean)
      .map((line) => JSON.parse(line))
      .filter((a) => a.includes('-X'))
      .map((a) => `${a[2]} ${a[3].replace(`repos/${REPO}/issues/`, '')}${a[5] ? ` ${a[5]}` : ''}`);
    return { out, status, writes };
  };
  return { dir, run, cleanup: () => rmSync(dir, { recursive: true, force: true }) };
}

const merged = (number, body) => ({ number, merged_at: '2026-11-28T10:00:00Z', body });
const labels = (...names) => names.map((name) => ({ name }));
const state = {
  pulls: {
    a: [merged(11, `Text.\r\n\r\nRefs: ${issue(5)}\r\nIssue: ${issue(6)}\r\nRelated: ${issue(7)}`)],
    b: [
      merged(12, `Closes: https://github.com/other/repo/issues/8\nTicket: ${issue(9)}, ${issue(6)}\nSee ${issue(20)}`),
      // An open PR that holds the same commit does not count.
      { number: 13, merged_at: null, body: `Refs: ${issue(21)}` },
    ],
    // c: a commit without a PR.
  },
  issues: {
    5: { number: 5, labels: labels('type::ci', 'deployed::production') },
    6: { number: 6, labels: [] },
    9: { number: 9, labels: labels('deployed::staging') },
  },
};

test('staging: linked issues get deployed::staging and lose deployed::production', () => {
  const { run, cleanup } = setup();
  try {
    const { out, status, writes } = run(['staging', 'base..HEAD'], state);
    assert.equal(status, 0, out);
    assert.match(out, new RegExp(`${issue(5)}: set deployed::staging, remove deployed::production`));
    assert.match(out, new RegExp(`${issue(9)}: has deployed::staging`));
    // Only Refs:/Issue:/Ticket:/Closes: lines of merged PRs in this repo; each issue once.
    assert.deepEqual(writes, [
      'POST 5/labels labels[]=deployed::staging',
      'DELETE 5/labels/deployed%3A%3Aproduction',
      'POST 6/labels labels[]=deployed::staging',
    ]);
  } finally {
    cleanup();
  }
});

test('production: the issues get deployed::production and lose deployed::staging', () => {
  const { run, cleanup } = setup();
  try {
    const { out, status, writes } = run(['production', 'base..HEAD'], state);
    assert.equal(status, 0, out);
    assert.match(out, new RegExp(`${issue(5)}: has deployed::production`));
    assert.deepEqual(writes, [
      'POST 6/labels labels[]=deployed::production',
      'POST 9/labels labels[]=deployed::production',
      'DELETE 9/labels/deployed%3A%3Astaging',
    ]);
  } finally {
    cleanup();
  }
});

test('only the commits of the range count, and DRY_RUN changes nothing', () => {
  const { run, cleanup } = setup();
  try {
    const onlyB = run(['staging', 'HEAD~2..HEAD~1'], state);
    assert.deepEqual(onlyB.writes, ['POST 6/labels labels[]=deployed::staging']);
    const dry = run(['staging', 'base..HEAD'], state, { DRY_RUN: '1' });
    assert.equal(dry.status, 0);
    assert.match(dry.out, new RegExp(`${issue(6)}: set deployed::staging`));
    assert.deepEqual(dry.writes, []);
    const none = run(['staging', 'HEAD~1..HEAD'], state);
    assert.match(none.out, /No linked issues in HEAD~1\.\.HEAD/);
    assert.deepEqual(none.writes, []);
  } finally {
    cleanup();
  }
});

test('a pull request link is skipped, an unreadable issue fails the run but not the others', () => {
  const { run, cleanup } = setup();
  try {
    const withPr = { ...state, issues: { ...state.issues, 6: { number: 6, labels: [], pull_request: {} } } };
    const pr = run(['staging', 'base..HEAD'], withPr);
    assert.equal(pr.status, 0, pr.out);
    assert.match(pr.out, new RegExp(`${issue(6)} is a pull request: skipped`));
    assert.ok(!pr.writes.some((w) => w.startsWith('POST 6/')));
    const broken = run(['staging', 'base..HEAD'], { ...state, broken: [5] });
    assert.equal(broken.status, 1);
    assert.match(broken.out, new RegExp(`::error::${issue(5)}: cannot read it`));
    assert.deepEqual(broken.writes, ['POST 6/labels labels[]=deployed::staging']);
  } finally {
    cleanup();
  }
});

test('bad calls change nothing', () => {
  const { run, cleanup } = setup();
  try {
    for (const args of [['ship', 'base..HEAD'], ['staging'], ['staging', ''], ['staging', 'a', 'b']]) {
      const { status, writes } = run(args, state);
      assert.equal(status, 2, args.join(' '));
      assert.deepEqual(writes, []);
    }
    const bad = run(['staging', 'nope..HEAD'], state);
    assert.notEqual(bad.status, 0, 'a bad range fails');
    assert.deepEqual(bad.writes, []);
  } finally {
    cleanup();
  }
});

test('staging workflow labels the pushed commits after every push to staging', () => {
  const workflow = load('deployed-labels.yml');
  assert.deepEqual(workflow.on, { push: { branches: ['staging'] } });
  assert.deepEqual(workflow.permissions, { contents: 'read', issues: 'write', 'pull-requests': 'read' });
  assert.deepEqual(Object.keys(workflow.jobs), ['label-issues']);
  const [checkout, label] = workflow.jobs['label-issues'].steps;
  assert.equal(checkout.uses, 'actions/checkout@v4');
  assert.deepEqual(checkout.with, { 'fetch-depth': 0, 'persist-credentials': false });
  assert.deepEqual(label.env, {
    GH_TOKEN: '${{ github.token }}',
    REPO: '${{ github.repository }}',
    BEFORE: '${{ github.event.before }}',
    AFTER: '${{ github.sha }}',
  });
  assert.equal(label.run, 'scripts/deployed-labels.sh staging "$BEFORE..$AFTER"');
});

test('production label job runs only after a real deploy, without the production keys', () => {
  const workflow = load('deploy-production.yml');
  const job = workflow.jobs['label-issues'];
  assert.equal(job.needs, 'deploy');
  assert.equal(job.if, "${{ !inputs.dry_run && inputs.rollback_tag == '' }}", 'not after a dry run or a rollback');
  assert.deepEqual(job.permissions, { contents: 'read', issues: 'write', 'pull-requests': 'read' });
  assert.equal(job.environment, undefined);
  assert.doesNotMatch(JSON.stringify(job), /secrets\.|steps\.keys|ssh/i);
  assert.equal(workflow.jobs.deploy.permissions, undefined, 'the deploy job keeps read-only GitHub access');
  const [checkout, label] = job.steps;
  assert.deepEqual(checkout.with, { 'fetch-depth': 0, 'persist-credentials': false });
  assert.deepEqual(label.env, { GH_TOKEN: '${{ github.token }}', REPO: '${{ github.repository }}' });

  // The range starts at the r* tag before main's head: for a release merge the
  // previous release, for a hotfix the release it fixes.
  const dir = mkdtempSync(path.join(tmpdir(), 'deployed-labels-prod-'));
  const env = {
    PATH: '/usr/bin:/bin',
    HOME: dir,
    GIT_AUTHOR_NAME: 't',
    GIT_AUTHOR_EMAIL: 't@t',
    GIT_COMMITTER_NAME: 't',
    GIT_COMMITTER_EMAIL: 't@t',
  };
  const git = (...args) => execFileSync('git', ['-C', dir, ...args], { env, stdio: 'pipe' }).toString().trim();
  try {
    git('init', '-q', '-b', 'main');
    mkdirSync(path.join(dir, 'scripts'));
    // Fake script: prints its arguments.
    writeFileSync(path.join(dir, 'scripts/deployed-labels.sh'), '#!/bin/bash\necho "$*"\n');
    chmodSync(path.join(dir, 'scripts/deployed-labels.sh'), 0o755);
    git('commit', '-q', '--allow-empty', '-m', 'release 1');
    git('tag', '-a', 'r1', '-m', 'Release r1');
    git('switch', '-qc', 'staging');
    git('commit', '-q', '--allow-empty', '-m', 'feature');
    git('switch', '-q', 'main');
    git('merge', '-q', '--no-ff', '-m', 'Merge release r2', 'staging');
    const step = () => execFileSync('/bin/bash', ['--noprofile', '--norc', '-e', '-c', label.run], { cwd: dir, env }).toString();
    assert.equal(step(), 'production r1..HEAD\n', 'release merge, not tagged yet');
    git('tag', '-a', 'r2', '-m', 'Release r2');
    assert.equal(step(), 'production r1..HEAD\n', 'release merge tagged r2');
    git('commit', '-q', '--allow-empty', '-m', 'hotfix (#99)');
    assert.equal(step(), 'production r2..HEAD\n', 'hotfix on top of r2');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
