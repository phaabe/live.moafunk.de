// A reload while on air (show 60 was silent for about 2 minutes on 2026-11-19):
// the page asks first, and after the reload the Studio goes back on air with
// only an input, no new soundcheck. The mock keeps the show on air after a
// drop, like the backend.
import { test, expect, type Page } from '@playwright/test';
import { pickInput, soundcheckAndGoLive } from '../lib/studio';

const MOCK = 'http://localhost:8000';
const STUDIO = '/admin/#/stream/live?show=1';

async function liveBroadcasts(page: Page): Promise<number> {
  return (await (await page.request.get(`${MOCK}/mock/live`)).json()).live;
}

test('a reload on air asks first and goes back on air without a soundcheck', async ({ page }) => {
  test.slow();
  expect((await page.request.get(`${MOCK}/mock/mode/ok`)).ok()).toBe(true);
  await page.goto(STUDIO);
  await soundcheckAndGoLive(page);
  await expect.poll(() => liveBroadcasts(page)).toBe(1);

  // Stay: the broadcast goes on.
  const asked = new Promise<string>((resolve) =>
    page.once('dialog', (dialog) => {
      resolve(dialog.type());
      void dialog.dismiss();
    })
  );
  await page.close({ runBeforeUnload: true });
  expect(await asked).toBe('beforeunload');
  await page.waitForTimeout(2000);
  expect(page.isClosed()).toBe(false);
  expect(await liveBroadcasts(page)).toBe(1);

  // Leave: the reload drops the broadcast, the show stays on air.
  page.once('dialog', (dialog) => void dialog.accept());
  await page.reload();
  await page.waitForURL(/#\/stream\/live/);
  await expect(page.getByText('Still on air · no audio')).toBeVisible({ timeout: 15_000 });
  expect(await liveBroadcasts(page)).toBe(0);
  await expect(page.getByRole('button', { name: 'Start test' })).toHaveCount(0);

  await pickInput(page);
  await page.getByRole('button', { name: 'Go back on air' }).click();
  await page.waitForURL(/#\/stream\/on-air/, { timeout: 30_000 });
  await expect.poll(() => liveBroadcasts(page)).toBe(1);
});
