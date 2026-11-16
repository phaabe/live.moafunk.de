import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect } from 'vitest';
import { checkScreen } from './check';

/**
 * Test helper: check a mounted screen against its flow screen. The result is
 * kept in flow/sync/<id>.json (`npx vitest run -u` rewrites it) and goes into
 * the build's /flow-sync.json. A `state` checks one more state of the screen,
 * for example a dropped connection, against the same flow screen.
 */
export async function expectFlow(id: string, root: Element, state?: string): Promise<void> {
  const { result, unexplained, stale } = checkScreen(id, root, state);
  // Not new URL(..., import.meta.url): under jsdom that is jsdom's URL, which fileURLToPath refuses.
  const name = state ? `${id}@${state}` : id;
  const file = join(dirname(fileURLToPath(import.meta.url)), 'sync', `${name}.json`);
  await expect(JSON.stringify(result, null, 2) + '\n').toMatchFileSnapshot(file);
  expect(unexplained, `${name}: drift that flow-gaps.json does not name`).toEqual([]);
  expect(stale, `${name}: flow-gaps.json entries that no longer apply`).toEqual([]);
}
