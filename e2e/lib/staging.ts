// Helpers of the chaos test show: the staging box (through the CI key),
// Berlin times for bookings, and what listeners hear on /live.mp3.
import { execFile, execFileSync } from 'node:child_process';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { promisify } from 'node:util';
import { expect, type Page } from '@playwright/test';

export const STAGING_URL = process.env.STAGING_URL ?? 'https://staging.live.moafunk.de';
const STAGING_HOST = new URL(STAGING_URL).hostname;
/** Digits only: the box puts it into the hold reason. */
export const RUN = process.env.E2E_RUN ?? String(Date.now());
export const TONE_WAV = join(import.meta.dirname, '..', 'tone.wav');
export const SET_MP3 = join(import.meta.dirname, '..', 'set.mp3');
/** Extra minutes on air for the soak run before a release (0: the normal run). */
export const DURATION_MIN = Number(process.env.DURATION || 0);
if (!Number.isInteger(DURATION_MIN) || DURATION_MIN < 0 || DURATION_MIN > 120) {
  throw new Error(`DURATION must be whole minutes from 0 to 120, got "${process.env.DURATION}"`);
}

/** The tone after the stream chain's MP3 encode is about -24 dB; silence is -91 dB. */
const TONE_DB = -40;
/** A sine's peak is 3 dB above its mean; a click reaches far above. */
const CLICK_DB = 6;

/** A request to the staging box through the CI key (staging.sh ci-deploy). */
export function staging(request: string): string {
  const key =
    process.env.STAGING_SSH_KEY ?? join(homedir(), '.config/moafunk-staging/ci_deploy_ed25519');
  const args = ['-i', key, '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes'];
  args.push('-o', 'ConnectTimeout=20');
  if (process.env.STAGING_KNOWN_HOSTS) {
    args.push('-o', `UserKnownHostsFile=${process.env.STAGING_KNOWN_HOSTS}`);
    args.push('-o', 'StrictHostKeyChecking=yes');
  }
  return execFileSync('ssh', [...args, `root@${STAGING_HOST}`, request], {
    encoding: 'utf8',
    timeout: 120_000,
  }).trim();
}

/** Today's date and a time `minutes` ahead in Berlin, whole minutes, as the booking form wants. */
export function berlinSlot(at: Date): { date: string; time: string } {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat('en-CA', {
      timeZone: 'Europe/Berlin',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hourCycle: 'h23',
    })
      .formatToParts(at)
      .map((p) => [p.type, p.value])
  );
  return { date: `${parts.year}-${parts.month}-${parts.day}`, time: `${parts.hour}:${parts.minute}` };
}

/** The next full minute at least `seconds` from now. */
export function nextMinute(seconds: number): Date {
  const at = new Date(Date.now() + seconds * 1000);
  at.setSeconds(0, 0);
  return new Date(at.getTime() + 60_000);
}

interface Heard {
  meanDb: number;
  maxDb: number;
}

/**
 * Icecast sends its burst (burst-size 64 KB, about 2 s at 256 kbps) at once
 * on connect. That audio is from the past, so a listen skips it.
 */
const BURST_S = 3;

/** Decode `seconds` of /live.mp3 as a listener would; null when it cannot be reached. */
export async function listen(seconds: number): Promise<Heard | null> {
  try {
    const { stderr } = await promisify(execFile)(
      'ffmpeg',
      ['-hide_banner', '-nostats', '-t', String(seconds + BURST_S)]
        .concat(['-i', `${STAGING_URL}/live.mp3`])
        .concat(['-af', `atrim=start=${BURST_S},volumedetect`, '-f', 'null', '-']),
      { timeout: (seconds + BURST_S + 20) * 1000 }
    );
    const read = (name: string) => Number(stderr.match(new RegExp(`${name}: (-?[\\d.]+) dB`))?.[1]);
    return { meanDb: read('mean_volume'), maxDb: read('max_volume') };
  } catch {
    return null;
  }
}

/**
 * Wait until /live.mp3 carries the tone again, at most `withinS` seconds,
 * then check 5 s of it for clicks. Returns how long it took.
 */
export async function toneBack(withinS: number): Promise<number> {
  const start = Date.now();
  let took = 0;
  let last: Heard | null = null;
  for (;;) {
    // The time a listen starts: a 3 s window with the tone had it from about then.
    took = (Date.now() - start) / 1000;
    expect(took, `no tone on /live.mp3 (last ${last?.meanDb ?? 'unreachable'} dB)`).toBeLessThan(
      withinS
    );
    last = await listen(3);
    if (last && last.meanDb > TONE_DB) break;
  }
  const heard = await listen(5);
  expect(heard?.meanDb, 'the tone stays on /live.mp3').toBeGreaterThan(TONE_DB);
  expect(heard!.maxDb - heard!.meanDb, 'a click on /live.mp3').toBeLessThan(CLICK_DB);
  return took;
}

/** Log in on the admin as the e2e host (people use staging-host, so they never take over this stream). */
export async function login(page: Page): Promise<void> {
  // shortcut: until nightly.yml is on main, the nightly run takes its workflow
  // from main, which sends only STAGING_HOST_PASSWORD. Drop the fallback once
  // a nightly run started by nightly.yml (from staging) is green.
  const e2e = process.env.STAGING_E2E_HOST_PASSWORD;
  const [username, password] = e2e ? ['e2e-host', e2e] : ['staging-host', process.env.STAGING_HOST_PASSWORD];
  if (!password) throw new Error('set STAGING_E2E_HOST_PASSWORD (Bitwarden Secrets Manager)');
  await page.goto('/#/login');
  await page.locator('input[autocomplete="username"]').fill(username);
  await page.locator('input[autocomplete="current-password"]').fill(password);
  await page.getByRole('button', { name: 'Login' }).click();
  await page.waitForURL(/#\/dashboard/);
}

/** Create a Show with "No fixed rhythm"; returns its id and slug. */
export async function createShow(page: Page, name: string): Promise<{ id: number; slug: string }> {
  await page.goto('/#/shows/new');
  await page.locator('.form-input:has-text("Name") input').fill(name);
  await page.getByLabel('Repeats').selectOption('none');
  const created = page.waitForResponse(
    (r) => r.url().endsWith('/api/series') && r.request().method() === 'POST'
  );
  await page.getByRole('button', { name: 'Create show' }).click();
  const show = (await (await created).json()) as { id: number; slug: string };
  await page.waitForURL(new RegExp(`#/shows/${show.slug}$`));
  return show;
}

/** Book the open Show's slot from `start` to `end`; returns the episode id. */
export async function bookSlot(page: Page, start: Date, end: Date): Promise<number> {
  const from = berlinSlot(start);
  const to = berlinSlot(end);
  // shortcut: a slot that runs past midnight is cut at 23:59; runs start long before.
  const endTime = to.date === from.date ? to.time : '23:59';
  await page.getByLabel('Date').fill(from.date);
  await page.getByLabel('Start time').fill(from.time);
  await page.getByLabel('End time').fill(endTime);
  const booked = page.waitForResponse(
    (r) => r.url().endsWith('/episodes') && r.request().method() === 'POST'
  );
  await page.getByRole('button', { name: 'Book slot' }).click();
  // A refused booking (a taken slot) keeps the form open: fail now, not at the test timeout.
  const res = await booked;
  if (!res.ok()) throw new Error(`booking refused (${res.status()}): ${await res.text()}`);
  await page.waitForURL(/#\/shows\/\d+$/);
  const id = Number(page.url().match(/#\/shows\/(\d+)$/)![1]);
  // Booking arms the on-air story; without this every live test posts to the Instagram test account.
  const story = await page.request.delete(`/api/shows/${id}/announcements/onair`);
  if (!story.ok()) throw new Error(`cancel on-air story: ${story.status()} ${await story.text()}`);
  return id;
}

/**
 * Delete the test's episodes, then every e2e Show of the host that has no
 * episodes left. No API route deletes a Show, so the box does it.
 */
export async function deleteShow(page: Page, episodes: number[]): Promise<void> {
  for (const id of episodes) {
    const res = await page.request.delete(`/api/shows/${id}`);
    if (!res.ok()) throw new Error(`DELETE /api/shows/${id}: ${res.status()} ${await res.text()}`);
  }
  console.log(staging('e2e-cleanup'));
}

/**
 * Delete the e2e Shows of an earlier run whose clean-up failed: their slots
 * block this run's bookings. Runs never overlap (concurrency group
 * staging-box), so every e2e Show here is a leftover.
 */
export async function deleteLeftovers(page: Page): Promise<void> {
  const res = await page.request.get('/api/series');
  if (!res.ok()) throw new Error(`GET /api/series: ${res.status()}`);
  const { series } = (await res.json()) as { series: { slug: string; name: string }[] };
  const episodes: number[] = [];
  for (const s of series.filter((x) => x.name.startsWith('e2e-'))) {
    const detail = await page.request.get(`/api/series/${encodeURIComponent(s.slug)}`);
    if (!detail.ok()) throw new Error(`GET /api/series/${s.slug}: ${detail.status()}`);
    const found = ((await detail.json()) as { episodes: { id: number }[] }).episodes;
    console.log(`deleting the leftover Show ${s.name} and ${found.length} episode(s)`);
    episodes.push(...found.map((e) => e.id));
  }
  await deleteShow(page, episodes);
}
