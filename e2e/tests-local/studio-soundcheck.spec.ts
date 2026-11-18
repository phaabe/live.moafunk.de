// The Studio soundcheck with a real MediaRecorder and real Web Audio: the
// fake mic plays a tone, the mock counts the chunks and sends them back as
// the relay does (docs/stream-rework/studio-soundcheck-local.md).
import { test, expect, type Page } from '@playwright/test';
import { pickInput, runSoundcheck, soundcheckAndGoLive, soundcheckRow } from '../lib/studio';

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
