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

const step = load('stream-system.yml').jobs.system.steps.find((s) => s.name === 'Run the system tests');
const source = path.join(root, 'backend/src', path.basename(step.env.SYSTEM_TESTS_SOURCE));
// The test names in the real source, in order.
const names = [
  ...readFileSync(source, 'utf8').matchAll(/run by the stream-system CI job"\]\s*\n\s*async fn (\w+)/g),
].map((m) => m[1]);
const ok = (name) => `test stream_system_tests::${name} ... ok`;

// Runs the step with a fake cargo that prints FAKE_LOG and exits FAKE_EXIT.
function run(log, { exit = 0, sourceFile = source } = {}) {
  const dir = mkdtempSync(path.join(tmpdir(), 'stream-system-step-'));
  const bin = path.join(dir, 'bin');
  mkdirSync(bin);
  writeFileSync(path.join(bin, 'cargo'), '#!/bin/bash\nprintf "%s\\n" "$FAKE_LOG"\nexit "$FAKE_EXIT"\n');
  chmodSync(path.join(bin, 'cargo'), 0o755);
  try {
    return execFileSync('/bin/bash', ['--noprofile', '--norc', '-e', '-c', step.run], {
      cwd: dir,
      env: {
        PATH: `${bin}:/usr/bin:/bin`,
        RUNNER_TEMP: dir,
        SYSTEM_TESTS_SOURCE: sourceFile,
        FAKE_LOG: log,
        FAKE_EXIT: String(exit),
      },
      stdio: 'pipe',
    }).toString();
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}
const fails = (fn, message) =>
  assert.throws(fn, (error) => message.test(error.stdout.toString()));

test('the stream-system step reads the real source and finds its system tests', () => {
  assert.equal(step.env.SYSTEM_TESTS_SOURCE, 'src/stream_system_tests.rs');
  assert.equal(step['working-directory'], 'backend');
  assert.equal(step['continue-on-error'], undefined);
  assert.ok(names.length >= 3, `found ${names.length} system tests`);
});

test('the stream-system step passes only when every system test ran and passed', () => {
  const all = ['running 3 tests', ...names.map(ok), 'test result: ok.'].join('\n');
  assert.match(run(all), /stream_system_tests::\w+ \.\.\. ok/);
  // A renamed filter: cargo is green with nothing run.
  fails(() => run('running 0 tests\ntest result: ok. 0 passed'), new RegExp(`0 of ${names.length} system tests passed`));
  fails(() => run(names.slice(1).map(ok).join('\n')), new RegExp(`${names.length - 1} of ${names.length}`));
  // cargo fails: pipefail keeps its exit code through tee.
  assert.throws(() => run(all, { exit: 101 }));
});

test('the stream-system step fails when the source has no system tests', () => {
  const dir = mkdtempSync(path.join(tmpdir(), 'stream-system-source-'));
  const empty = path.join(dir, 'stream_system_tests.rs');
  writeFileSync(empty, '#[tokio::test]\nasync fn other() {}\n');
  try {
    fails(() => run('', { sourceFile: empty }), /No system tests found/);
    fails(() => run('', { sourceFile: path.join(dir, 'missing.rs') }), /No system tests found/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('both Playwright configs refuse test.only on CI', () => {
  for (const file of ['playwright.config.ts', 'playwright.local.config.ts']) {
    const text = readFileSync(path.join(root, 'e2e', file), 'utf8');
    assert.match(text, /^\s*forbidOnly: !!process\.env\.CI,$/m, file);
  }
});

test('the SoundCloud fallback shows a warning in the run summary', () => {
  const steps = load('frontend.yml').jobs.build.steps;
  const soundcloud = steps.find((s) => s.id === 'generate-tracks');
  assert.equal(soundcloud['continue-on-error'], true);
  const fallback = steps.find((s) => s.name === 'Use fallback re-listen.html');
  assert.equal(fallback.if, "steps.generate-tracks.outcome == 'failure'");
  assert.match(fallback.run, /^echo "::warning title=[^:,]+::[^"]+"$/m);
});
