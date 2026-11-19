const assert = require('node:assert/strict');
const { readdirSync, readFileSync } = require('node:fs');
const { createRequire } = require('node:module');
const path = require('node:path');
const { test } = require('node:test');

const root = path.resolve(__dirname, '../../..');
const yaml = createRequire(path.join(root, 'frontend/package.json'))('js-yaml');
const dir = path.join(root, '.github/workflows');

// The Actions list sorts by name, so the prefix groups the workflows.
const prefixes = ['CHECK', 'TEST', 'GATE', 'DEPLOY', 'OPS'];
// The epic guard is disabled and keeps its old names.
const unchanged = ['epic-guard.yml', 'epic-guard-tests.yml'];

test('every workflow name starts with its group and is unique', () => {
  const names = readdirSync(dir)
    .filter((file) => /\.ya?ml$/.test(file) && !unchanged.includes(file))
    .map((file) => [file, yaml.load(readFileSync(path.join(dir, file), 'utf8')).name]);
  assert.ok(names.length > 0);
  for (const [file, name] of names) {
    assert.match(name, new RegExp(`^(${prefixes.join('|')}) - \\S`), `${file}: "${name}"`);
  }
  const all = names.map(([, name]) => name);
  assert.deepEqual(all, [...new Set(all)], 'names are unique');
});
