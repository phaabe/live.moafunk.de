import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApp, h, nextTick, type App } from 'vue';
import { createPinia } from 'pinia';
import { createMemoryHistory, createRouter, RouterView, type Router } from 'vue-router';
import { useAuthStore } from '../stores/auth';
import BookSlotPage from '../pages/BookSlotPage.vue';
import { openReplaceFile } from '../replaceFile';
import { setLength, slotDate } from '../episodeRow';
import type { BookedTime, Series, SeriesSlot } from '../api';
import { expectFlow } from '../flow/expectFlow';
import {
  addDays,
  berlinNow,
  bookedWindows,
  clash,
  hhmm,
  lengthBetween,
  lengthText,
  nextFullHour,
  parseHhmm,
  problemOf,
  windowOf,
} from '../bookSlot';
import { episodeDay } from '../episodeRow';

function series(slug: string, name: string): Series {
  return {
    id: slug.length,
    slug,
    name,
    description: null,
    cover_url: null,
    rhythm: { repeat_rule: 'weekly', weekday: 7, start_time: '12:00', end_time: '14:00' },
    rhythm_line: 'Sundays · 12:00–14:00',
  };
}

const BRUNCHTIME = series('brunchtime', 'Brunchtime');
/** "No fixed rhythm": a free date and time. */
const STEPPER: Series = {
  ...series('stepper', 'Most Silent Stepper'),
  rhythm: null,
  rhythm_line: null,
};
const slot = (date: string): SeriesSlot => ({ date, start_time: '12:00', end_time: '14:00' });

let app: App | undefined;
let root: HTMLDivElement;
let slots: SeriesSlot[];
let booked: BookedTime[];
let bookedLoads: number;
let requests: { url: string; method: string; body: unknown }[];
let bookResponse: () => Response;
let finalizeResponse: () => Response;
/** File name the upload finalize answers with. */
let setName: string;

beforeEach(() => {
  // The flow's example time: Di 27.10.2026, 22:13 in Berlin (winter time, UTC+1).
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(new Date('2026-10-27T21:13:20Z'));
  slots = [slot('2026-11-01'), slot('2026-11-08')];
  booked = [];
  bookedLoads = 0;
  requests = [];
  bookResponse = () => Response.json({ id: 77 }, { status: 201 });
  setName = 'set.mp3';
  finalizeResponse = () => Response.json({ upload_id: 's1', filename: setName, duration: 7120 });
  root = document.createElement('div');
  document.body.appendChild(root);
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, options?: RequestInit) => {
      const method = options?.method ?? 'GET';
      const body = typeof options?.body === 'string' ? JSON.parse(options.body) : options?.body;
      requests.push({ url, method, body });
      if (url === '/api/series/brunchtime') return Response.json({ ...BRUNCHTIME, episodes: [] });
      if (url === '/api/series/stepper') return Response.json({ ...STEPPER, episodes: [] });
      // Another host's Show: viewable, but not bookable.
      if (url === '/api/series/other')
        return Response.json({ ...series('other', 'Other'), can_edit: false, episodes: [] });
      if (url === '/api/series/brunchtime/slots') return Response.json({ slots });
      if (url === '/api/booked-times?from=2026-10-27&to=2026-11-10') {
        bookedLoads += 1;
        return Response.json({ times: booked });
      }
      if (url.endsWith('/episodes')) return bookResponse();
      if (url === '/api/series/brunchtime/upload/init') return Response.json({ session_id: 's1' });
      if (url.startsWith('/api/series/brunchtime/upload/chunk/s1'))
        return Response.json({ index: 0 });
      if (url === '/api/series/brunchtime/upload/finalize/s1') return finalizeResponse();
      if (url === '/api/shows/43/prerecorded') return Response.json({ id: 43 });
      throw new Error(`Unexpected API request: ${method} ${url}`);
    })
  );
});

afterEach(() => {
  app?.unmount();
  app = undefined;
  root.remove();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

async function mount(path: string): Promise<Router> {
  const stub = (text: string) => ({ render: () => h('div', text) });
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/shows', component: stub('My Shows list') },
      { path: '/shows/:slug/book', component: BookSlotPage },
      { path: '/shows/:id(\\d+)', component: stub('Episode screen') },
      { path: '/shows/:slug', component: stub('Show page') },
    ],
  });
  const pinia = createPinia();
  app = createApp({ render: () => h(RouterView) });
  app.use(pinia).use(router);
  useAuthStore(pinia).user = { id: 1, username: 'host', role: 'host' };
  await router.push(path);
  await router.isReady();
  app.mount(root);
  await settle();
  return router;
}

/** Let fetch promises and renders run. */
async function settle() {
  for (let i = 0; i < 5; i++) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    await nextTick();
  }
}

function button(name: string): HTMLButtonElement {
  const found = [...root.querySelectorAll('button')].find(
    (b) => (b.getAttribute('aria-label') ?? b.textContent?.trim()) === name
  );
  if (!found) throw new Error(`No button "${name}"`);
  return found;
}

async function click(name: string) {
  button(name).click();
  await settle();
}

/** The overview rows: "Start | So 01.11. · 12:00". */
const rows = () =>
  [...root.querySelectorAll('dl > div')].map((r) =>
    [...r.children].map((c) => c.textContent?.replace(/\s+/g, ' ').trim()).join(' | ')
  );

const bookBody = () => requests.find((r) => r.url.endsWith('/episodes'))?.body;

function pick(name: string) {
  const input = root.querySelector('input[type="file"]') as HTMLInputElement;
  Object.defineProperty(input, 'files', { value: [new File(['abc'], name)], configurable: true });
  input.dispatchEvent(new Event('change'));
}

async function uploadSet() {
  pick(setName);
  await vi.waitFor(() => expect(root.textContent).toContain('uploaded'));
  await nextTick();
}

function type(field: string, value: string) {
  const input = [...root.querySelectorAll('label')]
    .find((l) => l.querySelector('.field-name')?.textContent === field)
    ?.querySelector('input') as HTMLInputElement;
  input.value = value;
  input.dispatchEvent(new Event('change'));
}

describe('Book Slot', () => {
  it('opens a rhythm Show on its next free date and books it', async () => {
    const router = await mount('/shows/brunchtime/book');
    const text = root.textContent ?? '';
    for (const part of ['Book an episode', 'Brunchtime', 'Every week · Sundays · 12:00–14:00']) {
      expect(text).toContain(part);
    }
    // No Show picker: Book Slot belongs to one Show.
    expect(root.querySelector('select')).toBeNull();
    expect(rows()).toEqual([
      'Start | So 01.11. · 12:00',
      'End | So 01.11. · 14:00',
      'Length | 2 h',
    ]);
    expect(button('So 01.11.').getAttribute('aria-pressed')).toBe('true');
    expect(button('12').getAttribute('aria-pressed')).toBe('true');

    await click('Book episode');
    expect(router.currentRoute.value.fullPath).toBe('/shows/77');
    expect(bookBody()).toEqual({
      date: '2026-11-01',
      start_time: '12:00',
      end_time: '14:00',
      mode: 'live',
    });
  });

  it('books a show that passes midnight', async () => {
    await mount('/shows/stepper/book');
    expect(root.textContent).toContain('No fixed rhythm · pick any free time');
    // No rhythm: today at the next full hour, 2 h.
    expect(rows()).toEqual([
      'Start | Di 27.10. · 23:00',
      'End | Mi 28.10. · 01:00 · +1 day',
      'Length | 2 h',
    ]);
    expect(root.querySelector('.midnight-label')?.textContent).toBe('00:00');
    // The past hours of today are off.
    expect(button('21').disabled).toBe(true);
    expect(button('22').disabled).toBe(false);

    await click('Book episode');
    expect(bookBody()).toEqual({
      date: '2026-10-27',
      start_time: '23:00',
      end_time: '01:00',
      mode: 'live',
    });
  });

  it('Start show now books the current minute and opens the Social media tab', async () => {
    const router = await mount('/shows/brunchtime/book');
    await click('Now · 22:13');
    expect(root.textContent).toContain('Starts right away at 22:13.');
    expect(root.textContent).not.toContain('Start · ');
    expect(rows()).toEqual([
      'Start | Now · Di 27.10. · 22:13',
      'End | Mi 28.10. · 00:13 · +1 day',
      'Length | 2 h',
    ]);

    await click('Start show now');
    expect(router.currentRoute.value.fullPath).toBe('/shows/77');
    expect(router.options.history.state).toMatchObject({ episodeSocial: 77 });
    expect(bookBody()).toEqual({
      date: '2026-10-27',
      start_time: '22:13',
      end_time: '00:13',
      mode: 'live',
    });
  });

  it('marks taken times and names a clash in the overview', async () => {
    booked = [{ date: '2026-10-31', start_time: '21:00', end_time: '23:00' }];
    await mount('/shows/stepper/book');
    expect(button('Sa 31.10.').classList.contains('busy')).toBe(true);
    await click('Sa 31.10.');
    expect(button('21').disabled).toBe(true);
    expect(button('21').classList.contains('taken')).toBe(true);
    expect(button('23').disabled).toBe(false);

    await click('20');
    expect(rows()[0]).toBe('Start | Sa 31.10. · 20:00');
    expect(root.textContent).toContain(
      'Another show has Sa 31.10. 21:00–23:00. Pick another time or make it shorter.'
    );
    expect(button('Book episode').disabled).toBe(true);

    await click('1 h');
    expect(root.querySelector('[role="alert"]')).toBeNull();
    expect(button('Book episode').disabled).toBe(false);
  });

  it('books over an idle show only after Book over it', async () => {
    booked = [{ date: '2026-10-27', start_time: '21:30', end_time: '23:30', take_over: true }];
    await mount('/shows/stepper/book');
    // An idle show does not block its time.
    expect(button('22').disabled).toBe(false);
    await click('Now · 22:13');
    expect(root.querySelector('[role="alert"]')).toBeNull();

    await click('Start show now');
    expect(bookBody()).toBeUndefined();
    expect(root.textContent).toContain(
      'The show booked 21:30–23:30 has not been on air for 10 min. Book over it? It will end at 22:13.'
    );
    await click('Cancel');
    expect(root.textContent).not.toContain('Book over it?');
    expect(bookBody()).toBeUndefined();

    await click('Start show now');
    await click('Book over it');
    expect(bookBody()).toEqual({
      date: '2026-10-27',
      start_time: '22:13',
      end_time: '00:13',
      mode: 'live',
      take_over: true,
    });
  });

  it('takes typed times in Edit manually; an end before the start is the next day', async () => {
    await mount('/shows/stepper/book');
    await click('Edit manually');
    type('Date', '2026-11-04');
    type('Start', '23:30');
    type('End', '00:15');
    await settle();
    await click('Done');
    expect(rows()).toEqual([
      'Start | Mi 04.11. · 23:30',
      'End | Do 05.11. · 00:15 · +1 day',
      'Length | 45 min',
    ]);

    await click('Edit manually');
    type('End', '23:40');
    await settle();
    expect(root.textContent).toContain('The end must be at least 15 minutes after the start.');
    type('End', '12:00');
    await settle();
    expect(root.textContent).toContain('A show can be 12 h at most.');
    expect(button('Book episode').disabled).toBe(true);
  });

  it('shows the server error and loads the taken times again', async () => {
    const router = await mount('/shows/brunchtime/book');
    bookResponse = () =>
      Response.json(
        { error: 'This time overlaps an existing show "Other" on 2026-11-01' },
        { status: 409 }
      );
    await click('Book episode');
    expect(root.textContent).toContain('This time overlaps an existing show');
    expect(bookedLoads).toBe(2);
    expect(router.currentRoute.value.fullPath).toBe('/shows/brunchtime/book');
  });

  it('opens on the free date picked on the Show page (?date=) and keeps it for Pre-recorded', async () => {
    const router = await mount('/shows/brunchtime/book?date=2026-11-08');
    expect(rows()[0]).toBe('Start | So 08.11. · 12:00');
    // So 08.11. is the 13th day: More days is open.
    expect(button('So 08.11.').getAttribute('aria-pressed')).toBe('true');
    expect(requests.some((r) => r.url.endsWith('/slots'))).toBe(false);

    await click('Pre-recorded');
    expect(router.currentRoute.value.fullPath).toBe(
      '/shows/brunchtime/book?date=2026-11-08&mode=prerecorded'
    );
    expect(rows()[0]).toBe('Start | So 08.11. · 12:00');
  });

  it("sends another host back to the Show's read-only page", async () => {
    const router = await mount('/shows/other/book');
    await vi.waitFor(() => expect(router.currentRoute.value.fullPath).toBe('/shows/other'));
    expect(requests.some((r) => r.url === '/api/series/other/slots')).toBe(false);
  });
});

describe('Book Slot · Pre-recorded', () => {
  it('books only after the upload, without a Now card', async () => {
    const router = await mount('/shows/brunchtime/book');
    await click('Pre-recorded');
    expect(router.currentRoute.value.fullPath).toBe('/shows/brunchtime/book?mode=prerecorded');
    expect(button('Pre-recorded').getAttribute('aria-pressed')).toBe('true');
    expect(root.textContent).toContain('Drop your set here · MP3 or WAV');
    expect(root.textContent).toContain('Pre-recorded · Brunchtime');
    expect(() => button('Now · 22:13')).toThrow();
    expect(button('So 01.11.').disabled).toBe(true);
    expect(button('Book episode').disabled).toBe(true);

    await uploadSet();
    expect(root.textContent).toContain('set.mp3 · 1:58:40 · uploaded');
    expect(button('So 01.11.').disabled).toBe(false);
    await click('Book episode');
    expect(router.currentRoute.value.fullPath).toBe('/shows/77');
    expect(bookBody()).toEqual({
      date: '2026-11-01',
      start_time: '12:00',
      end_time: '14:00',
      mode: 'prerecorded',
      upload_id: 's1',
    });
  });

  it('shows an upload error and keeps Book episode off', async () => {
    finalizeResponse = () =>
      Response.json({ error: 'This file is not a readable MP3 or WAV' }, { status: 400 });
    await mount('/shows/brunchtime/book?mode=prerecorded');

    pick('set.flac');
    await settle();
    expect(root.textContent).toContain('Upload an MP3 or WAV file');
    expect(requests.some((r) => r.url.includes('/upload/'))).toBe(false);

    pick('set.mp3');
    await vi.waitFor(() =>
      expect(root.textContent).toContain('This file is not a readable MP3 or WAV')
    );
    expect(root.textContent).not.toContain('uploaded');
    expect(button('Book episode').disabled).toBe(true);
  });

  it('replaces the file of a booked episode without booking again', async () => {
    const router = await mount('/shows');
    await openReplaceFile(router, 'brunchtime', {
      id: 43,
      date: '2026-11-08',
      start_time: '12:00',
      end_time: '14:00',
    });
    expect(router.currentRoute.value.fullPath).toBe('/shows/brunchtime/book?mode=prerecorded');
    await settle();
    expect(rows()[0]).toBe('Start | So 08.11. · 12:00');

    await uploadSet();
    // The day and time stay: only the file changes.
    expect(button('So 08.11.').disabled).toBe(true);
    expect(button('Edit manually').disabled).toBe(true);
    await click('Book episode');
    expect(router.currentRoute.value.fullPath).toBe('/shows/43');
    expect(requests.find((r) => r.url === '/api/shows/43/prerecorded')?.body).toEqual({
      upload_id: 's1',
    });
    expect(requests.some((r) => r.url.endsWith('/episodes'))).toBe(false);
    expect(requests.some((r) => r.url.endsWith('/slots'))).toBe(false);
  });
});

describe('Book Slot time math', () => {
  it('reads the Berlin clock in summer and winter time', () => {
    expect(berlinNow(new Date('2026-10-27T21:13:00Z'))).toEqual({
      date: '2026-10-27',
      minute: 1333,
    });
    expect(berlinNow(new Date('2026-07-01T22:30:00Z'))).toEqual({ date: '2026-07-02', minute: 30 });
  });

  it('puts an end at or before the start on the next day', () => {
    expect(lengthBetween(23 * 60, 60)).toBe(120);
    expect(lengthBetween(20 * 60, 22 * 60)).toBe(120);
    expect(lengthBetween(20 * 60, 20 * 60)).toBe(1440);
    expect(hhmm(22 * 60 + 13 + 120)).toBe('00:13');
    expect(parseHhmm('8:00')).toBeNull();
    expect(parseHhmm('24:00')).toBeNull();
    expect(addDays('2026-12-31', 1)).toBe('2027-01-01');
  });

  it('starts at the next full hour, after 23:00 on the next day', () => {
    expect(nextFullHour({ date: '2026-10-27', minute: 1333 })).toEqual({
      date: '2026-10-27',
      minute: 1380,
    });
    expect(nextFullHour({ date: '2026-10-27', minute: 1390 })).toEqual({
      date: '2026-10-28',
      minute: 0,
    });
  });

  it('finds clashes across midnight, like the backend', () => {
    const taken = bookedWindows([
      { date: '2026-10-31', start_time: '23:00', end_time: '01:00' },
      { date: '2026-11-02', start_time: '20:00', end_time: null },
    ]);
    expect(clash(taken, windowOf('2026-11-01', 30, 60))).toBeDefined();
    expect(clash(taken, windowOf('2026-11-01', 60, 60))).toBeUndefined();
    // A show without an end clashes at its start only.
    expect(clash(taken, windowOf('2026-11-02', 19 * 60, 60))).toBeUndefined();
    expect(clash(taken, windowOf('2026-11-02', 19 * 60, 61))).toBeDefined();
  });

  it('names the problem as the overview does', () => {
    const now = windowOf('2026-10-27', 1333, 0)[0];
    const at = (start: number, length: number, startsNow = false) =>
      problemOf(windowOf('2026-10-27', start, length), now, startsNow, [], episodeDay);
    expect(at(1380, 120)).toBeNull();
    expect(at(1333, 120, true)).toBeNull();
    expect(at(1320, 120)).toBe('This time is over. Pick a later one.');
    expect(at(1380, 10)).toBe('The end must be at least 15 minutes after the start.');
    expect(at(1380, 721)).toBe('A show can be 12 h at most.');
    expect(lengthText(90)).toBe('1½ h');
    expect(lengthText(135)).toBe('2 h 15 min');
  });

  it('writes dates and set lengths as the flow does', () => {
    expect(slotDate('2026-10-25')).toBe('So 25.10.2026');
    expect(setLength(7120)).toBe('1:58:40');
    expect(setLength(3520)).toBe('58:40');
  });
});

describe('flow', () => {
  it('Book Slot matches the flow', async () => {
    await mount('/shows/brunchtime/book');
    await expectFlow('book-slot', root);
  });

  it('Book Slot · Now matches the flow', async () => {
    await mount('/shows/brunchtime/book');
    await click('Now · 22:13');
    await expectFlow('book-now', root);
  });

  it('Book Slot · Pre-recorded matches the flow', async () => {
    setName = 'brunchtime-0111.mp3';
    await mount('/shows/brunchtime/book?mode=prerecorded');
    await uploadSet();
    await expectFlow('upload', root);
  });
});
