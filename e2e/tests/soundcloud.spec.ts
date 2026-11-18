// SoundCloud on staging: the server is connected to the test account, and its
// token still works at SoundCloud (refreshed when it expires). Runs every night,
// so a lost connection shows up before a host needs it.
import { test, expect, type Page } from '@playwright/test';
import { login } from '../lib/staging';

/** The staging test account (SOUNDCLOUD_ACCOUNT in backend/staging/staging.env.example). */
const TEST_ACCOUNT = 'moafunk-test';

let page: Page;

test.beforeAll(async ({ browser }) => {
  page = await browser.newPage();
  await login(page);
});

test.afterAll(async () => {
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
