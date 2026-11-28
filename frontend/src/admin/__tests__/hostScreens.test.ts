import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApp, h, nextTick, type App, type Component } from 'vue';
import { createPinia, setActivePinia } from 'pinia';
import { createMemoryHistory, createRouter, RouterView, type Router } from 'vue-router';
import { useAuthStore } from '../stores/auth';
import appRouter from '../router';
import { hostPage } from '../hostPage';
import { getBerlinNowISO } from '../showTime';
import NavBar from '@shared/components/NavBar.vue';
import LoginPage from '../pages/LoginPage.vue';
import HostMyShowsPage from '../pages/HostMyShowsPage.vue';
import type { Series, SeriesDetail, User } from '../api';
import { expectFlow } from '../flow/expectFlow';
// Load the composables barrel now, while FontFace is stubbed: the guest
// landing route imports it later, after afterEach unstubs the globals.
import '../composables';

// jsdom does not implement font loading used by the composables barrel.
vi.hoisted(() => {
  vi.stubGlobal(
    'FontFace',
    class {
      load() {
        return Promise.resolve(this);
      }
    }
  );
  Object.defineProperty(document, 'fonts', { value: { add() {} }, configurable: true });
});

const BRUNCHTIME: Series = {
  id: 3,
  slug: 'brunchtime',
  name: 'Brunchtime',
  description: null,
  cover_url: null,
  rhythm: { repeat_rule: 'weekly', weekday: 7, start_time: '12:00', end_time: '14:00' },
  rhythm_line: 'Sundays · 12:00–14:00',
};
const NIGHT: Series = { ...BRUNCHTIME, id: 4, slug: 'night', name: 'Night', rhythm_line: null };

const DETAIL: SeriesDetail = {
  ...BRUNCHTIME,
  episodes: [
    // Newest first, as the API sends them.
    {
      id: 43,
      title: 'Brunchtime #3',
      date: '2026-11-08',
      start_time: '12:00',
      end_time: '14:00',
      mode: 'prerecorded',
      mode_label: 'Pre-recorded',
      status: 'upcoming',
      status_label: 'Upcoming',
    },
    {
      id: 42,
      title: 'Brunchtime #2',
      date: '2026-10-25',
      start_time: '12:00',
      end_time: '14:00',
      mode: 'live',
      mode_label: 'Live',
      status: 'upcoming',
      status_label: 'Upcoming',
    },
    {
      id: 40,
      title: 'Brunchtime #1',
      date: '2026-10-11',
      start_time: '12:00',
      end_time: '14:00',
      mode: 'live',
      mode_label: 'Live',
      status: 'finished',
      status_label: 'Finished',
    },
  ],
};

let app: App | undefined;
let root: HTMLDivElement;
let streamActive: boolean;
let mySeries: Series[];
let loginRole: User['role'];
/** Air time of the show in /api/shows-overview. */
let onAir: { start: string; end: string };
/** API paths that answer 500 in this test. */
let failing: string[];

beforeEach(() => {
  streamActive = false;
  mySeries = [BRUNCHTIME, NIGHT];
  loginRole = 'host';
  onAir = { start: '00:00', end: '23:59' };
  failing = [];
  root = document.createElement('div');
  document.body.appendChild(root);
  // Pin the clock to 12:30 or 13:30 in Berlin today. With the real clock the
  // 00:00–23:59 show has ended in the last minute before midnight.
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(new Date(`${getBerlinNowISO().slice(0, 10)}T11:30:00Z`));
  const today = getBerlinNowISO().slice(0, 10);
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      if (failing.includes(url)) return Response.json({ error: 'boom' }, { status: 500 });
      switch (url) {
        case '/api/stream/status':
          return Response.json({ active: streamActive, recording: false });
        case '/api/shows-overview':
          return Response.json({
            shows: [
              {
                id: 42,
                title: 'Brunchtime #2',
                date: today,
                start_time: onAir.start,
                end_time: onAir.end,
                status: 'scheduled',
                show_type: 'external',
                host_has_logged_in: true,
                artists: [],
              },
            ],
          });
        case '/api/series':
          return Response.json({ series: mySeries });
        case '/api/upcoming-episodes':
          return Response.json({
            episodes: [
              {
                id: 31,
                title: 'Brunchtime #31',
                date: '2099-01-04',
                start_time: '12:00',
                end_time: '14:00',
                show_name: 'Brunchtime',
                series_slug: 'brunchtime',
                host_username: 'lena',
                mode: 'live',
                mode_label: 'Live',
              },
            ],
          });
        case '/api/series/brunchtime':
          return Response.json(DETAIL);
        case '/api/series/night':
          return Response.json({ ...NIGHT, episodes: [] });
        case '/api/auth/login':
          return Response.json({ user: { id: 1, username: 'lena', role: loginRole } });
        case '/api/my-show':
          return Response.json({
            assigned: true,
            shows: [{ id: 39, title: 'Guest show', date: '2099-01-04', artists: [] }],
          });
        default:
          throw new Error(`Unexpected API request: ${url}`);
      }
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

async function mount(component: Component, role: User['role'], path = '/'): Promise<Router> {
  const stub = (text: string) => ({ render: () => h('div', text) });
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component },
      { path: '/login', component },
      { path: '/dashboard', component: stub('Dashboard screen') },
      { path: '/schedule', component: stub('Schedule screen') },
      { path: '/calendar', component: stub('Calendar') },
      { path: '/shows', component: stub('Shows') },
      { path: '/shows/new', component: stub('New Show screen') },
      { path: '/shows/:slug', component: stub('Show page') },
      { path: '/shows/:id(\\d+)', component: stub('Episode screen') },
      { path: '/stream/on-air', component: stub('On Air screen') },
    ],
  });
  const pinia = createPinia();
  app = createApp({ render: () => h(RouterView) });
  app.use(pinia).use(router);
  if (role) useAuthStore(pinia).user = { id: 1, username: 'lena', role };
  await router.push(path);
  await router.isReady();
  app.mount(root);
  for (let i = 0; i < 5; i++) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    await nextTick();
  }
  return router;
}

/** A row's parts as the flow check reads them: "So 25.10. · Brunchtime | Upcoming · Live". */
const rowParts = (row: Element | null) =>
  [...(row?.children ?? [])]
    .map((c) => (c.textContent ?? '').replace(/\s+/g, ' ').trim())
    .filter(Boolean)
    .join(' | ');

const links = (selector: string) =>
  [...root.querySelectorAll(`${selector} a`)].map((a) => [
    a.textContent?.trim(),
    a.getAttribute('href'),
  ]);

describe('nav', () => {
  it('gives hosts exactly Dashboard and My Show, on desktop and mobile', async () => {
    await mount(NavBar, 'host');
    const expected = [
      ['Dashboard', '/dashboard'],
      ['My Show', '/shows'],
    ];
    expect(links('.nav-links')).toEqual(expected);
    expect(links('.mobile-menu-links')).toEqual(expected);
    expect(root.querySelector('.mobile-menu-links')?.textContent).not.toContain('Radio');
  });

  it('links the logo and Admin to the Dashboard', async () => {
    await mount(NavBar, 'host');
    const brand = root.querySelector('a.nav-brand');
    expect(brand?.getAttribute('href')).toBe('/dashboard');
    expect(brand?.querySelector('img.logo')).not.toBeNull();
  });

  it('keeps the admin items', async () => {
    await mount(NavBar, 'admin');
    expect(links('.nav-links').map(([label]) => label)).toEqual([
      'Dashboard',
      'Calendar',
      'Shows',
      'Artists',
      'Overlay',
      'Recording',
      'Users',
      'Config',
      'Instagram',
    ]);
    expect(root.querySelector('.mobile-menu-links')?.textContent).toContain('Radio');
  });
});

describe('host pages on shared paths', () => {
  it('render the host screen for hosts and the old page for others', async () => {
    const page = hostPage(
      async () => ({ render: () => h('div', 'host screen') }),
      async () => ({ render: () => h('div', 'admin page') })
    );
    await mount(page, 'host');
    await vi.waitFor(() => expect(root.textContent).toBe('host screen'));
    app?.unmount();
    await mount(page, 'superadmin');
    await vi.waitFor(() => expect(root.textContent).toBe('admin page'));
  });
});

describe('login', () => {
  async function logIn(role: User['role'], path = '/login') {
    loginRole = role;
    const router = await mount(LoginPage, null as unknown as User['role'], path);
    const [username, password] = [...root.querySelectorAll('input')] as HTMLInputElement[];
    username.value = 'lena';
    username.dispatchEvent(new Event('input'));
    password.value = 'secret';
    password.dispatchEvent(new Event('input'));
    await nextTick();
    root.querySelector('form')?.dispatchEvent(new Event('submit'));
    return router;
  }

  it('shows the flow login screen', async () => {
    await mount(LoginPage, null as unknown as User['role'], '/login');
    const text = root.textContent ?? '';
    for (const part of ['Admin Login', 'Username', 'Password', 'Login', 'Forgot password?']) {
      expect(text).toContain(part);
    }
    expect(root.querySelector('img[alt="Moafunk"]')).not.toBeNull();
  });

  it.each([
    ['host', '/dashboard'],
    ['admin', '/calendar'],
    ['superadmin', '/calendar'],
  ] as const)('sends a %s to %s', async (role, target) => {
    const router = await logIn(role);
    await vi.waitFor(() => expect(router.currentRoute.value.fullPath).toBe(target));
  });

  it.each(['host', 'admin'] as const)('honours an explicit redirect for a %s', async (role) => {
    const router = await logIn(role, '/login?redirect=/shows/brunchtime');
    await vi.waitFor(() => expect(router.currentRoute.value.fullPath).toBe('/shows/brunchtime'));
  });
});

describe('router guard', () => {
  beforeEach(async () => {
    setActivePinia(createPinia());
    useAuthStore().initialized = true;
    // The app router is shared: start each test somewhere else, or a push to
    // the same path would skip the guards.
    await appRouter.replace('/login');
  });

  it('sends a host from the calendar to Dashboard', async () => {
    useAuthStore().user = { id: 1, username: 'lena', role: 'host' };
    await appRouter.push('/calendar');
    expect(appRouter.currentRoute.value.fullPath).toBe('/dashboard');
  });

  it('lets an admin open the calendar', async () => {
    useAuthStore().user = { id: 2, username: 'admin', role: 'admin' };
    await appRouter.push('/calendar');
    expect(appRouter.currentRoute.value.fullPath).toBe('/calendar');
  });

  it('sends a guest from the calendar to their show', async () => {
    useAuthStore().user = { id: 3, username: 'guest', role: 'guest' };
    await appRouter.push('/calendar');
    expect(appRouter.currentRoute.value.fullPath).toBe('/shows/39');
  });
});

describe('My Shows', () => {
  /** A card's details as "Label | value". */
  const facts = (card: Element) => [...card.querySelectorAll('dl > div')].map((d) => rowParts(d));
  const cardButtons = (card: Element) =>
    [...card.querySelectorAll('button')].map((b) => ({
      text: b.textContent?.trim(),
      primary: b.classList.contains('variant-primary'),
    }));

  it('shows a single Show as one card with its details as plain text', async () => {
    mySeries = [
      {
        ...BRUNCHTIME,
        description: 'Sunday brunch sounds, every week from 12:00.',
        instagram_handle: 'brunchtime',
      },
    ];
    const router = await mount(HostMyShowsPage, 'host');
    await vi.waitFor(() => expect(root.querySelectorAll('.my-show-card')).toHaveLength(1));
    expect(root.querySelector('h1')?.textContent).toBe('My Show');
    expect(root.querySelector('.show-grid')).toBeNull();
    const card = root.querySelector('.my-show-card')!;
    expect(card.querySelector('h2')?.textContent?.trim()).toBe('Brunchtime');
    expect(card.querySelector('.description')?.textContent).toBe(
      'Sunday brunch sounds, every week from 12:00.'
    );
    expect(facts(card)).toEqual([
      'Next episode | So 04.01. · 12:00–14:00',
      'Rhythm | Every week · Sundays · 12:00–14:00',
      'Instagram | @brunchtime',
    ]);
    expect(card.textContent).not.toContain('Read only');
    expect(card.querySelectorAll('input, select, textarea')).toHaveLength(0);
    expect(cardButtons(card)).toEqual([
      { text: 'Book Slot', primary: false },
      { text: 'Edit', primary: false },
    ]);
    expect(card.querySelector('.name-link')?.getAttribute('href')).toBe('/shows/brunchtime');
    const text = root.textContent ?? '';
    for (const absent of ['UNHEARD', 'Episodes', 'Upcoming', 'Free slots']) {
      expect(text).not.toContain(absent);
    }

    [...root.querySelectorAll('button')].find((b) => b.textContent?.trim() === 'New show')?.click();
    await vi.waitFor(() => expect(router.currentRoute.value.fullPath).toBe('/shows/new'));
  });

  it('puts more Shows in a grid, each card opening its Show page', async () => {
    await mount(HostMyShowsPage, 'host');
    await vi.waitFor(() => expect(root.querySelectorAll('.my-show-card')).toHaveLength(2));
    expect(root.querySelector('h1')?.textContent).toBe('Your shows');
    const cards = [...root.querySelectorAll('.show-grid > .my-show-card')];
    expect(cards.map((c) => c.querySelector('.name-link')?.getAttribute('href'))).toEqual([
      '/shows/brunchtime',
      '/shows/night',
    ]);
    // The same facts in the same places, with or without a rhythm or an episode.
    expect(facts(cards[0])).toEqual([
      'Next episode | So 04.01. · 12:00–14:00',
      'Rhythm | Every week · Sundays · 12:00–14:00',
      'Instagram | Not set',
    ]);
    expect(facts(cards[1])).toEqual([
      'Next episode | None booked',
      'Rhythm | No fixed rhythm',
      'Instagram | Not set',
    ]);
    for (const card of cards) {
      expect(cardButtons(card)).toEqual([
        { text: 'Book Slot', primary: false },
        { text: 'Edit', primary: false },
      ]);
    }
  });

  it('offers New show when the host has no Shows yet', async () => {
    mySeries = [];
    await mount(HostMyShowsPage, 'host');
    await vi.waitFor(() => expect(root.textContent).toContain('You have no show yet.'));
    expect(root.textContent).toContain('New show');
    expect(root.querySelectorAll('.my-show-card')).toHaveLength(0);
  });
});

describe('My Shows states', () => {
  it('shows each Show’s cover in its card', async () => {
    mySeries = [{ ...BRUNCHTIME, cover_url: '/covers/brunchtime.jpg' }, NIGHT];
    await mount(HostMyShowsPage, 'host');
    await vi.waitFor(() => expect(root.querySelectorAll('.my-show-card')).toHaveLength(2));
    const covers = [...root.querySelectorAll('.my-show-card')].map(
      (card) => card.querySelector('.cover img')?.getAttribute('src') ?? null
    );
    expect(covers).toEqual(['/covers/brunchtime.jpg', null]);
  });

  it('says so when the list cannot load', async () => {
    failing = ['/api/series'];
    await mount(HostMyShowsPage, 'host');
    await vi.waitFor(() =>
      expect(root.querySelector('[role="alert"]')?.textContent).toBe('Could not load your shows.')
    );
    expect(root.textContent).not.toContain('You have no show yet.');
  });
});

describe('flow', () => {
  it('My Shows matches the flow', async () => {
    mySeries = [BRUNCHTIME];
    await mount({ render: () => [h(NavBar), h(HostMyShowsPage)] }, 'host');
    await vi.waitFor(() => expect(root.querySelectorAll('.my-show-card')).toHaveLength(1));
    await expectFlow('my-shows', root);
  });

  it('Admin Login matches the flow', async () => {
    await mount(LoginPage, null as unknown as User['role'], '/login');
    await expectFlow('login', root);
  });
});
