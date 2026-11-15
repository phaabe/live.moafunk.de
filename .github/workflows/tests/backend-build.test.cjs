const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const { mkdtempSync, readFileSync, rmSync } = require('node:fs');
const { createRequire } = require('node:module');
const { tmpdir } = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const { runInNewContext } = require('node:vm');

const frontendRequire = createRequire(path.resolve(__dirname, '../../../frontend/package.json'));
const yaml = frontendRequire('js-yaml');
const workflow = yaml.load(readFileSync(path.resolve(__dirname, '../backend.yml'), 'utf8'));
const build = workflow.jobs['build-and-push'];
const monitoring = workflow.jobs['deploy-monitoring'];
const staging = workflow.jobs['deploy-staging'];

// These workflow conditions use only property reads, booleans and comparisons.
// GitHub preserves boolean workflow_dispatch inputs in the inputs context.
function enabled(condition, event, inputs, ref = 'refs/heads/main') {
  if (condition === undefined) return true;
  return runInNewContext(
    `Boolean(${condition})`,
    { github: { event_name: event, ref }, inputs },
    { timeout: 1000, contextCodeGeneration: { strings: false, wasm: false } }
  );
}

test('only image build, staging deploy and explicitly requested monitoring jobs remain', () => {
  assert.deepEqual(Object.keys(workflow.jobs).sort(), [
    'build-and-push',
    'deploy-monitoring',
    'deploy-staging',
  ]);
  assert.deepEqual(Object.keys(workflow.on).sort(), ['push', 'workflow_dispatch']);
  assert.deepEqual(workflow.on.push.branches, ['main', 'staging']);
  assert.deepEqual(Object.keys(workflow.on.workflow_dispatch.inputs).sort(), [
    'build',
    'deploy_monitoring',
  ]);
  assert.equal(workflow.on.workflow_dispatch.inputs.build.type, 'boolean');
  assert.equal(workflow.on.workflow_dispatch.inputs.build.default, true);
  assert.equal(workflow.on.workflow_dispatch.inputs.deploy_monitoring.type, 'boolean');
  assert.equal(workflow.on.workflow_dispatch.inputs.deploy_monitoring.default, false);
});

const cases = [
  ['push', {}, ['build-and-push']],
  ['push', {}, ['build-and-push', 'deploy-staging'], 'refs/heads/staging'],
  ['workflow_dispatch', { build: true, deploy_monitoring: false }, ['build-and-push'], 'refs/heads/staging'],
  ['workflow_dispatch', { build: true, deploy_monitoring: false }, ['build-and-push']],
  ['workflow_dispatch', { build: false, deploy_monitoring: false }, []],
  ['workflow_dispatch', { build: false, deploy_monitoring: true }, ['deploy-monitoring']],
  [
    'workflow_dispatch',
    { build: true, deploy_monitoring: true },
    ['build-and-push', 'deploy-monitoring'],
  ],
  // Removed inputs must not leave an executable deployment path behind.
  [
    'workflow_dispatch',
    { build: true, deploy_hetzner: true, setup_nginx: true, init_db: true },
    ['build-and-push'],
  ],
];
for (const [event, inputs, expected, ref = 'refs/heads/main'] of cases) {
  test(`${event} on ${ref} ${JSON.stringify(inputs)} selects ${expected.join(', ') || 'no jobs'}`, () => {
    const selected = Object.entries(workflow.jobs)
      .filter(([, job]) => enabled(job.if, event, inputs, ref))
      .map(([id]) => id);
    assert.deepEqual(selected, expected);
    for (const id of selected.filter((id) => id === 'build-and-push')) {
      for (const step of workflow.jobs[id].steps) {
        assert.ok(enabled(step.if, event, inputs, ref), `${step.name} must run in a selected build`);
      }
    }
  });
}

test('build has only registry access and no host mutation path', () => {
  assert.deepEqual(workflow.permissions, { contents: 'read' });
  assert.deepEqual(build.permissions, { contents: 'read', packages: 'write' });
  assert.equal(build.uses, undefined);
  assert.equal(build.environment, undefined);
  assert.deepEqual(
    build.steps.filter((step) => step.uses).map((step) => step.uses),
    [
      'actions/checkout@v4',
      'docker/setup-buildx-action@v3',
      'docker/login-action@v3',
      'docker/metadata-action@v5',
      'docker/build-push-action@v6',
    ]
  );
  assert.equal(build.steps[0].with['persist-credentials'], false);
  const buildx = build.steps.find((step) => step.uses === 'docker/setup-buildx-action@v3');
  assert.equal(buildx.with['driver-opts'], 'image=mirror.gcr.io/moby/buildkit:buildx-stable-1');
  const text = JSON.stringify(build);
  assert.deepEqual(
    [...text.matchAll(/secrets\.([A-Z_]+)/g)].map((match) => match[1]),
    ['GITHUB_TOKEN']
  );
  assert.doesNotMatch(
    text,
    /bitwarden|\bssh\b|\bscp\b|deploy_hetzner|systemctl|init_db|setup_nginx/i
  );
  assert.deepEqual(
    build.steps.filter((step) => step.run).map((step) => step.id),
    ['image-reference']
  );
});

test('build cancellation cannot cancel monitoring or a legacy deployment run', () => {
  assert.equal(workflow.concurrency, undefined);
  assert.deepEqual(build.concurrency, {
    group: 'backend-image-build-${{ github.ref }}',
    'cancel-in-progress': true,
  });
  assert.deepEqual(monitoring.concurrency, {
    group: 'backend-monitoring-production',
    'cancel-in-progress': false,
  });
  assert.equal(monitoring.needs, undefined);
  assert.equal(monitoring.permissions, undefined);
});

test('build preserves SHA tags and exports the digest, exact image and source SHA', () => {
  const metadata = build.steps.find((step) => step.id === 'meta');
  assert.match(metadata.with.tags, /type=raw,value=latest,enable=\{\{is_default_branch\}\}/);
  assert.match(metadata.with.tags, /^type=sha,prefix=sha-$/m);
  assert.match(metadata.with.tags, /^type=sha,prefix=sha-,format=long$/m);
  const image = build.steps.find((step) => step.id === 'image');
  assert.equal(image.uses, 'docker/build-push-action@v6');
  assert.equal(image.with.push, true);
  assert.equal(image.with.context, '.');
  assert.equal(image.with.file, 'backend/Dockerfile');
  assert.equal(image.with.platforms, 'linux/amd64');
  assert.equal(image.with.tags, '${{ steps.meta.outputs.tags }}');
  assert.equal(image.with.labels, '${{ steps.meta.outputs.labels }}');
  assert.deepEqual(build.outputs, {
    digest: '${{ steps.image.outputs.digest }}',
    image: '${{ steps.image-reference.outputs.image }}',
    'source-sha': '${{ github.sha }}',
  });
});

test('image summary records the exact image and source without contacting a host', () => {
  const step = build.steps.find((step) => step.id === 'image-reference');
  assert.ok(step);
  assert.equal(step.env.DIGEST, '${{ steps.image.outputs.digest }}');
  assert.equal(step.env.SOURCE_SHA, '${{ github.sha }}');
  assert.equal(step.env.IMAGE, '${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}');
  const dir = mkdtempSync(path.join(tmpdir(), 'backend-build-summary-'));
  const image = 'ghcr.io/example/live.moafunk.de-backend';
  const digest = `sha256:${'a'.repeat(64)}`;
  const source = 'b'.repeat(40);
  const env = {
    PATH: '/usr/bin:/bin',
    IMAGE: image,
    DIGEST: digest,
    SOURCE_SHA: source,
    GITHUB_OUTPUT: path.join(dir, 'output'),
    GITHUB_STEP_SUMMARY: path.join(dir, 'summary'),
  };
  try {
    execFileSync('/bin/bash', ['--noprofile', '--norc', '-e', '-c', step.run], { env });
    assert.equal(readFileSync(env.GITHUB_OUTPUT, 'utf8'), `image=${image}@${digest}\n`);
    const summary = readFileSync(env.GITHUB_STEP_SUMMARY, 'utf8');
    assert.ok(summary.includes(`${image}@${digest}`));
    assert.ok(summary.includes(source));
    assert.throws(() =>
      execFileSync('/bin/bash', ['--noprofile', '--norc', '-e', '-c', step.run], {
        env: { ...env, DIGEST: '' },
        stdio: 'pipe',
      })
    );
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('staging deploy reaches only the staging box, with the exact image built', () => {
  assert.equal(staging.needs, 'build-and-push');
  assert.deepEqual(staging.permissions, { contents: 'read' });
  assert.deepEqual(staging.concurrency, { group: 'staging-deploy', 'cancel-in-progress': false });
  assert.deepEqual(staging.environment, { name: 'staging', url: 'https://staging.live.moafunk.de' });
  const text = JSON.stringify(staging);
  assert.deepEqual(
    [...text.matchAll(/secrets\.([A-Z_]+)/g)].map((match) => match[1]),
    ['BW_ACCESS_TOKEN']
  );
  assert.doesNotMatch(text, /HETZNER|178\.104\.160\.103|admin\.live|deploy_hetzner|scp/i);
  const key = staging.steps.find((step) => step.id === 'key');
  assert.equal(key.uses, 'bitwarden/sm-action@v2');
  assert.match(
    key.with.secrets.trim(),
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12} > STAGING_DEPLOY_SSH_KEY$/,
    'exactly one Bitwarden secret: the staging deploy key'
  );
  const deploy = staging.steps.find((step) => step.id === 'deploy');
  assert.equal(deploy.env.DIGEST, '${{ needs.build-and-push.outputs.digest }}');
  assert.equal(deploy.env.SOURCE_SHA, '${{ needs.build-and-push.outputs.source-sha }}');
  assert.match(deploy.env.HOST_KEY, /^staging\.live\.moafunk\.de ssh-ed25519 \S+$/);
  assert.match(deploy.run, /StrictHostKeyChecking=yes/);
  assert.match(deploy.run, /root@staging\.live\.moafunk\.de "deploy \$tag \$DIGEST"/);
});

test('staging deploy step passes tag and digest, treats a hold as success, fails otherwise', () => {
  const deploy = staging.steps.find((step) => step.id === 'deploy');
  const dir = mkdtempSync(path.join(tmpdir(), 'staging-deploy-'));
  const bin = path.join(dir, 'bin');
  execFileSync('/bin/mkdir', [bin]);
  // Fake ssh: records its last argument and exits with FAKE_EXIT.
  execFileSync('/bin/bash', ['-c', `printf '%s\\n' '#!/bin/bash' 'echo "\${@: -1}" >"$RUNNER_TEMP/ssh-request"' 'exit "\${FAKE_EXIT:-0}"' >"${bin}/ssh" && chmod +x "${bin}/ssh"`]);
  const digest = `sha256:${'c'.repeat(64)}`;
  const source = `abc1234${'d'.repeat(33)}`;
  const run = (env) =>
    execFileSync('/bin/bash', ['--noprofile', '--norc', '-e', '-c', deploy.run], {
      env: {
        PATH: `${bin}:/usr/bin:/bin`,
        RUNNER_TEMP: dir,
        DEPLOY_KEY: 'fake-key',
        HOST_KEY: 'staging.live.moafunk.de ssh-ed25519 AAAA',
        SOURCE_SHA: source,
        DIGEST: digest,
        ...env,
      },
      stdio: 'pipe',
    }).toString();
  try {
    run({});
    assert.equal(readFileSync(path.join(dir, 'ssh-request'), 'utf8'), `deploy sha-abc1234 ${digest}\n`);
    assert.match(run({ FAKE_EXIT: '3' }), /Staging is on hold/);
    assert.throws(() => run({ FAKE_EXIT: '1' }));
    rmSync(path.join(dir, 'ssh-request'));
    assert.throws(() => run({ DIGEST: '' }));
    assert.throws(() => readFileSync(path.join(dir, 'ssh-request')), 'no ssh without a digest');
    assert.throws(() => readFileSync(path.join(dir, 'staging-deploy-key')), 'the key file is removed');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('regression CI always checks main and staging PRs and pushes without publishing or production access', () => {
  const ci = yaml.load(readFileSync(path.resolve(__dirname, '../backend-build-tests.yml'), 'utf8'));
  assert.deepEqual(ci.on, {
    pull_request: { branches: ['main', 'staging'] },
    push: { branches: ['main', 'staging'] },
  });
  assert.deepEqual(ci.permissions, { contents: 'read' });
  assert.deepEqual(Object.keys(ci.jobs), ['backend-build-tests']);
  const job = ci.jobs['backend-build-tests'];
  assert.equal(job.if, undefined);
  assert.equal(job.permissions, undefined);
  assert.equal(job.environment, undefined);
  assert.equal(job.uses, undefined);
  assert.deepEqual(
    job.steps.filter((step) => step.uses).map((step) => step.uses),
    ['actions/checkout@v4', 'actions/setup-node@v4']
  );
  assert.equal(job.steps[0].with['persist-credentials'], false);
  assert.deepEqual(
    job.steps.filter((step) => step.run).map((step) => [step.run, step['working-directory']]),
    [
      ['npm ci --ignore-scripts', 'frontend'],
      ['node --test .github/workflows/tests/backend-build.test.cjs', undefined],
      ['node --test .github/workflows/tests/deploy-production.test.cjs', undefined],
    ]
  );
  for (const step of job.steps) {
    assert.equal(step.if, undefined);
    assert.equal(step['continue-on-error'], undefined);
  }
  assert.doesNotMatch(
    JSON.stringify(ci),
    /secrets\.|bitwarden|docker\/|\bssh\b|\bscp\b|packages|id-token|upload-artifact/i
  );
});
