// SoundCloud on staging: the server is connected to the test account and its
// token still works at SoundCloud (refreshed when it expires). Then a host
// uploads a recording to an episode and sends it to SoundCloud. Runs every
// night, so a lost connection shows up before a host needs it. Staging keeps
// only the test account's newest tracks (SOUNDCLOUD_KEEP_TRACKS).
import { test, expect, type Page } from '@playwright/test';
import {
  SET_MP3,
  login,
  createShow,
  bookSlot,
  deleteShow,
  deleteLeftovers,
  nextMinute,
} from '../lib/staging';

/** The staging test account (SOUNDCLOUD_ACCOUNT in backend/staging/staging.env.example). */
const TEST_ACCOUNT = 'moafunk-test';
/** One name for every run, so all runs share one playlist on the test account. */
const SHOW_NAME = 'e2e-soundcloud';

test.describe.configure({ mode: 'serial' });

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

test('staging is connected to the SoundCloud test account', async () => {
  const status = await page.request.get('/api/soundcloud/status');
  expect(status.ok(), `GET /api/soundcloud/status: ${status.status()}`).toBe(true);
  const { configured, authorized } = (await status.json()) as {
    configured: boolean;
    authorized: boolean;
  };
  expect(configured, 'staging has no SoundCloud app: run backend/staging/push-secrets.sh').toBe(true);
  expect(
    authorized,
    `staging is not connected: click Connect on staging and log in as ${TEST_ACCOUNT}`
  ).toBe(true);

  // Asks SoundCloud itself, with the token an upload would use.
  const check = await page.request.get('/api/soundcloud/check');
  const body = await check.text();
  expect(check.ok(), `GET /api/soundcloud/check: ${check.status()} ${body}`).toBe(true);
  expect((JSON.parse(body) as { account: string }).account).toBe(TEST_ACCOUNT);
});

test('a host uploads a recording and sends it to SoundCloud', async () => {
  test.setTimeout(6 * 60_000);

  await test.step('create a Show and book a live slot a day ahead', async () => {
    show = await createShow(page, SHOW_NAME);
    await page.locator('a, button').filter({ hasText: /^Book Slot$/ }).click();
    // A day ahead: the episode never goes on air during the test.
    const start = nextMinute(24 * 3600);
    episode = await bookSlot(page, start, new Date(start.getTime() + 60 * 60_000));
  });

  const send = page.getByRole('button', { name: 'Upload to SoundCloud' });

  await test.step('upload the recording on the episode page', async () => {
    await page.getByRole('tab', { name: /Recording/ }).click();
    await page.getByLabel('Recording file').setInputFiles(SET_MP3);
    // The button opens once the episode has its recording.
    await expect(send).toBeEnabled({ timeout: 2 * 60_000 });
  });

  await test.step('Upload to SoundCloud gives a track link', async () => {
    await send.click();
    await expect(page.getByRole('link', { name: 'Open on SoundCloud' })).toBeVisible({
      timeout: 3 * 60_000,
    });
    await expect(page.getByRole('alert')).toHaveCount(0);
    const res = await page.request.get(`/api/shows/${episode}`);
    expect(res.ok(), `GET /api/shows/${episode}: ${res.status()}`).toBe(true);
    const detail = (await res.json()) as { soundcloud_track_id?: string };
    expect(detail.soundcloud_track_id, 'the episode stores its SoundCloud track').toBeTruthy();
  });
});
