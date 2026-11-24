// An uploaded short set, booked a minute ahead, starts by itself and is
// published.
import { test, expect, type Page } from '@playwright/test';
import {
  RUN,
  SET_MP3,
  login,
  createShow,
  bookSlot,
  deleteShow,
  deleteLeftovers,
  nextMinute,
  toneBack,
} from '../lib/staging';

/** The scheduler starts a set up to 30 s late; the harbor buffers a few more. */
const SET_STARTS_S = 75;

let page: Page;
let show: { id: number; slug: string } | null = null;
let episode: number | null = null;

test.beforeAll(async ({ browser }) => {
  page = await browser.newPage();
  await login(page);
  await deleteLeftovers(page);
});

test.afterAll(async () => {
  if (show) await deleteShow(page, episode ? [episode] : []);
  await page.close();
});

test('an uploaded set starts by itself and is published', async () => {
  test.setTimeout(10 * 60_000);
  const start = nextMinute(90);
  const end = new Date(start.getTime() + 2 * 60_000);

  await test.step('create a Show, upload a set, book it a minute ahead', async () => {
    show = await createShow(page, `e2e-${RUN}-set`);
    await page.getByRole('button', { name: 'Book another date' }).click();
    // The radio switches the page to ?mode=prerecorded; check() would wait for
    // the old radio forever.
    await page.getByLabel('Pre-recorded').click();
    await page.waitForURL(/mode=prerecorded/);
    await page.locator('input[type=file]').setInputFiles(SET_MP3);
    await expect(page.getByText(/· uploaded/)).toBeVisible({ timeout: 60_000 });
    episode = await bookSlot(page, start, end);
  });

  await test.step('the set goes on air by itself', async () => {
    await page.waitForTimeout(Math.max(0, start.getTime() - Date.now()));
    await toneBack(SET_STARTS_S);
  });

  await test.step('after its end: Publish episode, Finished', async () => {
    await page.waitForTimeout(Math.max(0, end.getTime() - Date.now()) + 5_000);
    // After its end the episode is "wrapup_open": the page opens on its Recording card.
    await page.reload();
    const publish = page.getByRole('button', { name: 'Publish episode' });
    await expect(publish).toBeEnabled({ timeout: 60_000 });
    await publish.click();
    await expect(publish).toHaveCount(0);
    await page.goto(`/#/shows/${show!.slug}`);
    await expect(page.getByText('Finished', { exact: true })).toBeVisible();
  });
});
