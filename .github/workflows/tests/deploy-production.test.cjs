const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const { mkdtempSync, readFileSync, rmSync, writeFileSync, chmodSync, mkdirSync } = require('node:fs');
const { createRequire } = require('node:module');
const { tmpdir } = require('node:os');
const path = require('node:path');
const { test } = require('node:test');

const frontendRequire = createRequire(path.resolve(__dirname, '../../../frontend/package.json'));
const yaml = frontendRequire('js-yaml');
const workflow = yaml.load(readFileSync(path.resolve(__dirname, '../deploy-production.yml'), 'utf8'));
const job = workflow.jobs.deploy;
const step = (id) => job.steps.find((s) => s.id === id);
const uuid = '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}';

function bash(script, env) {
  return execFileSync('/bin/bash', ['--noprofile', '--norc', '-e', '-c', script], {
    env: { PATH: '/usr/bin:/bin', ...env },
    stdio: 'pipe',
  }).toString();
}

test('production deploy runs only by hand, from main, one at a time', () => {
  assert.deepEqual(Object.keys(workflow.on), ['workflow_dispatch']);
  const inputs = workflow.on.workflow_dispatch.inputs;
  assert.deepEqual(Object.keys(inputs).sort(), ['confirm', 'dry_run', 'hotfix', 'rollback_tag']);
  assert.equal(inputs.dry_run.type, 'boolean');
  assert.equal(inputs.dry_run.default, true, 'a dry run unless asked otherwise');
  assert.equal(inputs.hotfix.type, 'boolean');
  assert.equal(inputs.hotfix.default, false, 'the staging check runs unless asked otherwise');
  assert.deepEqual(workflow.permissions, { contents: 'read', 'pull-requests': 'read' });
  assert.deepEqual(workflow.concurrency, { group: 'production-deploy', 'cancel-in-progress': false });
  assert.deepEqual(Object.keys(workflow.jobs), ['deploy']);
  assert.equal(job.if, "github.ref == 'refs/heads/main'");
  assert.deepEqual(job.environment, { name: 'api-production', url: 'https://admin.live.moafunk.de' });
  assert.equal(job.steps[0].id, 'gate', 'the gate runs before anything else');
});

test('production deploy uses only the Bitwarden token and three named keys', () => {
  const text = JSON.stringify(workflow);
  assert.deepEqual([...new Set([...text.matchAll(/secrets\.([A-Z_]+)/g)].map((m) => m[1]))], ['BW_ACCESS_TOKEN']);
  const keys = step('keys');
  assert.equal(keys.uses, 'bitwarden/sm-action@v2');
  assert.match(
    keys.with.secrets.trim(),
    new RegExp(`^${uuid} > HETZNER_IP\\n${uuid} > HETZNER_SSH_KEY\\n${uuid} > STAGING_DEPLOY_SSH_KEY$`)
  );
  const checkout = job.steps.find((s) => s.uses === 'actions/checkout@v4');
  assert.equal(checkout.with['fetch-depth'], 0);
  assert.equal(checkout.with['persist-credentials'], false);
});

test('gate lets only Anton and phaabe deploy, with the typed confirmation', () => {
  const gate = step('gate');
  assert.equal(gate.env.HOTFIX, '${{ inputs.hotfix }}');
  const run = (env) => bash(gate.run, { CONFIRM: 'deploy production', ROLLBACK_TAG: '', HOTFIX: 'false', ...env });
  run({ ACTOR: 'anneoneone' });
  run({ ACTOR: 'phaabe' });
  run({ ACTOR: 'phaabe', ROLLBACK_TAG: 'sha-1234abc' });
  run({ ACTOR: 'anneoneone', HOTFIX: 'true' });
  assert.throws(() => run({ ACTOR: 'anneoneone', HOTFIX: 'true', ROLLBACK_TAG: 'sha-1234abc' }), 'hotfix and rollback');
  assert.throws(() => run({ ACTOR: 'someone-else' }));
  assert.throws(() => run({ ACTOR: 'anneoneone', CONFIRM: 'yes' }));
  assert.throws(() => run({ ACTOR: 'anneoneone', ROLLBACK_TAG: 'latest' }));
  assert.throws(() => run({ ACTOR: 'anneoneone', ROLLBACK_TAG: 'sha-1234abc; reboot' }));
});

test('staging check passes only when main has exactly the code staging runs', () => {
  const check = step('staging');
  assert.equal(check.if, "inputs.rollback_tag == '' && !inputs.hotfix", 'skipped only for a rollback or a hotfix');
  assert.match(check.env.STAGING_HOST_KEY, /^staging\.live\.moafunk\.de ssh-ed25519 \S+$/);
  const dir = mkdtempSync(path.join(tmpdir(), 'deploy-production-'));
  const repo = path.join(dir, 'repo');
  const bin = path.join(dir, 'bin');
  mkdirSync(bin);
  // Fake ssh: prints what the staging box would answer to "status".
  writeFileSync(path.join(bin, 'ssh'), '#!/bin/bash\nprintf "tag: %s\\nhold: no\\n" "$FAKE_TAG"\n');
  chmodSync(path.join(bin, 'ssh'), 0o755);
  const git = (...args) =>
    execFileSync('git', ['-C', repo, '-c', 'user.name=t', '-c', 'user.email=t@t', ...args]).toString().trim();
  try {
    mkdirSync(repo);
    git('init', '-q', '-b', 'main');
    writeFileSync(path.join(repo, 'a.txt'), 'release 0\n');
    git('add', '.');
    git('commit', '-qm', 'release 0');
    git('switch', '-qc', 'staging');
    writeFileSync(path.join(repo, 'a.txt'), 'release 1\n');
    git('commit', '-qam', 'feature');
    const tested = git('rev-parse', 'HEAD');
    git('switch', '-q', 'main');
    git('merge', '-q', '--no-ff', '-m', 'release 1', 'staging');
    const run = (tag, extra = {}) =>
      bash(`cd ${JSON.stringify(repo)}\n${check.run}`, {
        PATH: `${bin}:/usr/bin:/bin`,
        RUNNER_TEMP: dir,
        GITHUB_STEP_SUMMARY: path.join(dir, 'summary'),
        STAGING_KEY: 'fake',
        STAGING_HOST_KEY: check.env.STAGING_HOST_KEY,
        FAKE_TAG: tag,
        ...extra,
      });
    assert.match(run(`sha-${tested.slice(0, 7)}`), /main has exactly the code of sha-/);
    assert.throws(() => run('none'), 'staging runs no release image');
    assert.throws(() => run('sha-0000000'), 'a commit that is not in the repository');
    // A change on main that staging never ran.
    writeFileSync(path.join(repo, 'b.txt'), 'untested\n');
    git('add', '.');
    git('commit', '-qm', 'untested change on main');
    assert.throws(() => run(`sha-${tested.slice(0, 7)}`), 'main differs from staging');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('hotfix mode needs main head to be the merge of a PR with label hotfix', () => {
  const hotfix = step('hotfix');
  assert.equal(hotfix.if, 'inputs.hotfix');
  assert.deepEqual(hotfix.env, {
    GH_TOKEN: '${{ github.token }}',
    REPO: '${{ github.repository }}',
    SOURCE_SHA: '${{ github.sha }}',
  });
  const ids = job.steps.map((s) => s.id);
  assert.ok(ids.indexOf('hotfix') < ids.indexOf('keys'), 'checked before any key is fetched');
  const dir = mkdtempSync(path.join(tmpdir(), 'deploy-hotfix-'));
  const bin = path.join(dir, 'bin');
  mkdirSync(bin);
  // Fake gh: answers "commits/<sha>/pulls" with FAKE_PULLS, fails on any other call.
  writeFileSync(
    path.join(bin, 'gh'),
    '#!/bin/bash\n[[ "$1 $2" == "api repos/phaabe/live.moafunk.de/commits/$SOURCE_SHA/pulls" ]] || exit 9\necho "$FAKE_PULLS"\n'
  );
  chmodSync(path.join(bin, 'gh'), 0o755);
  const sha = 'a'.repeat(40);
  const pr = (extra = {}) => ({
    number: 901,
    merge_commit_sha: sha,
    merged_at: '2026-11-16T10:00:00Z',
    base: { ref: 'main' },
    labels: [{ name: 'type::backend' }, { name: 'hotfix' }],
    ...extra,
  });
  const run = (pulls) =>
    bash(hotfix.run, {
      PATH: `${bin}:/usr/bin:/bin`,
      GITHUB_STEP_SUMMARY: path.join(dir, 'summary'),
      REPO: 'phaabe/live.moafunk.de',
      SOURCE_SHA: sha,
      FAKE_PULLS: JSON.stringify(pulls),
    });
  try {
    assert.match(run([pr()]), /main's head is https:\/\/github\.com\/phaabe\/live\.moafunk\.de\/pull\/901/);
    assert.throws(() => run([]), 'no PR for this commit');
    assert.throws(() => run([pr({ labels: [{ name: 'release::fix' }] })]), 'no hotfix label');
    assert.throws(() => run([pr({ merged_at: null })]), 'not merged');
    assert.throws(() => run([pr({ merge_commit_sha: 'b'.repeat(40) })]), 'a later commit on main');
    assert.throws(() => run([pr({ base: { ref: 'staging' } })]), 'merged into another branch');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('deploy step pins the production host key and runs only the deploy script', () => {
  const deploy = job.steps.find((s) => s.name === 'Deploy');
  assert.match(deploy.env.PROD_HOST_KEY, /^ssh-ed25519 \S+$/);
  assert.equal(deploy.env.TAG, '${{ steps.image.outputs.tag }}');
  assert.equal(deploy.env.DIGEST, '${{ steps.image.outputs.digest }}');
  assert.equal(deploy.env.DRY_RUN, "${{ inputs.dry_run && '1' || '0' }}");
  assert.match(deploy.run, /StrictHostKeyChecking=yes/);
  assert.match(
    deploy.run,
    /"TAG=\$TAG DIGEST=\$DIGEST DRY_RUN=\$DRY_RUN bash -s" <backend\/deploy\/deploy-production\.sh$/m
  );
  // The values are checked before they reach the remote command.
  assert.throws(() => bash(deploy.run, { TAG: 'sha-1234567; reboot', DIGEST: `sha256:${'a'.repeat(64)}`, DRY_RUN: '1' }));
  assert.throws(() => bash(deploy.run, { TAG: 'sha-1234567', DIGEST: 'latest', DRY_RUN: '1' }));
});

test('smoke test after a real deploy: public endpoints, admin page and the exact running image', () => {
  const smoke = job.steps.find((s) => s.name === 'Smoke test (read-only)');
  const deploy = job.steps.find((s) => s.name === 'Deploy');
  assert.equal(job.steps.at(-1), smoke, 'the run ends with the smoke test');
  assert.equal(smoke.if, '${{ !inputs.dry_run }}');
  assert.equal(smoke.env.PROD_HOST_KEY, deploy.env.PROD_HOST_KEY);
  assert.equal(smoke.env.DIGEST, '${{ steps.image.outputs.digest }}');
  assert.match(smoke.run, /StrictHostKeyChecking=yes/);
  const dir = mkdtempSync(path.join(tmpdir(), 'deploy-smoke-'));
  const bin = path.join(dir, 'bin');
  mkdirSync(bin);
  // Fake curl answers like production; fake ssh prints the running image's digests.
  writeFileSync(
    path.join(bin, 'curl'),
    [
      '#!/bin/bash',
      'for url; do :; done',
      'case "$url" in',
      '  */health) echo "{\\"app\\":\\"unheard-backend\\",\\"status\\":\\"${FAKE_HEALTH:-healthy}\\"}" ;;',
      '  */api/stream/status) echo \'{"active":false,"user":null,"recording":false}\' ;;',
      '  */assets/*) exit 0 ;;',
      '  */) echo \'<title>Moafunk Admin</title><script type="module" src="/assets/admin-x1.js"></script>\' ;;',
      'esac',
    ].join('\n') + '\n'
  );
  writeFileSync(path.join(bin, 'ssh'), '#!/bin/bash\necho "$FAKE_RUNNING"\n');
  chmodSync(path.join(bin, 'curl'), 0o755);
  chmodSync(path.join(bin, 'ssh'), 0o755);
  const digest = `sha256:${'a'.repeat(64)}`;
  const run = (extra = {}) =>
    bash(smoke.run, {
      PATH: `${bin}:/usr/bin:/bin`,
      RUNNER_TEMP: dir,
      GITHUB_STEP_SUMMARY: path.join(dir, 'summary'),
      PUBLIC: smoke.env.PUBLIC,
      PROD_IP: '192.0.2.1',
      PROD_KEY: 'fake',
      PROD_HOST_KEY: smoke.env.PROD_HOST_KEY,
      DIGEST: digest,
      FAKE_RUNNING: `ghcr.io/phaabe/live.moafunk.de-backend@${digest}`,
      ...extra,
    });
  try {
    assert.match(run(), /Smoke test passed/);
    assert.throws(() => run({ FAKE_RUNNING: `ghcr.io/phaabe/live.moafunk.de-backend@sha256:${'b'.repeat(64)}` }), 'another image runs');
    assert.throws(() => run({ FAKE_RUNNING: '' }), 'no image runs');
    assert.throws(() => run({ FAKE_HEALTH: 'starting' }), 'the API is not healthy');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
