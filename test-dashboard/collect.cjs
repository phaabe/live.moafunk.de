// Collects the latest result of every test level (L0–L8, see the testing
// concept) into summary.json, appends it to history.json (last 50 runs) and
// writes the static site into the folder given as the first argument.
// Runs in CI (.github/workflows/test-dashboard.yml) with GH_TOKEN (actions: read).
// Usage: node test-dashboard/collect.cjs <out-dir>
'use strict';

const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const KEEP = 50;

const LEVELS = [
  ['L0', 'Static checks'],
  ['L1', 'Unit tests'],
  ['L2', 'Screens (flow)'],
  ['L3', 'Backend integration'],
  ['L4', 'Contracts'],
  ['L5', 'Stream system'],
  ['L6', 'End-to-end on staging'],
  ['L7', 'Soak and restore'],
  ['L8', 'Production'],
];

// GitHub conclusion → dashboard status.
const statusOf = (conclusion) =>
  ({ success: 'pass', failure: 'fail', timed_out: 'fail' })[conclusion] ?? 'not run';

const seconds = (from, to) =>
  from && to ? Math.round((Date.parse(to) - Date.parse(from)) / 1000) : null;

const sum = (values) => {
  const known = values.filter((v) => v != null);
  return known.length ? known.reduce((a, b) => a + b, 0) : null;
};

// Worst status wins: a level is "pass" only when every check passed.
const worst = (statuses) =>
  statuses.includes('fail') ? 'fail' : statuses.every((s) => s === 'pass') ? 'pass' : 'not run';

const countStatus = (t) => (t.failed > 0 ? 'fail' : t.passed > 0 ? 'pass' : 'not run');

function step(job, name, label) {
  const s = job?.steps?.find((x) => x.name === name);
  return { name: label, status: statusOf(s?.conclusion), seconds: seconds(s?.started_at, s?.completed_at) };
}

const runRef = (run, job) =>
  run && { url: job?.html_url ?? run.html_url, sha: run.head_sha, at: run.updated_at };

// `test <name> ... ok|FAILED|ignored` lines of `cargo test`. Top-level
// `*_tests` modules (booking_tests, broadcast_tests, …) run real handlers with
// SQLite and a fake R2: that is L3. Everything else is L1, except
// stream_system_tests: ignored here, stream-system.yml runs them (L5).
function parseCargo(text) {
  const out = { unit: { passed: 0, failed: 0, skipped: 0 }, integration: { passed: 0, failed: 0, skipped: 0 } };
  for (const [, name, result] of text.matchAll(/^test (\S+) \.\.\. (ok|FAILED|ignored)/gm)) {
    if (name.startsWith('stream_system_tests::')) continue;
    const t = /^[a-z_]+_tests::/.test(name) ? out.integration : out.unit;
    t[{ ok: 'passed', FAILED: 'failed', ignored: 'skipped' }[result]] += 1;
  }
  return out;
}

// Vitest JSON report. Tests in a describe('flow') block and in
// flowSync.test.ts check the screens against Flow Lab: that is L2.
function parseVitest(report) {
  const out = {
    unit: { passed: 0, failed: 0, skipped: 0 },
    screens: { passed: 0, failed: 0, skipped: 0, seconds: 0 },
  };
  for (const file of report.testResults ?? []) {
    const flowFile = path.basename(file.name) === 'flowSync.test.ts';
    for (const a of file.assertionResults ?? []) {
      const t = flowFile || a.ancestorTitles?.includes('flow') ? out.screens : out.unit;
      t[a.status === 'passed' ? 'passed' : a.status === 'failed' ? 'failed' : 'skipped'] += 1;
      if (t === out.screens) t.seconds += (a.duration ?? 0) / 1000;
    }
  }
  out.screens.seconds = Math.round(out.screens.seconds);
  return out;
}

const addCounts = (a, b) =>
  a && b ? { passed: a.passed + b.passed, failed: a.failed + b.failed, skipped: a.skipped + b.skipped } : (a ?? b);

const round1 = (n) => (typeof n === 'number' ? Math.round(n * 10) / 10 : null);

function level(id, fields) {
  const name = LEVELS.find(([l]) => l === id)[1];
  const checks = fields.checks ?? [];
  return {
    id,
    name,
    status: fields.status ?? worst(checks.map((c) => c.status)),
    checks,
    tests: fields.tests ?? null,
    seconds: sum(checks.map((c) => c.seconds)),
    coverage: fields.coverage ?? null,
    run: fields.run ?? null,
    note: fields.note ?? null,
  };
}

/**
 * Builds one summary from API data that main() fetched.
 * - epic: { run, jobs } latest finished epic-ci push run on staging
 * - stream: { run, jobs } latest finished stream-system push run on staging
 * - deploys: [{ run, jobs }] latest Deploy production runs, newest first
 * - current: { run, jobs } the run that calls this script
 * - results: { cargo, vitest, rustCoverage, frontendCoverage } from the epic-ci artifacts, each may be null
 * - soakMinutes: the chaos run's `duration` input (0 = no soak)
 * - previous: the last summary in history.json, or null
 */
function buildSummary({ now, epic, stream, deploys, current, results, soakMinutes, previous }) {
  const backend = epic?.jobs.find((j) => j.name === 'backend-ci');
  const frontend = epic?.jobs.find((j) => j.name === 'frontend-ci');
  const cargo = results.cargo && parseCargo(results.cargo);
  const vitest = results.vitest && parseVitest(results.vitest);
  const coverage = {
    rust: round1(results.rustCoverage?.data?.[0]?.totals?.lines?.percent),
    frontend: round1(results.frontendCoverage?.total?.lines?.pct),
  };
  const epicRef = runRef(epic?.run);
  const prev = (id) => previous?.levels?.find((l) => l.id === id);

  const cargoStep = step(backend, 'Run backend tests', 'cargo test');
  const vitestStep = step(frontend, 'Run frontend tests', 'vitest');

  const levels = [];
  levels.push(
    level('L0', {
      checks: [
        step(backend, 'Check backend formatting', 'rustfmt'),
        step(backend, 'Run Clippy', 'Clippy'),
        step(backend, 'Check the shell scripts (shellcheck)', 'shellcheck'),
        step(frontend, 'Run frontend lint', 'ESLint'),
        step(frontend, 'Run frontend typecheck', 'vue-tsc'),
      ],
      run: epicRef,
    })
  );
  levels.push(
    level('L1', {
      checks: [
        cargo ? { ...cargoStep, status: countStatus(cargo.unit) } : cargoStep,
        vitest ? { ...vitestStep, status: countStatus(vitest.unit) } : vitestStep,
        step(frontend, 'Test workflow rules', 'workflow rules'),
      ],
      tests: addCounts(cargo?.unit, vitest?.unit),
      coverage: coverage.rust != null || coverage.frontend != null ? coverage : null,
      run: epicRef,
      note: 'Coverage includes the L2 and L3 tests, which run in the same test runs.',
    })
  );
  const screens = vitest && { passed: vitest.screens.passed, failed: vitest.screens.failed, skipped: vitest.screens.skipped };
  levels.push(
    level('L2', {
      checks: [{ name: 'Flow Lab checks', status: screens ? countStatus(screens) : vitestStep.status, seconds: vitest ? vitest.screens.seconds : null }],
      tests: screens,
      run: epicRef,
      note: "Vitest tests in describe('flow') blocks and flowSync.test.ts.",
    })
  );
  levels.push(
    level('L3', {
      checks: [{ name: 'handlers + SQLite + fake R2', status: cargo ? countStatus(cargo.integration) : cargoStep.status, seconds: null }],
      tests: cargo?.integration ?? null,
      run: epicRef,
      note: 'The *_tests modules in backend/src. They run inside cargo test (L1), so no own duration.',
    })
  );
  levels.push(
    level('L4', {
      status: 'not built',
      note: 'Parked: https://github.com/phaabe/live.moafunk.de/issues/874',
    })
  );
  const streamJob = stream?.jobs.find((j) => j.name === 'stream-system');
  levels.push(
    level('L5', {
      checks: [step(streamJob, 'Run the system tests', 'Liquidsoap + Icecast')],
      run: runRef(stream?.run, streamJob),
    })
  );

  // The chaos test show of the calling run: after a staging deploy, every night,
  // or by hand. A run with extra minutes on air is a soak (L7).
  const chaos = current?.jobs.find(
    (j) => /(^|\/ )chaos-show$/.test(j.name) && ['success', 'failure', 'timed_out'].includes(j.conclusion)
  );
  const chaosLevel = (id, note) =>
    level(id, {
      checks: [{ name: 'chaos test show', status: statusOf(chaos.conclusion), seconds: seconds(chaos.started_at, chaos.completed_at) }],
      run: runRef(current.run, chaos),
      note,
    });
  const soak = Number(soakMinutes) > 0;
  levels.push(chaos && !soak ? chaosLevel('L6') : (prev('L6') ?? level('L6', { status: 'not run' })));
  levels.push(
    chaos && soak
      ? chaosLevel('L7', `Soak: ${Number(soakMinutes)} extra minutes on air. Restore drill: by hand, not reported.`)
      : (prev('L7') ??
          level('L7', {
            status: 'not run',
            note: 'No soak yet (a manual chaos run with duration > 0). Restore drill: by hand, not reported.',
          }))
  );

  // The newest production deploy that ran its smoke test (dry runs skip it).
  const smoke = (deploys ?? [])
    .map(({ run, jobs }) => {
      const job = jobs.find((j) => j.name === 'deploy');
      return { run, job, check: step(job, 'Smoke test (read-only)', 'post-deploy smoke test') };
    })
    .find((d) => d.check.status !== 'not run');
  levels.push(
    level('L8', {
      checks: smoke ? [smoke.check] : [],
      status: smoke ? undefined : 'not run',
      run: smoke ? runRef(smoke.run, smoke.job) : null,
      note: 'Show-day timer: on the box, not reported.',
    })
  );

  return {
    generated_at: now,
    run: current?.run ? { url: current.run.html_url, event: current.run.event, sha: current.run.head_sha } : null,
    coverage,
    levels,
  };
}

const appendHistory = (history, summary) => [...history, summary].slice(-KEEP);

// ── I/O: everything below talks to GitHub, the live site and the disk ──────

const repo = () => process.env.REPO;
const api = (p) => JSON.parse(execFileSync('gh', ['api', `repos/${repo()}/${p}`], { encoding: 'utf8' }));
const jobs = (id) => api(`actions/runs/${id}/jobs?per_page=100`).jobs;

function latest(workflow, query) {
  const runs = api(`actions/workflows/${workflow}/runs?${query}&status=completed&per_page=10`).workflow_runs;
  const run = runs.find((r) => ['success', 'failure'].includes(r.conclusion));
  return run ? { run, jobs: jobs(run.id) } : null;
}

// The live history; a missing file starts a new one, any other error stops
// the run, so a bad fetch never replaces the history with one entry.
async function liveHistory(site) {
  const res = await fetch(`${site}/history.json`, { cache: 'no-store' });
  if (res.status === 404) return [];
  if (!res.ok) throw new Error(`GET ${site}/history.json: ${res.status}`);
  const history = await res.json();
  if (!Array.isArray(history)) throw new Error('history.json is not a list');
  return history;
}

function readResults(epicRun) {
  const results = { cargo: null, vitest: null, rustCoverage: null, frontendCoverage: null };
  if (!epicRun) return results;
  const names = api(`actions/runs/${epicRun.id}/artifacts`).artifacts.map((a) => a.name);
  const wanted = names.filter((n) => n.startsWith('test-results-'));
  if (!wanted.length) return results;
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'test-results-'));
  // One call each: a single -n unpacks into -D itself, not into a folder per name.
  for (const name of wanted) {
    execFileSync('gh', ['run', 'download', String(epicRun.id), '-R', repo(), '-n', name, '-D', path.join(dir, name)], {
      stdio: 'inherit',
    });
  }
  const file = (...p) => {
    const f = path.join(dir, ...p);
    return fs.existsSync(f) ? fs.readFileSync(f, 'utf8') : null;
  };
  const json = (...p) => {
    const text = file(...p);
    return text == null ? null : JSON.parse(text);
  };
  results.cargo = file('test-results-backend', 'cargo-test.txt');
  results.rustCoverage = json('test-results-backend', 'coverage.json');
  results.vitest = json('test-results-frontend', 'vitest.json');
  results.frontendCoverage = json('test-results-frontend', 'coverage-summary.json');
  return results;
}

async function main(outDir) {
  if (!outDir || !repo() || !process.env.RUN_ID || !process.env.SITE_URL) {
    throw new Error('usage: REPO=… RUN_ID=… SITE_URL=… node collect.cjs <out-dir>');
  }
  const history = await liveHistory(process.env.SITE_URL);
  const epic = latest('epic-ci.yml', 'branch=staging&event=push');
  const runs = api('actions/workflows/deploy-production.yml/runs?branch=main&status=completed&per_page=10').workflow_runs;
  const summary = buildSummary({
    now: new Date().toISOString(),
    epic,
    stream: latest('stream-system.yml', 'branch=staging&event=push'),
    deploys: runs.map((run) => ({ run, jobs: jobs(run.id) })),
    current: { run: api(`actions/runs/${process.env.RUN_ID}`), jobs: jobs(process.env.RUN_ID) },
    results: readResults(epic?.run),
    soakMinutes: process.env.SOAK_MINUTES ?? '0',
    previous: history.at(-1) ?? null,
  });
  fs.mkdirSync(outDir, { recursive: true });
  fs.copyFileSync(path.join(__dirname, 'index.html'), path.join(outDir, 'index.html'));
  fs.writeFileSync(path.join(outDir, 'summary.json'), `${JSON.stringify(summary, null, 2)}\n`);
  fs.writeFileSync(path.join(outDir, 'history.json'), `${JSON.stringify(appendHistory(history, summary))}\n`);
  for (const l of summary.levels) console.log(`${l.id} ${l.status.padEnd(9)} ${l.name}`);
}

if (require.main === module) {
  main(process.argv[2]).catch((err) => {
    console.error(err.message);
    process.exit(1);
  });
}

module.exports = { buildSummary, appendHistory, parseCargo, parseVitest, KEEP };
