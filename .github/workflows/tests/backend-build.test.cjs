const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const { chmodSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } = require('node:fs');
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
    // cancelled() is false: these cases model runs nobody cancelled.
    { github: { event_name: event, ref }, inputs, cancelled: () => false },
    { timeout: 1000, contextCodeGeneration: { strings: false, wasm: false } }
  );
}

test('only image build, staging deploy, the test dashboard and explicitly requested monitoring jobs remain', () => {
  assert.deepEqual(Object.keys(workflow.jobs).sort(), [
    'build-and-push',
    'deploy-monitoring',
    'deploy-staging',
    'test-dashboard',
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
  // A merge into staging deploys and never waits for the chaos test show.
  ['push', {}, ['build-and-push', 'deploy-staging', 'test-dashboard'], 'refs/heads/staging'],
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

test('the image plays the stream mounts of the host that serves it', () => {
  // Staging and production run the same image. A full URL here made staging's
  // "What listeners hear" play the production stream
  // (https://github.com/phaabe/live.moafunk.de/issues/1114).
  const image = build.steps.find((step) => step.id === 'image');
  const args = Object.fromEntries(
    image.with['build-args']
      .trim()
      .split('\n')
      .map((line) => line.trim().split(/=(.*)/s, 2))
  );
  assert.equal(args.VITE_STREAM_ICECAST_LIVE_URL, '/live.mp3');
  assert.equal(args.VITE_STREAM_ICECAST_TEST_URL, '/test.mp3');
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

test('no staging deploy starts the chaos test show, and deploys never share the tests\' concurrency group', () => {
  const load = (file) => yaml.load(readFileSync(path.resolve(__dirname, '..', file), 'utf8'));
  assert.equal(staging.outputs, undefined, 'nothing reads a deploy result any more');
  for (const job of Object.values(workflow.jobs)) {
    assert.notEqual(job.uses, './.github/workflows/e2e-staging.yml');
  }
  // GitHub keeps one waiting job per group and cancels the older one, so a
  // shared group drops deploys and test runs (issue 1014). A test holds the
  // box instead, and the deploy waits for that hold.
  const tests = [load('e2e-staging.yml').jobs['chaos-show'], load('e2e-soundcloud.yml').jobs.soundcloud];
  for (const job of tests) {
    assert.notEqual(job.concurrency.group, staging.concurrency.group);
    const names = job.steps.map((step) => step.name ?? step.uses);
    const hold = job.steps.find((step) => step.name === 'Hold staging for this run');
    assert.match(hold.run, /StrictHostKeyChecking=yes/);
    assert.match(hold.run, /"e2e-hold \$E2E_RUN"/);
    assert.ok(names.indexOf(hold.name) < names.indexOf('actions/setup-node@v4'), 'hold before the install');
  }
  assert.ok(staging['timeout-minutes'] > Number(staging.steps.find((step) => step.id === 'deploy').env.WAIT_MINUTES));
  assert.ok(Number(staging.steps.find((step) => step.id === 'deploy').env.WAIT_MINUTES) > tests[0]['timeout-minutes']);
});

test('staging deploy step passes tag and digest, waits for a test hold, skips a release hold, fails otherwise', () => {
  const deploy = staging.steps.find((step) => step.id === 'deploy');
  const dir = mkdtempSync(path.join(tmpdir(), 'staging-deploy-'));
  const bin = path.join(dir, 'bin');
  execFileSync('/bin/mkdir', [bin]);
  // Fake ssh: records its last argument, counts its calls, prints FAKE_OUT and
  // exits with FAKE_EXIT; from call FAKE_OK_FROM on it exits 0.
  writeFileSync(
    path.join(bin, 'ssh'),
    [
      '#!/bin/bash',
      'echo "${@: -1}" >"$RUNNER_TEMP/ssh-request"',
      'echo x >>"$RUNNER_TEMP/ssh-calls"',
      'calls=$(wc -l <"$RUNNER_TEMP/ssh-calls")',
      '[[ -n "${FAKE_OK_FROM:-}" && "$calls" -ge "$FAKE_OK_FROM" ]] && { echo deployed; exit 0; }',
      'printf "%b\\n" "${FAKE_OUT:-}"',
      'exit "${FAKE_EXIT:-0}"',
    ].join('\n')
  );
  chmodSync(path.join(bin, 'ssh'), 0o755);
  const calls = () => {
    const file = path.join(dir, 'ssh-calls');
    const n = readFileSync(file, 'utf8').trim().split('\n').length;
    rmSync(file);
    return n;
  };
  const testHold = 'staging is on hold (2026-11-19T10:00Z | sha-1111111 | e2e run 44507662622); sha-abc1234 was not deployed';
  const releaseHold = 'staging is on hold (2026-11-19T10:00Z | sha-1111111 | r2 release checks); sha-abc1234 was not deployed';
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
        WAIT_MINUTES: '5',
        RETRY_SECONDS: '0',
        ...env,
      },
      stdio: 'pipe',
    }).toString();
  try {
    run({});
    assert.equal(readFileSync(path.join(dir, 'ssh-request'), 'utf8'), `deploy sha-abc1234 ${digest}\n`);
    assert.equal(calls(), 1);
    // A person's release hold: skip this image, green, with a notice.
    assert.match(run({ FAKE_EXIT: '3', FAKE_OUT: releaseHold }), /Staging is on hold/);
    assert.equal(calls(), 1);
    // A test's hold: try again until the test has removed it, then deploy.
    const waited = run({ FAKE_EXIT: '3', FAKE_OUT: testHold, FAKE_OK_FROM: '3' });
    assert.match(waited, /A test holds staging/);
    assert.match(waited, /deployed/);
    assert.doesNotMatch(waited, /Staging is on hold/);
    assert.equal(calls(), 3);
    // A test's hold that does not end: red, not a silent skip.
    assert.throws(
      () => run({ FAKE_EXIT: '3', FAKE_OUT: testHold, WAIT_MINUTES: '0' }),
      (error) => /Staging stays on a test hold/.test(error.stdout.toString())
    );
    assert.equal(calls(), 1);
    assert.throws(() => run({ FAKE_EXIT: '1' }));
    calls();
    rmSync(path.join(dir, 'ssh-request'));
    assert.throws(() => run({ DIGEST: '' }));
    assert.throws(() => readFileSync(path.join(dir, 'ssh-request')), 'no ssh without a digest');
    assert.throws(() => readFileSync(path.join(dir, 'staging-deploy-key')), 'the key file is removed');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('the workflow tests always run on main and staging PRs and pushes, once, without publishing or production access', () => {
  const ci = yaml.load(readFileSync(path.resolve(__dirname, '../epic-ci.yml'), 'utf8'));
  for (const event of ['pull_request', 'push']) {
    assert.equal(ci.on[event].paths, undefined, `${event}: no path filter`);
    for (const branch of ['main', 'staging']) {
      assert.ok(ci.on[event].branches.includes(branch), `${event} on ${branch}`);
    }
  }
  assert.deepEqual(ci.permissions, { contents: 'read' });
  const job = ci.jobs.frontend;
  assert.equal(job.name, 'frontend-ci');
  assert.equal(job.if, undefined);
  assert.equal(job.needs, undefined);
  assert.equal(job.permissions, undefined);
  assert.equal(job.environment, undefined);
  assert.equal(job.uses, undefined);
  assert.equal(job.steps[0].with['persist-credentials'], false);
  // The first two commands: install without scripts, then the workflow tests.
  // So no code from a dependency runs before they read the checkout.
  const [checkout, node, install, tests] = job.steps;
  assert.deepEqual([checkout.uses, node.uses], ['actions/checkout@v4', 'actions/setup-node@v4']);
  assert.equal(install.run, 'npm ci --ignore-scripts');
  assert.equal(tests.run, 'node --test ../.github/workflows/tests/*.test.cjs');
  for (const step of [install, tests]) {
    assert.equal(step.if, undefined);
    assert.equal(step['continue-on-error'], undefined);
  }
  assert.doesNotMatch(JSON.stringify(job), /secrets\.|bitwarden|docker\/|\bssh\b|\bscp\b|packages|id-token/i);
  // No other job runs them a second time.
  const runners = readdirSync(path.resolve(__dirname, '..'))
    .filter((file) => /\.ya?ml$/.test(file))
    .flatMap((file) => {
      const jobs = yaml.load(readFileSync(path.resolve(__dirname, '..', file), 'utf8')).jobs ?? {};
      return Object.entries(jobs)
        .filter(([, j]) => (j.steps ?? []).some((step) => /workflows\/tests\//.test(step.run ?? '')))
        .map(([id]) => `${file}:${id}`);
    });
  assert.deepEqual(runners, ['epic-ci.yml:frontend']);
});

// shortcut: only `**` and `*` are handled, enough for these path filters.
function globToRegExp(glob) {
  const body = glob
    .split('**')
    .map((part) => part.replace(/[.+?^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '[^/]*'))
    .join('.*');
  return new RegExp(`^${body}$`);
}

// Sources of the build-context COPY lines (not COPY --from=<stage>).
function copySources(dockerfile) {
  return dockerfile
    .split('\n')
    .map((line) => line.trim().split(/\s+/))
    .filter(([cmd, ...args]) => cmd === 'COPY' && !args.some((arg) => arg.startsWith('--from=')))
    .flatMap(([, ...args]) => args.filter((arg) => !arg.startsWith('--')).slice(0, -1));
}

// A source is missing when no paths entry matches it. For a directory, a file
// deep inside it must match, so the whole tree is covered.
function uncoveredSources(sources, paths, isDir) {
  const patterns = paths.map(globToRegExp);
  return sources.filter((source) => {
    const sample = isDir(source) ? `${source}/zz-any/zz-file.ts` : source.replace(/\*/g, 'x');
    return !patterns.some((pattern) => pattern.test(sample));
  });
}

test('glob helper matches like the GitHub path filter for ** and *', () => {
  assert.ok(globToRegExp('frontend/**').test('frontend/src/shared/api.ts'));
  assert.ok(globToRegExp('frontend/package*.json').test('frontend/package-lock.json'));
  assert.ok(!globToRegExp('frontend/package*.json').test('frontend/src/package.json'));
  assert.ok(!globToRegExp('frontend/src/admin/**').test('frontend/src/shared/api.ts'));
  assert.ok(!globToRegExp('backend/**').test('backend-old/x'));
});

test('a COPY source with no paths entry is reported', () => {
  const dockerfile = 'COPY --chown=1 frontend ./\nCOPY --from=builder /app/x ./x\nCOPY a.txt b.txt ./\n';
  assert.deepEqual(copySources(dockerfile), ['frontend', 'a.txt', 'b.txt']);
  // The old filter: a change in frontend/src/shared built no image (issue 1017).
  const oldPaths = ['backend/**', 'frontend/src/admin/**', 'frontend/package*.json'];
  assert.deepEqual(uncoveredSources(['frontend'], oldPaths, () => true), ['frontend']);
  assert.deepEqual(uncoveredSources(['frontend/package*.json'], oldPaths, () => false), []);
});

test('every file the backend Dockerfile copies triggers an image build', () => {
  const root = path.resolve(__dirname, '../../..');
  const sources = copySources(readFileSync(path.join(root, 'backend/Dockerfile'), 'utf8'));
  assert.ok(sources.includes('frontend'), 'the admin build copies all of frontend/');
  const isDir = (source) => {
    try {
      return statSync(path.join(root, source)).isDirectory();
    } catch {
      return false; // a glob such as frontend/package*.json
    }
  };
  assert.deepEqual(uncoveredSources(sources, workflow.on.push.paths, isDir), []);
  assert.ok(workflow.on.push.paths.includes('.github/workflows/backend.yml'));
});

test('the backend Dockerfile pulls every base image from mirror.gcr.io', () => {
  // Anonymous ECR Public pulls fail on shared runner IPs (429: rate and data limits).
  const dockerfile = readFileSync(path.resolve(__dirname, '../../../backend/Dockerfile'), 'utf8');
  const images = [...dockerfile.matchAll(/^FROM\s+(\S+)/gm)].map((match) => match[1]);
  assert.ok(images.length > 0);
  for (const image of images) assert.match(image, /^mirror\.gcr\.io\/library\//);
});
