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

// These workflow conditions use only property reads, booleans and comparisons.
// GitHub preserves boolean workflow_dispatch inputs in the inputs context.
function enabled(condition, event, inputs) {
  if (condition === undefined) return true;
  return runInNewContext(
    `Boolean(${condition})`,
    { github: { event_name: event, ref: 'refs/heads/main' }, inputs },
    { timeout: 1000, contextCodeGeneration: { strings: false, wasm: false } }
  );
}

test('only image build and explicitly requested monitoring jobs remain', () => {
  assert.deepEqual(Object.keys(workflow.jobs).sort(), ['build-and-push', 'deploy-monitoring']);
  assert.deepEqual(Object.keys(workflow.on).sort(), ['push', 'workflow_dispatch']);
  assert.deepEqual(workflow.on.push.branches, ['main']);
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
for (const [event, inputs, expected] of cases) {
  test(`${event} ${JSON.stringify(inputs)} selects ${expected.join(', ') || 'no jobs'}`, () => {
    const selected = Object.entries(workflow.jobs)
      .filter(([, job]) => enabled(job.if, event, inputs))
      .map(([id]) => id);
    assert.deepEqual(selected, expected);
    for (const id of selected.filter((id) => id === 'build-and-push')) {
      for (const step of workflow.jobs[id].steps) {
        assert.ok(enabled(step.if, event, inputs), `${step.name} must run in a selected build`);
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
      'docker/setup-qemu-action@v3',
      'docker/setup-buildx-action@v3',
      'docker/login-action@v3',
      'docker/metadata-action@v5',
      'docker/build-push-action@v6',
    ]
  );
  assert.equal(build.steps[0].with['persist-credentials'], false);
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
