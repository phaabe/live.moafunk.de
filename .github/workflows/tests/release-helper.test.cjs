const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const { mkdtempSync, readFileSync, rmSync, writeFileSync, chmodSync, mkdirSync } = require('node:fs');
const { tmpdir } = require('node:os');
const path = require('node:path');
const { test } = require('node:test');

const script = path.resolve(__dirname, '../../../scripts/release.sh');

// Fake gh: logs every call (with the body or notes file) and answers like GitHub.
const fakeGh = `#!/usr/bin/env node
const fs = require('fs');
const args = process.argv.slice(2);
const files = {};
for (const flag of ['--body-file', '--notes-file']) {
  const i = args.indexOf(flag);
  if (i >= 0) files[flag] = fs.readFileSync(args[i + 1], 'utf8');
}
fs.appendFileSync(process.env.GH_LOG, JSON.stringify({ args, files }) + '\\n');
if (args[0] === 'api' && args[1].endsWith('/releases/generate-notes')) {
  console.log(JSON.stringify({ name: 'x', body: process.env.FAKE_NOTES }));
} else if (args[0] === 'api' && args[1].includes('/pulls?')) {
  console.log(process.env.FAKE_PULLS);
} else if (args[0] === 'pr' || args[0] === 'release') {
  console.log('https://github.com/phaabe/live.moafunk.de/pull/1');
} else {
  process.exit(9);
}
`;

test('release helper opens the release PR, then tags the merge commit and creates the Release', () => {
  const dir = mkdtempSync(path.join(tmpdir(), 'release-helper-'));
  const origin = path.join(dir, 'origin.git');
  const work = path.join(dir, 'work');
  const bin = path.join(dir, 'bin');
  const log = path.join(dir, 'gh.log');
  mkdirSync(bin);
  writeFileSync(path.join(bin, 'gh'), fakeGh);
  chmodSync(path.join(bin, 'gh'), 0o755);
  const env = {
    PATH: `${bin}:${path.dirname(process.execPath)}:/usr/bin:/bin`,
    HOME: dir,
    GH_LOG: log,
    GIT_AUTHOR_NAME: 't',
    GIT_AUTHOR_EMAIL: 't@t',
    GIT_COMMITTER_NAME: 't',
    GIT_COMMITTER_EMAIL: 't@t',
    FAKE_NOTES: "## What's Changed\n### Features\n* feat: x by @a in https://github.com/phaabe/live.moafunk.de/pull/2",
  };
  const git = (...args) => execFileSync('git', ['-C', work, ...args], { env, stdio: 'pipe' }).toString().trim();
  const originGit = (...args) => execFileSync('git', ['--git-dir', origin, ...args], { env }).toString().trim();
  const calls = () =>
    readFileSync(log, 'utf8')
      .trim()
      .split('\n')
      .map((l) => JSON.parse(l));
  const release = (args, extra = {}) => {
    writeFileSync(log, '');
    return execFileSync('/bin/bash', [script, ...args], { cwd: work, env: { ...env, ...extra }, stdio: 'pipe' }).toString();
  };
  // A refusal must fail with its own message, not by accident.
  const refuses = (args, message, extra = {}) =>
    assert.throws(() => release(args, extra), (err) => message.test(err.stderr.toString()));
  try {
    execFileSync('git', ['init', '-q', '--bare', '-b', 'main', origin], { env });
    execFileSync('git', ['clone', '-q', origin, work], { env, stdio: 'pipe' });
    git('symbolic-ref', 'HEAD', 'refs/heads/main');
    writeFileSync(path.join(work, 'a.txt'), 'release 0\n');
    git('add', '.');
    git('commit', '-qm', 'release 0');
    git('tag', '-a', 'r0', '-m', 'Release r0');
    git('push', '-q', 'origin', 'main', 'r0');
    git('switch', '-qc', 'staging');
    writeFileSync(path.join(work, 'a.txt'), 'release 1\n');
    git('commit', '-qam', 'feature');
    const tested = git('rev-parse', 'HEAD');
    // Staging moves on after the hold; the release is cut from the tested commit.
    writeFileSync(path.join(work, 'b.txt'), 'later\n');
    git('add', '.');
    git('commit', '-qm', 'later feature');
    git('push', '-q', 'origin', 'staging');
    git('switch', '-qc', 'side', 'main');
    writeFileSync(path.join(work, 'c.txt'), 'never on staging\n');
    git('add', '.');
    git('commit', '-qm', 'side');
    const side = git('rev-parse', 'HEAD');

    // Bad calls change nothing.
    refuses(['pr', 'v1', tested], /usage/);
    refuses(['pr', 'r1'], /usage/);
    refuses(['ship', 'r1'], /usage/);
    refuses(['pr', 'r1', side], /is not on staging/);
    refuses(['pr', 'r0', tested], /no release tag before r0/);
    assert.equal(originGit('branch', '--list', 'release/*'), '');

    // pr: the branch at the tested commit, notes since r0, PR into main.
    assert.match(release(['pr', 'r1', `sha-${tested.slice(0, 7)}`]), /merge commit/);
    assert.equal(originGit('rev-parse', 'release/r1'), tested);
    const [notes, pr] = calls();
    assert.deepEqual(notes.args, [
      'api',
      'repos/phaabe/live.moafunk.de/releases/generate-notes',
      '-f',
      'tag_name=r1',
      '-f',
      'target_commitish=release/r1',
      '-f',
      'previous_tag_name=r0',
    ]);
    assert.deepEqual(pr.args.slice(0, -1), [
      'pr', 'create', '--repo', 'phaabe/live.moafunk.de', '--base', 'main', '--head', 'release/r1',
      '--title', 'Release r1', '--body-file',
    ]);
    assert.equal(pr.files['--body-file'].trim(), env.FAKE_NOTES);
    refuses(['pr', 'r1', tested], /release\/r1 exists already/);

    // tag: refuses before a merge, and after a squash.
    const pulls = (merge) =>
      JSON.stringify([
        { number: 900, merged_at: null, merge_commit_sha: 'f'.repeat(40), head: { sha: tested } },
        ...(merge ? [{ number: 901, merged_at: '2026-11-16T10:00:00Z', merge_commit_sha: merge, head: { sha: tested } }] : []),
      ]);
    refuses(['tag', 'r1'], /no merged PR from release\/r1 into main/, { FAKE_PULLS: pulls() });
    assert.match(calls()[0].args[1], /^repos\/phaabe\/live\.moafunk\.de\/pulls\?state=closed&base=main&head=phaabe:release\/r1$/);
    git('switch', '-q', 'main');
    git('merge', '-q', '--squash', 'origin/release/r1');
    git('commit', '-qm', 'Release r1 (squash)');
    git('push', '-q', 'origin', 'main');
    refuses(['tag', 'r1'], /was not merged with a merge commit/, { FAKE_PULLS: pulls(git('rev-parse', 'HEAD')) });
    assert.equal(originGit('tag', '--list', 'r1'), '');

    // tag after a merge commit: annotated tag rN on it, Release with the same notes.
    git('reset', '-q', '--hard', 'origin/main~1');
    git('merge', '-q', '--no-ff', '-m', 'Merge release r1', 'origin/release/r1');
    git('push', '-q', '--force', 'origin', 'main');
    const merge = git('rev-parse', 'HEAD');
    release(['tag', 'r1'], { FAKE_PULLS: pulls(merge) });
    assert.equal(originGit('cat-file', '-t', 'r1'), 'tag');
    assert.equal(originGit('rev-parse', 'r1^{commit}'), merge);
    const [, again, created] = calls();
    assert.deepEqual(again.args.slice(4), ['-f', 'target_commitish=' + tested, '-f', 'previous_tag_name=r0']);
    assert.deepEqual(created.args.slice(0, -1), [
      'release', 'create', 'r1', '--repo', 'phaabe/live.moafunk.de', '--verify-tag', '--title', 'Release r1',
      '--notes-file',
    ]);
    assert.equal(created.files['--notes-file'].trim(), env.FAKE_NOTES);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
