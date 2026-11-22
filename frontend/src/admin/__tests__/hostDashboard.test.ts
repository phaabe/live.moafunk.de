import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApp, h, nextTick, type App, type Component } from 'vue';
import { createPinia } from 'pinia';
import { createMemoryHistory, createRouter, RouterView, type Router } from 'vue-router';
import { useAuthStore } from '../stores/auth';
import NavBar from '@shared/components/NavBar.vue';
import HostDashboardPage from '../pages/HostDashboardPage.vue';
import type { ChalkNote, PastEpisode, SeriesOverviewItem, UpcomingEpisode } from '../api';
import { byEpisodes, progressOf, startsIn } from '../components/dashboard/dashboard';
import { expectFlow } from '../flow/expectFlow';

// Host Dashboard, redesign #1049. The scene is the design's: Fr 23.10.2026,
// 18:42:10 in Berlin, Dub Kitchen (mira) is on air. Rows use the flow's
// example data (Brunchtime, lena, So 25.10.).
const NOW = new Date('2026-10-23T16:42:10Z');

type Row = [string, string, string, string, string, string, string, number];

function episode([date, start, end, show, host, mode, title, id]: Row): UpcomingEpisode {
  return {
    id,
    title,
    date,
    start_time: start,
    end_time: end,
    show_name: show,
    series_slug: show.toLowerCase().replace(/\s+/g, '-'),
    host_username: host,
    mode: mode === 'Live' ? 'live' : 'prerecorded',
    mode_label: mode,
  };
}

/** The flow's Dashboard rows: lena's own episodes and two of other hosts. */
const UPCOMING: UpcomingEpisode[] = (
  [
    ['2026-10-25', '12:00', '14:00', 'Brunchtime', 'lena', 'Live', 'Brunchtime #31', 42],
    ['2026-10-27', '20:00', '22:00', 'Night Shift', 'jonas', 'Live', 'Night Shift #12', 44],
    ['2026-10-30', '18:00', '20:00', 'Dub Kitchen', 'mira', 'Pre-recorded', 'Dub Kitchen #6', 45],
    ['2026-11-08', '12:00', '14:00', 'Brunchtime', 'lena', 'Pre-recorded', 'Brunchtime #32', 43],
  ] as Row[]
).map(episode);

/** On air in the scene: Fr 23.10. 18:00–20:00. */
const DUB_ON_AIR = episode([
  '2026-10-23',
  '18:00',
  '20:00',
  'Dub Kitchen',
  'mira',
  'Pre-recorded',
  'Dub Kitchen #5',
  41,
]);

const finished = (row: Row, wrapupOpen = false): PastEpisode => ({
  ...episode(row),
  status: wrapupOpen ? 'wrapup_open' : 'finished',
  status_label: wrapupOpen ? 'Finished · wrap-up open' : 'Finished',
});

const PAST: PastEpisode[] = [
  finished(['2026-10-20', '20:00', '22:00', 'Night Shift', 'jonas', 'Live', 'Night #11', 39]),
  finished(['2026-10-18', '12:00', '14:00', 'Brunchtime', 'lena', 'Live', 'Brunch #30', 38], true),
  finished(['2026-10-16', '18:00', '20:00', 'Dub Kitchen', 'mira', 'Pre-recorded', 'Dub #4', 37]),
  finished([
    '2026-10-11',
    '12:00',
    '14:00',
    'Brunchtime',
    'lena',
    'Pre-recorded',
    'Brunch #29',
    36,
  ]),
];

/** Not in episode order, so the grid has to sort. */
const SHOWS: SeriesOverviewItem[] = [
  ['dub-kitchen', 'Dub Kitchen', 'mira', 5, null],
  ['brunchtime', 'Brunchtime', 'lena', 31, '/covers/brunchtime.jpg'],
  ['unheard', 'UNHEARD', 'moafunk', 8, null],
  ['night-shift', 'Night Shift', 'jonas', 12, '/covers/night.jpg'],
].map(([slug, name, host, n, cover], i) => ({
  id: i + 1,
  slug: slug as string,
  name: name as string,
  host_username: host as string,
  finished_episodes: n as number,
  cover_url: cover as string | null,
  cover_srcset: cover ? `${cover}?w=320 320w, ${cover}?w=640 640w` : null,
}));

let app: App | undefined;
let root: HTMLDivElement;
let streamActive: boolean;
/** What /api/stream/metrics answers: listeners now, or null when Icecast is offline. */
let listening: number | null;
let upcoming: UpcomingEpisode[];
let past: PastEpisode[];
/** The chalkboard's notes, as /api/chalkboard answers them. */
let notes: ChalkNote[];
/** Texts POSTed to /api/chalkboard. */
let posted: string[];
/** API paths that answer 500 in this test. */
let failing: string[];

beforeEach(() => {
  streamActive = false;
  listening = 37;
  upcoming = [...UPCOMING];
  past = [...PAST];
  failing = [];
  notes = [];
  posted = [];
  root = document.createElement('div');
  document.body.appendChild(root);
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(NOW);
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      if (failing.includes(url)) return Response.json({ error: 'boom' }, { status: 500 });
      if (url === '/api/chalkboard' && init?.method === 'POST') {
        const { text } = JSON.parse(String(init.body)) as { text: string };
        posted.push(text);
        const note = { id: 100 + posted.length, author: 'lena', text, ts: NOW.getTime() / 1000 };
        notes = [...notes, note];
        return Response.json(note, { status: 201 });
      }
      switch (url) {
        case '/api/chalkboard':
          return Response.json({ notes });
        case '/api/stream/status':
          return Response.json({ active: streamActive, recording: false });
        case '/api/stream/metrics':
          return Response.json({
            online: listening !== null,
            total_listeners: listening ?? 0,
            mounts: [],
          });
        case '/api/upcoming-episodes':
          return Response.json({ episodes: upcoming });
        case '/api/past-episodes':
          return Response.json({ episodes: past });
        case '/api/series-overview':
          return Response.json({ series: SHOWS });
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

async function mount(component: Component = HostDashboardPage): Promise<Router> {
  const stub = (text: string) => ({ render: () => h('div', text) });
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component },
      { path: '/dashboard', component: stub('Dashboard screen') },
      { path: '/schedule', component: stub('Schedule screen') },
      { path: '/shows', component: stub('Shows') },
      { path: '/shows/:slug', component: stub('Show page') },
      { path: '/shows/:id(\\d+)', component: stub('Episode screen') },
      { path: '/stream/on-air', component: stub('On Air screen') },
    ],
  });
  const pinia = createPinia();
  app = createApp({ render: () => h(RouterView) });
  app.use(pinia).use(router);
  useAuthStore(pinia).user = { id: 1, username: 'lena', role: 'host' };
  await router.push('/');
  await router.isReady();
  app.mount(root);
  for (let i = 0; i < 5; i++) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    await nextTick();
  }
  return router;
}

const text = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim();

/** A row's parts as the flow check reads them: "So 25.10. · Brunchtime | Upcoming · Live". */
const rowParts = (row: Element | null) =>
  [...(row?.children ?? [])]
    .map((c) => text(c))
    .filter(Boolean)
    .join(' | ');

const rows = () =>
  [...root.querySelectorAll('.episodes li a')].map((a) => ({
    text: rowParts(a),
    href: a.getAttribute('href'),
  }));

const card = (selector: string) => root.querySelector(selector) as HTMLElement;

function pager(label: string): HTMLButtonElement {
  return root.querySelector(`.pager-button[aria-label="${label}"]`) as HTMLButtonElement;
}

function tab(label: string): HTMLButtonElement {
  return [...root.querySelectorAll<HTMLButtonElement>('[role="tab"]')].find(
    (b) => text(b) === label
  )!;
}

describe('Dashboard', () => {
  it('shows the cards of the design', async () => {
    await mount();
    expect(text(root.querySelector('h1'))).toBe('Dashboard');
    expect([...root.querySelectorAll('h2')].map(text)).toEqual([
      'On air',
      'My next show',
      'Episodes',
      'Chalkboard',
      'Shows',
    ]);
    expect(root.textContent).not.toContain('Last show');
    expect(root.textContent).not.toContain('Telegram');
  });
});

describe('Dashboard episodes', () => {
  it("opens on Past, with all hosts' past episodes, three at a time", async () => {
    await mount();
    await vi.waitFor(() => expect(rows()).toHaveLength(3));
    expect(tab('Past').getAttribute('aria-selected')).toBe('true');
    expect(tab('Upcoming').getAttribute('aria-selected')).toBe('false');
    expect(rows()).toEqual([
      {
        text: 'Di 20.10. · 20:00–22:00 · Night Shift · jonas | Finished · Live',
        href: '/shows/39',
      },
      {
        text: 'So 18.10. · 12:00–14:00 · Brunchtime · lena | Finished · wrap-up open · Live',
        href: '/shows/38',
      },
      {
        text: 'Fr 16.10. · 18:00–20:00 · Dub Kitchen · mira | Finished · Pre-recorded',
        href: '/shows/37',
      },
    ]);
    const tags = [...root.querySelectorAll('.episodes .row-tags .tag:first-child')].map(
      (t) => t.className
    );
    expect(tags).toEqual(['tag tag-finished', 'tag tag-wrapup_open', 'tag tag-finished']);
    expect(text(card('.pager-range'))).toBe('1–3 of 4');
    pager('Next episodes').click();
    await nextTick();
    expect(rows()).toEqual([
      {
        text: 'So 11.10. · 12:00–14:00 · Brunchtime · lena | Finished · Pre-recorded',
        href: '/shows/36',
      },
    ]);
    expect(text(card('.pager-range'))).toBe('4–4 of 4');
  });

  it("switches to Upcoming: all hosts' upcoming episodes, as in the flow", async () => {
    await mount();
    await vi.waitFor(() => expect(rows()).toHaveLength(3));
    tab('Upcoming').click();
    await nextTick();
    expect(tab('Upcoming').getAttribute('aria-selected')).toBe('true');
    const all = [...rows()];
    pager('Next episodes').click();
    await nextTick();
    all.push(...rows());
    expect(all).toEqual([
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
    tab('Past').click();
    await nextTick();
    expect(rows()[0].href).toBe('/shows/39');
  });

  it('moves between the tabs with the arrow keys', async () => {
    await mount();
    tab('Past').dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
    await nextTick();
    expect(tab('Upcoming').getAttribute('aria-selected')).toBe('true');
    expect(document.activeElement).toBe(tab('Upcoming'));
  });

  it('marks the host’s own episodes and shows other hosts’ ones as read-only', async () => {
    await mount();
    await vi.waitFor(() => expect(rows()).toHaveLength(3));
    const marks = () =>
      [...root.querySelectorAll('.episodes li')].map((li) => ({
        mine: li.querySelector('.date-chip')!.classList.contains('mine'),
        readOnly: li.querySelector('.row-icon')!.classList.contains('read-only'),
      }));
    const own = { mine: true, readOnly: false };
    const other = { mine: false, readOnly: true };
    expect(marks()).toEqual([other, own, other]);
    tab('Upcoming').click();
    await nextTick();
    expect(marks()).toEqual([own, other, other]);
  });

  it('pages through the episodes with the arrows; a tab starts on its first page', async () => {
    past = Array.from({ length: 7 }, (_, i) => ({ ...PAST[0], id: 100 + i }));
    await mount();
    await vi.waitFor(() => expect(rows()).toHaveLength(3));
    const ids = () => rows().map((r) => r.href);
    expect(pager('Previous episodes').disabled).toBe(true);
    pager('Next episodes').click();
    await nextTick();
    expect(ids()).toEqual(['/shows/103', '/shows/104', '/shows/105']);
    pager('Next episodes').click();
    await nextTick();
    expect(ids()).toEqual(['/shows/106']);
    expect(text(card('.pager-range'))).toBe('7–7 of 7');
    expect(pager('Next episodes').disabled).toBe(true);
    pager('Previous episodes').click();
    await nextTick();
    expect(ids()).toEqual(['/shows/103', '/shows/104', '/shows/105']);
    tab('Upcoming').click();
    await nextTick();
    tab('Past').click();
    await nextTick();
    expect(ids()).toEqual(['/shows/100', '/shows/101', '/shows/102']);
  });

  it('says so when a tab has no episodes', async () => {
    past = [];
    upcoming = [];
    await mount();
    await vi.waitFor(() => expect(root.textContent).toContain('No past episodes.'));
    tab('Upcoming').click();
    await nextTick();
    expect(root.textContent).toContain('No upcoming episodes.');
    expect(rows()).toHaveLength(0);
  });

  it('says so when a list cannot load', async () => {
    failing = ['/api/past-episodes'];
    await mount();
    await vi.waitFor(() =>
      expect(text(card('.episodes-card [role="alert"]'))).toBe('Could not load the past episodes.')
    );
    expect(root.textContent).not.toContain('No past episodes.');
    tab('Upcoming').click();
    await nextTick();
    expect(card('.episodes-card [role="alert"]')).toBeNull();
    expect(rows()).toHaveLength(3);
  });
});

describe('Dashboard on air', () => {
  it('shows the show on air with its clock, progress and Listen live', async () => {
    streamActive = true;
    upcoming = [DUB_ON_AIR, ...UPCOMING];
    await mount();
    const air = () => card('.on-air-card');
    await vi.waitFor(() => expect(air().textContent).toContain('Dub Kitchen'));
    expect(air().classList.contains('live')).toBe(true);
    expect(text(air().querySelector('.head-line'))).toBe('mira · Pre-recorded');
    expect(text(air().querySelector('.head-time'))).toBe('18:00–20:00');
    expect(text(air().querySelector('.air-clock'))).toBe('00:42:10');
    await vi.waitFor(() =>
      expect(text(air().querySelector('.air-listeners'))).toBe('37 listening')
    );
    expect(air().querySelector('[role="progressbar"]')?.getAttribute('aria-valuenow')).toBe('35');
    const listen = air().querySelector('a');
    expect(text(listen)).toBe('Listen live');
    expect(listen?.getAttribute('href')).toBe('https://live.moafunk.de');
    expect(air().textContent).not.toContain('Back to On Air');
  });

  it('leaves the listener count out while Icecast cannot be read', async () => {
    streamActive = true;
    listening = null;
    upcoming = [DUB_ON_AIR, ...UPCOMING];
    await mount();
    const air = () => card('.on-air-card');
    await vi.waitFor(() => expect(air().textContent).toContain('Dub Kitchen'));
    await vi.waitFor(() =>
      expect(vi.mocked(fetch).mock.calls.some(([url]) => url === '/api/stream/metrics')).toBe(true)
    );
    expect(air().querySelector('.air-listeners')).toBeNull();
    expect(air().textContent).not.toContain('listening');
  });

  it('does not read the listener count while nothing is on air', async () => {
    await mount();
    await vi.waitFor(() =>
      expect(card('.on-air-card').textContent).toContain('Nothing on air right now.')
    );
    expect(vi.mocked(fetch).mock.calls.some(([url]) => url === '/api/stream/metrics')).toBe(false);
  });

  it('offers Back to On Air when the show on air is the host’s own', async () => {
    streamActive = true;
    const own = { ...DUB_ON_AIR, show_name: 'Brunchtime', host_username: 'lena', id: 40 };
    upcoming = [own, ...UPCOMING];
    const router = await mount();
    const air = () => card('.on-air-card');
    await vi.waitFor(() => expect(air().textContent).toContain('Back to On Air'));
    expect(air().textContent).not.toContain('Listen live');
    (air().querySelector('a') as HTMLAnchorElement).click();
    await vi.waitFor(() =>
      expect(router.currentRoute.value.fullPath).toBe('/stream/on-air?show=40')
    );
  });

  it('says so when nothing is on air, and names the next show', async () => {
    upcoming = [DUB_ON_AIR, ...UPCOMING];
    await mount();
    const air = () => card('.on-air-card');
    await vi.waitFor(() => expect(air().textContent).toContain('Nothing on air right now.'));
    expect(air().classList.contains('live')).toBe(false);
    // The stream is off, so the episode at this time is not on air: it is next.
    expect(text(air().querySelector('.air-next'))).toBe(
      'Next: Dub Kitchen · Fr 23.10. · 18:00–20:00 · mira'
    );
    expect(air().querySelector('a')).toBeNull();
  });

  it('offers Listen live while the stream is live outside any episode', async () => {
    streamActive = true;
    await mount();
    const air = () => card('.on-air-card');
    await vi.waitFor(() => expect(air().textContent).toContain('Live now'));
    expect(text(air().querySelector('a'))).toBe('Listen live');
  });
});

describe('Dashboard my next show', () => {
  it('shows the host’s next episode with a countdown and Open episode', async () => {
    const router = await mount();
    const next = () => card('.next-show-card');
    await vi.waitFor(() => expect(next().textContent).toContain('Brunchtime #31'));
    expect(text(next().querySelector('.head-line'))).toBe('Live · Host: lena');
    expect(text(next().querySelector('.head-time'))).toBe('So 25.10. · 12:00–14:00');
    // Fr 23.10. 18:42 to So 25.10. 12:00 (the clocks go back on the 25th).
    expect(text(next().querySelector('.countdown'))).toBe('Starts in 1 day 18 h');
    expect(next().querySelector('.show-cover img')?.getAttribute('src')).toBe(
      '/covers/brunchtime.jpg'
    );
    const open = next().querySelector('a.card-action.primary') as HTMLAnchorElement;
    expect(text(open)).toBe('Open episode');
    open.click();
    await vi.waitFor(() => expect(router.currentRoute.value.fullPath).toBe('/shows/42'));
  });

  it('skips the host’s own episode while it is on air', async () => {
    streamActive = true;
    const own = { ...DUB_ON_AIR, show_name: 'Brunchtime', host_username: 'lena', id: 40 };
    upcoming = [own, ...UPCOMING];
    await mount();
    await vi.waitFor(() => expect(card('.next-show-card').textContent).toContain('Brunchtime #31'));
  });

  it('says so when the host has no upcoming episode', async () => {
    upcoming = UPCOMING.filter((e) => e.host_username !== 'lena');
    await mount();
    await vi.waitFor(() =>
      expect(card('.next-show-card').textContent).toContain('You have no upcoming episode.')
    );
    expect(card('.next-show-card').querySelector('a')).toBeNull();
  });
});

describe('Dashboard shows', () => {
  it('lists every Show, most finished episodes first, each opening its page', async () => {
    const router = await mount();
    await vi.waitFor(() => expect(root.querySelectorAll('.show-tiles li')).toHaveLength(4));
    const tiles = [...root.querySelectorAll('.show-tiles a')].map((a) => ({
      text: rowParts(a),
      href: a.getAttribute('href'),
      cover: a.querySelector('img')?.getAttribute('src') ?? null,
    }));
    expect(tiles).toEqual([
      {
        text: 'Brunchtime · Yours | lena · 31 episodes',
        href: '/shows/brunchtime',
        cover: '/covers/brunchtime.jpg',
      },
      {
        text: 'Night Shift | jonas · 12 episodes',
        href: '/shows/night-shift',
        cover: '/covers/night.jpg',
      },
      { text: 'UNHEARD | moafunk · 8 episodes', href: '/shows/unheard', cover: null },
      { text: 'Dub Kitchen | mira · 5 episodes', href: '/shows/dub-kitchen', cover: null },
    ]);
    (root.querySelector('.show-tiles a[href="/shows/night-shift"]') as HTMLAnchorElement).click();
    await vi.waitFor(() => expect(router.currentRoute.value.fullPath).toBe('/shows/night-shift'));
  });

  it('loads small copies of the covers, not the full uploads', async () => {
    await mount();
    await vi.waitFor(() => expect(root.querySelectorAll('.show-tiles img')).toHaveLength(2));
    const tile = root.querySelector('.show-tiles a[href="/shows/brunchtime"] img')!;
    expect(tile.getAttribute('srcset')).toBe(
      '/covers/brunchtime.jpg?w=320 320w, /covers/brunchtime.jpg?w=640 640w'
    );
    expect(tile.getAttribute('sizes')).toBe('(max-width: 640px) 50vw, 300px');
    const head = card('.next-show-card').querySelector('.show-cover img')!;
    expect(head.getAttribute('srcset')).toBe(tile.getAttribute('srcset'));
    expect(head.getAttribute('sizes')).toBe('84px');
  });

  it('says so when the Shows cannot load', async () => {
    failing = ['/api/series-overview'];
    await mount();
    await vi.waitFor(() =>
      expect(text(card('.shows-card [role="alert"]'))).toBe('Could not load the shows.')
    );
  });
});

describe('Dashboard chalkboard', () => {
  const at = (hhmm: string) => new Date(`2026-10-23T${hhmm}:00+02:00`).getTime() / 1000;
  const NOTES: ChalkNote[] = [
    { id: 1, author: 'jonas', text: 'Can someone take Di 03.11.?', ts: at('10:03') },
    { id: 2, author: 'lena', text: 'I can do it.', ts: at('10:20') },
    { id: 3, author: 'mira', text: 'Mic 2 is broken, use mic 1.', ts: at('18:01') },
  ];
  const board = () =>
    [...root.querySelectorAll('.chalk-note')].map((m) => ({
      author: text(m.querySelector('.chalk-author')),
      time: text(m.querySelector('.chalk-time')),
      text: text(m.querySelector('.chalk-text')),
      mine: m.classList.contains('mine'),
    }));

  it('shows the notes of all hosts and marks the host’s own', async () => {
    notes = [...NOTES];
    await mount();
    await vi.waitFor(() => expect(board()).toHaveLength(3));
    expect(board()).toEqual([
      { author: 'jonas', time: '10:03', text: 'Can someone take Di 03.11.?', mine: false },
      { author: 'lena · you', time: '10:20', text: 'I can do it.', mine: true },
      { author: 'mira', time: '18:01', text: 'Mic 2 is broken, use mic 1.', mine: false },
    ]);
  });

  it('writes a note on the board', async () => {
    await mount();
    await vi.waitFor(() => expect(root.textContent).toContain('The board is empty.'));
    const input = root.querySelector('#dashboard-chalk-input') as HTMLInputElement;
    const post = root.querySelector('.chalk-post') as HTMLButtonElement;
    expect(post.disabled).toBe(true);
    input.value = '  See you on Sunday ';
    input.dispatchEvent(new Event('input'));
    await nextTick();
    expect(post.disabled).toBe(false);
    root.querySelector('.chalk-compose')!.dispatchEvent(new Event('submit'));
    await vi.waitFor(() => expect(board()).toHaveLength(1));
    expect(posted).toEqual(['See you on Sunday']);
    expect(board()[0]).toMatchObject({ author: 'lena · you', text: 'See you on Sunday' });
    expect(input.value).toBe('');
  });

  it('keeps the draft when the note is not saved', async () => {
    failing = ['/api/chalkboard'];
    await mount();
    await vi.waitFor(() =>
      expect(text(card('.chalkboard-card [role="alert"]'))).toBe('Could not load the chalkboard.')
    );
    const input = root.querySelector('#dashboard-chalk-input') as HTMLInputElement;
    input.value = 'Hello';
    input.dispatchEvent(new Event('input'));
    await nextTick();
    root.querySelector('.chalk-compose')!.dispatchEvent(new Event('submit'));
    await vi.waitFor(() => expect(root.textContent).toContain('The note was not saved.'));
    expect(input.value).toBe('Hello');
  });
});

describe('Dashboard helpers', () => {
  it('counts down to the start', () => {
    const min = 60_000;
    expect(startsIn((24 * 60 + 17 * 60 + 5) * min)).toBe('Starts in 1 day 17 h');
    expect(startsIn(3 * 24 * 60 * min)).toBe('Starts in 3 days 0 h');
    expect(startsIn((3 * 60 + 5) * min)).toBe('Starts in 3 h 5 min');
    expect(startsIn(12 * min + 30_000)).toBe('Starts in 12 min');
    expect(startsIn(30_000)).toBe('Starts now');
  });

  it('tells how far a show on air is, also over midnight', () => {
    const late = { date: '2026-10-23', start_time: '23:00', end_time: '01:00' };
    const at = new Date('2026-10-23T22:30:00Z').getTime(); // 00:30 in Berlin
    expect(progressOf(late, at)).toEqual({ elapsed: '01:30:00', percent: 75 });
    expect(progressOf({ ...late, end_time: null }, at)).toEqual({
      elapsed: '01:30:00',
      percent: null,
    });
  });

  it('orders Shows by finished episodes, then by name', () => {
    const show = (name: string, n: number, id = 1) => ({
      ...SHOWS[0],
      id,
      name,
      finished_episodes: n,
    });
    expect(
      byEpisodes([show('b', 1), show('A', 1), show('c', 3), show('d', 0)]).map((s) => s.name)
    ).toEqual(['c', 'A', 'b', 'd']);
  });
});

describe('flow', () => {
  it('Dashboard matches the flow', async () => {
    // The design's scene: another host's show is on air.
    streamActive = true;
    upcoming = [DUB_ON_AIR, ...UPCOMING];
    await mount({ render: () => [h(NavBar), h(HostDashboardPage)] });
    await vi.waitFor(() => expect(rows()).toHaveLength(3));
    await vi.waitFor(() => expect(root.querySelectorAll('.show-tiles li')).toHaveLength(4));
    await vi.waitFor(() => expect(root.textContent).toContain('Listen live'));
    await expectFlow('dashboard', root);
  });
});
