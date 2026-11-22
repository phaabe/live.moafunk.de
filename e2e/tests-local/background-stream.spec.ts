// The host is on air and opens other admin pages (ticket 1061,
// https://github.com/phaabe/live.moafunk.de/issues/1061). Does the broadcast
// keep sending audio, and what does the host still see? This test records
// how the app behaves today; it is the evidence for that ticket.
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

test('standalone On Air: the stream goes on across admin pages, the warnings do not', async ({
  page,
}) => {
  test.slow();
  const polls = countPolls(page);
  await page.goto('/admin/#/stream/live?show=1');
  await soundcheckAndGoLive(page);
  await expect.poll(async () => (await mockLive(page)).live).toBe(1);
  await stillSends(page, 'On Air (live panel)');
  const lockOnPanel = await wakeLockHeld(page);

  const pollsBefore = polls.count;
  await open(page, '/dashboard');
  await stillSends(page, 'Dashboard');
  const lockAway = await wakeLockHeld(page);
  await open(page, '/shows');
  await stillSends(page, 'My Show');
  await open(page, '/shows/2');
  await stillSends(page, 'another episode (/shows/2)');
  // Unplugged while away: nothing on this page says so.
  await unplugInput(page);
  await expect(page.getByText(DEVICE_ENDED)).toHaveCount(0);
  await stillSends(page, 'another episode, input unplugged');
  const pollsAway = polls.count - pollsBefore;

  // Back the way a host gets there: the Studio's link sends a live tab to On Air.
  await open(page, '/stream/live?show=1');
  await page.waitForURL(/#\/stream\/on-air/);
  await expect(page.locator('.on-air-pill')).toBeVisible();
  await stillSends(page, 'back on On Air');
  // The unplugged input is not shown on the live panel either.
  const warningAfterReturn = await page.getByText(DEVICE_ENDED).count();
  const lockBack = await wakeLockHeld(page);
  console.log(
    `wake lock: panel=${lockOnPanel} away=${lockAway} back=${lockBack} | polls while away=${pollsAway} | unplug warning after return=${warningAfterReturn}`
  );
  expect(lockOnPanel).toBe(true);
  expect(lockAway).toBe(false);
  expect(lockBack).toBe(true);
  expect(pollsAway).toBe(0);
  expect(warningAfterReturn).toBe(0);

  await endEpisode(page);
});

test('episode page: the stream goes on across admin pages, the Show time tab comes back', async ({
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
  // Another tab of the same page keeps the live panel mounted.
  await page.getByRole('tab', { name: /Social media/ }).click();
  await stillSends(page, 'episode page, Social media tab');
  const lockOtherTab = await wakeLockHeld(page);

  await open(page, '/dashboard');
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
  const clockAfter = await page.locator('.on-air-clock').textContent();
  // The input is still gone, but the new live panel does not know it.
  const warningAfterReturn = await page.getByText(DEVICE_ENDED).count();
  console.log(
    `on-air clock: before leaving=${clockBefore} after return=${clockAfter} | wake lock: other tab=${lockOtherTab} away=${lockAway} | unplug warning after return=${warningAfterReturn}`
  );
  expect(lockOtherTab).toBe(true);
  expect(lockAway).toBe(false);
  expect(warningAfterReturn).toBe(0);

  await endEpisode(page);
});

test('admin: leaving the Recording page ends the live broadcast', async ({ page }) => {
  test.slow();
  expect((await page.request.get(`${MOCK}/mock/role/admin`)).ok()).toBe(true);
  await page.goto('/admin/#/stream/live?show=1');
  await soundcheckAndGoLive(page);
  await expect.poll(async () => (await mockLive(page)).live).toBe(1);
  await stillSends(page, 'admin, On Air');

  await open(page, '/recording');
  await stillSends(page, 'admin, Recording page');
  await open(page, '/dashboard');
  // The Recording page stops the stream when it closes: "stop" ends the broadcast.
  await expect.poll(async () => (await mockLive(page)).live).toBe(0);
  console.log('admin, left Recording page: live=0, broadcast ended');
  await open(page, '/stream/on-air?show=1');
  await page.waitForURL(/#\/stream\/live/);
});
