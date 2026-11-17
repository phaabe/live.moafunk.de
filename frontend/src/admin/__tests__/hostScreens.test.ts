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
import HostDashboardPage from '../pages/HostDashboardPage.vue';
import HostSchedulePage from '../pages/HostSchedulePage.vue';
import HostMyShowsPage from '../pages/HostMyShowsPage.vue';
import type { Series, SeriesDetail, UpcomingEpisode, User } from '../api';
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

/** The flow's Dashboard rows: lena's own episodes and two of other hosts. */
const UPCOMING: UpcomingEpisode[] = [
  ['2026-10-25', '12:00', '14:00', 'Brunchtime', 'lena', 'Live', 42],
  ['2026-10-27', '20:00', '22:00', 'Night Shift', 'jonas', 'Live', 44],
  ['2026-10-30', '18:00', '20:00', 'Dub Kitchen', 'mira', 'Pre-recorded', 45],
  ['2026-11-08', '12:00', '14:00', 'Brunchtime', 'lena', 'Pre-recorded', 43],
].map(([date, start_time, end_time, show_name, host_username, mode_label, id]) => ({
  id: id as number,
  date: date as string,
  start_time: start_time as string,
  end_time: end_time as string,
  show_name: show_name as string,
  host_username: host_username as string,
  mode: mode_label === 'Live' ? 'live' : 'prerecorded',
  mode_label: mode_label as string,
}));

let app: App | undefined;
let root: HTMLDivElement;
let streamActive: boolean;
let mySeries: Series[];
let loginRole: User['role'];
/** Air time of the show in /api/shows-overview. */
let onAir: { start: string; end: string };

beforeEach(() => {
  streamActive = false;
  mySeries = [BRUNCHTIME, NIGHT];
  loginRole = 'host';
  onAir = { start: '00:00', end: '23:59' };
  root = document.createElement('div');
  document.body.appendChild(root);
  const today = getBerlinNowISO().slice(0, 10);
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
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
          return Response.json({ episodes: UPCOMING });
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

const links = (selector: string) =>
  [...root.querySelectorAll(`${selector} a`)].map((a) => [
    a.textContent?.trim(),
    a.getAttribute('href'),
  ]);

describe('nav', () => {
  it('gives hosts exactly Dashboard, Schedule and My Shows, on desktop and mobile', async () => {
    await mount(NavBar, 'host');
    const expected = [
      ['Dashboard', '/dashboard'],
      ['Schedule', '/schedule'],
      ['My Shows', '/shows'],
    ];
    expect(links('.nav-links')).toEqual(expected);
    expect(links('.mobile-menu-links')).toEqual(expected);
    expect(root.querySelector('.mobile-menu-links')?.textContent).not.toContain('Radio');
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

describe('Dashboard', () => {
  it("lists all hosts' upcoming shows, each opening its episode", async () => {
    await mount(HostDashboardPage, 'host');
    await vi.waitFor(() => expect(root.querySelectorAll('.episodes li')).toHaveLength(4));
    expect(root.querySelector('h1')?.textContent).toBe('Dashboard');
    const rows = [...root.querySelectorAll('.episodes li')].map((li) => ({
      text: [...li.querySelectorAll('span')].map((s) => s.textContent).join(' | '),
      href: li.querySelector('a')?.getAttribute('href') ?? null,
    }));
    expect(rows).toEqual([
      { text: 'So 25.10. · 12:00–14:00 · Brunchtime · lena | Upcoming · Live', href: '/shows/42' },
      {
        text: 'Di 27.10. · 20:00–22:00 · Night Shift · jonas | Upcoming · Live',
        href: '/shows/44',
      },
      {
        text: 'Fr 30.10. · 18:00–20:00 · Dub Kitchen · mira | Upcoming · Pre-recorded',
        href: '/shows/45',
      },
      {
        text: 'So 08.11. · 12:00–14:00 · Brunchtime · lena | Upcoming · Pre-recorded',
        href: '/shows/43',
      },
    ]);
  });
});

describe('Schedule', () => {
  it("lists the host's next episodes, soonest first", async () => {
    await mount(HostSchedulePage, 'host');
    await vi.waitFor(() => expect(root.querySelectorAll('.episodes li')).toHaveLength(2));
    const text = root.textContent ?? '';
    expect(text).toContain('Schedule');
    expect(text).toContain('Your next episodes');
    expect(text).not.toContain('On air now');
    for (const absent of ['Month', 'Open calendar', 'Free slots']) {
      expect(text).not.toContain(absent);
    }
    const rows = [...root.querySelectorAll('.episodes a')].map((a) => ({
      text: [...a.querySelectorAll('span')].map((s) => s.textContent).join(' | '),
      href: a.getAttribute('href'),
    }));
    expect(rows).toEqual([
      { text: 'So 25.10. · Brunchtime | Upcoming · Live', href: '/shows/42' },
      { text: 'So 08.11. · Brunchtime | Upcoming · Pre-recorded', href: '/shows/43' },
    ]);
  });

  it('shows what is on air while the stream is live', async () => {
    streamActive = true;
    await mount(HostSchedulePage, 'host');
    await vi.waitFor(() => expect(root.textContent).toContain('On air · Brunchtime · until 23:59'));
    expect(root.textContent).toContain('On air now');
  });
});

describe('My Shows', () => {
  it("lists the host's Shows with their rhythm", async () => {
    const router = await mount(HostMyShowsPage, 'host');
    await vi.waitFor(() => expect(root.querySelectorAll('.shows li')).toHaveLength(2));
    const rows = [...root.querySelectorAll('.shows a')].map((a) => ({
      text: [...a.querySelectorAll('span')].map((s) => s.textContent?.trim()).join(' | '),
      href: a.getAttribute('href'),
    }));
    expect(rows).toEqual([
      { text: 'Brunchtime | Sundays · 12:00–14:00', href: '/shows/brunchtime' },
      { text: 'Night | ', href: '/shows/night' },
    ]);
    const text = root.textContent ?? '';
    expect(text).toContain('My Shows');
    for (const absent of ['UNHEARD', 'Prepare to stream', 'All Shows']) {
      expect(text).not.toContain(absent);
    }

    [...root.querySelectorAll('button')].find((b) => b.textContent?.trim() === 'New show')?.click();
    await vi.waitFor(() => expect(router.currentRoute.value.fullPath).toBe('/shows/new'));
  });

  it('offers New show when the host has no Shows yet', async () => {
    mySeries = [];
    await mount(HostMyShowsPage, 'host');
    await vi.waitFor(() => expect(root.textContent).toContain('New show'));
    expect(root.querySelectorAll('.shows li')).toHaveLength(0);
  });
});

describe('flow', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('Schedule matches the flow', async () => {
    streamActive = true;
    onAir = { start: '12:00', end: '14:00' };
    vi.useFakeTimers({ toFake: ['Date'] });
    // 12:30 or 13:30 in Berlin, so the 12:00–14:00 show is on air.
    vi.setSystemTime(new Date(`${getBerlinNowISO().slice(0, 10)}T11:30:00Z`));
    mySeries = [BRUNCHTIME];
    await mount({ render: () => [h(NavBar), h(HostSchedulePage)] }, 'host');
    await vi.waitFor(() => expect(root.textContent).toContain('On air now'));
    await vi.waitFor(() => expect(root.querySelectorAll('.episodes li')).toHaveLength(2));
    await expectFlow('schedule', root);
  });

  it('Dashboard matches the flow', async () => {
    await mount({ render: () => [h(NavBar), h(HostDashboardPage)] }, 'host');
    await vi.waitFor(() => expect(root.querySelectorAll('.episodes li')).toHaveLength(4));
    await expectFlow('dashboard', root);
  });

  it('My Shows matches the flow', async () => {
    mySeries = [BRUNCHTIME];
    await mount({ render: () => [h(NavBar), h(HostMyShowsPage)] }, 'host');
    await vi.waitFor(() => expect(root.querySelectorAll('.shows li')).toHaveLength(1));
    await expectFlow('my-shows', root);
  });

  it('Admin Login matches the flow', async () => {
    await mount(LoginPage, null as unknown as User['role'], '/login');
    await expectFlow('login', root);
  });
});
