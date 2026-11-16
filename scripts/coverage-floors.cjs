#!/usr/bin/env node
// Fails when a critical backend module's line coverage drops below its floor.
// Only the files in the floors file are gated; all others stay reported.
//
// Usage: node scripts/coverage-floors.cjs <cargo llvm-cov JSON> <floors JSON>
// The floors file maps a path under backend/ to its lowest line %.
'use strict';

const { readFileSync } = require('node:fs');

/** One result line per floor, and whether every file holds its floor. */
function checkFloors(coverage, floors) {
  const files = coverage.data[0].files;
  const lines = [];
  let ok = true;
  for (const [file, floor] of Object.entries(floors)) {
    const found = files.find((f) => f.filename.endsWith(`/${file}`));
    if (!found) {
      ok = false;
      lines.push(`FAIL ${file}: not in the coverage report`);
      continue;
    }
    const percent = found.summary.lines.percent;
    const held = percent >= floor;
    ok &&= held;
    lines.push(`${held ? 'ok  ' : 'FAIL'} ${file}: ${percent.toFixed(1)} % of lines, floor ${floor} %`);
  }
  return { ok, lines };
}

if (require.main === module) {
  const [coveragePath, floorsPath] = process.argv.slice(2);
  const read = (file) => JSON.parse(readFileSync(file, 'utf8'));
  const { ok, lines } = checkFloors(read(coveragePath), read(floorsPath));
  console.log(lines.join('\n'));
  if (!ok) {
    console.error(`\nA critical module is below its coverage floor. Add tests, or lower the floor in ${floorsPath} with a reason.`);
    process.exit(1);
  }
}

module.exports = { checkFloors };
