const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { createRequire } = require('node:module');
const path = require('node:path');
const { test } = require('node:test');

// Use the YAML parser already locked by the frontend's ESLint dependency.
const frontendRequire = createRequire(path.resolve(__dirname, '../../../frontend/package.json'));
const yaml = frontendRequire('js-yaml');
const workflow = (name) => yaml.load(readFileSync(path.resolve(__dirname, '..', name), 'utf8'));
const ci = workflow('epic-ci.yml');
const frontend = workflow('frontend.yml');
const branches = ['main', 'dev/312-interim', 'dev/streaming-architecture'];
const mainOnly = "github.ref == 'refs/heads/main' && github.event_name != 'pull_request'";
const afterSetup = (id) => '${{ !cancelled() && steps.' + id + ".outcome == 'success' }}";

for (const event of ['pull_request', 'push']) {
  for (const branch of branches) {
    test(`${event} on ${branch} runs backend and frontend checks`, () => {
      assert.ok(ci.on[event].branches.includes(branch));
      assert.equal(ci.on[event].paths, undefined);
      assert.equal(ci.on[event]['paths-ignore'], undefined);
      assert.ok(frontend.on[event].branches.includes(branch));
      assert.ok(frontend.on[event].paths.includes('frontend/**'));
      for (const job of Object.values(ci.jobs)) {
        assert.equal(job.if, undefined);
        assert.equal(job['continue-on-error'], undefined);
      }
    });
  }
}

test('required check names stay stable and runs do not cancel other branches', () => {
  assert.equal(ci.jobs.backend.name, 'backend-ci');
  assert.equal(ci.jobs.frontend.name, 'frontend-ci');
  assert.equal(
    ci.concurrency.group,
    'epic-ci-${{ github.event.pull_request.number || github.ref }}'
  );
});

test('backend formatting, tests and Clippy use the pinned toolchain and fail the job', () => {
  const job = ci.jobs.backend;
  assert.equal(job.defaults.run['working-directory'], 'backend');
  const toolchain = job.steps.find((step) => step.id === 'toolchain');
  assert.equal(toolchain.uses, 'dtolnay/rust-toolchain@1.98.0');
  assert.ok(toolchain.with.components.includes('rustfmt'));
  assert.ok(toolchain.with.components.includes('clippy'));
  for (const command of [
    'cargo fmt --check',
    'cargo test --locked --all-targets',
    'cargo clippy --locked --all-targets',
  ]) {
    const step = job.steps.find((candidate) => candidate.run === command);
    assert.ok(step, `Missing blocking command: ${command}`);
    assert.equal(step['continue-on-error'], undefined);
    assert.equal(step.if, command === 'cargo fmt --check' ? undefined : afterSetup('toolchain'));
  }
});

test('frontend lint, types, tests and workflow regressions fail the required job', () => {
  const job = ci.jobs.frontend;
  assert.equal(job.defaults.run['working-directory'], 'frontend');
  for (const command of [
    'npm ci',
    'npm run lint',
    'npm run typecheck',
    'npm test -- --run',
    'node --test ../.github/workflows/tests/*.test.cjs',
  ]) {
    const step = job.steps.find((candidate) => candidate.run === command);
    assert.ok(step, `Missing blocking command: ${command}`);
    assert.equal(step['continue-on-error'], undefined);
    assert.equal(
      step.if,
      ['npm ci', 'npm run lint'].includes(command) ? undefined : afterSetup('dependencies')
    );
  }
});

test('required checks have read-only permissions and no production access', () => {
  assert.deepEqual(ci.permissions, { contents: 'read' });
  const text = JSON.stringify(ci);
  assert.doesNotMatch(text, /secrets\.|bitwarden|deploy-pages|ssh |scp |docker push/);
  for (const job of Object.values(ci.jobs)) {
    assert.equal(job.permissions, undefined);
    assert.equal(job.environment, undefined);
    assert.equal(
      job.steps.find((step) => step.uses === 'actions/checkout@v4').with['persist-credentials'],
      false
    );
  }
});

test('Pages preparation, secrets and publishing are restricted to main outside PRs', () => {
  assert.deepEqual(frontend.permissions, { contents: 'read' });
  const build = frontend.jobs.build;
  assert.deepEqual(build.permissions, { contents: 'read', pages: 'read' });
  for (const name of [
    'Setup Pages',
    'Setup Python',
    'Install uv',
    'Get secrets from Bitwarden',
    'Generate tracks JSON from SoundCloud',
    'Upload artifact',
  ]) {
    assert.equal(build.steps.find((step) => step.name === name).if, mainOnly, name);
  }
  assert.equal(frontend.jobs.deploy.if, mainOnly);
  assert.equal(frontend.jobs.deploy.needs, 'build');
  assert.deepEqual(frontend.jobs.deploy.permissions, { pages: 'write', 'id-token': 'write' });
  assert.match(
    build.steps.find((step) => step.name === 'Use fallback re-listen.html').if,
    /'skipped'/
  );
  assert.equal(build.steps.find((step) => step.name === 'Run tests').run, 'npm test -- --run');
});

test('backend production workflow cannot run for an integration push', () => {
  assert.deepEqual(workflow('backend.yml').on.push.branches, ['main']);
});
