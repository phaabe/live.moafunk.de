import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApp, h, nextTick, type App } from 'vue';
import { createPinia } from 'pinia';
import { createMemoryHistory, createRouter, RouterView, type Router } from 'vue-router';
import { useAuthStore } from '../stores/auth';
import BookSlotPage from '../pages/BookSlotPage.vue';
import { openReplaceFile } from '../replaceFile';
import { setLength, slotDate } from '../episodeRow';
import type { Series, SeriesSlot } from '../api';
import { expectFlow } from '../flow/expectFlow';

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
const NIGHT = series('night', 'Night');
/** "No fixed rhythm": no slots, a free date and time. */
const STEPPER: Series = {
  ...series('stepper', 'Most Silent Stepper'),
  rhythm: null,
  rhythm_line: null,
};
const slot = (date: string): SeriesSlot => ({ date, start_time: '12:00', end_time: '14:00' });

let app: App | undefined;
let root: HTMLDivElement;
let slots: SeriesSlot[];
let slotLoads: number;
let requests: { url: string; method: string; body: unknown }[];
let bookResponse: () => Response;
/** File name the upload finalize answers with. */
let setName: string;

beforeEach(() => {
  slots = [slot('2026-10-25'), slot('2026-11-01')];
  slotLoads = 0;
  requests = [];
  bookResponse = () => Response.json({ id: 77 }, { status: 201 });
  setName = 'set.mp3';
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
      if (url === '/api/series/stepper/episodes') return bookResponse();
      if (url === '/api/series') return Response.json({ series: [BRUNCHTIME, NIGHT] });
      if (url === '/api/series/brunchtime/slots') {
        slotLoads += 1;
        return Response.json({ slots });
      }
      if (url === '/api/series/brunchtime/episodes') return bookResponse();
      if (url === '/api/series/brunchtime/upload/init') return Response.json({ session_id: 's1' });
      if (url.startsWith('/api/series/brunchtime/upload/chunk/s1'))
        return Response.json({ index: 0 });
      if (url === '/api/series/brunchtime/upload/finalize/s1')
        return Response.json({ upload_id: 's1', filename: setName, duration: 7120 });
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

const select = (label: string) =>
  [...root.querySelectorAll('label')]
    .find((l) => l.querySelector('.label')?.textContent === label)
    ?.querySelector('select') as HTMLSelectElement;

const options = (label: string) =>
  [...select(label).querySelectorAll('option')].map((o) => o.textContent?.trim());

function button(text: string): HTMLButtonElement {
  const found = [...root.querySelectorAll('button')].find((b) => b.textContent?.trim() === text);
  if (!found) throw new Error(`No button "${text}"`);
  return found;
}

async function uploadSet() {
  const input = root.querySelector('input[type="file"]') as HTMLInputElement;
  const file = new File(['abc'], 'set.mp3', { type: 'audio/mpeg' });
  Object.defineProperty(input, 'files', { value: [file] });
  input.dispatchEvent(new Event('change'));
  await vi.waitFor(() => expect(root.textContent).toContain('uploaded'));
  await nextTick();
}

describe('Book Slot', () => {
  it('shows the flow screen and books a live slot', async () => {
    const router = await mount('/shows/brunchtime/book');
    const text = root.textContent ?? '';
    for (const part of [
      '< Brunchtime',
      'Book Slot',
      'Show',
      'Mode',
      'Live',
      'Pre-recorded',
      'Date',
      'Time',
      'Pre-recorded shows need the file before you pick a time.',
      'Book slot',
    ]) {
      expect(text).toContain(part);
    }
    expect(options('Show')).toEqual(['Brunchtime', 'Night']);
    expect(options('Date')).toEqual(['So 25.10.2026', 'So 01.11.2026']);
    expect(options('Time')).toEqual(['12:00–14:00']);

    button('Book slot').click();
    await vi.waitFor(() => expect(router.currentRoute.value.fullPath).toBe('/shows/77'));
    expect(requests.find((r) => r.url.endsWith('/episodes'))?.body).toEqual({
      date: '2026-10-25',
      start_time: '12:00',
      end_time: '14:00',
      mode: 'live',
    });
  });

  it('shows the server error and reloads the slots when the slot was taken', async () => {
    const router = await mount('/shows/brunchtime/book');
    bookResponse = () => {
      slots = [slot('2026-11-01')];
      return Response.json(
        { error: 'This time overlaps an existing show "Other" on 2026-10-25' },
        { status: 409 }
      );
    };
    button('Book slot').click();
    await vi.waitFor(() => expect(root.textContent).toContain('This time overlaps'));
    await settle();
    expect(slotLoads).toBe(2);
    expect(options('Date')).toEqual(['So 01.11.2026']);
    expect(router.currentRoute.value.fullPath).toBe('/shows/brunchtime/book');
  });

  it('switches to another show of the host', async () => {
    const router = await mount('/shows/brunchtime/book');
    const show = select('Show');
    show.value = 'night';
    show.dispatchEvent(new Event('change'));
    await vi.waitFor(() => expect(router.currentRoute.value.fullPath).toBe('/shows/night/book'));
  });
});

describe('Book Slot for a show with no fixed rhythm', () => {
  const field = (selector: string) => root.querySelector(selector) as HTMLInputElement;
  function fill(selector: string, value: string) {
    field(selector).value = value;
    field(selector).dispatchEvent(new Event('input'));
  }

  it('books a free date and time', async () => {
    const router = await mount('/shows/stepper/book');
    expect(requests.some((r) => r.url.endsWith('/slots'))).toBe(false);
    const today = new Intl.DateTimeFormat('en-CA', { timeZone: 'Europe/Berlin' }).format(
      new Date()
    );
    expect(field('input[type="date"]').min).toBe(today);
    expect(button('Book slot').disabled).toBe(true);

    fill('input[type="date"]', '2030-11-19');
    fill('input[aria-label="Start time"]', '20:00');
    fill('input[aria-label="End time"]', '22:00');
    await nextTick();
    button('Book slot').click();
    await settle();

    expect(requests.find((r) => r.url === '/api/series/stepper/episodes')?.body).toEqual({
      date: '2030-11-19',
      start_time: '20:00',
      end_time: '22:00',
      mode: 'live',
    });
    expect(router.currentRoute.value.fullPath).toBe('/shows/77');
  });

  it('refuses an end time before the start', async () => {
    await mount('/shows/stepper/book');
    fill('input[type="date"]', '2030-11-19');
    fill('input[aria-label="Start time"]', '22:00');
    fill('input[aria-label="End time"]', '20:00');
    await nextTick();
    expect(root.textContent).toContain('The end time must be after the start time');
    expect(button('Book slot').disabled).toBe(true);
  });
});

describe('Upload Your Set', () => {
  it('opens from Pre-recorded and books only after the upload', async () => {
    const router = await mount('/shows/brunchtime/book');
    const prerecorded = root.querySelector('input[value="prerecorded"]') as HTMLInputElement;
    prerecorded.checked = true;
    prerecorded.dispatchEvent(new Event('change'));
    await vi.waitFor(() =>
      expect(router.currentRoute.value.fullPath).toBe('/shows/brunchtime/book?mode=prerecorded')
    );
    await settle();

    const text = root.textContent ?? '';
    for (const part of [
      '< Book Slot',
      'Upload Your Set',
      'Pre-recorded episodes start with the file.',
      'Drop your set here · MP3 or WAV',
    ]) {
      expect(text).toContain(part);
    }
    expect(text).not.toContain('Pre-recorded shows need the file');
    expect(select('Date').disabled).toBe(true);
    expect(select('Time').disabled).toBe(true);
    expect(button('Book slot').disabled).toBe(true);

    await uploadSet();
    expect(root.textContent).toContain('set.mp3 · 1:58:40 · uploaded');
    expect(select('Date').disabled).toBe(false);
    expect(button('Book slot').disabled).toBe(false);

    button('Book slot').click();
    await vi.waitFor(() => expect(router.currentRoute.value.fullPath).toBe('/shows/77'));
    expect(requests.find((r) => r.url.endsWith('/episodes'))?.body).toEqual({
      date: '2026-10-25',
      start_time: '12:00',
      end_time: '14:00',
      mode: 'prerecorded',
      upload_id: 's1',
    });
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
    expect(options('Date')).toEqual(['So 08.11.2026']);
    expect(select('Date').disabled).toBe(true);

    await uploadSet();
    button('Book slot').click();
    await vi.waitFor(() => expect(router.currentRoute.value.fullPath).toBe('/shows/43'));
    expect(requests.find((r) => r.url === '/api/shows/43/prerecorded')?.body).toEqual({
      upload_id: 's1',
    });
    expect(requests.some((r) => r.url.endsWith('/episodes'))).toBe(false);
    expect(requests.some((r) => r.url.endsWith('/slots'))).toBe(false);
  });
});

describe('Book Slot formats', () => {
  it('write dates and set lengths as the flow does', () => {
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

  it('Upload Your Set matches the flow', async () => {
    setName = 'brunchtime-0811.mp3';
    await mount('/shows/brunchtime/book?mode=prerecorded');
    await uploadSet();
    await expectFlow('upload', root);
  });
});
