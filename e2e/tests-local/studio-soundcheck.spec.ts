// The Studio soundcheck with a real MediaRecorder and real Web Audio: the
// fake mic plays a tone, the mock counts the chunks and sends them back as
// the relay does (docs/stream-rework/studio-soundcheck-local.md).
import { test, expect, type Page } from '@playwright/test';
import {
  open,
  pickInput,
  runSoundcheck,
  soundcheckAndGoLive,
  soundcheckRow,
  unplugInput,
} from '../lib/studio';

const MOCK = 'http://localhost:8000';
const STUDIO = '/admin/#/stream/live?show=1';

async function mode(page: Page, name: string): Promise<void> {
  expect((await page.request.get(`${MOCK}/mock/mode/${name}`)).ok()).toBe(true);
}

test.afterEach(({ page }) => mode(page, 'ok'));

test('a tone passes the soundcheck and opens Go Live', async ({ page }) => {
  await mode(page, 'ok');
  await page.goto(STUDIO);
  await expect(page.getByRole('button', { name: 'Go Live' })).toBeDisabled();
  await soundcheckAndGoLive(page);
});

test('the input gain sets the level that is metered and tested', async ({ page }) => {
  await mode(page, 'ok');
  await page.goto(STUDIO);
  await pickInput(page);
  await page.getByLabel('Input gain').fill('0');
  await expect(page.locator('.db-meter-readout').first()).toHaveText(/∞/);
  await soundcheckRow(page, 'Music').getByRole('button', { name: 'Start test' }).click();
  // The meter's peak hold may still carry the level from before the fader moved.
  await expect(soundcheckRow(page, 'Music')).toContainText(/No signal|Too quiet/, {
    timeout: 30_000,
  });
});

test('Go Live stays closed when the server gets no audio', async ({ page }) => {
  await mode(page, 'no-chunks');
  await page.goto(STUDIO);
  await pickInput(page);
  await runSoundcheck(page);
  await expect(soundcheckRow(page, 'Server')).toContainText('No audio arrived');
  await expect(page.getByRole('alert')).toContainText('The server got no audio.');
  await expect(page.getByRole('button', { name: 'Go Live' })).toBeDisabled();
});

test('Go Live stays closed when nothing comes back from the relay', async ({ page }) => {
  // The page waits 20 s for the relay.
  test.slow();
  await mode(page, 'no-return');
  await page.goto(STUDIO);
  await pickInput(page);
  await runSoundcheck(page);
  await expect(soundcheckRow(page, 'Server')).toContainText('Received your audio');
  await expect(soundcheckRow(page, 'Return feed')).toContainText('Nothing came back');
  await expect(page.getByRole('button', { name: 'Go Live' })).toBeDisabled();
});

test('a passed soundcheck stays across admin pages; a lost input clears it', async ({ page }) => {
  await mode(page, 'ok');
  await page.goto('/admin/#/shows/1');
  const soundcheckTab = page.getByRole('tab', { name: /Soundcheck/ });
  await soundcheckTab.click();
  await pickInput(page);
  await runSoundcheck(page);
  const ready = page.getByText('Sounds good — ready to go live');
  await expect(ready).toBeVisible();
  // Another admin page, then back to the episode: the result and its rows stay.
  await open(page, '/dashboard');
  await expect(ready).toBeHidden();
  await open(page, '/shows/1');
  await soundcheckTab.click();
  await expect(ready).toBeVisible();
  await expect(soundcheckRow(page, 'Music')).toContainText('Good');
  // Unplugged, then back to the episode: the result is gone, and the same
  // input can be picked again. (Firefox ignores the helper's synthetic
  // 'ended', so the Studio's check on opening is what clears it there.)
  await unplugInput(page);
  await open(page, '/dashboard');
  await open(page, '/shows/1');
  await soundcheckTab.click();
  await expect(soundcheckRow(page, 'Music')).toContainText('Not checked yet');
  await expect(ready).toBeHidden();
  await expect(page.getByLabel('Audio input')).toHaveValue('');
});
