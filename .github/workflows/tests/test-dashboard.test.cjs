const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { createRequire } = require('node:module');
const path = require('node:path');
const { test } = require('node:test');

const frontendRequire = createRequire(path.resolve(__dirname, '../../../frontend/package.json'));
const yaml = frontendRequire('js-yaml');
const load = (file) => yaml.load(readFileSync(path.resolve(__dirname, '..', file), 'utf8'));
const { buildSummary, buildTests, appendHistory, parseCargo, parseVitest, KEEP } = require('../../../test-dashboard/collect.cjs');

const T0 = '2026-11-16T09:00:00Z';
const at = (s) => new Date(Date.parse(T0) + s * 1000).toISOString();
const step = (name, conclusion, from, to) => ({ name, conclusion, started_at: at(from), completed_at: at(to) });
const run = (id, sha, extra = {}) => ({
  id,
  html_url: `https://github.com/o/r/actions/runs/${id}`,
  head_sha: sha.padEnd(40, '0'),
  updated_at: T0,
  event: 'push',
  ...extra,
});
const job = (name, steps, extra = {}) => ({ name, html_url: `https://github.com/o/r/job/${name}`, steps, ...extra });

const epic = {
  run: run(1, 'aaaaaaa'),
  jobs: [
    job('backend-ci', [
      step('Check backend formatting', 'success', 0, 1),
      step('Run backend tests', 'failure', 1, 101),
      step('Run Clippy', 'success', 101, 181),
      step('Check the shell scripts (shellcheck)', 'success', 181, 184),
    ]),
    job('frontend-ci', [
      step('Run frontend lint', 'success', 0, 2),
      step('Test workflow rules', 'success', 2, 3),
      step('Run frontend typecheck', 'skipped', 3, 3),
      step('Run frontend tests', 'success', 3, 15),
    ]),
  ],
};
const stream = { run: run(2, 'bbbbbbb'), jobs: [job('stream-system', [step('Run the system tests', 'success', 0, 90)])] };
const deploys = [
  // Newest first: a dry run skips the smoke test, so the older real deploy counts.
  { run: run(3, 'ccccccc', { event: 'workflow_dispatch' }), jobs: [job('deploy', [step('Smoke test (read-only)', 'skipped', 0, 0)])] },
  { run: run(4, 'ddddddd', { event: 'workflow_dispatch' }), jobs: [job('deploy', [step('Smoke test (read-only)', 'success', 0, 4)])] },
];
const chaosJob = job('e2e-staging / chaos-show', [], { conclusion: 'failure', started_at: at(0), completed_at: at(500) });
const current = { run: run(5, 'eeeeeee'), jobs: [job('build-and-push', []), chaosJob, job('test-dashboard / dashboard', [], { conclusion: null })] };

const cargo = [
  'running 4 tests',
  'test scheduler::tests::picks_next_slot ... ok',
  'test booking_tests::books_a_slot ... ok',
  'test booking_tests::refuses_a_taken_slot ... FAILED',
  'test stream_system_tests::live_reaches_listeners ... ignored, needs Liquidsoap',
  'test auth::tests::rejects_bad_password ... ok',
  'test result: FAILED. 3 passed; 1 failed; 1 ignored',
].join('\n');
const vitest = {
  testResults: [
    {
      name: '/w/frontend/src/admin/__tests__/studio.test.ts',
      assertionResults: [
        { ancestorTitles: ['Studio'], title: 'mounts', status: 'passed', duration: 100 },
        { ancestorTitles: ['Studio'], title: 'later', status: 'skipped' },
        { ancestorTitles: ['Studio', 'flow'], status: 'passed', duration: 1500 },
      ],
    },
    {
      name: '/w/frontend/src/admin/__tests__/flowSync.test.ts',
      assertionResults: [{ ancestorTitles: ['flowSync'], status: 'failed', duration: 600 }],
    },
  ],
};
const results = {
  cargo,
  vitest,
  rustCoverage: { data: [{ totals: { lines: { percent: 61.234, covered: 612, count: 1000 } } }] },
  frontendCoverage: { total: { lines: { pct: 48.16, covered: 4816, total: 10000 } } },
};
const base = { now: T0, epic, stream, deploys, current, results, soakMinutes: '0', previous: null };
const byId = (summary) => Object.fromEntries(summary.levels.map((l) => [l.id, l]));

test('cargo test lines split into unit (L1) and the *_tests integration modules (L3)', () => {
  // stream_system_tests are ignored here: stream-system.yml runs them (L5).
  const { tests, ...counts } = parseCargo(cargo);
  assert.deepEqual(counts, {
    unit: { passed: 2, failed: 0, skipped: 0 },
    integration: { passed: 1, failed: 1, skipped: 0 },
  });
  assert.deepEqual(tests.L3, [
    { suite: 'booking_tests', name: 'books_a_slot', status: 'passed' },
    { suite: 'booking_tests', name: 'refuses_a_taken_slot', status: 'failed' },
  ]);
});

test('tests.json lists every single test of L1, L2 and L3', () => {
  const tests = buildTests(results);
  assert.deepEqual(tests.L1, [
    { suite: 'scheduler::tests', name: 'picks_next_slot', status: 'passed' },
    { suite: 'auth::tests', name: 'rejects_bad_password', status: 'passed' },
    { suite: 'src/admin/__tests__/studio.test.ts', name: 'Studio > mounts', status: 'passed' },
    { suite: 'src/admin/__tests__/studio.test.ts', name: 'Studio > later', status: 'skipped' },
  ]);
  assert.deepEqual(tests.L2.map((t) => [t.suite, t.status]), [
    ['src/admin/__tests__/studio.test.ts', 'passed'],
    ['src/admin/__tests__/flowSync.test.ts', 'failed'],
  ]);
  assert.equal(tests.L3.length, 2);
  // The lists match the counts on the summary cards.
  const l = byId(buildSummary(base));
  for (const id of ['L1', 'L2', 'L3']) {
    assert.equal(tests[id].length, l[id].tests.passed + l[id].tests.failed + l[id].tests.skipped, id);
  }
  assert.deepEqual(buildTests({ cargo: null, vitest: null }), { L1: [], L2: [], L3: [] });
});

test("vitest tests in describe('flow') and flowSync.test.ts are the screen checks (L2)", () => {
  const { tests: _tests, ...counts } = parseVitest(vitest);
  assert.deepEqual(counts, {
    unit: { passed: 1, failed: 0, skipped: 1 },
    screens: { passed: 1, failed: 1, skipped: 0, seconds: 2 },
  });
});

test('every level L0–L8 gets a status, counts, duration, coverage and a run link', () => {
  const summary = buildSummary(base);
  const l = byId(summary);
  assert.deepEqual(Object.keys(l), ['L0', 'L1', 'L2', 'L3', 'L4', 'L5', 'L6', 'L7', 'L8']);

  // A skipped check is not a pass.
  assert.equal(l.L0.status, 'not run');
  assert.deepEqual(l.L0.checks.map((c) => [c.name, c.status]), [
    ['rustfmt', 'pass'],
    ['Clippy', 'pass'],
    ['shellcheck', 'pass'],
    ['ESLint', 'pass'],
    ['vue-tsc', 'not run'],
  ]);
  assert.equal(l.L0.seconds, 1 + 80 + 3 + 2 + 0);
  assert.equal(l.L0.run.url, epic.run.html_url);

  // cargo test failed because of an L3 test: the unit tests themselves passed.
  assert.equal(l.L1.status, 'pass');
  assert.deepEqual(l.L1.tests, { passed: 3, failed: 0, skipped: 1 });
  assert.deepEqual(l.L1.coverage, { rust: 61.2, frontend: 48.2 });
  assert.deepEqual(buildSummary(base).coverage_lines, {
    rust: { covered: 612, total: 1000 },
    frontend: { covered: 4816, total: 10000 },
  });
  assert.equal(l.L2.status, 'fail');
  assert.deepEqual(l.L2.tests, { passed: 1, failed: 1, skipped: 0 });
  assert.equal(l.L2.seconds, 2);
  assert.equal(l.L3.status, 'fail');
  assert.deepEqual(l.L3.tests, { passed: 1, failed: 1, skipped: 0 });

  assert.equal(l.L4.status, 'not built');
  assert.equal(l.L5.status, 'pass');
  assert.equal(l.L5.seconds, 90);
  assert.equal(l.L6.status, 'fail');
  assert.equal(l.L6.seconds, 500);
  assert.equal(l.L6.run.url, chaosJob.html_url);
  assert.equal(l.L7.status, 'not run');
  assert.equal(l.L8.status, 'pass');
  assert.equal(l.L8.run.sha, deploys[1].run.head_sha);
  // The page explains each level; only a soak brings its own note.
  assert.deepEqual(summary.levels.filter((x) => x.note), []);
});

test('without result files the levels fall back to the step results', () => {
  const l = byId(buildSummary({ ...base, results: { cargo: null, vitest: null, rustCoverage: null, frontendCoverage: null } }));
  assert.equal(l.L1.status, 'fail');
  assert.equal(l.L1.tests, null);
  assert.equal(l.L1.coverage, null);
  assert.deepEqual(buildSummary({ ...base, results: { cargo: null, vitest: null, rustCoverage: null, frontendCoverage: null } }).coverage_lines, { rust: null, frontend: null });
  assert.equal(l.L2.status, 'pass');
  assert.equal(l.L3.status, 'fail');
});

test('a soak goes to L7; levels the run has no data for keep their last result', () => {
  const previous = buildSummary(base);
  const soak = byId(buildSummary({ ...base, soakMinutes: '90', previous }));
  assert.equal(soak.L7.status, 'fail');
  assert.match(soak.L7.note, /90 extra minutes/);
  assert.deepEqual(soak.L6, byId(previous).L6);

  const plain = { ...base, current: { run: run(6, 'fffffff'), jobs: [] }, deploys: [] };
  const later = byId(buildSummary({ ...plain, previous: buildSummary({ ...base, soakMinutes: '90', previous }) }));
  assert.deepEqual(later.L6, byId(previous).L6);
  assert.equal(later.L7.status, 'fail');
  assert.equal(later.L8.status, 'not run');
  assert.equal(byId(buildSummary(plain)).L6.status, 'not run');
});

test('the Instagram e2e job is its own check in L6 and keeps its result when a run lacks it', () => {
  const igJob = job('e2e-staging / instagram-e2e', [], { conclusion: 'success', started_at: at(600), completed_at: at(1500) });
  const both = { ...base, current: { ...current, jobs: [...current.jobs, igJob] } };
  const l6 = byId(buildSummary(both)).L6;
  assert.deepEqual(
    l6.checks.map((c) => [c.name, c.status, c.seconds]),
    [
      ['chaos test show', 'fail', 500],
      ['Instagram announcements', 'pass', 900],
    ]
  );
  assert.equal(l6.status, 'fail');

  // A soak run has no L6 chaos check: both keep their last result.
  const soak = byId(buildSummary({ ...both, soakMinutes: '90', previous: buildSummary(both) })).L6;
  assert.deepEqual(soak.checks.map((c) => c.name), ['chaos test show', 'Instagram announcements']);
  assert.equal(soak.checks[1].status, 'pass');

  // Only the Instagram job ran: the chaos check comes from the last summary.
  const onlyIg = { ...base, current: { run: run(7, '1111111'), jobs: [igJob] }, previous: buildSummary(base) };
  const l = byId(buildSummary(onlyIg)).L6;
  assert.deepEqual(l.checks.map((c) => [c.name, c.status]), [
    ['chaos test show', 'fail'],
    ['Instagram announcements', 'pass'],
  ]);
  assert.equal(l.run.url, igJob.html_url);
});

test('history keeps the last runs only', () => {
  let history = [];
  for (let i = 0; i < KEEP + 5; i++) history = appendHistory(history, { generated_at: String(i) });
  assert.equal(KEEP, 50);
  assert.equal(history.length, KEEP);
  assert.equal(history[0].generated_at, '5');
  assert.equal(history.at(-1).generated_at, String(KEEP + 4));
});

test('the dashboard runs after staging deploys and after every chaos run, with read access only', () => {
  const dash = load('test-dashboard.yml');
  assert.deepEqual(Object.keys(dash.on), ['workflow_call']);
  assert.deepEqual(dash.permissions, { contents: 'read', actions: 'read' });
  const text = JSON.stringify(dash);
  assert.deepEqual([...text.matchAll(/secrets\.([A-Z_]+)/g)].map((m) => m[1]), ['BW_ACCESS_TOKEN']);
  assert.doesNotMatch(text, /HETZNER|STAGING_|\bssh\b|"(packages|contents|id-token)":"write"/i);
  const { steps, concurrency } = dash.jobs.dashboard;
  assert.deepEqual(concurrency, { group: 'test-dashboard', 'cancel-in-progress': false });
  assert.equal(steps[0].with['persist-credentials'], false);
  assert.match(steps.find((s) => /collect\.cjs/.test(s.run ?? '')).env.GH_TOKEN, /github\.token/);
  const keys = steps.find((s) => s.id === 'vercel').with.secrets.trim().split('\n');
  assert.deepEqual(keys.map((k) => k.split(' > ')[1]), ['VERCEL_TOKEN']);
  assert.match(keys[0], /^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12} > /, 'a Bitwarden secret id');
  const deploy = steps.at(-1).env;
  assert.match(deploy.VERCEL_ORG_ID, /^team_\w+$/);
  assert.match(deploy.VERCEL_PROJECT_ID, /^prj_\w+$/);
  assert.match(steps.at(-1).run, /vercel@\d+\.\d+\.\d+ deploy "\$RUNNER_TEMP\/site" --prod/);

  for (const [file, id, needs] of [
    ['backend.yml', 'test-dashboard', 'deploy-staging'],
    ['e2e-staging.yml', 'dashboard', 'chaos-show'],
  ]) {
    const caller = load(file).jobs[id];
    assert.equal(caller.uses, './.github/workflows/test-dashboard.yml');
    assert.deepEqual(caller.needs, needs);
    assert.deepEqual(caller.permissions, { contents: 'read', actions: 'read' });
    assert.match(caller.if, /!cancelled\(\)/, `${file}: also after a failed deploy or chaos test show`);
  }
  // Nightly, by hand and on a release PR; the release soak goes to L7.
  const chaosDashboard = load('e2e-staging.yml').jobs.dashboard;
  assert.equal(chaosDashboard.if, '${{ !cancelled() }}');
  assert.equal(chaosDashboard.with['soak-minutes'], "${{ inputs.duration || '0' }}");
  // The nested dashboard job may ask only for what the caller grants.
  assert.deepEqual(load('e2e-release.yml').jobs['e2e-staging'].permissions, { contents: 'read', actions: 'read' });
});

test('result files only on pushes to staging; frontend PR runs stay plain', () => {
  const ci = load('epic-ci.yml');
  for (const [id, artifact] of [
    ['backend', 'test-results-backend'],
    ['frontend', 'test-results-frontend'],
  ]) {
    const steps = ci.jobs[id].steps;
    const tests = steps.find((s) => s.name === `Run ${id} tests`);
    assert.match(tests.run, /set -euo pipefail/);
    const upload = steps.find((s) => s.uses === 'actions/upload-artifact@v4');
    assert.equal(upload.with.name, artifact);
    assert.match(upload.if, /github\.event_name == 'push' && github\.ref == 'refs\/heads\/staging'/);
  }
  const frontend = ci.jobs.frontend.steps.find((s) => s.name === 'Run frontend tests');
  assert.equal(frontend.env.REPORT, "${{ github.event_name == 'push' && github.ref == 'refs/heads/staging' }}");
});

test('every backend run measures coverage and checks the floors', () => {
  const steps = load('epic-ci.yml').jobs.backend.steps;
  const names = steps.map((s) => s.name);
  const install = steps.find((s) => s.name === 'Install the coverage tool');
  assert.doesNotMatch(install.if, /github\.ref|event_name/, 'PR runs measure coverage too');
  const tests = steps.find((s) => s.name === 'Run backend tests');
  assert.equal(tests.id, 'tests');
  assert.equal(tests.env, undefined);
  assert.doesNotMatch(tests.run, /cargo test/);
  const floors = steps.find((s) => s.name === 'Check coverage floors');
  assert.ok(names.indexOf('Check coverage floors') === names.indexOf('Run backend tests') + 1);
  assert.equal(floors.if, "${{ !cancelled() && steps.tests.outcome == 'success' }}");
  assert.equal(floors.run, 'node scripts/coverage-floors.cjs results/coverage.json backend/coverage-floors.json');
});

test('coverage counts product code only and also writes lcov', () => {
  const ci = load('epic-ci.yml');
  const run = (id) => ci.jobs[id].steps.find((s) => s.name === `Run ${id} tests`).run;
  const backend = run('backend');
  assert.match(backend, /ignore='src\/bin\/'/);
  assert.match(backend, /--no-report \| tee \.\.\/results\/cargo-test\.txt/);
  const reports = backend.split('cargo llvm-cov report').slice(1);
  assert.deepEqual(reports.map((r) => r.match(/--output-path (\S+)/)[1]), ['../results/coverage.json', '../results/lcov.info']);
  for (const r of reports) assert.match(r, /--ignore-filename-regex "\$ignore"/);
  assert.match(run('frontend'), /cp coverage\/coverage-summary\.json coverage\/lcov\.info \.\.\/results\//);

  const vitest = readFileSync(path.resolve(__dirname, '../../../frontend/vitest.config.ts'), 'utf8');
  assert.match(vitest, /provider: 'istanbul'/);
  assert.match(vitest, /include: \['src\/\*\*'\]/);
  assert.match(vitest, /reporter: \[[^\]]*'lcov'\]/);
});
