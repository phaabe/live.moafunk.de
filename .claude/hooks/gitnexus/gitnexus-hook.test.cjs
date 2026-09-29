// Run: node --test .claude/hooks/gitnexus/gitnexus-hook.test.cjs
const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawnSync } = require('child_process');

const HOOK = path.join(__dirname, 'gitnexus-hook.cjs');

function staleRepo(stats) {
  const dir = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'gitnexus-hook-')));
  const git = (...args) => spawnSync('git', args, { cwd: dir, encoding: 'utf-8' });
  git('init', '-q');
  git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '--allow-empty', '-m', 'init');
  fs.mkdirSync(path.join(dir, '.gitnexus'));
  fs.writeFileSync(
    path.join(dir, '.gitnexus', 'meta.json'),
    JSON.stringify({ lastCommit: 'old', stats }),
  );
  return dir;
}

function staleAdvice(stats) {
  const cwd = staleRepo(stats);
  try {
    const result = spawnSync('node', [HOOK], {
      encoding: 'utf-8',
      input: JSON.stringify({
        hook_event_name: 'PostToolUse',
        tool_name: 'Bash',
        tool_input: { command: 'git commit -m x' },
        cwd,
      }),
    });
    assert.strictEqual(result.status, 0, result.stderr);
    return JSON.parse(result.stdout).hookSpecificOutput.additionalContext;
  } finally {
    fs.rmSync(cwd, { recursive: true, force: true });
  }
}

test('stale advice keeps tracked docs unchanged', () => {
  assert.match(staleAdvice({}), /`gitnexus analyze --skip-agents-md --index-only`/);
});

test('stale advice keeps embeddings and tracked docs unchanged', () => {
  assert.match(
    staleAdvice({ embeddings: 3 }),
    /`gitnexus analyze --embeddings --skip-agents-md --index-only`/,
  );
});
