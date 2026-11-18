// The Studio soundcheck as a host clicks it. Shared by the chaos test show on
// staging and the local run against the mock, so a changed Studio page fails
// the local run on its PR and not only the nightly show.
import { expect, type Locator, type Page } from '@playwright/test';

/** One row of the soundcheck: Music, Mic, Server or Return feed. */
export function soundcheckRow(page: Page, name: string): Locator {
  return page.getByRole('listitem').filter({ has: page.getByText(name, { exact: true }) });
}

/** Pick the (fake) mic. */
export async function pickInput(page: Page): Promise<void> {
  const input = page.getByLabel('Audio input');
  // Not "Select audio input" and not "Screen audio".
  const device = input.locator('option:not([value=""]):not([value="screen"])').first();
  await expect(device).toBeAttached({ timeout: 15_000 });
  await input.selectOption((await device.getAttribute('value'))!);
}

/**
 * Test the music (10 s of the fake mic's tone), skip the mic, and wait until
 * the server and the return feed are checked.
 */
export async function runSoundcheck(page: Page): Promise<void> {
  await soundcheckRow(page, 'Music').getByRole('button', { name: 'Start test' }).click();
  // Skip is enabled again once the music test is over.
  await soundcheckRow(page, 'Mic').getByRole('button', { name: 'Skip' }).click({ timeout: 30_000 });
  await expect(page.getByText('Keep the sound on until the checks are done.')).toBeHidden({
    timeout: 60_000,
  });
}

/** From the Studio: pick the (fake) mic, pass the soundcheck, Go Live. */
export async function soundcheckAndGoLive(page: Page): Promise<void> {
  await page.waitForURL(/#\/stream\/live/);
  await pickInput(page);
  await runSoundcheck(page);
  await expect(soundcheckRow(page, 'Music')).toContainText('Good');
  // The server counted real audio chunks from this browser.
  await expect(soundcheckRow(page, 'Server')).toContainText('Received your audio');
  await expect(soundcheckRow(page, 'Return feed')).toContainText('Your audio comes back');
  await expect(page.getByText('Sounds good — ready to go live')).toBeVisible();
  const goLive = page.getByRole('button', { name: 'Go Live' });
  await expect(goLive).toBeEnabled({ timeout: 180_000 });
  await goLive.click();
  await page.waitForURL(/#\/stream\/on-air/, { timeout: 30_000 });
}
