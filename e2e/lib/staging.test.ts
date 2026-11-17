// Unit tests of the clean-up helpers: no browser and no staging box (ssh is a stub).
// Run: npm run unit
import assert from 'node:assert/strict';
import { chmodSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { beforeEach, test } from 'node:test';
import type { Page } from '@playwright/test';
import { deleteLeftovers, deleteShow } from './staging.ts';

// Stub ssh: logs the request (its last argument) and answers like the box.
const bin = mkdtempSync(join(tmpdir(), 'e2e-ssh-'));
const requests = join(bin, 'requests');
writeFileSync(
  join(bin, 'ssh'),
  `#!/bin/sh\nfor last; do :; done\necho "$last" >>"${requests}"\necho 'deleted 0 test Show(s)'\n`
);
chmodSync(join(bin, 'ssh'), 0o755);
process.env.PATH = `${bin}:${process.env.PATH}`;
const boxRequests = () => readFileSync(requests, 'utf8').split('\n').filter(Boolean);

function response(status: number, body: unknown = {}) {
  return {
    ok: () => status >= 200 && status < 300,
    status: () => status,
    text: async () => JSON.stringify(body),
    json: async () => body,
  };
}

/** A page whose API answers from `routes` ("METHOD url"), 404 otherwise; it logs each call. */
function fakePage(routes: Record<string, ReturnType<typeof response>>) {
  const calls: string[] = [];
  const call = (method: string) => async (url: string) => {
    calls.push(`${method} ${url}`);
    return routes[`${method} ${url}`] ?? response(404);
  };
  const page = { request: { get: call('GET'), delete: call('DELETE') } } as unknown as Page;
  return { page, calls };
}

beforeEach(() => writeFileSync(requests, ''));

test('deleteShow deletes the episodes, then runs e2e-cleanup on the box', async () => {
  const { page, calls } = fakePage({
    'DELETE /api/shows/7': response(200),
    'DELETE /api/shows/8': response(200),
  });
  await deleteShow(page, [7, 8]);
  assert.deepEqual(calls, ['DELETE /api/shows/7', 'DELETE /api/shows/8']);
  assert.deepEqual(boxRequests(), ['e2e-cleanup']);
});

test('deleteShow fails on a refused episode delete and leaves the Show for the next run', async () => {
  const { page } = fakePage({ 'DELETE /api/shows/7': response(403, { error: 'Forbidden' }) });
  await assert.rejects(deleteShow(page, [7]), /DELETE \/api\/shows\/7: 403/);
  assert.deepEqual(boxRequests(), []);
});

test('deleteLeftovers deletes the episodes of e2e Shows only, then cleans up once', async () => {
  const { page, calls } = fakePage({
    'GET /api/series': response(200, {
      series: [
        { slug: 'e2e-1-live', name: 'e2e-1-live' },
        { slug: 'jazz', name: 'Jazz' },
        { slug: 'e2e-1-set', name: 'e2e-1-set' },
      ],
    }),
    'GET /api/series/e2e-1-live': response(200, { episodes: [{ id: 3 }] }),
    'GET /api/series/e2e-1-set': response(200, { episodes: [] }),
    'DELETE /api/shows/3': response(200),
  });
  await deleteLeftovers(page);
  assert.deepEqual(calls, [
    'GET /api/series',
    'GET /api/series/e2e-1-live',
    'GET /api/series/e2e-1-set',
    'DELETE /api/shows/3',
  ]);
  assert.deepEqual(boxRequests(), ['e2e-cleanup']);
});
