const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const { mkdtempSync, readFileSync, rmSync, writeFileSync } = require('node:fs');
const { tmpdir } = require('node:os');
const path = require('node:path');
const { test } = require('node:test');

const script = path.resolve(__dirname, '../../../scripts/coverage-floors.cjs');
const { checkFloors } = require(script);

// A cargo llvm-cov JSON summary with absolute paths, as CI writes it.
const report = (files) => ({
  data: [
    {
      files: Object.entries(files).map(([file, percent]) => ({
        filename: `/home/runner/work/r/r/backend/${file}`,
        summary: { lines: { percent } },
      })),
    },
  ],
});

test('a module at or above its floor passes', () => {
  const { ok, lines } = checkFloors(report({ 'src/auth.rs': 93, 'src/scheduler.rs': 78 }), {
    'src/auth.rs': 91,
    'src/scheduler.rs': 78,
  });
  assert.equal(ok, true);
  assert.deepEqual(lines, [
    'ok   src/auth.rs: 93.0 % of lines, floor 91 %',
    'ok   src/scheduler.rs: 78.0 % of lines, floor 78 %',
  ]);
});

test('a module below its floor fails; other files are not gated', () => {
  const { ok, lines } = checkFloors(report({ 'src/auth.rs': 90.9, 'src/other.rs': 1 }), { 'src/auth.rs': 91 });
  assert.equal(ok, false);
  assert.deepEqual(lines, ['FAIL src/auth.rs: 90.9 % of lines, floor 91 %']);
});

test('a gated module missing from the report fails', () => {
  const { ok, lines } = checkFloors(report({ 'src/auth.rs': 93 }), { 'src/booking.rs': 50 });
  assert.equal(ok, false);
  assert.deepEqual(lines, ['FAIL src/booking.rs: not in the coverage report']);
});

test('the path matches whole directories: src/recording.rs is not src/handlers/recording.rs', () => {
  const { ok } = checkFloors(report({ 'src/handlers/recording.rs': 90 }), { 'src/recording.rs': 10 });
  assert.equal(ok, false);
});

test('the command exits 1 below a floor and 0 otherwise', () => {
  const dir = mkdtempSync(path.join(tmpdir(), 'floors-'));
  try {
    const coverage = path.join(dir, 'coverage.json');
    const floors = path.join(dir, 'floors.json');
    writeFileSync(coverage, JSON.stringify(report({ 'src/auth.rs': 93 })));
    writeFileSync(floors, JSON.stringify({ 'src/auth.rs': 91 }));
    assert.match(execFileSync('node', [script, coverage, floors], { encoding: 'utf8' }), /ok {3}src\/auth\.rs/);

    writeFileSync(floors, JSON.stringify({ 'src/auth.rs': 95 }));
    assert.throws(() => execFileSync('node', [script, coverage, floors], { stdio: 'pipe' }), (e) => e.status === 1);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('the floors cover the critical modules and are whole percents', () => {
  const floors = JSON.parse(readFileSync(path.resolve(__dirname, '../../../backend/coverage-floors.json'), 'utf8'));
  assert.deepEqual(Object.keys(floors).sort(), [
    'src/auth.rs',
    'src/handlers/booking.rs',
    'src/handlers/recording.rs',
    'src/handlers/stream_ws.rs',
    'src/recording.rs',
    'src/recording_finalizer.rs',
    'src/scheduler.rs',
    'src/stream_bridge.rs',
  ]);
  for (const floor of Object.values(floors)) assert.ok(Number.isInteger(floor) && floor > 0 && floor <= 100);
});
