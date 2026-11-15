import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApp, h, nextTick, type App } from 'vue';
import { createPinia } from 'pinia';
import { createMemoryHistory, createRouter, RouterView, type Router } from 'vue-router';
import { useAuthStore } from '../stores/auth';
import appRouter from '../router';
import { episodeDay, episodeTarget } from '../episodeRow';
import ShowCreatePage from '../pages/ShowCreatePage.vue';
import ShowSeriesPage from '../pages/ShowSeriesPage.vue';
import type { SeriesDetail, User } from '../api';

// The wizard has its own tests; here it only has to be the admin page.
vi.mock('../pages/ShowWizard.vue', () => ({
  __esModule: true,
  default: { render: () => h('div', 'Show wizard') },
}));

const BRUNCHTIME: SeriesDetail = {
  id: 3,
  slug: 'brunchtime',
  name: 'Brunchtime',
  description: 'Sunday brunch sounds, every week from 12:00.',
  cover_url: null,
  rhythm: { repeat_rule: 'weekly', weekday: 7, start_time: '12:00', end_time: '14:00' },
  rhythm_line: 'Sundays · 12:00–14:00',
  episodes: [
    episode(42, '2026-10-25', 'Live', 'upcoming', 'Upcoming'),
    episode(41, '2026-10-18', 'Live', 'wrapup_open', 'Finished · wrap-up open'),
    episode(40, '2026-10-11', 'Pre-recorded', 'finished', 'Finished'),
  ],
};

function episode(
  id: number,
  date: string,
  mode_label: string,
  status: SeriesDetail['episodes'][number]['status'],
  status_label: string
): SeriesDetail['episodes'][number] {
  return {
    id,
    title: `Brunchtime #${id}`,
    date,
    start_time: '12:00',
    end_time: '14:00',
    mode: mode_label === 'Live' ? 'live' : 'prerecorded',
    mode_label,
    status,
    status_label,
  };
}

let app: App | undefined;
let root: HTMLDivElement;
let posted: FormData | undefined;

beforeEach(() => {
  posted = undefined;
  root = document.createElement('div');
  document.body.appendChild(root);
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, options?: RequestInit) => {
      if (url === '/api/series' && options?.method === 'POST') {
        posted = options.body as FormData;
        return Response.json({ ...BRUNCHTIME, episodes: undefined }, { status: 201 });
      }
      if (url === '/api/series/brunchtime') return Response.json(BRUNCHTIME);
      if (url === '/api/series/someone-else')
        return Response.json({ error: 'Show not found' }, { status: 404 });
      throw new Error(`Unexpected API request: ${url}`);
    })
  );
});

afterEach(() => {
  app?.unmount();
  app = undefined;
  root.remove();
  vi.unstubAllGlobals();
});

async function mount(role: User['role'], path: string): Promise<Router> {
  const stub = (text: string) => ({ render: () => h('div', text) });
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/shows', component: stub('My Shows list') },
      { path: '/shows/new', component: ShowCreatePage },
      { path: '/shows/:slug', component: ShowSeriesPage },
      { path: '/shows/:slug/book', component: stub('Book Slot screen') },
      { path: '/shows/:id(\\d+)', component: stub('Episode screen') },
    ],
  });
  const pinia = createPinia();
  app = createApp({ render: () => h(RouterView) });
  app.use(pinia).use(router);
  useAuthStore(pinia).user = { id: 1, username: 'operator', role };
  await router.push(path);
  await router.isReady();
  app.mount(root);
  await vi.waitFor(() => expect(root.textContent?.trim()).not.toBe(''));
  await nextTick();
  return router;
}

function input(selector: string, value: string, event = 'input') {
  const el = root.querySelector(selector) as HTMLInputElement | HTMLSelectElement;
  el.value = value;
  el.dispatchEvent(new Event(event));
}

function button(text: string): HTMLButtonElement {
  const found = [...root.querySelectorAll('button')].find((b) => b.textContent?.trim() === text);
  if (!found) throw new Error(`No button "${text}"`);
  return found;
}

describe('show routes', () => {
  it('keeps /shows/new and numbers out of the slug route', () => {
    expect(appRouter.resolve('/shows/new').name).toBe('show-create');
    expect(appRouter.resolve('/shows/123').name).toBe('show-detail');
    expect(appRouter.resolve('/shows/brunchtime').name).toBe('show-series');
    expect(appRouter.resolve('/shows/show-2024').name).toBe('show-series');
  });
});

describe('episode rows', () => {
  it('write the day as the flow does', () => {
    expect(episodeDay('2026-10-25')).toBe('So 25.10.');
    expect(episodeDay('2026-11-16')).toBe('Mo 16.11.');
  });

  it('link upcoming and wrap-up rows but not finished ones', () => {
    expect(episodeTarget({ id: 42, status: 'upcoming' })).toBe('/shows/42');
    expect(episodeTarget({ id: 41, status: 'wrapup_open' })).toBe('/shows/41');
    expect(episodeTarget({ id: 40, status: 'finished' })).toBeNull();
  });
});

describe('New Show', () => {
  it('is the host screen at /shows/new; admins keep the wizard', async () => {
    await mount('admin', '/shows/new');
    await vi.waitFor(() => expect(root.textContent).toContain('Show wizard'));
    app?.unmount();

    await mount('host', '/shows/new');
    await vi.waitFor(() => expect(root.textContent).toContain('New Show'));
    const text = root.textContent ?? '';
    for (const part of [
      '< My Shows',
      'A show is your series. You book its episodes after this.',
      'Show cover · drop an image',
      'Name',
      'Description',
      'Rhythm',
      'Repeats',
      'Day',
      'Time',
      'Create show',
    ]) {
      expect(text).toContain(part);
    }
    expect(text).not.toContain('Show wizard');
  });

  it('creates the series and opens its Show page', async () => {
    const router = await mount('host', '/shows/new');
    await vi.waitFor(() => expect(root.textContent).toContain('Create show'));
    expect(button('Create show').disabled).toBe(true);

    input('.form-input input', 'Brunchtime');
    input('textarea', 'Sunday brunch sounds.');
    input('input[aria-label="Start time"]', '12:00');
    input('input[aria-label="End time"]', '14:00');
    await nextTick();
    button('Create show').click();

    await vi.waitFor(() => expect(router.currentRoute.value.fullPath).toBe('/shows/brunchtime'));
    expect(Object.fromEntries(posted!.entries())).toEqual({
      name: 'Brunchtime',
      description: 'Sunday brunch sounds.',
      repeat_rule: 'weekly',
      weekday: '7',
      start_time: '12:00',
      end_time: '14:00',
    });
  });

  it('creates a show with no fixed rhythm, without day and time', async () => {
    const router = await mount('host', '/shows/new');
    await vi.waitFor(() => expect(root.textContent).toContain('Create show'));
    input('.form-input input', 'Most Silent Stepper');
    input('select', 'none', 'change');
    await nextTick();
    expect([...root.querySelectorAll('option')].map((o) => o.textContent)).toContain(
      'No fixed rhythm'
    );
    expect(root.querySelector('input[aria-label="Start time"]')).toBeNull();
    expect([...root.querySelectorAll('.label')].map((l) => l.textContent)).not.toContain('Day');
    button('Create show').click();

    await vi.waitFor(() => expect(router.currentRoute.value.fullPath).toBe('/shows/brunchtime'));
    expect(Object.fromEntries(posted!.entries())).toEqual({
      name: 'Most Silent Stepper',
      description: '',
      repeat_rule: 'none',
    });
  });

  it('refuses an end time before the start', async () => {
    await mount('host', '/shows/new');
    await vi.waitFor(() => expect(root.textContent).toContain('Create show'));
    input('.form-input input', 'Brunchtime');
    input('input[aria-label="Start time"]', '14:00');
    input('input[aria-label="End time"]', '12:00');
    await nextTick();
    expect(root.textContent).toContain('The end time must be after the start time');
    expect(button('Create show').disabled).toBe(true);
  });
});

describe('Show page', () => {
  it('shows the series and its episodes as the flow lists them', async () => {
    const router = await mount('host', '/shows/brunchtime');
    await vi.waitFor(() => expect(root.textContent).toContain('Brunchtime'));
    const text = root.textContent ?? '';
    for (const part of [
      '< My Shows',
      'Sunday brunch sounds, every week from 12:00.',
      'Book Slot',
      'Episodes',
    ]) {
      expect(text).toContain(part);
    }

    const rows = [...root.querySelectorAll('.episodes li')].map((li) => ({
      text: [...li.querySelectorAll('span')].map((s) => s.textContent).join(' | '),
      href: li.querySelector('a')?.getAttribute('href') ?? null,
    }));
    expect(rows).toEqual([
      { text: 'So 25.10. · Live | Upcoming', href: '/shows/42' },
      { text: 'So 18.10. · Live | Finished · wrap-up open', href: '/shows/41' },
      { text: 'So 11.10. · Pre-recorded | Finished', href: null },
    ]);

    button('Book Slot').click();
    await vi.waitFor(() =>
      expect(router.currentRoute.value.fullPath).toBe('/shows/brunchtime/book')
    );
  });

  it("sends a host away from another host's show", async () => {
    const router = await mount('host', '/shows/someone-else');
    await vi.waitFor(() => expect(router.currentRoute.value.fullPath).toBe('/shows'));
  });
});
