// The chaos test show: a live episode on staging that must survive a
// network cut, a Liquidsoap restart and a page reload by itself, and end in
// exactly one recording as long as the time on air.
import { test, expect, type Dialog, type Page } from '@playwright/test';
import {
  RUN,
  DURATION_MIN,
  staging,
  login,
  createShow,
  bookSlot,
  deleteShow,
  deleteLeftovers,
  nextMinute,
  toneBack,
} from '../lib/staging';
import { pickInput, soundcheckAndGoLive } from '../lib/studio';

/** After each disturbance /live.mp3 carries the tone again within this. */
const RECOVER_S = 15;
/** The first time on air: the harbor buffers a new source for several seconds. */
const FIRST_TONE_S = 45;
const CUT_S = 20;
/** Liquidsoap's harbor keeps up to 20 s of a source (`input.harbor` max). */
const HARBOR_MAX_S = 20;
/** Recorded length vs. time on air (Go Live and End Episode are clicks). */
const LENGTH_TOLERANCE_S = 5;

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

test('a live show recovers by itself and ends in one recording', async () => {
  test.setTimeout((35 + DURATION_MIN) * 60_000);
  let onAirMs = 0;
  let offAirMs = 0;

  await test.step('create a Show and book a live slot a minute ahead', async () => {
    show = await createShow(page, `e2e-${RUN}-live`);
    await page.locator('a, button').filter({ hasText: /^Book Slot$/ }).click();
    const start = nextMinute(60);
    episode = await bookSlot(page, start, new Date(start.getTime() + (60 + DURATION_MIN) * 60_000));
  });

  const live = await test.step('soundcheck, then Go Live at the start time', async () => {
    // The Studio on its own page: the reload below relies on its On Air route.
    await page.goto(`/#/stream/live?show=${episode}`);
    await soundcheckAndGoLive(page);
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
    // On air the page asks before it unloads: leave.
    const leave = (dialog: Dialog) => void dialog.accept();
    page.once('dialog', leave);
    await page.reload();
    page.off('dialog', leave);
    // On Air sends a tab that is not live to the Studio.
    // The show stays on air: the Studio goes back on air with only an input, no soundcheck.
    await page.waitForURL(/#\/stream\/live/);
    await pickInput(page);
    await page.getByRole('button', { name: 'Go back on air' }).click();
    await page.waitForURL(/#\/stream\/on-air/, { timeout: 30_000 });
    offAirMs += Date.now() - reloaded;
    // Up to 20 s after the reload, the harbor can still play the old page's
    // audio. So the tone must not stop for 15 s until then, and be there after.
    while (Date.now() < reloaded + HARBOR_MAX_S * 1000) await toneBack(RECOVER_S);
    console.log(`tone back ${await toneBack(RECOVER_S)} s after the old audio ran out`);
  });

  if (DURATION_MIN > 0) {
    await test.step(`soak: ${DURATION_MIN} minutes on air`, async () => {
      // A check every full minute: the listening must not stretch the soak.
      const start = Date.now();
      for (let minute = 1; minute <= DURATION_MIN; minute++) {
        await page.waitForTimeout(Math.max(0, start + minute * 60_000 - Date.now()));
        await toneBack(RECOVER_S);
      }
    });
  }

  await test.step('End Episode, Publish episode, Finished', async () => {
    // End Episode asks once: the second click ends the broadcast.
    await page.getByRole('button', { name: 'End Episode' }).click();
    await expect(page.getByText('End the episode?')).toBeVisible();
    await page.getByRole('button', { name: 'End Episode' }).click();
    onAirMs = Date.now() - live - offAirMs;
    await page.waitForURL(new RegExp(`#/shows/${episode}$`));
    // The episode opens on its Recording card, which waits while the recording converts.
    const publish = page.getByRole('button', { name: 'Publish episode' });
    await expect(publish).toBeEnabled({ timeout: 5 * 60_000 });
    await publish.click();
    await expect(publish).toHaveCount(0);
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
