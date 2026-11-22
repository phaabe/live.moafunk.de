import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApp, h, nextTick, type App } from 'vue';
import { createPinia, setActivePinia } from 'pinia';
import { createMemoryHistory, createRouter, RouterView, type Router } from 'vue-router';
import { useAuthStore } from '../stores/auth';
import appRouter from '../router';
import { episodeDay, episodeTarget } from '../episodeRow';
import ShowCreatePage from '../pages/ShowCreatePage.vue';
import ShowSeriesPage from '../pages/ShowSeriesPage.vue';
import {
  countdown,
  hostsLine,
  nextStep,
  rhythmText,
  splitEpisodes,
  timeLeft,
} from '../components/show-page/showPage';
import type { SeriesDetail, SeriesSlot, ShowOverviewItem, User } from '../api';
import { expectFlow } from '../flow/expectFlow';

// The wizard has its own tests; here it only has to be the admin page.
vi.mock('../pages/ShowWizard.vue', () => ({
  __esModule: true,
  default: { render: () => h('div', 'Show wizard') },
}));

type Episode = SeriesDetail['episodes'][number];

function episode(
  id: number,
  date: string,
  mode_label: string,
  status: Episode['status'],
  status_label: string,
  extra: Partial<Episode> = {}
): Episode {
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
    has_file: mode_label !== 'Live',
    soundcloud: null,
    ...extra,
  };
}

/** lena's Show, with the design's example dates. Newest first, as the API sends them. */
const BRUNCHTIME: SeriesDetail = {
  id: 3,
  slug: 'brunchtime',
  name: 'Brunchtime',
  description: 'Sunday brunch sounds, every week from 12:00.',
  cover_url: null,
  rhythm: { repeat_rule: 'weekly', weekday: 7, start_time: '12:00', end_time: '14:00' },
  rhythm_line: 'Sundays · 12:00–14:00',
  instagram_handle: 'brunchtime',
  can_edit: true,
  hosts: [{ username: 'lena' }],
  episodes: [
    episode(43, '2026-11-08', 'Pre-recorded', 'upcoming', 'Upcoming'),
    episode(42, '2026-10-25', 'Live', 'upcoming', 'Upcoming'),
    episode(41, '2026-10-18', 'Live', 'wrapup_open', 'Finished · wrap-up open'),
    episode(40, '2026-10-11', 'Pre-recorded', 'finished', 'Finished', { soundcloud: 'public' }),
  ],
};

/** jonas's Show: lena may view it, not manage it. */
const NIGHT: SeriesDetail = {
  ...BRUNCHTIME,
  id: 4,
  slug: 'night-shift',
  name: 'Night Shift',
  description: null,
  instagram_handle: null,
  rhythm: { repeat_rule: 'biweekly', weekday: 2, start_time: '20:00', end_time: '22:00' },
  rhythm_line: 'Every other Tuesday · 20:00–22:00',
  can_edit: false,
  hosts: [{ username: 'jonas' }],
  episodes: [
    episode(44, '2026-10-27', 'Live', 'upcoming', 'Upcoming', {
      title: 'Night Shift',
      start_time: '20:00',
      end_time: '22:00',
    }),
    episode(39, '2026-10-13', 'Live', 'finished', 'Finished', {
      start_time: '20:00',
      end_time: '22:00',
      soundcloud: 'private',
    }),
  ],
};

const slot = (date: string): SeriesSlot => ({ date, start_time: '12:00', end_time: '14:00' });

/** Fr 23.10., 09:00 in Berlin: So 25.10. 12:00 (CET) is 2 days 4 h away. */
const FRIDAY = '2026-10-23T07:00:00Z';
/** So 25.10., 12:30 in Berlin: episode 42 is on air. */
const ON_AIR = '2026-10-25T11:30:00Z';

let app: App | undefined;
let root: HTMLDivElement;
let posted: FormData | undefined;
let requests: string[];
let brunchtime: SeriesDetail;
let slots: SeriesSlot[];
let slotsFail: boolean;
let streamActive: boolean;
let overview: ShowOverviewItem[];

function airing(id: number, title: string, host: string): ShowOverviewItem {
  return {
    id,
    title,
    date: '2026-10-25',
    start_time: '12:00',
    end_time: '14:00',
    status: 'scheduled',
    show_type: 'external',
    host_username: host,
    host_has_logged_in: true,
    artists: [],
  };
}

beforeEach(() => {
  posted = undefined;
  requests = [];
  brunchtime = JSON.parse(JSON.stringify(BRUNCHTIME));
  slots = [slot('2026-11-01'), slot('2026-11-15')];
  slotsFail = false;
  streamActive = false;
  overview = [airing(42, 'Brunchtime #42', 'lena')];
  root = document.createElement('div');
  document.body.appendChild(root);
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(new Date(FRIDAY));
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, options?: RequestInit) => {
      requests.push(url);
      if (url === '/api/series' && options?.method === 'POST') {
        posted = options.body as FormData;
        return Response.json({ ...BRUNCHTIME, episodes: undefined }, { status: 201 });
      }
      switch (url) {
        case '/api/series/brunchtime':
          return Response.json(brunchtime);
        case '/api/series/night-shift':
          return Response.json(NIGHT);
        case '/api/series/brunchtime/slots':
          return slotsFail
            ? Response.json({ error: 'boom' }, { status: 500 })
            : Response.json({ slots });
        case '/api/series/no-such-show':
          return Response.json({ error: 'Show not found' }, { status: 404 });
        case '/api/stream/status':
          return Response.json({ active: streamActive, recording: false });
        case '/api/shows-overview':
          return Response.json({ shows: overview });
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

async function mount(
  role: User['role'],
  path: string,
  username = role === 'host' ? 'lena' : 'operator'
): Promise<Router> {
  const stub = (text: string) => ({ render: () => h('div', text) });
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/dashboard', component: stub('Dashboard screen') },
      { path: '/shows', component: stub('My Shows list') },
      { path: '/shows/new', component: ShowCreatePage },
      { path: '/shows/:slug', component: ShowSeriesPage },
      { path: '/shows/:slug/book', component: stub('Book Slot screen') },
      { path: '/shows/:id(\\d+)', component: stub('Episode screen') },
      { path: '/stream/on-air', component: stub('On Air screen') },
    ],
  });
  const pinia = createPinia();
  app = createApp({ render: () => h(RouterView) });
  app.use(pinia).use(router);
  useAuthStore(pinia).user = { id: 1, username, role };
  await router.push(path);
  await router.isReady();
  app.mount(root);
  await vi.waitFor(() => expect(root.textContent?.trim()).not.toBe(''));
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

const buttons = () => [...root.querySelectorAll('button')].map((b) => b.textContent?.trim());

/** A row's parts as the flow check reads them: "So 25.10. · 12:00–14:00 · Live | Upcoming". */
const rowParts = (row: Element | null) =>
  [...(row?.children ?? [])]
    .map((c) => (c.textContent ?? '').replace(/\s+/g, ' ').trim())
    .filter(Boolean)
    .join(' | ');

/** Rows of an episodes card: text, link and the chevron (mine) or eye (read-only). */
const rows = (kind: 'upcoming' | 'past') =>
  [...root.querySelectorAll(`.${kind}-episodes li`)].map((li) => ({
    text: rowParts(li.querySelector('.board-row')),
    href: li.querySelector('a')?.getAttribute('href') ?? null,
  }));

const chips = (kind: 'upcoming' | 'past') =>
  [...root.querySelectorAll(`.${kind}-episodes .date-chip`)].map((c) =>
    c.classList.contains('mine') ? 'mine' : c.classList.contains('wrapup') ? 'wrapup' : ''
  );

describe('show routes', () => {
  it('keeps /shows/new and numbers out of the slug route', () => {
    expect(appRouter.resolve('/shows/new').name).toBe('show-create');
    expect(appRouter.resolve('/shows/123').name).toBe('show-detail');
    expect(appRouter.resolve('/shows/brunchtime').name).toBe('show-series');
    expect(appRouter.resolve('/shows/show-2024').name).toBe('show-series');
  });

  it('sends the old Schedule (/schedule) to My Show: its content is on the Show pages', async () => {
    setActivePinia(createPinia());
    const auth = useAuthStore();
    auth.initialized = true;
    auth.user = { id: 1, username: 'lena', role: 'host' };
    await appRouter.push('/schedule');
    expect(appRouter.currentRoute.value.fullPath).toBe('/shows');
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
      '< My Show',
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

describe('Show page helpers', () => {
  it('split upcoming (soonest first) from past (newest first) episodes', () => {
    const { upcoming, past } = splitEpisodes(BRUNCHTIME.episodes);
    expect(upcoming.map((e) => e.id)).toEqual([42, 43]);
    expect(past.map((e) => e.id)).toEqual([41, 40]);
  });

  it('count down as the design writes it', () => {
    const min = 60_000;
    expect(timeLeft((2 * 24 * 60 + 4 * 60) * min)).toBe('2 days 4 h');
    expect(timeLeft(24 * 60 * min)).toBe('1 day');
    expect(timeLeft((5 * 60 + 20) * min)).toBe('5 h 20 min');
    expect(timeLeft(3 * 60 * min)).toBe('3 h');
    expect(timeLeft(12 * min - 5000)).toBe('12 min');
    expect(timeLeft(10_000)).toBe('1 min');

    const next = BRUNCHTIME.episodes[1];
    expect(countdown(next, Date.parse(FRIDAY))).toBe('Starts in 2 days 4 h');
    expect(countdown(next, Date.parse(ON_AIR))).toBe('On air now · until 14:00');
    expect(countdown({ ...next, start_time: null }, Date.parse(FRIDAY))).toBe('');
  });

  it("give the flow's next step for live and pre-recorded episodes", () => {
    const [prerecorded, live] = BRUNCHTIME.episodes;
    const friday = Date.parse(FRIDAY);
    expect(nextStep(live, friday)).toBe('Test your audio in the Studio before air time.');
    expect(nextStep(prerecorded, friday)).toBe(
      'Nothing to do. The set plays at air time. Wrap-up opens after 14:00.'
    );
    expect(nextStep({ ...prerecorded, has_file: false }, friday)).toBe(
      'Upload your set before air time. Without a file nothing plays.'
    );
    expect(nextStep(live, Date.parse(ON_AIR))).toBe('The Studio is open.');
  });

  it('write the rhythm and the hosts', () => {
    expect(rhythmText(BRUNCHTIME)).toBe('Every week · Sundays · 12:00–14:00');
    expect(rhythmText(NIGHT)).toBe('Every other Tuesday · 20:00–22:00');
    expect(rhythmText({ rhythm: null, rhythm_line: null })).toBe('No fixed rhythm');
    expect(hostsLine([{ username: 'lena' }])).toBe('Host: lena');
    expect(hostsLine([{ username: 'lena' }, { username: 'jonas' }])).toBe('Hosts: lena, jonas');
    expect(hostsLine(undefined)).toBe('');
  });
});

describe('Show page (owner)', () => {
  it('shows the Show card read-only, with Edit opening My Show in edit mode', async () => {
    const router = await mount('host', '/shows/brunchtime');
    const card = root.querySelector('.show-card');
    expect(card?.querySelector('h1')?.textContent).toBe('Brunchtime');
    const text = card?.textContent ?? '';
    for (const part of [
      'Sunday brunch sounds, every week from 12:00.',
      'Every week · Sundays · 12:00–14:00',
      '@brunchtime',
      'Host: lena',
    ]) {
      expect(text).toContain(part);
    }
    // Read-only: no form fields on this page.
    expect(root.querySelectorAll('input, textarea, select')).toHaveLength(0);
    expect(root.querySelector('.back-link')?.textContent).toBe('< My Show');

    button('Edit').click();
    await vi.waitFor(() =>
      expect(router.currentRoute.value.fullPath).toBe('/shows?edit=brunchtime')
    );
  });

  it('counts down to the next episode and opens it', async () => {
    const router = await mount('host', '/shows/brunchtime');
    const next = root.querySelector('.next-episode');
    expect(next?.querySelector('.next-day')?.textContent).toBe('So 25.10.');
    expect(next?.querySelector('.next-slot')?.textContent).toBe('12:00–14:00 · Live');
    expect(next?.querySelector('.next-count')?.textContent).toBe('Starts in 2 days 4 h');
    expect(next?.querySelector('.next-hint')?.textContent).toBe(
      'Test your audio in the Studio before air time.'
    );
    // The page's one yellow button.
    expect(root.querySelectorAll('button.variant-primary')).toHaveLength(1);
    button('Open episode').click();
    await vi.waitFor(() => expect(router.currentRoute.value.fullPath).toBe('/shows/42'));
  });

  it('lists upcoming and past episodes with their recording state', async () => {
    await mount('host', '/shows/brunchtime');
    expect(rows('upcoming')).toEqual([
      { text: 'So 25.10. · 12:00–14:00 · Live | Upcoming', href: '/shows/42' },
      { text: 'So 08.11. · 12:00–14:00 · Pre-recorded | Upcoming', href: '/shows/43' },
    ]);
    expect(rows('past')).toEqual([
      {
        text: 'So 18.10. · 12:00–14:00 · Live | Finished · wrap-up open',
        href: '/shows/41',
      },
      {
        text: 'So 11.10. · 12:00–14:00 · Pre-recorded | Finished · Public on SoundCloud',
        href: null,
      },
    ]);
    expect(chips('upcoming')).toEqual(['mine', 'mine']);
    expect(chips('past')).toEqual(['wrapup', '']);
    expect(root.querySelectorAll('.upcoming-episodes .row-icon.read-only')).toHaveLength(0);
  });

  it('lists the free slots of the rhythm; Book and Book Slot open Book Slot', async () => {
    const router = await mount('host', '/shows/brunchtime');
    const free = [...root.querySelectorAll('.free-slots li')].map((li) =>
      rowParts(li.querySelector('.board-row')).replace(/ \| Book$/, '')
    );
    expect(free).toEqual(['So 01.11. | · 12:00–14:00 Free', 'So 15.11. | · 12:00–14:00 Free']);

    (root.querySelector('.free-slots li button') as HTMLButtonElement).click();
    await vi.waitFor(() =>
      expect(router.currentRoute.value.fullPath).toBe('/shows/brunchtime/book?date=2026-11-01')
    );
    await router.push('/shows/brunchtime');
    await settle();
    button('Book Slot').click();
    await vi.waitFor(() =>
      expect(router.currentRoute.value.fullPath).toBe('/shows/brunchtime/book')
    );
  });

  it('shows at most three free slots', async () => {
    slots = ['2026-11-01', '2026-11-15', '2026-11-22', '2026-11-29'].map(slot);
    await mount('host', '/shows/brunchtime');
    expect(root.querySelectorAll('.free-slots li')).toHaveLength(3);
  });

  it('says so when the free slots cannot load', async () => {
    slotsFail = true;
    await mount('host', '/shows/brunchtime');
    expect(root.querySelector('.free-slots [role="alert"]')?.textContent).toBe(
      'Could not load the free slots.'
    );
    // The rest of the page still works.
    expect(rows('upcoming')).toHaveLength(2);
  });

  it('has no next episode and says so when nothing is booked', async () => {
    brunchtime.episodes = brunchtime.episodes.filter((e) => e.status !== 'upcoming');
    await mount('host', '/shows/brunchtime');
    expect(root.querySelector('.next-episode')).toBeNull();
    expect(root.querySelector('.episodes-card.upcoming')?.textContent).toContain(
      'No upcoming episodes.'
    );
    expect(rows('past')).toHaveLength(2);
  });

  it('offers Book Slot without slots for a Show with no fixed rhythm', async () => {
    brunchtime.rhythm = null;
    brunchtime.rhythm_line = null;
    await mount('host', '/shows/brunchtime');
    expect(root.textContent).toContain('No fixed rhythm. Book Slot picks any free date.');
    expect(buttons()).toContain('Book Slot');
    expect(requests).not.toContain('/api/series/brunchtime/slots');
  });

  it('sends a host away from a Show that does not exist', async () => {
    const router = await mount('host', '/shows/no-such-show');
    await vi.waitFor(() => expect(router.currentRoute.value.fullPath).toBe('/shows'));
  });
});

describe('Show page (another host)', () => {
  it('is read-only: no Edit, no free slots, rows open the episode read-only', async () => {
    const router = await mount('host', '/shows/night-shift');
    expect(root.querySelector('h1')?.textContent).toBe('Night Shift');
    expect(root.querySelector('.show-card')?.textContent).toContain('Host: jonas');
    expect(root.querySelector('.back-link')?.textContent).toBe('< Dashboard');
    const offered = buttons();
    for (const absent of ['Edit', 'Book Slot', 'Book', 'Open episode']) {
      expect(offered).not.toContain(absent);
    }
    expect(root.querySelector('.free-slots')).toBeNull();
    expect(requests).not.toContain('/api/series/night-shift/slots');

    // Next episode: date and countdown, no next step.
    const next = root.querySelector('.next-episode');
    expect(next?.querySelector('.next-day')?.textContent).toBe('Di 27.10.');
    expect(next?.querySelector('.next-count')?.textContent).toBe('Starts in 4 days 12 h');
    expect(next?.querySelector('.next-hint')).toBeNull();
    expect(root.querySelectorAll('button.variant-primary')).toHaveLength(0);

    expect(rows('upcoming')).toEqual([
      { text: 'Di 27.10. · 20:00–22:00 · Live | Upcoming', href: '/shows/44' },
    ]);
    expect(rows('past')).toEqual([
      { text: 'Di 13.10. · 20:00–22:00 · Live | Finished · Private on SoundCloud', href: null },
    ]);
    expect(chips('upcoming')).toEqual(['']);
    expect(root.querySelectorAll('.upcoming-episodes .row-icon.read-only')).toHaveLength(1);

    button('View episode').click();
    await vi.waitFor(() => expect(router.currentRoute.value.fullPath).toBe('/shows/44'));
  });

  it('lets an admin book but not edit (Edit lives on the host page My Show)', async () => {
    await mount('admin', '/shows/brunchtime');
    const offered = buttons();
    expect(offered).toContain('Book Slot');
    expect(offered).not.toContain('Edit');
    expect(root.querySelector('.back-link')?.textContent).toBe('< Shows');
  });
});

describe('On air now', () => {
  it('is hidden while nothing is on air', async () => {
    await mount('host', '/shows/brunchtime');
    expect(requests).toContain('/api/stream/status');
    expect(root.querySelector('.on-air-strip')).toBeNull();
  });

  it("opens On Air when the episode on air is the viewer's own", async () => {
    vi.setSystemTime(new Date(ON_AIR));
    streamActive = true;
    const router = await mount('host', '/shows/brunchtime');
    const strip = root.querySelector('a.on-air-strip');
    expect(rowParts(strip)).toBe('On air now | Brunchtime | lena | until 14:00');
    (strip as HTMLAnchorElement).click();
    await vi.waitFor(() =>
      expect(router.currentRoute.value.fullPath).toBe('/stream/on-air?show=42')
    );
  });

  it("shows another host's episode on air without a link", async () => {
    vi.setSystemTime(new Date(ON_AIR));
    streamActive = true;
    overview = [airing(45, 'Dub Kitchen', 'mira')];
    await mount('host', '/shows/brunchtime');
    const strip = root.querySelector('.on-air-strip');
    expect(strip?.tagName).toBe('SECTION');
    expect(rowParts(strip)).toBe('On air now | Dub Kitchen | mira | until 14:00');
  });

  it("does not link the owner's episode for another host viewing the Show", async () => {
    vi.setSystemTime(new Date(ON_AIR));
    streamActive = true;
    brunchtime.can_edit = false;
    await mount('host', '/shows/brunchtime', 'jonas');
    expect(root.querySelector('.on-air-strip')?.tagName).toBe('SECTION');
  });

  it("links the viewer's own episode on air from another host's Show", async () => {
    vi.setSystemTime(new Date(ON_AIR));
    streamActive = true;
    brunchtime.can_edit = false;
    overview = [airing(44, 'Night Shift', 'jonas')];
    const router = await mount('host', '/shows/brunchtime', 'jonas');
    (root.querySelector('a.on-air-strip') as HTMLAnchorElement).click();
    await vi.waitFor(() =>
      expect(router.currentRoute.value.fullPath).toBe('/stream/on-air?show=44')
    );
  });
});

describe('flow', () => {
  it('New Show matches the flow', async () => {
    await mount('host', '/shows/new');
    await vi.waitFor(() => expect(root.textContent).toContain('New Show'));
    await expectFlow('new-show', root);
  });

  it('Show matches the flow (owner, nothing on air)', async () => {
    await mount('host', '/shows/brunchtime');
    await vi.waitFor(() => expect(root.querySelectorAll('.free-slots li')).toHaveLength(2));
    await expectFlow('show', root);
  });

  // Schedule merged into the Show page (issue 1051): its check runs here.
  it('Schedule matches the flow on the Show page (owner, own episode on air)', async () => {
    vi.setSystemTime(new Date(ON_AIR));
    streamActive = true;
    await mount('host', '/shows/brunchtime');
    await vi.waitFor(() => expect(root.querySelector('a.on-air-strip')).not.toBeNull());
    await vi.waitFor(() => expect(root.querySelectorAll('.free-slots li')).toHaveLength(2));
    await expectFlow('schedule', root);
  });
});
