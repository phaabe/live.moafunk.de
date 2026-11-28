// Instagram on staging: a host announces an episode on the Instagram test
// account (INSTAGRAM_ACCOUNT=dev): the pre-announcement with Post now, the
// on-air story at Go Live, and the post-announcement once the SoundCloud
// track is public (only the flag, set on the box: soundcloud.spec.ts tests
// SoundCloud). For the run the box raises the station limits, turns off the
// approval and offers one feed time a few minutes ahead (e2e-instagram); the
// old settings come back after the run. One run sends 2 feed posts and
// 1 story (Instagram allows 50 per day).
import { test, expect, type Page } from '@playwright/test';
import {
  RUN,
  staging,
  login,
  createShow,
  bookSlot,
  deleteShow,
  deleteLeftovers,
  nextMinute,
  berlinSlot,
} from '../lib/staging';
import { soundcheckAndGoLive } from '../lib/studio';

type Slot = 'pre' | 'onair' | 'post';
interface Announcement {
  slot: Slot;
  state: string;
  scheduled_at: string;
  permalink: string | null;
  error: string | null;
}

test.describe.configure({ mode: 'serial' });

let page: Page;
let show: { id: number; slug: string } | null = null;
let episode: number | null = null;
let start = new Date();

test.beforeAll(async ({ browser }) => {
  page = await browser.newPage();
  await login(page);
  await deleteLeftovers(page);
  // A feed time far ahead until the post-announcement sets its own.
  console.log(staging(`e2e-instagram on ${berlinSlot(nextMinute(3600)).time}`));
});

test.afterAll(async () => {
  try {
    console.log(staging('e2e-instagram off'));
  } finally {
    if (show) await deleteShow(page, episode ? [episode] : []);
    await page.close();
  }
});

async function announcement(slot: Slot): Promise<Announcement | undefined> {
  const res = await page.request.get(`/api/shows/${episode}/announcements`);
  expect(res.ok(), `GET /api/shows/${episode}/announcements: ${res.status()}`).toBe(true);
  return ((await res.json()) as Announcement[]).find((a) => a.slot === slot);
}

/** Wait until the announcement is on Instagram; a failed send stops at once. */
async function sent(slot: Slot, timeout: number): Promise<Announcement> {
  let last: Announcement | undefined;
  await expect
    .poll(
      async () => {
        last = await announcement(slot);
        if (last?.state === 'failed') throw new Error(`${slot} failed: ${last.error}`);
        return last?.state;
      },
      { timeout, intervals: [5_000] }
    )
    .toBe('sent');
  expect(last!.permalink, `${slot} has its Instagram link`).toMatch(/^https:\/\/www\.instagram\.com\//);
  console.log(`${slot}: ${last!.permalink}`);
  return last!;
}

/** Fill the composer (pre or post) and send it with the chosen time, or Post now. */
async function compose(slot: 'pre' | 'post', time: RegExp | 'now'): Promise<void> {
  await page.goto(`/#/shows/${episode}/announce/${slot}`);
  // The composer fills its fields once its data is loaded, over anything typed
  // before: type until the text stays.
  const headline = page.getByLabel('Headline on the image');
  await expect(async () => {
    await headline.fill(`e2e ${slot}`);
    await page.getByLabel('Caption').fill(`Test of run ${RUN}. Please ignore.`);
    await page.waitForTimeout(1_500);
    expect(await headline.inputValue()).toBe(`e2e ${slot}`);
  }).toPass({ timeout: 30_000 });
  // When: Book a time opens the time picker (Post now: staging only).
  await page.getByRole('button', { name: /^(Goes out|Book a time)/ }).click();
  const picker = page.getByRole('dialog');
  if (time === 'now') {
    await picker.getByRole('button', { name: 'Post now' }).click();
  } else {
    // The feed time is offered on every day ahead: the first one is today.
    await picker.getByRole('radio', { name: time }).first().check();
    await picker.getByRole('button', { name: 'Book this time' }).click();
  }
  await expect(picker).toBeHidden();
  const saved = page.waitForResponse(
    (r) => r.url().endsWith(`/announcements/${slot}`) && r.request().method() === 'PUT'
  );
  await page.getByRole('button', { name: 'Schedule post' }).click();
  const res = await saved;
  expect(res.ok(), `PUT ${slot}: ${res.status()} ${await res.text()}`).toBe(true);
}

test('a host announces an episode on Instagram', async () => {
  test.setTimeout(30 * 60_000);

  await test.step('create a Show and book a live slot a few minutes ahead', async () => {
    show = await createShow(page, `e2e-${RUN}-instagram`);
    await page.getByRole('button', { name: 'Book another date' }).click();
    // Time for the pre-announcement while the episode is still upcoming.
    start = nextMinute(4 * 60);
    episode = await bookSlot(page, start, new Date(start.getTime() + 30 * 60_000));
  });

  await test.step('pre-announcement with Post now', async () => {
    await compose('pre', 'now');
    await sent('pre', 3 * 60_000);
  });

  await test.step('set up the on-air story', async () => {
    // A booking no longer arms it: the host sets it up on the Social media tab.
    await page.goto(`/#/shows/${episode}`);
    await page.getByRole('tab', { name: /Social media/ }).click();
    await page
      .getByRole('group', { name: 'On-air story' })
      .getByRole('button', { name: 'Set up' })
      .click();
    const saved = page.waitForResponse(
      (r) => r.url().endsWith('/announcements/onair') && r.request().method() === 'PUT'
    );
    await page.getByRole('button', { name: 'Schedule story' }).click();
    const res = await saved;
    expect(res.ok(), `PUT onair: ${res.status()} ${await res.text()}`).toBe(true);
    expect((await announcement('onair'))?.state).toBe('armed');
  });

  await test.step('Go Live sends the on-air story', async () => {
    // Go Live opens at the start; the soundcheck helper waits 3 minutes for it.
    await page.waitForTimeout(Math.max(0, start.getTime() - 150_000 - Date.now()));
    // The Studio on its own page: the episode page keeps it in its Soundcheck tab.
    await page.goto(`/#/stream/live?show=${episode}`);
    await soundcheckAndGoLive(page);
    await sent('onair', 3 * 60_000);
  });

  await test.step('End Episode', async () => {
    // End Episode asks once: the second click ends the broadcast.
    await page.getByRole('button', { name: 'End Episode' }).click();
    await expect(page.getByText('End the episode?')).toBeVisible();
    await page.getByRole('button', { name: 'End Episode' }).click();
    await page.waitForURL(new RegExp(`#/shows/${episode}$`));
  });

  await test.step('the SoundCloud track is public', async () => {
    // Only the flag that unlocks the post: soundcloud.spec.ts tests SoundCloud itself.
    console.log(staging(`e2e-instagram public ${episode}`));
  });

  const at = nextMinute(150);
  await test.step('set up the post-announcement at its feed time', async () => {
    const { time } = berlinSlot(at);
    console.log(staging(`e2e-instagram on ${time}`));
    await compose('post', new RegExp(time));
    const post = await announcement('post');
    expect(post?.state).toBe('scheduled');
    expect(new Date(post!.scheduled_at).getTime()).toBe(at.getTime());
  });

  await test.step('the post-announcement goes out at its time', async () => {
    await sent('post', at.getTime() - Date.now() + 3 * 60_000);
  });
});
