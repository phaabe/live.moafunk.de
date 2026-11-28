const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const { existsSync, mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } = require('node:fs');
const { createRequire } = require('node:module');
const { tmpdir } = require('node:os');
const path = require('node:path');
const { test } = require('node:test');

const root = path.resolve(__dirname, '../../..');
const script = path.join(root, 'scripts/pr-touches.sh');
const frontendRequire = createRequire(path.join(root, 'frontend/package.json'));
const yaml = frontendRequire('js-yaml');
const load = (file) => yaml.load(readFileSync(path.resolve(__dirname, '..', file), 'utf8'));

// The two slow checks, their scope job and the output that gates them.
const checks = [
  { file: 'epic-ci.yml', job: 'backend', name: 'backend-ci', scope: 'backend-scope', output: 'backend' },
  { file: 'stream-system.yml', job: 'system', name: 'stream-system', scope: 'stream-scope', output: 'stream' },
];
// The Studio soundcheck: not a required check, so its workflow keeps its paths.
const studio = { file: 'e2e-unit.yml', job: 'studio-soundcheck', scope: 'studio-scope', output: 'studio' };

const patternOf = ({ file, scope }) => {
  const step = load(file).jobs[scope].steps.find((s) => s.id === 'touches');
  return step.run.match(/scripts\/pr-touches\.sh '([^']+)'/)[1];
};
const matches = (pattern, file) => {
  try {
    execFileSync('grep', ['-qE', '--', pattern], { input: `${file}\n` });
    return true;
  } catch (err) {
    if (err.status === 1) return false;
    throw err;
  }
};

test('pr-touches.sh: only a PR into staging can skip, and only when no file matches', () => {
  const dir = mkdtempSync(path.join(tmpdir(), 'pr-touches-'));
  const git = (...args) => execFileSync('git', ['-C', dir, ...args], { encoding: 'utf8' }).trim();
  const commit = (file, message) => {
    mkdirSync(path.dirname(path.join(dir, file)), { recursive: true });
    writeFileSync(path.join(dir, file), message);
    git('add', '.');
    git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', message);
    return git('rev-parse', 'HEAD');
  };
  // HEAD as actions/checkout leaves it on a PR: a merge commit whose first
  // parent is the base and whose tree is the PR's.
  const prMergeCommit = (base, file) => {
    git('checkout', '-q', '--detach', base);
    const head = commit(file, `change ${file}`);
    const tree = git('rev-parse', `${head}^{tree}`);
    const merged = execFileSync(
      'git',
      ['-C', dir, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit-tree', tree, '-p', base, '-p', head, '-m', 'pr'],
      { encoding: 'utf8' }
    ).trim();
    git('checkout', '-q', '--detach', merged);
  };
  const run = (env, pattern = '^backend/') =>
    execFileSync('/bin/bash', ['--noprofile', '--norc', script, pattern], {
      cwd: dir,
      env: { PATH: process.env.PATH, ...env },
      encoding: 'utf8',
      stdio: 'pipe',
    }).trim();
  const pr = { EVENT: 'pull_request', BASE_REF: 'staging' };
  try {
    git('init', '-q');
    const base = commit('README.md', 'base');

    prMergeCommit(base, 'docs/notes.md');
    assert.equal(run(pr), 'false');
    // The base's own files never count, only what the PR changes.
    assert.equal(run(pr, '^README\\.md$'), 'false');

    prMergeCommit(base, 'backend/src/main.rs');
    assert.equal(run(pr), 'true');
    assert.throws(() => run(pr, ''), (err) => /usage: pr-touches\.sh/.test(err.stderr));

    // Pushes, manual runs and PRs into main always run.
    assert.equal(run({ EVENT: 'push', BASE_REF: '' }), 'true');
    assert.equal(run({ EVENT: 'workflow_dispatch' }), 'true');
    assert.equal(run({ EVENT: 'pull_request', BASE_REF: 'main' }), 'true');

    // Without the merge commit's parents it fails instead of guessing.
    git('checkout', '-q', '--detach', base);
    assert.throws(() => run(pr), (err) => err.status !== 0);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

// The job waits for its scope job and runs unless the scope says false.
function assertScoped(c) {
  const workflow = load(c.file);
  const job = workflow.jobs[c.job];
  assert.equal(job.needs, c.scope);
  // A failed or empty scope runs the check; only an explicit false skips it.
  assert.equal(job.if, `\${{ !cancelled() && needs.${c.scope}.outputs.${c.output} != 'false' }}`);

  const scope = workflow.jobs[c.scope];
  assert.equal(scope.if, undefined);
  assert.equal(scope.outputs[c.output], `\${{ steps.touches.outputs.${c.output} }}`);
  assert.equal(scope.steps[0].uses, 'actions/checkout@v4');
  assert.equal(scope.steps[0].with['fetch-depth'], 2);
  const touches = scope.steps.find((s) => s.id === 'touches');
  assert.deepEqual(touches.env, { EVENT: '${{ github.event_name }}', BASE_REF: '${{ github.base_ref }}' });
  // An assignment of its own: inside echo, a failed script would not stop the step.
  assert.match(touches.run, /set -euo pipefail\n\w+="\$\(scripts\/pr-touches\.sh '/);
  assert.match(touches.run, new RegExp(`echo "${c.output}=\\$\\w+" >>"\\$GITHUB_OUTPUT"`));
}

test('backend-ci and stream-system keep their names and always start', () => {
  for (const c of checks) {
    const workflow = load(c.file);
    assert.equal(workflow.on.pull_request.paths, undefined, `${c.file}: a required check must always start`);
    assert.equal(workflow.jobs[c.job].name, c.name);
    assertScoped(c);
  }
});

test('Rust builds are cached; only staging pushes save the cache', () => {
  for (const c of checks) {
    const steps = load(c.file).jobs[c.job].steps;
    const names = steps.map((s) => s.name);
    const cache = steps.find((s) => s.name === 'Cache Rust builds');
    assert.equal(cache.uses, 'Swatinem/rust-cache@v2');
    assert.deepEqual(cache.with, {
      workspaces: 'backend',
      'save-if': "${{ github.event_name == 'push' && github.ref == 'refs/heads/staging' }}",
    });
    // The cache key uses the installed rustc version.
    assert.ok(names.indexOf('Install Rust') >= 0 && names.indexOf('Install Rust') < names.indexOf('Cache Rust builds'));
    const firstCargo = steps.findIndex((s) => /\bcargo\b/.test(s.run ?? ''));
    assert.ok(names.indexOf('Cache Rust builds') < firstCargo, `${c.file}: cache before the first cargo step`);
  }
});

test('CI Rust builds keep only line tables as debug info', () => {
  for (const c of checks) {
    // On the job, not in Cargo.toml: local builds keep full debug info.
    // The test profile inherits it from dev.
    assert.equal(load(c.file).jobs[c.job].env.CARGO_PROFILE_DEV_DEBUG, 'line-tables-only', c.file);
  }
});

test('the scope patterns cover what each check tests', () => {
  const backend = patternOf(checks[0]);
  for (const file of [
    'backend/src/main.rs',
    'backend/Cargo.lock',
    'backend/coverage-floors.json',
    'scripts/release.sh',
    'scripts/pr-touches.sh',
    'scripts/coverage-floors.cjs',
    '.github/workflows/epic-ci.yml',
    '.github/actions/apt-install/action.yml',
  ]) {
    assert.ok(matches(backend, file), `backend-ci must run for ${file}`);
  }
  for (const file of ['frontend/src/main.ts', 'docs/deployment.md', 'e2e/tests/a.spec.ts', 'scripts/release.sh.md']) {
    assert.ok(!matches(backend, file), `backend-ci may skip ${file}`);
  }

  const stream = patternOf(checks[1]);
  for (const file of [
    'backend/src/handlers/stream_ws.rs',
    'backend/src/handlers/recording.rs',
    'backend/src/stream_bridge.rs',
    'backend/src/stream_system_tests.rs',
    'backend/src/recording.rs',
    'backend/src/recording_finalizer.rs',
    'backend/src/broadcast_tests.rs',
    'backend/src/auth.rs',
    'backend/src/main.rs',
    'backend/Cargo.toml',
    'backend/Cargo.lock',
    'docs/stream-rework/prod/moafunk.liq',
    'docs/stream-rework/prod/icecast-kh.Dockerfile',
    '.github/workflows/stream-system.yml',
    '.github/actions/apt-install/action.yml',
    'scripts/pr-touches.sh',
  ]) {
    assert.ok(matches(stream, file), `stream-system must run for ${file}`);
  }
  for (const file of [
    'backend/src/soundcloud.rs',
    'backend/src/handlers/api.rs',
    'backend/src/handlers/recovery.rs',
    'backend/src/mainx.rs',
    'backend/staging/staging.sh',
    'frontend/src/admin/main.ts',
    'docs/stream-rework/notes.md',
  ]) {
    assert.ok(!matches(stream, file), `stream-system may skip ${file}`);
  }
});

test('the Studio soundcheck runs only on PRs that change what its specs run', () => {
  assertScoped(studio);
  const pattern = patternOf(studio);
  for (const file of [
    'frontend/src/admin/pages/flow/FlowLive.vue',
    'frontend/src/admin/pages/flow/StudioOnAir.vue',
    // The episode page and its Soundcheck and Show time tabs.
    'frontend/src/admin/pages/HostEpisodePage.vue',
    'frontend/src/admin/components/episode/SoundcheckCard.vue',
    'frontend/src/admin/components/live-panel/LivePanel.vue',
    'frontend/src/admin/components/LiveSetupTest.vue',
    // The admin test leaves the Recording page while on air.
    'frontend/src/admin/pages/RecordingPage.vue',
    'frontend/src/admin/composables/useSoundcheck.ts',
    'frontend/src/admin/composables/useWakeLock.ts',
    // The on-air bar on the other admin pages.
    'frontend/src/admin/App.vue',
    'frontend/src/admin/liveSession.ts',
    'frontend/src/admin/router.ts',
    'frontend/src/admin/studioLog.ts',
    'frontend/scripts/studio-mock.mjs',
    'frontend/vite.config.ts',
    'frontend/package-lock.json',
    'e2e/tests-local/studio-soundcheck.spec.ts',
    'e2e/tests-local/background-stream.spec.ts',
    'e2e/lib/studio.ts',
    'e2e/lib/staging.ts',
    'e2e/playwright.config.ts',
    'e2e/playwright.local.config.ts',
    'e2e/local-setup.ts',
    'e2e/global-setup.ts',
    'e2e/package-lock.json',
    '.github/workflows/e2e-unit.yml',
    '.github/actions/apt-install/action.yml',
    'scripts/pr-touches.sh',
  ]) {
    // A renamed file must not drop out of the pattern unnoticed.
    assert.ok(existsSync(path.join(root, file)), `${file} is gone: update the studio-scope pattern`);
    assert.ok(matches(pattern, file), `studio-soundcheck must run for ${file}`);
  }
  for (const file of [
    'frontend/src/admin/pages/ProfilePage.vue',
    'frontend/src/admin/pages/BookSlotPage.vue',
    'frontend/src/admin/components/PromotionCard.vue',
    'frontend/src/admin/routerx.ts',
    'frontend/src/shared/components/NavBar.vue',
    'frontend/scripts/fetch_soundcloud_tracks.py',
    'e2e/tests/chaos-show.spec.ts',
    'e2e/ci/stack.sh',
    'backend/src/main.rs',
    'docs/stream-rework/studio-soundcheck-local.md',
  ]) {
    assert.ok(!matches(pattern, file), `studio-soundcheck may skip ${file}`);
  }
});
