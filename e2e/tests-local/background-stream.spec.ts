// The host is on air and opens other admin pages (ticket 1061,
// https://github.com/phaabe/live.moafunk.de/issues/1061). The broadcast keeps
// sending audio, and the live session keeps the screen on and remembers a gone
// input (ticket 1063, https://github.com/phaabe/live.moafunk.de/issues/1063).
import { test, expect, type Page } from '@playwright/test';
import { pickInput, runSoundcheck, soundcheckAndGoLive } from '../lib/studio';

const MOCK = process.env.STUDIO_MOCK_URL ?? 'http://localhost:8000';
const DEVICE_ENDED = 'Audio device is no longer active. Re-select your device.';
/** How long each page stays open while the chunks are counted. */
const DWELL_MS = 5000;

test.skip(({ browserName }) => browserName !== 'chromium', 'Chromium fake media is enough here');

interface MockLive {
  live: number;
  chunks: number[];
}

async function mockLive(page: Page): Promise<MockLive> {
  return (await page.request.get(`${MOCK}/mock/live`)).json();
}

/** Open an app page as a link does: the router, no page load. */
async function open(page: Page, path: string): Promise<void> {
  await page.evaluate(async (to) => {
    type Router = { push(to: string): Promise<unknown> };
    const app = document.querySelector('#app') as unknown as {
      __vue_app__: { config: { globalProperties: { $router: Router } } };
    };
    await app.__vue_app__.config.globalProperties.$router.push(to);
  }, path);
}

/** Stay on the page; the one LIVE broadcast must get new chunks. Logs the counts. */
async function stillSends(page: Page, label: string): Promise<void> {
  const before = await mockLive(page);
  await page.waitForTimeout(DWELL_MS);
  const after = await mockLive(page);
  console.log(
    `${label.padEnd(34)} url=${new URL(page.url()).hash.padEnd(28)} live=${after.live} chunks ${before.chunks[0]} -> ${after.chunks[0]} (+${after.chunks[0] - before.chunks[0]})`
  );
  expect(before.live).toBe(1);
  expect(after.live).toBe(1);
  expect(after.chunks[0]).toBeGreaterThan(before.chunks[0]);
}

/** Remember every screen wake lock the page takes. */
async function watchWakeLocks(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const wakeLock = navigator.wakeLock;
    if (!wakeLock) return;
    const locks: WakeLockSentinel[] = [];
    (window as unknown as { wakeLocks: WakeLockSentinel[] }).wakeLocks = locks;
    const request = wakeLock.request.bind(wakeLock);
    wakeLock.request = async (type) => {
      const lock = await request(type);
      locks.push(lock);
      return lock;
    };
  });
}

/** Is a screen wake lock held right now? */
async function wakeLockHeld(page: Page): Promise<boolean> {
  return page.evaluate(() =>
    ((window as unknown as { wakeLocks?: WakeLockSentinel[] }).wakeLocks ?? []).some(
      (lock) => !lock.released
    )
  );
}

/** Count the live panel's polls (listeners, REC). */
function countPolls(page: Page): { count: number } {
  const polls = { count: 0 };
  page.on('request', (request) => {
    const path = new URL(request.url()).pathname;
    if (path === '/api/stream/metrics' || path === '/api/recording/status') polls.count++;
  });
  return polls;
}

/** The input is unplugged: its track stops and fires 'ended', as a real device does. */
async function unplugInput(page: Page): Promise<void> {
  await page.evaluate(async () => {
    const url = performance
      .getEntriesByType('resource')
      .map((entry) => entry.name)
      .find((name) => name.includes('/composables/useAudioCapture.ts'));
    if (!url) throw new Error('useAudioCapture module not found');
    const capture = (await import(url)) as {
      useAudioCapture(): { mediaStream: { value: MediaStream | null } };
    };
    const track = capture.useAudioCapture().mediaStream.value?.getAudioTracks()[0];
    if (!track) throw new Error('no capture track');
    track.stop();
    track.dispatchEvent(new Event('ended'));
  });
}

async function endEpisode(page: Page): Promise<void> {
  await page.getByRole('button', { name: 'End Episode' }).click();
  await page.getByRole('button', { name: 'End Episode' }).last().click();
  await expect.poll(async () => (await mockLive(page)).live).toBe(0);
}

/** The on-air bar under the nav bar, on every admin page but the live panel. */
const onAirBar = (page: Page) => page.getByRole('region', { name: 'On air' });

/** On another admin page while live: the bar and the tab title say so. */
async function expectBar(page: Page): Promise<void> {
  await expect(onAirBar(page)).toBeVisible();
  await expect(onAirBar(page).locator('.pill')).toHaveText(/^On air · \d\d:\d\d:\d\d$/);
  await expect(page).toHaveTitle(/^● ON AIR/);
}

/** Every browser dialog the page shows, by type. */
function collectDialogs(page: Page): string[] {
  const dialogs: string[] = [];
  page.on('dialog', async (dialog) => {
    dialogs.push(dialog.type());
    // Stay on the page: the show stays on air.
    await dialog.dismiss();
  });
  return dialogs;
}

test.beforeEach(async ({ page, browser, baseURL }) => {
  expect((await page.request.get(`${MOCK}/mock/mode/ok`)).ok()).toBe(true);
  await watchWakeLocks(page);
  // Headless Chromium refuses the screen wake lock unless it is granted.
  const cdp = await page.context().newCDPSession(page);
  const { targetInfo } = await cdp.send('Target.getTargetInfo');
  await (
    await browser.newBrowserCDPSession()
  ).send('Browser.setPermission', {
    permission: { name: 'screen-wake-lock' },
    setting: 'granted',
    origin: new URL(baseURL!).origin,
    browserContextId: targetInfo.browserContextId,
  });
});

test('standalone On Air: the stream, the bar and the warnings go on across admin pages', async ({
  page,
}) => {
  test.slow();
  const polls = countPolls(page);
  const dialogs = collectDialogs(page);
  await page.goto('/admin/#/stream/live?show=1');
  await soundcheckAndGoLive(page);
  await expect.poll(async () => (await mockLive(page)).live).toBe(1);
  await stillSends(page, 'On Air (live panel)');
  const lockOnPanel = await wakeLockHeld(page);
  // The live panel has its own status bar.
  await expect(onAirBar(page)).toHaveCount(0);

  const pollsBefore = polls.count;
  await open(page, '/dashboard');
  await expectBar(page);
  await stillSends(page, 'Dashboard');
  const lockAway = await wakeLockHeld(page);
  // Moving between admin pages asked nothing. A close still asks, also here
  // (Go Live was the user gesture Chromium needs); Stay keeps the show on air.
  expect(dialogs).toEqual([]);
  await page.close({ runBeforeUnload: true });
  await expect.poll(() => dialogs).toEqual(['beforeunload']);
  expect(page.isClosed()).toBe(false);
  await stillSends(page, 'Dashboard, stayed on "Leave site?"');
  await open(page, '/shows');
  await expectBar(page);
  await stillSends(page, 'My Show');
  await open(page, '/shows/2');
  await expectBar(page);
  await stillSends(page, 'another episode (/shows/2)');
  // Unplugged while away: the bar on this page says so.
  await unplugInput(page);
  await expect(onAirBar(page).getByText(DEVICE_ENDED)).toBeVisible();
  await stillSends(page, 'another episode, input unplugged');
  const pollsAway = polls.count - pollsBefore;

  // Back the way a host gets there: the Studio's link sends a live tab to On Air.
  await open(page, '/stream/live?show=1');
  await page.waitForURL(/#\/stream\/on-air/);
  await expect(page.locator('.on-air-pill')).toBeVisible();
  await stillSends(page, 'back on On Air');
  // The live panel shows the input that went while the host was away.
  await expect(page.getByText(DEVICE_ENDED)).toBeVisible();
  const warningAfterReturn = await page.getByText(DEVICE_ENDED).count();
  const lockBack = await wakeLockHeld(page);
  console.log(
    `wake lock: panel=${lockOnPanel} away=${lockAway} back=${lockBack} | polls while away=${pollsAway} | unplug warning after return=${warningAfterReturn}`
  );
  expect(lockOnPanel).toBe(true);
  expect(lockAway).toBe(true);
  expect(lockBack).toBe(true);
  // The listener and REC polls stay on the live panel.
  expect(pollsAway).toBe(0);
  await expect(onAirBar(page)).toHaveCount(0);

  await endEpisode(page);
  await expect(page).not.toHaveTitle(/ON AIR/);
  expect(dialogs).toEqual(['beforeunload']);
});

test('episode page: the stream and the bar go on across admin pages, the Show time tab comes back', async ({
  page,
}) => {
  test.slow();
  await page.goto('/admin/#/shows/1');
  await page.getByRole('tab', { name: /Soundcheck/ }).click();
  await pickInput(page);
  await runSoundcheck(page);
  const goLive = page.getByRole('button', { name: 'Go Live' });
  await expect(goLive).toBeEnabled({ timeout: 180_000 });
  await goLive.click();
  await expect(page.locator('.on-air-pill')).toBeVisible({ timeout: 30_000 });
  await expect.poll(async () => (await mockLive(page)).live).toBe(1);
  await stillSends(page, 'episode page, Show time tab');
  // Unplugged while the live panel is open: the warning shows.
  await unplugInput(page);
  await expect(page.getByText(DEVICE_ENDED)).toBeVisible();
  const clockBefore = await page.locator('.on-air-clock').textContent();
  // Another tab of the same page keeps the live panel mounted; the bar shows the warning.
  await page.getByRole('tab', { name: /Social media/ }).click();
  await expectBar(page);
  await expect(onAirBar(page).getByText(DEVICE_ENDED)).toBeVisible();
  await stillSends(page, 'episode page, Social media tab');
  const lockOtherTab = await wakeLockHeld(page);

  await open(page, '/dashboard');
  await expectBar(page);
  await expect(onAirBar(page).getByText(DEVICE_ENDED)).toBeVisible();
  await stillSends(page, 'Dashboard');
  const lockAway = await wakeLockHeld(page);
  await open(page, '/shows');
  await stillSends(page, 'My Show');
  await open(page, '/shows/2');
  await stillSends(page, 'another episode (/shows/2)');

  await open(page, '/shows/1');
  await expect(page.locator('.on-air-pill')).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole('tab', { name: /Show time/ })).toHaveAttribute(
    'aria-selected',
    'true'
  );
  await stillSends(page, 'back on the episode page');
  await expect(onAirBar(page)).toHaveCount(0);
  const clockAfter = await page.locator('.on-air-clock').textContent();
  // The input is still gone, and the live panel still says so.
  await expect(page.getByText(DEVICE_ENDED)).toBeVisible();
  const warningAfterReturn = await page.getByText(DEVICE_ENDED).count();
  console.log(
    `on-air clock: before leaving=${clockBefore} after return=${clockAfter} | wake lock: other tab=${lockOtherTab} away=${lockAway} | unplug warning after return=${warningAfterReturn}`
  );
  expect(lockOtherTab).toBe(true);
  expect(lockAway).toBe(true);

  // End Episode in the bar, on another tab of this episode: the Wrap-up opens.
  await page.getByRole('tab', { name: /Social media/ }).click();
  await expectBar(page);
  await endEpisode(page);
  await expect(page.getByRole('heading', { level: 1, name: 'Wrap-up' })).toBeVisible();
  await expect(onAirBar(page)).toHaveCount(0);
});

// Ticket 1062 (https://github.com/phaabe/live.moafunk.de/issues/1062): the
// Recording page leaves a broadcast from another page alone.
test('admin: leaving the Recording page keeps the live broadcast', async ({ page }) => {
  test.slow();
  expect((await page.request.get(`${MOCK}/mock/role/admin`)).ok()).toBe(true);
  await page.goto('/admin/#/stream/live?show=1');
  await soundcheckAndGoLive(page);
  await expect.poll(async () => (await mockLive(page)).live).toBe(1);
  await stillSends(page, 'admin, On Air');

  await open(page, '/recording');
  await expect(page.getByText('This tab is already live from another page.')).toBeVisible();
  await expect(page.getByRole('button', { name: /Stop Stream/ })).toHaveCount(0);
  await stillSends(page, 'admin, Recording page');
  await open(page, '/dashboard');
  await stillSends(page, 'admin, left Recording page');

  // Back on the live panel, which still owns the broadcast.
  await open(page, '/stream/live?show=1');
  await page.waitForURL(/#\/stream\/on-air/);
  await stillSends(page, 'admin, back on On Air');
  await endEpisode(page);
});
