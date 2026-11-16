// The chaos test show: a live episode on staging that must survive a
// network cut, a Liquidsoap restart and a page reload by itself, and end in
// exactly one recording as long as the time on air.
import { test, expect, type Page } from '@playwright/test';
import {
  RUN,
  DURATION_MIN,
  staging,
  login,
  createShow,
  bookSlot,
  deleteShow,
  nextMinute,
  toneBack,
} from '../lib/staging';

/** After each disturbance /live.mp3 carries the tone again within this. */
const RECOVER_S = 15;
/** The first time on air: the harbor buffers a new source for several seconds. */
const FIRST_TONE_S = 45;
const CUT_S = 20;
/** Recorded length vs. time on air (Go Live and End Episode are clicks). */
const LENGTH_TOLERANCE_S = 5;

test.describe.configure({ mode: 'serial' });

let page: Page;
let show: { id: number; slug: string } | null = null;
let episode: number | null = null;

test.beforeAll(async ({ browser }) => {
  page = await browser.newPage();
  await login(page);
});

test.afterAll(async () => {
  if (show) await deleteShow(page, show.id, episode ? [episode] : []);
  await page.close();
});

/** From the Studio: pick the (fake) mic, run the soundcheck, Go Live. */
async function soundcheckAndGoLive(): Promise<void> {
  await page.waitForURL(/#\/stream\/live/);
  const input = page.getByLabel('Audio input');
  await expect(input.locator('option')).not.toHaveCount(2, { timeout: 15_000 });
  await input.selectOption({ index: 1 });
  await page.getByRole('button', { name: 'Start test' }).click();
  const sounds = page.getByLabel('Test broadcast sounds right');
  await expect(sounds).toBeEnabled({ timeout: 30_000 });
  await sounds.check();
  const goLive = page.getByRole('button', { name: 'Go Live' });
  await expect(goLive).toBeEnabled({ timeout: 180_000 });
  await goLive.click();
  await page.waitForURL(/#\/stream\/on-air/, { timeout: 30_000 });
}

test('a live show recovers by itself and ends in one recording', async () => {
  test.setTimeout((25 + DURATION_MIN) * 60_000);
  let onAirMs = 0;
  let offAirMs = 0;

  await test.step('create a Show and book a live slot a minute ahead', async () => {
    show = await createShow(page, `e2e-${RUN}-live`);
    await page.locator('a, button').filter({ hasText: /^Book Slot$/ }).click();
    const start = nextMinute(60);
    episode = await bookSlot(page, start, new Date(start.getTime() + (60 + DURATION_MIN) * 60_000));
  });

  const live = await test.step('soundcheck, then Go Live at the start time', async () => {
    await page.getByRole('button', { name: 'Open Studio' }).click();
    await soundcheckAndGoLive();
    const at = Date.now();
    await toneBack(FIRST_TONE_S);
    return at;
  });

  await test.step(`a ${CUT_S} s network cut`, async () => {
    console.log(staging(`e2e-cut ${CUT_S}`));
    await page.waitForTimeout(CUT_S * 1000);
    console.log(`tone back ${await toneBack(RECOVER_S)} s after the cut`);
  });

  await test.step('a Liquidsoap restart on staging', async () => {
    staging('e2e-restart-liquidsoap');
    console.log(`tone back ${await toneBack(RECOVER_S)} s after the restart`);
  });

  await test.step('a page reload', async () => {
    const reloaded = Date.now();
    await page.reload();
    // On Air sends a tab that is not live to the Studio.
    await soundcheckAndGoLive();
    offAirMs += Date.now() - reloaded;
    console.log(`tone back ${await toneBack(RECOVER_S)} s after Go Live`);
  });

  if (DURATION_MIN > 0) {
    await test.step(`soak: ${DURATION_MIN} minutes on air`, async () => {
      const until = Date.now() + DURATION_MIN * 60_000;
      while (Date.now() < until) {
        await page.waitForTimeout(60_000);
        await toneBack(RECOVER_S);
      }
    });
  }

  await test.step('End Episode, Wrap-up, Publish, Finished', async () => {
    await page.getByRole('button', { name: 'End Episode' }).click();
    onAirMs = Date.now() - live - offAirMs;
    await page.waitForURL(new RegExp(`#/shows/${episode}$`));
    const publish = page.getByRole('button', { name: 'Publish' });
    // The wrap-up waits while the recording converts.
    await expect(publish).toBeEnabled({ timeout: 5 * 60_000 });
    await publish.click();
    await expect(page.getByRole('heading', { name: 'Episode published' })).toBeVisible();
    await page.goto(`/#/shows/${show!.slug}`);
    await expect(page.getByText('Finished', { exact: true })).toBeVisible();
  });

  await test.step('exactly one recording, as long as the time on air, in the archive', async () => {
    let lines: string[] = [];
    await expect
      .poll(
        () => {
          lines = staging(`e2e-recordings ${episode}`).split('\n').filter(Boolean);
          return lines.map((l) => l.split(' ')[2]);
        },
        { timeout: 5 * 60_000, intervals: [10_000] }
      )
      .toEqual(['1']);
    const [status, ms] = lines[0].split(' ');
    console.log(`recording ${status}, ${Number(ms) / 1000} s, on air ${onAirMs / 1000} s`);
    // A live show ends as "raw"; "finalized" comes from a later post-production step.
    expect(['raw', 'finalized']).toContain(status);
    expect(Math.abs(Number(ms) - onAirMs) / 1000).toBeLessThan(LENGTH_TOLERANCE_S);
  });
});
