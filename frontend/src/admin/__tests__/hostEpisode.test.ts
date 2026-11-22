import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApp, h, nextTick, type App } from 'vue';
import { createPinia } from 'pinia';
import { createMemoryHistory, createRouter, RouterView, type Router } from 'vue-router';
import { useAuthStore } from '../stores/auth';
import { hostPage } from '../hostPage';
import { currentReplaceTarget } from '../replaceFile';
import type { ShowDetail, User } from '../api';
import { expectFlow } from '../flow/expectFlow';

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

// Today's show page has its own tests.
vi.mock('../pages/ShowDetailPage.vue', () => ({
  __esModule: true,
  default: { render: () => h('div', "Today's show page") },
}));
// Like the real player: no file name, and 0:00 until its first Play.
vi.mock('../components/AudioPlayer.vue', () => ({
  __esModule: true,
  default: {
    props: ['src'],
    render(this: { src: string }) {
      return h('div', { class: 'audio-player', 'data-src': this.src }, 'Waveform 0:00 / 0:00');
    },
  },
}));

// The Studio and the live panel have their own tests (studio*.test.ts). Here
// they are stand-ins that show where the page puts them; `live.showId` is the
// episode this browser tab streams. `live.ready` is the Studio's Go Live state
// (soundcheck passed, input on, air time); its goLive streams episode 42.
const live = vi.hoisted(() => ({
  showId: null as number | null,
  ready: null as import('vue').Ref<boolean> | null,
}));
vi.mock('../composables', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../composables')>()),
  liveStreamShowId: () => live.showId,
}));
vi.mock('../pages/flow/FlowLive.vue', async () => {
  const { defineComponent, h, ref } = await import('vue');
  live.ready = ref(false);
  return {
    __esModule: true,
    default: defineComponent({
      props: { embedded: Boolean },
      emits: ['live'],
      setup(props, { emit, expose }) {
        expose({
          checks: [
            { id: 'music', name: 'Music', state: 'ok', detail: 'Good · peak −9 dB' },
            { id: 'mic', name: 'Mic', state: 'running', detail: 'Now talk into the mic.' },
            { id: 'server', name: 'Server', state: 'pending', detail: 'Not checked yet' },
            { id: 'return', name: 'Return feed', state: 'pending', detail: 'Not checked yet' },
          ],
          canGoLive: live.ready,
          goingLive: false,
          goLiveError: null,
          goLive: async () => {
            live.showId = 42;
            emit('live');
          },
        });
        return () =>
          h('div', { class: 'studio-stub' }, props.embedded ? 'Studio · embedded' : 'Studio');
      },
    }),
  };
});
vi.mock('../pages/flow/StudioOnAir.vue', async () => {
  const { defineComponent, h } = await import('vue');
  const { useLiveSession } = await import('../liveSession');
  return {
    __esModule: true,
    default: defineComponent({
      props: { embedded: Boolean, shown: { type: Boolean, default: true } },
      setup(props) {
        // End Episode ends the live session; the page watches that (episode 42).
        const end = () => (useLiveSession().ended.value = { showId: 42 });
        return () =>
          h('div', { class: 'onair-stub', 'data-shown': String(props.shown) }, [
            props.embedded ? 'Live panel · embedded' : 'Live panel',
            h('button', { type: 'button', onClick: end }, 'End Episode'),
          ]);
      },
    }),
  };
});

// jsdom cannot decode audio; the upload goes on without peaks.
vi.mock('../../pages/waveformExtractor', () => ({
  extractWaveformPeaksJson: async () => undefined,
}));

import HostEpisodePage from '../pages/HostEpisodePage.vue';
import HostAnnouncePage from '../pages/HostAnnouncePage.vue';
import type { Announcement, AnnouncementMeta, AnnouncementSlot, AnnouncementTime } from '../api';

const FUTURE = '2099-01-04';
const PAST = '2020-01-05';

function episode(overrides: Partial<ShowDetail> = {}): ShowDetail {
  return {
    id: 42,
    title: 'Brunchtime #31',
    date: FUTURE,
    start_time: '12:00',
    end_time: '14:00',
    status: 'scheduled',
    show_type: 'external',
    created_at: '2026-10-01T10:00:00Z',
    artists: [],
    available_artists: [],
    artists_left: 4,
    host_user_id: 1,
    host_username: 'lena',
    created_by: 1,
    host_has_logged_in: true,
    stream_mode: 'live',
    series_slug: 'brunchtime',
    series_name: 'Brunchtime',
    episode_status: 'upcoming',
    on_air: false,
    ...overrides,
  };
}

const BRUNCHTIME_SERIES = {
  id: 3,
  slug: 'brunchtime',
  name: 'Brunchtime',
  description: null,
  cover_url: null,
  rhythm: { repeat_rule: 'weekly', weekday: 7, start_time: '12:00', end_time: '14:00' },
  rhythm_line: 'Sundays · 12:00–14:00',
  instagram_handle: 'brunchtime',
};

function announcement(slot: AnnouncementSlot, overrides: Partial<Announcement> = {}): Announcement {
  return {
    id: { pre: 1, onair: 2, post: 3 }[slot],
    show_id: 42,
    slot,
    channel: 'instagram',
    state: slot === 'onair' ? 'armed' : 'scheduled',
    scheduled_at: null,
    requested_at: null,
    headline: 'Sunday brunch',
    caption: 'Brunchtime is back this Sunday.',
    image_source: 'cover',
    image_key: null,
    layout: 'band',
    collaborator: slot !== 'onair',
    extra_collaborators: [],
    user_tags: [],
    sent_at: null,
    permalink: null,
    error: null,
    send_now: false,
    updated_at: '2026-10-20T10:00:00Z',
    ...overrides,
  };
}

/** Fr 23.10. · 18:00 Berlin (summer time). */
const FR_2310_1800 = '2026-10-23T16:00:00Z';

let app: App | undefined;
let root: HTMLDivElement;
let current: ShowDetail;
let announcements: Announcement[];
let times: AnnouncementTime[];
let saveError: Response | undefined;
let requests: { url: string; method: string; body: unknown }[];
let uploaded: FormData | undefined;
let soundcloud: Response;
let meta: AnnouncementMeta;
const ON = { soundcloud: true, instagram: true };
let photoUpload: Response | undefined;
/** Holds GET /api/series/brunchtime until it resolves. */
let seriesGate: Promise<void> | undefined;

beforeEach(() => {
  current = episode();
  announcements = [];
  times = [];
  saveError = undefined;
  requests = [];
  uploaded = undefined;
  meta = { paused: false, needs_approval: false, services: ON };
  photoUpload = undefined;
  seriesGate = undefined;
  soundcloud = Response.json({
    success: true,
    track_id: '555',
    track_url: 'https://soundcloud.com/radio-moafunk/brunchtime-31',
  });
  root = document.createElement('div');
  document.body.appendChild(root);
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, options?: RequestInit) => {
      const method = options?.method ?? 'GET';
      const body = typeof options?.body === 'string' ? JSON.parse(options.body) : undefined;
      requests.push({ url, method, body });
      if (url === '/api/shows/42' && method === 'GET') return Response.json(current);
      if (url === '/api/shows/42' && method === 'PUT') {
        current = { ...current, ...(body as Partial<ShowDetail>) };
        return Response.json(current);
      }
      if (url === '/api/shows/42/booking')
        return Response.json({ success: true, series_slug: 'brunchtime' });
      if (url === '/api/shows/42/publish') {
        current = { ...current, episode_status: 'finished' };
        return Response.json({ id: 42, status: 'finished', status_label: 'Finished' });
      }
      if (url === '/api/series/brunchtime') {
        await seriesGate;
        return Response.json({ ...BRUNCHTIME_SERIES, episodes: [] });
      }
      if (url === '/api/series/brunchtime/slots')
        return Response.json({
          slots: [{ date: '2099-01-11', start_time: '12:00', end_time: '14:00' }],
        });
      if (url === '/api/my-show')
        return Response.json({ assigned: true, shows: [{ ...current, artists: [] }] });
      if (url === '/api/shows/42/upload-recording' && method === 'POST') {
        uploaded = options?.body as FormData;
        const name = (uploaded.get('file') as File).name;
        current = { ...current, recording_filename: name, recording_url: `https://r2/${name}` };
        return Response.json({ success: true, key: `recordings/${name}` });
      }
      if (url === '/api/shows/42/soundcloud/upload' && method === 'POST') return soundcloud;
      if (url === '/api/shows/42/soundcloud/privacy' && method === 'POST') {
        const isPublic = (body as { public: boolean }).public;
        current = { ...current, soundcloud_public: isPublic };
        return Response.json({ success: true, track_id: '555', track_url: current.soundcloud_url });
      }
      if (url === '/api/shows/42/announcements') return Response.json(announcements);
      // Every host: where each slot is, without text, image or error.
      if (url === '/api/shows/42/announcements/status')
        return Response.json(
          announcements
            .filter((a) => a.state !== 'draft')
            .map((a) => ({
              slot: a.slot,
              channel: a.channel,
              state: a.state,
              scheduled_at: a.scheduled_at,
              sent_at: a.sent_at,
              permalink: a.state === 'sent' ? a.permalink : null,
              image_version: a.state === 'sent' ? a.updated_at : null,
            }))
        );
      if (url === '/api/shows/42/announcements/meta') return Response.json(meta);
      if (url === '/api/shows/42/announcements/uploads' && method === 'POST') {
        uploaded = options?.body as FormData;
        return photoUpload ?? Response.json({ image_key: 'shows/42/announcements/uploads/a.jpg' });
      }
      if (url.startsWith('/api/shows/42/announcements/slots?'))
        return Response.json({ slots: times });
      const slot = url.match(/^\/api\/shows\/42\/announcements\/(pre|onair|post)$/)?.[1] as
        | AnnouncementSlot
        | undefined;
      if (slot && method === 'PUT') {
        if (saveError) return saveError;
        const saved = announcement(slot, body as Partial<Announcement>);
        announcements = [...announcements.filter((a) => a.slot !== slot), saved];
        return Response.json(saved);
      }
      if (slot && method === 'DELETE') {
        announcements = announcements.map((a) =>
          a.slot === slot ? { ...a, state: 'cancelled' } : a
        );
        return Response.json(announcements.find((a) => a.slot === slot));
      }
      throw new Error(`Unexpected API request: ${method} ${url}`);
    })
  );
});

afterEach(() => {
  app?.unmount();
  app = undefined;
  root.remove();
  live.showId = null;
  if (live.ready) live.ready.value = false;
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

async function mount(
  role: User['role'] = 'host',
  userId = 1,
  state?: Record<string, number>,
  path = '/shows/42'
): Promise<Router> {
  const stub = (text: string) => ({ render: () => h('div', text) });
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/schedule', component: stub('Schedule screen') },
      { path: '/shows', component: stub('My Shows') },
      { path: '/shows/:slug/book', component: stub('Upload Your Set') },
      {
        path: '/shows/:id(\\d+)',
        component: hostPage(
          async () => HostEpisodePage,
          async () => (await import('../pages/ShowDetailPage.vue')).default
        ),
      },
      { path: '/shows/:id(\\d+)/announce/:slot(pre|post)', component: HostAnnouncePage },
      { path: '/shows/:slug', component: stub('Show page') },
      { path: '/stream/live', component: stub('Studio') },
      { path: '/stream/on-air', component: stub('On Air') },
    ],
  });
  const pinia = createPinia();
  app = createApp({ render: () => h(RouterView) });
  app.use(pinia).use(router);
  useAuthStore(pinia).user = { id: userId, username: 'lena', role };
  await router.push(state ? { path, state } : path);
  await router.isReady();
  app.mount(root);
  await settle();
  return router;
}

async function settle() {
  for (let i = 0; i < 6; i++) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    await nextTick();
  }
}

const text = () => root.textContent ?? '';

function button(label: string): HTMLButtonElement {
  const found = [...root.querySelectorAll('button')].find((b) => b.textContent?.trim() === label);
  if (!found) throw new Error(`No button "${label}" in: ${text()}`);
  return found;
}

/** The post widget popup (teleported to <body>), or null. */
const dialog = () => document.body.querySelector<HTMLElement>('[role="dialog"]');

function dialogButton(label: string): HTMLButtonElement {
  const found = [...(dialog()?.querySelectorAll('button') ?? [])].find(
    (b) => b.textContent?.trim() === label
  );
  if (!found) throw new Error(`No button "${label}" in the popup: ${dialog()?.textContent}`);
  return found;
}

/** A status card of the episode page, by its title. */
function card(title: string): HTMLElement {
  const found = [...root.querySelectorAll<HTMLElement>('.ep-card')].find(
    (c) => c.querySelector('h2')?.textContent?.trim() === title
  );
  if (!found) throw new Error(`No card "${title}" in: ${text()}`);
  return found;
}

/** The titles of the cards with the yellow frame. */
const currentCards = () =>
  [...root.querySelectorAll('.ep-card.current h2')].map((h) => h.textContent?.trim());

/** The yellow (primary) buttons on the page. */
const yellow = () =>
  [...root.querySelectorAll('button.variant-primary')].map((b) => b.textContent?.trim());

const cardStatus = (title: string) => card(title).querySelector('.ep-status')?.textContent?.trim();

/** The four step tabs: name, state (now, done, next) and whether it is selected. */
const tabs = () =>
  [...root.querySelectorAll('[role="tab"]')].map((t) => ({
    name: t.querySelector('.tn')?.textContent?.trim(),
    state: ['done', 'now', 'next'].find((c) => t.classList.contains(c)),
    selected: t.getAttribute('aria-selected') === 'true',
  }));
const tab = (i: number) => root.querySelectorAll<HTMLButtonElement>('[role="tab"]')[i];
/** What the icons of tab `i` say: each item and its status. */
const badges = (i: number) =>
  [...tab(i).querySelectorAll('[role="img"]')].map((e) => e.getAttribute('aria-label'));
/** The Show time tab's clock and its line below. */
const tabClock = () => ({
  clock: tab(2).querySelector('.n')?.textContent?.replace(' · ', '').trim(),
  sub: tab(2).querySelector('.s')?.textContent?.replace(' · ', '').trim(),
});

/** Not hidden by v-show (the tab panel shows one card at a time). */
function isShown(el: Element | null): boolean {
  for (let e = el; e; e = e.parentElement) {
    if (e instanceof HTMLElement && e.style.display === 'none') return false;
  }
  return !!el;
}
/** The titles of the cards the main tab panel shows. */
const shownCards = () =>
  [...root.querySelectorAll('.ep-card')]
    .filter(isShown)
    .map((c) => c.querySelector('h2')?.textContent?.trim());
const shown = (selector: string) => isShown(root.querySelector(selector));
/** The read-only live panel (Show time when this tab does not send) and its text. */
const panel = () => root.querySelector('.on-air')?.textContent ?? '';
/** The read-only Studio card (Soundcheck when the Studio cannot open here). */
const studioView = () => root.querySelector('[aria-label="Studio, read-only"]');

/** Each card's frame (current, done or next) and its step, in page order. */
const frames = () =>
  [...root.querySelectorAll<HTMLElement>('.ep-card')].map((c) => ({
    title: c.querySelector('h2')?.textContent?.trim(),
    frame: ['current', 'done', 'next'].find((x) => c.classList.contains(x)),
    step: c.querySelector('.ep-card-phase')?.textContent?.trim(),
  }));

function editButton(): HTMLButtonElement {
  const found = [...root.querySelectorAll('button')].find((b) =>
    b.textContent?.trim().startsWith('Edit')
  );
  if (!found) throw new Error(`No Edit button in: ${text()}`);
  return found;
}

async function openEdit() {
  button('Edit').click();
  await settle();
}

const LEFT_OUT = [
  'Announcements',
  'Create guest link',
  'SoundCloud',
  'Instagram',
  'Social Media',
  'Pre-recorded ▾',
];

describe('Episode', () => {
  it('shows the heading box, the four step tabs and the current step in the main card', async () => {
    await mount();
    expect(root.querySelector('.when')?.textContent).toBe('So 04.01. · 12:00–14:00');
    expect(root.querySelector('h1')?.textContent).toBe('Brunchtime #31');
    for (const part of ['< Brunchtime', 'Upcoming', 'Live', 'Host: lena']) {
      expect(text()).toContain(part);
    }
    // The steps as tabs, no step strip; nothing is set up yet, so step 1 is current and open.
    expect(tabs()).toEqual([
      { name: '1 · Social media', state: 'now', selected: true },
      { name: '2 · Soundcheck', state: 'next', selected: false },
      { name: '3 · Show time', state: 'next', selected: false },
      { name: '4 · Recording', state: 'next', selected: false },
    ]);
    expect(root.querySelector('.segs')).toBeNull();
    expect(shownCards()).toEqual(['Social media']);
    // One icon per item, its status in the badge.
    expect(badges(0)).toEqual([
      'Pre-announcement: Not set up',
      'On-air story: Not set up',
      'Post-announcement: Not set up',
    ]);
    expect(badges(1)).toEqual([
      'Music: Not checked yet',
      'Mic: Not checked yet',
      'Server: Not checked yet',
      'Return feed: Not checked yet',
    ]);
    expect(badges(3)).toEqual(['Recording: not started', 'SoundCloud: no track yet']);
    // The cards keep their step frames; Soundcheck and Show time are the
    // Studio and the live panel.
    expect(frames()).toEqual([
      { title: 'Social media', frame: 'current', step: 'Step 1' },
      { title: 'Recording & SoundCloud', frame: 'next', step: 'Step 4' },
    ]);
    // A tab shows its card in the main card.
    tab(3).click();
    await settle();
    expect(shownCards()).toEqual(['Recording & SoundCloud']);
    expect(tabs().map((t) => t.selected)).toEqual([false, false, false, true]);
    // Read-only details: no form until Edit.
    expect(root.querySelector('input[type="text"], textarea, select')).toBeNull();
    expect(text()).not.toContain('Cancel booking');
    // Show time is the live panel without values; Go Live waits for air time.
    expect(panel()).toContain('Not on air · --:--:--');
    expect(button('Go Live').disabled).toBe(true);
    // The Studio opens here, so there is no read-only Studio card.
    expect(studioView()).toBeNull();
    for (const part of LEFT_OUT.filter((p) => p !== 'SoundCloud' && p !== 'Instagram')) {
      expect(text()).not.toContain(part);
    }
    expect(root.querySelector('a')?.getAttribute('href')).toBe('/shows/brunchtime');
    // Days before the start the Studio is open too. It asks for the
    // microphone, so it opens only from the Soundcheck tab.
    expect(root.querySelector('.studio-stub')).toBeNull();
    tab(1).click();
    await settle();
    await vi.waitFor(() => expect(shown('.studio-stub')).toBe(true));
  });

  it('opens the Studio in the Soundcheck tab, with this episode selected, from 30 min before the start', async () => {
    const { useHostFlow } = await import('../composables');
    vi.useFakeTimers({ toFake: ['Date'] });
    try {
      // 11:45 in Berlin on the show day: 15 min before the 12:00 start.
      vi.setSystemTime(new Date(`${FUTURE}T10:45:00Z`));
      const router = await mount();
      expect(tabs()[1]).toEqual({ name: '2 · Soundcheck', state: 'now', selected: true });
      await vi.waitFor(() => expect(shown('.studio-stub')).toBe(true));
      expect(root.querySelector('.studio-stub')?.textContent).toContain('Studio · embedded');
      // The Studio takes the Soundcheck card's place; the page stays on the episode.
      expect(shownCards()).toEqual([]);
      expect(useHostFlow().showId.value).toBe(42);
      expect(router.currentRoute.value.fullPath).toBe('/shows/42');
      // The tab shows each check of the open Studio.
      expect(badges(1)).toEqual([
        'Music: Good · peak −9 dB',
        'Mic: Now talk into the mic.',
        'Server: Not checked yet',
        'Return feed: Not checked yet',
      ]);
      // Another tab hides the Studio but keeps it, with its checks and input.
      tab(0).click();
      await settle();
      expect(shown('.studio-stub')).toBe(false);
      expect(root.querySelector('.studio-stub')).not.toBeNull();
      expect(shownCards()).toEqual(['Social media']);
    } finally {
      vi.useRealTimers();
      useHostFlow().reset();
    }
  });

  it('shows the live panel in the Show time tab after Go Live, and Recording after End Episode', async () => {
    const { useHostFlow } = await import('../composables');
    vi.useFakeTimers({ toFake: ['Date'] });
    try {
      // 12:01 in Berlin: the start passed, so Show time is current; without a
      // soundcheck in this tab it warns and leads to the Soundcheck tab.
      vi.setSystemTime(new Date(`${FUTURE}T11:01:00Z`));
      await mount();
      expect(tabs()[2]).toMatchObject({ state: 'now', selected: true });
      expect(shown('.on-air')).toBe(true);
      expect(root.querySelector('.studio-stub')).toBeNull();
      button('Go to Soundcheck').click();
      await settle();
      expect(tabs()[1].selected).toBe(true);
      await vi.waitFor(() => expect(shown('.studio-stub')).toBe(true));
      // The soundcheck passed: Show time has Go Live, blinking until clicked.
      useHostFlow().setLiveTestPassed(true);
      live.ready!.value = true;
      tab(2).click();
      await settle();
      expect(panel()).not.toContain('No soundcheck');
      expect(button('Go Live').classList.contains('blink')).toBe(true);
      expect(yellow()).toContain('Go Live');
      // Go Live: this browser tab streams the episode now.
      button('Go Live').click();
      await settle();
      expect(shown('.onair-stub')).toBe(true);
      expect(root.querySelector('.onair-stub')?.textContent).toContain('Live panel · embedded');
      expect(tabs()[2].selected).toBe(true);
      expect(root.querySelector('.studio-stub')).toBeNull();
      expect(tabClock()).toEqual({ clock: '00:01:00', sub: 'of 2:00:00' });
      expect(badges(3)[0]).toBe('Recording: on air');
      // Another tab hides the live panel but keeps it running; the on-air bar shows.
      expect(root.querySelector('.onair-stub')?.getAttribute('data-shown')).toBe('true');
      tab(0).click();
      await settle();
      expect(shown('.onair-stub')).toBe(false);
      expect(root.querySelector('.onair-stub')).not.toBeNull();
      expect(root.querySelector('.onair-stub')?.getAttribute('data-shown')).toBe('false');
      tab(2).click();
      await settle();
      live.showId = null;
      button('End Episode').click();
      await settle();
      expect(tabs()[3].selected).toBe(true);
      expect(shownCards()).toEqual(['Recording & SoundCloud']);
    } finally {
      vi.useRealTimers();
      useHostFlow().reset();
    }
  });

  it('says "tested" only for a soundcheck passed in this tab', async () => {
    const { useHostFlow } = await import('../composables');
    const flow = useHostFlow();
    await flow.fetchMyShow();
    flow.selectShow(flow.shows.value.find((s) => s.id === 42)!);
    flow.setLiveTestPassed(true);
    try {
      await mount();
      expect(badges(1)).toEqual([
        'Music: Passed',
        'Mic: Passed',
        'Server: Passed',
        'Return feed: Passed',
      ]);
    } finally {
      flow.reset();
    }
  });

  it('while on air without audio from this tab, leads from Show time to the Studio to go back on air', async () => {
    const { useHostFlow } = await import('../composables');
    current = episode({ on_air: true });
    try {
      await mount();
      expect(text()).toContain('On air · until 14:00');
      expect(text()).not.toContain('Upcoming');
      expect(tabs()[2]).toMatchObject({ state: 'now', selected: true });
      expect(shown('.on-air')).toBe(true);
      expect(root.querySelector('.onair-badge')?.textContent).toBe('On air');
      expect(cardStatus('Recording & SoundCloud')).toBe('Rec');
      // No soundcheck Studio in Show time: Back to On Air opens it in the Soundcheck tab.
      expect(shownCards()).toEqual([]);
      expect(root.querySelector('.studio-stub')).toBeNull();
      button('Back to On Air').click();
      await settle();
      expect(tabs()[1].selected).toBe(true);
      await vi.waitFor(() => expect(shown('.studio-stub')).toBe(true));
      expect(root.querySelector('.onair-stub')).toBeNull();
    } finally {
      useHostFlow().reset();
    }
  });

  it('keeps the Studio out of Show time when the start passed and nobody is live', async () => {
    const { useHostFlow } = await import('../composables');
    vi.useFakeTimers({ toFake: ['Date'] });
    try {
      // 12:10 in Berlin on the show day (winter time).
      vi.setSystemTime(new Date(`${FUTURE}T11:10:00Z`));
      await mount();
      expect(tabs()[2]).toMatchObject({ state: 'now', selected: true });
      expect(root.querySelector('.onair-badge')?.textContent).toBe('Not on air');
      expect(text()).toContain('Show time · until 14:00');
      expect(tabClock()).toEqual({ clock: '00:10:00', sub: 'not on air yet' });
      expect(shown('.on-air')).toBe(true);
      // No soundcheck in Show time: a warning and a way to the Soundcheck tab.
      expect(panel()).toContain('No soundcheck in this browser yet. Do it before you go live.');
      expect(root.querySelector('.on-air .end button')?.textContent?.trim()).toBe(
        'Go to Soundcheck'
      );
      expect(root.querySelector('.studio-stub')).toBeNull();
      // The Soundcheck tab holds the Studio, also after the start.
      tab(1).click();
      await settle();
      await vi.waitFor(() => expect(shown('.studio-stub')).toBe(true));
      expect(shownCards()).toEqual([]);
    } finally {
      vi.useRealTimers();
      useHostFlow().reset();
    }
  });

  it('warns in Show time before the start only while the Studio is open and no soundcheck passed', async () => {
    const { useHostFlow } = await import('../composables');
    vi.useFakeTimers({ toFake: ['Date'] });
    const warning = 'No soundcheck in this browser yet. Do it before you go live.';
    try {
      // 11:20 in Berlin: the Studio opens at 11:30.
      vi.setSystemTime(new Date(`${FUTURE}T10:20:00Z`));
      await mount();
      expect(panel()).not.toContain(warning);
      app?.unmount();
      root.remove();
      // 11:45: open; the soundcheck is not done in this tab.
      vi.setSystemTime(new Date(`${FUTURE}T10:45:00Z`));
      await mount();
      expect(panel()).toContain(warning);
      expect(panel()).toContain('Go to Soundcheck');
      app?.unmount();
      root.remove();
      // Passed in this tab: no warning; Go Live waits for the start.
      const flow = useHostFlow();
      await flow.fetchMyShow();
      flow.selectShow(flow.shows.value.find((s) => s.id === 42)!);
      flow.setLiveTestPassed(true);
      await mount();
      expect(panel()).not.toContain(warning);
      expect(button('Go Live').disabled).toBe(true);
    } finally {
      vi.useRealTimers();
      useHostFlow().reset();
    }
  });

  it('turns the heading box into a form with Edit and saves the title', async () => {
    await mount();
    await openEdit();
    expect(text()).toContain('Editing episode details');
    const input = root.querySelector('input[type="text"]') as HTMLInputElement;
    expect(input.value).toBe('Brunchtime #31');
    input.value = 'Brunchtime #31 · Guest special';
    input.dispatchEvent(new Event('input'));
    button('Save').click();
    await vi.waitFor(() =>
      expect(requests.find((r) => r.method === 'PUT')?.body).toEqual({
        title: 'Brunchtime #31 · Guest special',
      })
    );
    // Saved: read-only again, with the new title.
    await vi.waitFor(() =>
      expect(root.querySelector('h1')?.textContent).toBe('Brunchtime #31 · Guest special')
    );
    expect(root.querySelector('input[type="text"]')).toBeNull();
  });

  it('leaves the form with Cancel and saves nothing', async () => {
    await mount();
    await openEdit();
    const input = root.querySelector('input[type="text"]') as HTMLInputElement;
    input.value = 'Something else';
    input.dispatchEvent(new Event('input'));
    button('Cancel').click();
    await settle();
    expect(root.querySelector('h1')?.textContent).toBe('Brunchtime #31');
    expect(requests.some((r) => r.method === 'PUT')).toBe(false);
  });

  it('moves the episode to a free slot and switches its mode with one Save', async () => {
    await mount();
    await openEdit();
    const [dateSelect] = [...root.querySelectorAll('select')] as HTMLSelectElement[];
    await vi.waitFor(() => expect(dateSelect.options).toHaveLength(2));
    expect([...dateSelect.options].map((o) => o.text)).toEqual(['So 04.01.2099', 'So 11.01.2099']);
    dateSelect.value = '2099-01-11';
    dateSelect.dispatchEvent(new Event('change'));
    const pre = root.querySelector('input[value="prerecorded"]') as HTMLInputElement;
    pre.checked = true;
    pre.dispatchEvent(new Event('change'));
    await nextTick();
    button('Save').click();
    await vi.waitFor(() =>
      expect(requests.find((r) => r.method === 'PUT')?.body).toEqual({
        date: '2099-01-11',
        start_time: '12:00',
        end_time: '14:00',
        stream_mode: 'prerecorded',
      })
    );
  });

  it.each([
    ['on air', { on_air: true }, 'Edit · locked on air'],
    [
      'after the show',
      { date: PAST, episode_status: 'wrapup_open' as const },
      'Edit · locked after the show',
    ],
  ])('locks Edit %s', async (_name, overrides, label) => {
    current = episode(overrides);
    await mount();
    expect(editButton().textContent?.trim()).toBe(label);
    expect(editButton().disabled).toBe(true);
  });

  it('asks for the set of a pre-recorded episode that has no file yet', async () => {
    current = episode({ stream_mode: 'prerecorded' });
    const router = await mount();
    expect(cardStatus('Soundcheck')).toBe('No file yet');
    expect(card('Soundcheck').textContent).toContain(
      'Upload your set before air time. Without a file nothing plays.'
    );
    expect(card('Show time').textContent).toContain('Plays at 12:00.');
    // Set up social media is still the current step, so the button is not yellow yet.
    expect(yellow()).toEqual([]);
    button('Upload your set').click();
    await vi.waitFor(() =>
      expect(router.currentRoute.value.fullPath).toBe('/shows/brunchtime/book?mode=prerecorded')
    );
  });

  it('asks once before it cancels the booking, then opens the Show page', async () => {
    const router = await mount();
    await openEdit();
    expect(text()).toContain('Cancel booking frees the date.');
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false);
    button('Cancel booking').click();
    await settle();
    expect(requests.some((r) => r.url.endsWith('/booking'))).toBe(false);

    confirm.mockReturnValueOnce(true);
    button('Cancel booking').click();
    await vi.waitFor(() => expect(router.currentRoute.value.fullPath).toBe('/shows/brunchtime'));
    expect(confirm).toHaveBeenCalledTimes(2);
    expect(requests.find((r) => r.url.endsWith('/booking'))?.method).toBe('DELETE');
  });
});

describe('Episode · phases', () => {
  afterEach(() => {
    vi.useRealTimers();
  });
  /** So 25.10.2026, 12:00–14:00 (winter time, UTC+1). */
  const day = (overrides: Partial<ShowDetail> = {}) =>
    episode({ date: '2026-10-25', ...overrides });
  const at = (berlin: string) => {
    vi.useFakeTimers({ toFake: ['Date'] });
    const [h, m] = berlin.split(':').map(Number);
    vi.setSystemTime(new Date(Date.UTC(2026, 9, 25, h - 1, m)));
  };
  const ready = {
    id: 1,
    version: 'v1',
    status: 'raw' as const,
    duration_ms: 7120000,
    download_url: 'https://r2.example/v1.mp3',
  };

  it('pre-show: moves on to Soundcheck when the Studio opens and counts down to the start', async () => {
    at('11:40');
    current = day();
    await mount();
    expect(tabs().map((t) => t.state)).toEqual(['done', 'now', 'next', 'next']);
    expect(tabs()[1].selected).toBe(true);
    expect(tabClock()).toEqual({ clock: '00:20:00', sub: 'until 12:00' });
    // The Studio is the Soundcheck tab's page; Go Live is not yellow before its step.
    await vi.waitFor(() => expect(shown('.studio-stub')).toBe(true));
    expect(yellow()).toEqual([]);
  });

  it('keeps Set up social media current until both posts are set up', async () => {
    at('11:00');
    current = day({ date: '2026-10-27' });
    announcements = [announcement('pre', { scheduled_at: FR_2310_1800 })];
    await mount();
    expect(tabs().map((t) => t.state)).toEqual(['now', 'next', 'next', 'next']);
    expect(badges(0)).toEqual([
      'Pre-announcement: Scheduled · Fr 23.10. · 18:00',
      'On-air story: Not set up',
      'Post-announcement: Not set up',
    ]);
    // More than a day before the start, the clock says the days.
    expect(tabClock()).toEqual({ clock: 'in 2 days', sub: 'until 12:00' });
    expect(currentCards()).toEqual(['Social media']);
    // Soundcheck is not the current step yet: no yellow button. Go Live waits.
    expect(yellow()).toEqual([]);
    expect(button('Go Live').disabled).toBe(true);
  });

  it('moves on to Soundcheck once both posts are set up, days before the start', async () => {
    at('11:00');
    current = day({ date: '2026-10-27' });
    announcements = [announcement('pre', { scheduled_at: FR_2310_1800 }), announcement('onair')];
    await mount();
    expect(tabs().map((t) => t.state)).toEqual(['done', 'now', 'next', 'next']);
    expect(badges(0).slice(0, 2)).toEqual([
      'Pre-announcement: Scheduled · Fr 23.10. · 18:00',
      'On-air story: Scheduled',
    ]);
    expect(frames().map((f) => f.frame)).toEqual(['done', 'next']);
  });

  it('on air: shows the time on air in the Show time tab', async () => {
    at('12:42');
    current = day({ on_air: true });
    announcements = [
      announcement('pre', { state: 'sent', sent_at: FR_2310_1800 }),
      announcement('onair', { state: 'sent', sent_at: '2026-10-25T10:00:00Z' }),
    ];
    await mount();
    expect(tabs().map((t) => t.state)).toEqual(['done', 'done', 'now', 'next']);
    expect(tabClock()).toEqual({ clock: '00:42:00', sub: 'of 2:00:00' });
    expect(badges(0)[0]).toBe('Pre-announcement: Sent · Fr 23.10. · 18:00');
    expect(badges(2)).toEqual(['On air']);
    // Frames follow the steps: done dim yellow, current yellow, next gray.
    expect(frames().map((f) => f.frame)).toEqual(['done', 'next']);
    expect(root.querySelector('section.ep-embed.now .on-air')).not.toBeNull();
    // Nothing saved by the host's browser yet: the time by the schedule.
    expect(root.querySelector('.on-air-clock')?.textContent).toBe('00:42:00');
    expect(yellow()).toEqual(['Back to On Air']);
  });

  it('post-show: opens on Recording, with the aired length in Show time', async () => {
    at('14:20');
    current = day({ episode_status: 'wrapup_open', latest_recording: ready });
    await mount();
    expect(text()).not.toContain('Wrap-up');
    expect(tabs().map((t) => t.state)).toEqual(['done', 'done', 'done', 'now']);
    expect(tabs()[3].selected).toBe(true);
    expect(tabClock()).toEqual({ clock: '1:58:40', sub: '12:00–14:00' });
    expect(badges(1)[0]).toBe('Music: No result in this browser');
    expect(badges(3)).toEqual(['Recording: ready', 'SoundCloud: no track yet']);
    expect(shownCards()).toEqual(['Recording & SoundCloud']);
    expect(currentCards()).toEqual(['Recording & SoundCloud']);
    expect(yellow()).toEqual(['Publish episode']);
    expect(root.querySelector('.onair-badge')?.textContent).toBe('Aired');
    expect(root.querySelector('.on-air-clock')?.textContent).toBe('01:58:40');
    expect(card('Recording & SoundCloud').textContent).toContain('Ready · 1:58:40');
    expect(root.querySelector('.audio-player')?.getAttribute('data-src')).toBe(
      'https://r2.example/v1.mp3'
    );
    // Publish episode closes the episode in place: Finished, the button goes.
    button('Publish episode').click();
    await vi.waitFor(() => expect(text()).not.toContain('Publish episode'));
    expect(requests.some((r) => r.url === '/api/shows/42/publish')).toBe(true);
    expect(tabs().map((t) => t.state)).toEqual(['done', 'done', 'done', 'done']);
    expect(tabs()[3].selected).toBe(true);
  });

  it('post-show: shows the public track and the sent post', async () => {
    at('14:20');
    current = day({
      episode_status: 'wrapup_open',
      soundcloud_url: 'https://soundcloud.com/radio-moafunk/brunchtime-31',
      soundcloud_public: true,
      latest_recording: ready,
    });
    announcements = [
      announcement('post', {
        state: 'sent',
        sent_at: '2026-10-25T13:10:00Z',
        permalink: 'https://instagram.com/p/post',
      }),
    ];
    await mount();
    expect(badges(3)).toEqual(['Recording: ready', 'SoundCloud: public track']);
    expect(badges(0)[2]).toBe('Post-announcement: Sent · So 25.10. · 14:10');
    expect(cardStatus('Recording & SoundCloud')).toBe('Public on SoundCloud');
  });

  it('keeps the one yellow button in the current card', async () => {
    at('14:20');
    current = day({ episode_status: 'wrapup_open', latest_recording: ready });
    await mount();
    const primary = [...root.querySelectorAll('button.variant-primary')];
    expect(primary).toHaveLength(1);
    expect(primary[0].closest('.ep-card')?.classList.contains('current')).toBe(true);
  });

  it('pre-recorded: shows the set with Replace file, and "Plays at" in Show time', async () => {
    at('11:40');
    current = day({
      stream_mode: 'prerecorded',
      prerecorded_filename: 'brunchtime-0811.mp3',
      prerecorded_url: 'https://r2.example/brunchtime-0811.mp3',
      prerecorded_duration_secs: 7120,
    });
    await mount();
    expect(cardStatus('Soundcheck')).toBe('Set uploaded');
    expect(card('Soundcheck').querySelector('button')?.textContent?.trim()).toBe('Replace file');
    expect(card('Show time').textContent).toContain('Plays at 12:00.');
    expect(text()).not.toContain('Open Studio');
    // A pre-recorded episode has its set instead of the four checks, and no Studio.
    expect(badges(1)).toEqual(['Set: uploaded · brunchtime-0811.mp3']);
    expect(root.querySelector('.studio-stub')).toBeNull();
    expect(shownCards()).toEqual(['Soundcheck']);
  });
});

describe('Episode · phone', () => {
  beforeEach(() => {
    vi.stubGlobal('matchMedia', (query: string) => ({
      matches: query === '(max-width: 640px)',
      addEventListener() {},
      removeEventListener() {},
    }));
  });
  it('shows the main card open, with no fold toggle; the tabs switch it', async () => {
    await mount();
    expect(shownCards()).toEqual(['Social media']);
    expect(root.querySelector('.ep-card h2 button')).toBeNull();
    expect(
      (card('Social media').querySelector('.ep-card-content') as HTMLElement).style.display
    ).toBe('');
    tab(1).click();
    await nextTick();
    // Soundcheck opens the Studio in the main card.
    await vi.waitFor(() => expect(shown('.studio-stub')).toBe(true));
    expect(shownCards()).toEqual([]);
    expect(tabs()[1].selected).toBe(true);
  });

  it('opens the Show time tab while on air', async () => {
    const { useHostFlow } = await import('../composables');
    current = episode({ on_air: true });
    try {
      await mount();
      expect(tabs()[2].selected).toBe(true);
      expect(shown('.on-air')).toBe(true);
      expect(root.querySelector('.studio-stub')).toBeNull();
    } finally {
      useHostFlow().reset();
    }
  });
});

describe('Episode · Recording', () => {
  function pick(name: string) {
    const label = [...root.querySelectorAll('label')].find((l) =>
      l.textContent?.includes('Recording file')
    );
    const input = label?.querySelector('input[type="file"]') as HTMLInputElement;
    Object.defineProperty(input, 'files', {
      value: [new File(['mp3'], name, { type: 'audio/mpeg' })],
      configurable: true,
    });
    input.dispatchEvent(new Event('change'));
  }

  it('uploads a picked file as the episode recording', async () => {
    await mount();
    expect(text()).not.toContain('Upload to SoundCloud');
    pick('brunchtime-2510.mp3');
    await vi.waitFor(() => expect(text()).toContain('brunchtime-2510.mp3'));
    expect((uploaded?.get('file') as File).name).toBe('brunchtime-2510.mp3');
    // Reloaded, so the button sees the new recording.
    expect(requests.filter((r) => r.url === '/api/shows/42').length).toBe(2);
    expect(button('Upload to SoundCloud').disabled).toBe(false);
    expect(cardStatus('Recording & SoundCloud')).toBe('File uploaded');
  });

  it('says SoundCloud is not available when an admin turned it off', async () => {
    meta = { paused: false, needs_approval: false, services: { ...ON, soundcloud: false } };
    current = episode({
      recording_filename: 'set.mp3',
      recording_url: 'https://r2/set.mp3',
      soundcloud_url: 'https://soundcloud.com/radio-moafunk/brunchtime-31',
    });
    await mount();
    expect(text()).toContain('SoundCloud is temporarily not available.');
    expect(button('Upload to SoundCloud').disabled).toBe(true);
    expect(button('Make track public').disabled).toBe(true);
  });

  it('sends the recording to SoundCloud and shows the track link', async () => {
    current = episode({ recording_filename: 'set.mp3', recording_url: 'https://r2/set.mp3' });
    await mount();
    button('Upload to SoundCloud').click();
    await vi.waitFor(() =>
      expect(root.querySelector('a[href^="https://soundcloud.com"]')?.textContent).toBe(
        'Open on SoundCloud'
      )
    );
    expect(root.querySelector('a[href^="https://soundcloud.com"]')?.getAttribute('href')).toBe(
      'https://soundcloud.com/radio-moafunk/brunchtime-31'
    );
    expect(requests.filter((r) => r.url.endsWith('/soundcloud/upload'))).toHaveLength(1);
  });
  it('makes a fresh private track public, which unlocks the post-announcement', async () => {
    current = episode({ recording_filename: 'set.mp3', recording_url: 'https://r2/set.mp3' });
    await mount();
    button('Upload to SoundCloud').click();
    await vi.waitFor(() => expect(text()).toContain('Private on SoundCloud'));
    button('Make track public').click();
    await vi.waitFor(() => expect(text()).toContain('Public on SoundCloud'));
    expect(requests.find((r) => r.url.endsWith('/soundcloud/privacy'))?.body).toEqual({
      public: true,
    });
    // Reloaded, so the page sees the public track.
    await vi.waitFor(() => expect(button('Make track private')).toBeTruthy());
  });

  it('lets the host switch a public track back to private', async () => {
    current = episode({
      soundcloud_url: 'https://soundcloud.com/radio-moafunk/brunchtime-31',
      soundcloud_public: true,
    });
    await mount();
    expect(text()).toContain('Public on SoundCloud');
    button('Make track private').click();
    await vi.waitFor(() => expect(text()).toContain('Private on SoundCloud'));
    expect(requests.find((r) => r.url.endsWith('/soundcloud/privacy'))?.body).toEqual({
      public: false,
    });
    expect(button('Make track public')).toBeTruthy();
  });

  it('has no switch before the track is on SoundCloud', async () => {
    current = episode({ recording_filename: 'set.mp3' });
    await mount();
    expect(text()).not.toContain('Make track public');
  });

  it('shows the SoundCloud error, and asks an admin to reconnect when not authorized', async () => {
    current = episode({ recording_filename: 'set.mp3' });
    soundcloud = Response.json({ success: false, error: 'SoundCloud API error (500): down' });
    await mount();
    button('Upload to SoundCloud').click();
    await vi.waitFor(() =>
      expect(root.querySelector('[role="alert"]')?.textContent).toBe(
        'SoundCloud API error (500): down'
      )
    );

    soundcloud = Response.json(
      { error: 'SoundCloud not authorized. Visit /api/soundcloud/auth to connect your account.' },
      { status: 400 }
    );
    button('Upload to SoundCloud').click();
    await vi.waitFor(() =>
      expect(root.querySelector('[role="alert"]')?.textContent).toBe(
        'SoundCloud is not connected. An admin must connect the account again.'
      )
    );
    expect(root.querySelector('a[href^="https://soundcloud.com"]')).toBeNull();
  });
});

describe('Episode · Pre-recorded', () => {
  const prerecorded = (overrides: Partial<ShowDetail> = {}) =>
    episode({
      stream_mode: 'prerecorded',
      prerecorded_filename: 'brunchtime-0811.mp3',
      prerecorded_url: 'https://r2.example/brunchtime-0811.mp3',
      prerecorded_duration_secs: 7120,
      prerecorded_confirmed_at: '2026-11-01T10:00:00Z',
      ...overrides,
    });

  it('shows the set in the Soundcheck card and Publish episode only after the show', async () => {
    current = prerecorded();
    await mount();
    for (const part of [
      '< Brunchtime',
      'Pre-recorded',
      'Nothing to do. The set plays at air time.',
      'brunchtime-0811.mp3 · 1:58:40',
      'Replace file',
      'Plays at 12:00.',
    ]) {
      expect(text()).toContain(part);
    }
    const leftOut = LEFT_OUT.filter((p) => p !== 'SoundCloud' && p !== 'Instagram');
    for (const part of [...leftOut, 'Cancel booking', 'Open Studio', 'Publish episode']) {
      expect(text()).not.toContain(part);
    }
    expect(cardStatus('Soundcheck')).toBe('Set uploaded');
    expect(cardStatus('Recording & SoundCloud')).toBe('Set ready');
  });

  it('opens on the Recording card after the show, with Publish episode', async () => {
    current = prerecorded({ date: PAST, episode_status: 'wrapup_open' });
    await mount();
    expect(text()).not.toContain('Wrap-up');
    expect(shownCards()).toEqual(['Recording & SoundCloud']);
    expect(currentCards()).toEqual(['Recording & SoundCloud']);
    expect(card('Recording & SoundCloud').textContent).toContain('Ready · 1:58:40');
    expect(text()).not.toContain('Replace file');
    expect(cardStatus('Show time')).toBe('Aired');
    expect(card('Show time').querySelector('.big b')?.textContent).toBe('1:58:40');
    expect(yellow()).toEqual(['Publish episode']);
  });
  it('names the booked set and its length next to the player', async () => {
    current = prerecorded();
    await mount();
    expect(root.querySelector('.set-file')?.textContent).toBe('brunchtime-0811.mp3 · 1:58:40');
    expect(root.querySelector('.audio-player')?.getAttribute('data-src')).toBe(
      'https://r2.example/brunchtime-0811.mp3'
    );
  });

  it.each([
    ['no length', { prerecorded_duration_secs: null }, 'brunchtime-0811.mp3', true],
    ['no file link', { prerecorded_url: undefined }, 'brunchtime-0811.mp3 · 1:58:40', false],
  ])('names the set with %s', async (_name, overrides, line, player) => {
    current = prerecorded(overrides);
    await mount();
    expect(root.querySelector('.set-file')?.textContent).toBe(line);
    expect(!!root.querySelector('.audio-player')).toBe(player);
  });

  it('opens Upload Your Set to replace the file of this episode', async () => {
    current = prerecorded();
    const router = await mount();
    button('Replace file').click();
    await vi.waitFor(() =>
      expect(router.currentRoute.value.fullPath).toBe('/shows/brunchtime/book?mode=prerecorded')
    );
    expect(currentReplaceTarget(router)).toEqual({
      id: 42,
      date: FUTURE,
      start_time: '12:00',
      end_time: '14:00',
    });
  });
});

describe('Recording card after the show (no Wrap-up page)', () => {
  it('publishes an ended episode in the Recording card', async () => {
    current = episode({
      date: PAST,
      episode_status: 'wrapup_open',
      latest_recording: {
        id: 1,
        version: 'v1',
        status: 'raw',
        duration_ms: 7120000,
        download_url: 'https://r2.example/v1.mp3',
      },
    });
    const router = await mount();
    expect(text()).not.toContain('Wrap-up');
    expect(text()).toContain('Finished · wrap-up open');
    expect(shownCards()).toEqual(['Recording & SoundCloud']);
    expect(card('Recording & SoundCloud').textContent).toContain('Ready · 1:58:40');
    const leftOut = LEFT_OUT.filter((p) => p !== 'SoundCloud' && p !== 'Instagram');
    for (const part of leftOut) expect(text()).not.toContain(part);

    button('Publish episode').click();
    await vi.waitFor(() => expect(text()).not.toContain('Publish episode'));
    expect(requests.filter((r) => r.url === '/api/shows/42/publish')).toHaveLength(1);
    expect(text()).not.toContain('Episode published');
    expect(shownCards()).toEqual(['Recording & SoundCloud']);
    expect(router.currentRoute.value.fullPath).toBe('/shows/42');
  });

  it('waits while the recording converts', async () => {
    current = episode({
      date: PAST,
      episode_status: 'wrapup_open',
      latest_recording: { id: 1, version: 'v1', status: 'raw' },
    });
    await mount();
    expect(card('Recording & SoundCloud').textContent).toContain('Processing…');
    expect(button('Publish episode').disabled).toBe(true);
  });

  it.each([
    [
      'a failed recording',
      { id: 1, version: 'v1', status: 'failed' as const, error_message: 'Shorter than 1 minute' },
      'No usable recording · Shorter than 1 minute',
    ],
    ['no recording long after the end', undefined, 'No usable recording · Nothing was recorded.'],
  ])('lets %s be published', async (_name, latest_recording, line) => {
    current = episode({ date: PAST, episode_status: 'wrapup_open', latest_recording });
    await mount();
    expect(card('Recording & SoundCloud').textContent).toContain(line);
    expect(button('Publish episode').disabled).toBe(false);
  });

  it('opens Recording after End Episode, while the recording is still saved', async () => {
    // Ended before its scheduled end, and the session is still closing.
    current = episode({ on_air: true });
    await mount('host', 1, { episodeEnded: 42 });
    expect(text()).not.toContain('Wrap-up');
    expect(tabs()[3].selected).toBe(true);
    expect(shownCards()).toEqual(['Recording & SoundCloud']);
    // Publish episode comes once the episode is off air.
    expect(text()).not.toContain('Publish episode');
  });

  it.each([
    [
      'an older failed version',
      { id: 1, version: 'v1', status: 'failed' as const, error_message: 'recovered' },
    ],
    [
      'an older saved version',
      { id: 1, version: 'v1', status: 'raw' as const, download_url: 'https://r2.example/v1.mp3' },
    ],
  ])('offers no Publish while on air, also with %s', async (_name, latest_recording) => {
    // A restart mid-show leaves an older version; the server refuses Publish
    // until the current session is saved.
    current = episode({ on_air: true, latest_recording });
    await mount('host', 1, { episodeEnded: 42 });
    expect(tabs()[3].selected).toBe(true);
    expect(text()).not.toContain('Publish episode');
  });

  it('counts the recording wait from an early End Episode', async () => {
    // Ended 20 min ago, long before its scheduled end; no recording was saved.
    current = episode({
      episode_status: 'wrapup_open',
      live_ended_at: new Date(Date.now() - 20 * 60 * 1000).toISOString(),
    });
    await mount('host', 1, { episodeEnded: 42 });
    expect(text()).toContain('No usable recording · Nothing was recorded.');
    expect(button('Publish episode').disabled).toBe(false);
  });

  it('still waits right after an early End Episode', async () => {
    current = episode({
      episode_status: 'wrapup_open',
      live_ended_at: new Date(Date.now() - 60 * 1000).toISOString(),
    });
    await mount('host', 1, { episodeEnded: 42 });
    expect(card('Recording & SoundCloud').textContent).toContain('Processing…');
    expect(button('Publish episode').disabled).toBe(true);
  });

  it('says when "Record this show" was off', async () => {
    current = episode({ episode_status: 'wrapup_open', live_recorded: false });
    await mount('host', 1, { episodeEnded: 42 });
    expect(text()).toContain('No usable recording · Record this show was off.');
    expect(button('Publish episode').disabled).toBe(false);
  });

  it('opens a finished episode on its Recording card, where the track can go public', async () => {
    current = episode({
      date: PAST,
      episode_status: 'finished',
      soundcloud_url: 'https://soundcloud.com/radio-moafunk/brunchtime-31',
      soundcloud_public: false,
      // A live episode with a recording.
      latest_recording: {
        id: 1,
        version: 'v1',
        status: 'raw',
        duration_ms: 7120000,
        download_url: 'https://r2.example/v1.mp3',
      },
    });
    await mount();
    expect(text()).not.toContain('Episode published');
    expect(text()).not.toContain('Wrap-up');
    expect(tabs().map((t) => t.state)).toEqual(['done', 'done', 'done', 'done']);
    expect(tabs().map((t) => t.selected)).toEqual([false, false, false, true]);
    expect(shownCards()).toEqual(['Recording & SoundCloud']);
    expect(text()).not.toContain('Publish episode');
    button('Make track public').click();
    await vi.waitFor(() => expect(text()).toContain('Public on SoundCloud'));
  });
});

describe('Episode · Social media card', () => {
  const row = (name: string) =>
    [...root.querySelectorAll('.promotion li')].find(
      (li) => li.querySelector('b')?.textContent === name
    );
  const chip = (name: string) => row(name)?.querySelector('.ep-status')?.textContent?.trim();
  const buttons = (name?: string) =>
    [...(name ? row(name)! : root.querySelector('.promotion')!).querySelectorAll('button')].map(
      (b) => b.textContent?.trim()
    );
  const picture = (name: string) => row(name)?.querySelector('img')?.getAttribute('src');

  it('starts with every slot not set up, each with its own Set up', async () => {
    await mount();
    for (const name of ['Pre-announcement', 'On-air story', 'Post-announcement']) {
      expect(chip(name)).toBe('Not set up');
      expect(picture(name)).toBeUndefined();
    }
    expect(buttons()).toEqual([
      'Set up pre-announcement',
      'Set up on-air story',
      'Set up post-announcement',
    ]);
    expect(row('On-air story')?.textContent).toContain('Instagram story · at go-live');
    expect(row('Post-announcement')?.textContent).toContain(
      'Instagram feed post · after the show, once the track is public'
    );
    expect(cardStatus('Social media')).toBe('Nothing set up');
    expect(root.querySelector('.promotion input')).toBeNull();
    expect(text()).toContain('Instagram posts go out on @moafunk_radio.');
  });

  it('shows a scheduled pre-announcement with its picture and edits it in the popup', async () => {
    announcements = [announcement('pre', { scheduled_at: FR_2310_1800 })];
    times = [{ scheduled_at: FR_2310_1800, free: true, default: true }];
    const router = await mount();
    expect(row('Pre-announcement')?.textContent).toContain(
      'Instagram feed post · Fr 23.10. · 18:00'
    );
    expect(chip('Pre-announcement')).toBe('Scheduled');
    expect(cardStatus('Social media')).toBe('1 scheduled');
    expect(picture('Pre-announcement')).toBe(
      '/api/shows/42/announcements/pre/image.jpg?v=2026-10-20T10%3A00%3A00Z&size=thumb'
    );

    button('Edit pre-announcement').click();
    await vi.waitFor(() => expect(dialog()?.textContent).toContain('Schedule post'));
    expect(dialog()?.querySelector('h2')?.textContent).toBe('Pre-announcement');
    expect(router.currentRoute.value.fullPath).toBe('/shows/42');
    dialogButton('Schedule post').click();
    await vi.waitFor(() => expect(dialog()).toBeNull());
    expect(requests.filter((r) => r.method === 'PUT').map((r) => r.url)).toEqual([
      '/api/shows/42/announcements/pre',
    ]);
  });

  it('closes the popup on Cancel without saving', async () => {
    await mount();
    button('Set up pre-announcement').click();
    await vi.waitFor(() => expect(dialog()?.textContent).toContain('Schedule post'));
    dialogButton('Cancel').click();
    await vi.waitFor(() => expect(dialog()).toBeNull());
    expect(requests.some((r) => r.method === 'PUT')).toBe(false);
  });

  it('cancels the pre-announcement after one confirm', async () => {
    announcements = [announcement('pre', { scheduled_at: FR_2310_1800 })];
    await mount();
    vi.spyOn(window, 'confirm').mockReturnValueOnce(true);
    button('Cancel pre-announcement').click();
    await vi.waitFor(() => expect(chip('Pre-announcement')).toBe('Cancelled'));
    expect(requests.find((r) => r.method === 'DELETE')?.url).toBe(
      '/api/shows/42/announcements/pre'
    );
    expect(row('Pre-announcement')?.textContent).toContain(
      'Instagram feed post · cancelled, not sent'
    );
    expect(buttons('Pre-announcement')).toEqual(['Set up pre-announcement']);
  });

  it('sets up the on-air story in the popup: no text, no people, at go-live', async () => {
    await mount();
    button('Set up on-air story').click();
    await vi.waitFor(() => expect(dialog()?.textContent).toContain('Schedule story'));
    const popup = dialog()!;
    expect(popup.querySelector('h2')?.textContent).toBe('On-air story');
    expect(popup.querySelector('textarea')).toBeNull();
    expect(popup.textContent).not.toContain('More collaborators');
    expect(popup.textContent).toContain('Goes out when you go live.');
    expect(requests.some((r) => r.url.includes('/slots?'))).toBe(false);

    dialogButton('Schedule story').click();
    await vi.waitFor(() => expect(dialog()).toBeNull());
    expect(requests.find((r) => r.method === 'PUT')).toEqual({
      url: '/api/shows/42/announcements/onair',
      method: 'PUT',
      body: {
        headline: '',
        caption: '',
        image_source: 'cover',
        layout: 'band',
        collaborator: false,
      },
    });
    await vi.waitFor(() => expect(chip('On-air story')).toBe('Scheduled'));
    expect(row('On-air story')?.textContent).toContain('Instagram story · when you go live');
  });

  it('locks the on-air story once the show is on air', async () => {
    current = episode({ on_air: true });
    announcements = [announcement('onair')];
    await mount();
    expect(button('Edit on-air story').disabled).toBe(true);
    expect(button('Cancel on-air story').disabled).toBe(true);
    // A pre-announcement nobody set up can't be set up any more.
    expect(row('Pre-announcement')?.textContent).toContain('Instagram feed post · not sent');
    expect(buttons('Pre-announcement')).toEqual([]);
  });

  it('shows a sent post with its link and picture, and no buttons', async () => {
    announcements = [
      announcement('pre', {
        state: 'sent',
        scheduled_at: FR_2310_1800,
        sent_at: FR_2310_1800,
        permalink: 'https://instagram.com/p/abc',
      }),
    ];
    await mount();
    expect(row('Pre-announcement')?.textContent).toContain(
      'Instagram feed post · sent Fr 23.10. · 18:00'
    );
    expect(chip('Pre-announcement')).toBe('Sent');
    const link = row('Pre-announcement')?.querySelector('a') as HTMLAnchorElement;
    expect(link.getAttribute('href')).toBe('https://instagram.com/p/abc');
    expect(link.textContent).toBe('View post');
    expect(buttons('Pre-announcement')).toEqual([]);
    expect(picture('Pre-announcement')).toContain('/api/shows/42/announcements/pre/image.jpg?');
    expect(cardStatus('Social media')).toBe('1 sent');
  });

  it('shows the frame when a picture does not load', async () => {
    announcements = [announcement('pre', { scheduled_at: FR_2310_1800 })];
    await mount();
    row('Pre-announcement')!.querySelector('img')!.dispatchEvent(new Event('error'));
    await nextTick();
    expect(picture('Pre-announcement')).toBeUndefined();
    expect(row('Pre-announcement')?.querySelector('.frame')).toBeTruthy();
  });

  it('links the sent on-air story', async () => {
    current = episode({ on_air: true });
    announcements = [
      announcement('onair', {
        state: 'sent',
        sent_at: FR_2310_1800,
        permalink: 'https://instagram.com/stories/x',
      }),
    ];
    await mount();
    const link = row('On-air story')?.querySelector('a');
    expect(link?.textContent).toBe('View story');
    expect(link?.getAttribute('href')).toBe('https://instagram.com/stories/x');
  });

  it('shows why a post failed', async () => {
    announcements = [announcement('pre', { state: 'failed', error: 'Token expired' })];
    await mount();
    expect(row('Pre-announcement')?.textContent).toContain('Token expired');
    expect(chip('Pre-announcement')).toBe('Failed');
    expect(cardStatus('Social media')).toBe('1 failed');
    expect(buttons('Pre-announcement')).toContain('Edit pre-announcement');
  });

  it('shows a post slot that waits for SoundCloud', async () => {
    announcements = [announcement('post')];
    await mount();
    expect(chip('Post-announcement')).toBe('Waiting for SoundCloud');
    expect(row('Post-announcement')?.textContent).toContain(
      'Instagram feed post · waits for the public track'
    );
    expect(cardStatus('Social media')).toBe('1 waiting');
  });

  it('counts only the slots that are set up', async () => {
    announcements = [
      announcement('pre', {
        state: 'sent',
        scheduled_at: FR_2310_1800,
        sent_at: FR_2310_1800,
        permalink: 'https://instagram.com/p/abc',
      }),
      announcement('onair', { state: 'cancelled' }),
      announcement('post'),
    ];
    await mount();
    expect(chip('On-air story')).toBe('Cancelled');
    expect(cardStatus('Social media')).toBe('1 sent · 1 waiting');
  });

  it('sets up the post-announcement before the show', async () => {
    await mount();
    button('Set up post-announcement').click();
    await vi.waitFor(() => expect(dialog()?.textContent).toContain('Waiting for SoundCloud'));
    expect(dialog()?.querySelector('h2')?.textContent).toBe('Post-announcement');
  });

  it('sets up the post-announcement after the show', async () => {
    current = episode({ date: PAST, episode_status: 'wrapup_open' });
    await mount();
    tab(0).click();
    await settle();
    expect(buttons('Pre-announcement')).toEqual([]);
    button('Set up post-announcement').click();
    await vi.waitFor(() =>
      expect(dialog()?.querySelector('h2')?.textContent).toBe('Post-announcement')
    );
  });

  it('says when an admin has paused all posting', async () => {
    meta = { paused: true, needs_approval: false, services: ON };
    await mount();
    expect(root.querySelector('.promotion .notice')?.textContent).toBe(
      'Posting is paused by the station.'
    );
  });

  it('says Instagram is not available and blocks set-up when an admin turned it off', async () => {
    meta = {
      paused: false,
      needs_approval: false,
      services: { ...ON, instagram: false },
    };
    await mount();
    expect(root.querySelector('.promotion .notice')?.textContent).toBe(
      'Instagram is temporarily not available.'
    );
    for (const label of [
      'Set up pre-announcement',
      'Set up on-air story',
      'Set up post-announcement',
    ]) {
      expect(button(label).disabled).toBe(true);
    }
  });

  it('has no paused note while posting runs', async () => {
    await mount();
    expect(text()).not.toContain('Posting is paused by the station.');
  });

  it('says from which time a moved post was moved', async () => {
    announcements = [
      announcement('pre', {
        scheduled_at: FR_2310_1800,
        requested_at: '2026-10-23T10:00:00Z',
      }),
    ];
    await mount();
    expect(row('Pre-announcement')?.textContent).toContain('Fr 23.10. · 18:00');
    expect(row('Pre-announcement')?.textContent).toContain(
      'Moved from 12:00: the station already posts that day.'
    );
  });

  it('names the day when a post moved to another day', async () => {
    announcements = [
      announcement('pre', {
        scheduled_at: '2026-10-24T10:00:00Z',
        requested_at: FR_2310_1800,
      }),
    ];
    await mount();
    expect(row('Pre-announcement')?.textContent).toContain(
      'Moved from Fr 23.10. · 18:00: the station already posts that day.'
    );
  });

  it('has no moved note when the post kept its time', async () => {
    announcements = [
      announcement('pre', {
        scheduled_at: FR_2310_1800,
        requested_at: '2026-10-23T16:00:00.000Z',
      }),
    ];
    await mount();
    expect(text()).not.toContain('Moved from');
  });
});
describe('Pre-announcement composer', () => {
  const field = (name: string) =>
    [...root.querySelectorAll('label')]
      .find((l) => l.querySelector('.label')?.textContent === name)
      ?.querySelector('input, textarea') as HTMLInputElement;
  const type = (name: string, value: string) => {
    field(name).value = value;
    field(name).dispatchEvent(new Event('input'));
  };
  const radio = (text: string) =>
    [...root.querySelectorAll('label')]
      .find((l) => l.textContent?.trim().startsWith(text))
      ?.querySelector('input') as HTMLInputElement;

  beforeEach(() => {
    current = episode({ description: 'Guest mix with Juno Park.' });
    times = [
      { scheduled_at: '2026-10-23T10:00:00Z', free: false, reason: 'Taken by another show' },
      { scheduled_at: FR_2310_1800, free: true, default: true },
    ];
  });

  async function open(slot = 'pre', action = 'Schedule post') {
    const router = await mount('host', 1, undefined, `/shows/42/announce/${slot}`);
    await vi.waitFor(() => expect(text()).toContain(action));
    return router;
  }

  it('drafts the caption from the episode and schedules the default time', async () => {
    const router = await open();
    expect(field('Caption').value).toBe('Guest mix with Juno Park.');
    expect(radio('Fr 23.10. · 18:00').checked).toBe(true);
    expect(radio('Fr 23.10. · 12:00').disabled).toBe(true);
    expect(text()).toContain('Fr 23.10. · 12:00 · Taken by another show');
    expect(radio('Upload a photo').disabled).toBe(false);
    expect(text()).toContain('After it is posted, the caption cannot be changed.');
    expect(text()).toContain('Uses 1 of 3 announcements for this episode.');

    type('Headline on the image', 'Sunday brunch');
    await nextTick();
    button('Schedule post').click();
    await vi.waitFor(() => expect(router.currentRoute.value.fullPath).toBe('/shows/42'));
    expect(requests.find((r) => r.method === 'PUT')).toEqual({
      url: '/api/shows/42/announcements/pre',
      method: 'PUT',
      body: {
        headline: 'Sunday brunch',
        caption: 'Guest mix with Juno Park.',
        image_source: 'cover',
        layout: 'band',
        collaborator: true,
        extra_collaborators: [],
        user_tags: [],
        scheduled_at: FR_2310_1800,
      },
    });
  });

  it('shows the free times by day, the suggested one dashed', async () => {
    await open();
    const days = [...root.querySelectorAll('.week .day')].map((d) => ({
      day: d.querySelector('legend')?.textContent,
      times: [...d.querySelectorAll('label')].map((l) => l.className.replace('time', '').trim()),
    }));
    expect(days).toEqual([{ day: 'Fr 23.10.', times: ['taken', 'on suggested'] }]);
    // The suggested time is the picked one: no hint needed.
    expect(text()).not.toContain('The dashed time is the suggested one.');
    expect(root.querySelector('.when-line')?.textContent).toBe('Goes out Fr 23.10. · 18:00.');
  });

  it('invites more collaborators and tags people, up to the limits', async () => {
    const router = await open();
    const add = async (name: string, value: string, label: string) => {
      type(name, value);
      await nextTick();
      button(label).click();
      await nextTick();
    };
    const chips = () => [...root.querySelectorAll('.chips li')].map((li) => li.textContent?.trim());
    // @brunchtime is invited already: two more fit.
    await add('More collaborators', '@Juno.Park', 'Invite');
    await add('More collaborators', 'juno.park', 'Invite');
    await add('More collaborators', 'dj_two', 'Invite');
    expect(chips()).toEqual(['@juno.park', '@dj_two']);
    expect(field('More collaborators').disabled).toBe(true);
    await add('Tag people', 'not a handle', 'Tag');
    expect(text()).toContain('Use up to 30 letters, numbers, dots or underscores.');
    await add('Tag people', '@label', 'Tag');
    expect(chips()).toEqual(['@juno.park', '@dj_two', '@label']);

    (root.querySelector('[aria-label="Remove @dj_two"]') as HTMLButtonElement).click();
    await nextTick();
    expect(chips()).toEqual(['@juno.park', '@label']);
    expect(field('More collaborators').disabled).toBe(false);

    type('Headline on the image', 'Sunday brunch');
    await nextTick();
    button('Schedule post').click();
    await vi.waitFor(() => expect(router.currentRoute.value.fullPath).toBe('/shows/42'));
    expect(requests.find((r) => r.method === 'PUT')?.body).toMatchObject({
      collaborator: true,
      extra_collaborators: ['juno.park'],
      user_tags: ['label'],
    });
  });

  it('opens a set-up slot with its people', async () => {
    announcements = [
      announcement('pre', {
        scheduled_at: FR_2310_1800,
        extra_collaborators: ['juno.park'],
        user_tags: ['label'],
      }),
    ];
    await open();
    expect([...root.querySelectorAll('.chips li')].map((li) => li.textContent?.trim())).toEqual([
      '@juno.park',
      '@label',
    ]);
    expect(
      (root.querySelector('img.preview') as HTMLImageElement | null)?.getAttribute('alt') ??
        'Preview · 4:5 post by moafunk_radio and brunchtime and juno.park'
    ).toBe('Preview · 4:5 post by moafunk_radio and brunchtime and juno.park');
  });

  it('keeps the fields locked until its data is loaded, so nothing typed is lost', async () => {
    let release!: () => void;
    seriesGate = new Promise((resolve) => (release = resolve));
    await mount('host', 1, undefined, '/shows/42/announce/pre');
    await vi.waitFor(() => expect(text()).toContain('Schedule post'));
    // The day groups of the time picker are locked through their parent.
    const fieldsets = [...root.querySelectorAll<HTMLFieldSetElement>('fieldset.group')];
    expect(fieldsets.length).toBeGreaterThan(0);
    expect(fieldsets.every((f) => f.disabled)).toBe(true);
    expect(button('Schedule post').disabled).toBe(true);

    release();
    await settle();
    expect(fieldsets.some((f) => f.disabled)).toBe(false);
    expect(field('Caption').value).toBe('Guest mix with Juno Park.');
  });

  it('refuses a headline over 18 and a caption over 600 characters', async () => {
    await open();
    type('Headline on the image', 'A headline too long');
    await nextTick();
    expect(text()).toContain('Headline: max 18 characters.');
    expect(button('Schedule post').disabled).toBe(true);

    type('Headline on the image', 'Sunday brunch');
    type('Caption', 'x'.repeat(601));
    await nextTick();
    expect(text()).toContain('Caption: max 600 characters.');
    expect(button('Schedule post').disabled).toBe(true);

    type('Caption', 'x'.repeat(600));
    await nextTick();
    expect(button('Schedule post').disabled).toBe(false);
  });

  it('shows why a time is taken when the backend refuses it', async () => {
    saveError = Response.json({ error: 'The station already posts that day.' }, { status: 409 });
    const router = await open();
    type('Headline on the image', 'Sunday brunch');
    await nextTick();
    button('Schedule post').click();
    await vi.waitFor(() =>
      expect(root.querySelector('[role="alert"]')?.textContent).toBe(
        'The station already posts that day.'
      )
    );
    expect(router.currentRoute.value.fullPath).toBe('/shows/42/announce/pre');
  });

  it('opens a set-up slot with its own values', async () => {
    announcements = [
      announcement('pre', { headline: 'Brunch!', caption: 'Mine', scheduled_at: FR_2310_1800 }),
      announcement('onair'),
    ];
    await open();
    expect(field('Headline on the image').value).toBe('Brunch!');
    expect(field('Caption').value).toBe('Mine');
    expect(text()).toContain('Uses 2 of 3 announcements for this episode.');
  });

  it('lets the post slot wait for SoundCloud without a time', async () => {
    times = [];
    await open('post');
    expect(text()).toContain('Post-announcement');
    expect(text()).toContain('Waiting for SoundCloud');
    type('Headline on the image', 'Listen back');
    await nextTick();
    button('Schedule post').click();
    await vi.waitFor(() => expect(requests.find((r) => r.method === 'PUT')).toBeDefined());
    const put = requests.find((r) => r.method === 'PUT')!;
    expect(put.url).toBe('/api/shows/42/announcements/post');
    expect(put.body).not.toHaveProperty('scheduled_at');
  });

  it('posts now without a time when Post now is chosen', async () => {
    const router = await open();
    expect(radio('Post now').checked).toBe(false);
    expect(text()).not.toContain('Post now sends it within a minute');
    radio('Post now').click();
    await nextTick();
    expect(text()).toContain('Post now sends it within a minute');
    type('Headline on the image', 'Sunday brunch');
    await nextTick();
    button('Schedule post').click();
    await vi.waitFor(() => expect(router.currentRoute.value.fullPath).toBe('/shows/42'));
    const put = requests.find((r) => r.method === 'PUT')!;
    expect(put.url).toBe('/api/shows/42/announcements/pre');
    expect(put.body).toMatchObject({ headline: 'Sunday brunch', send_now: true });
    expect(put.body).not.toHaveProperty('scheduled_at');
  });

  it('offers Post now when no time is free', async () => {
    times = [];
    await open();
    expect(text()).toContain('No free time left.');
    expect(radio('Post now')).toBeTruthy();
  });

  it('opens a sent-now row with Post now selected and asks for approval', async () => {
    meta = { paused: false, needs_approval: true, services: ON };
    announcements = [
      announcement('pre', { state: 'pending_approval', send_now: true, headline: 'Now!' }),
    ];
    await open('pre', 'Send for approval');
    expect(radio('Post now').checked).toBe(true);
    expect(radio('Fr 23.10. · 18:00').checked).toBe(false);
    button('Send for approval').click();
    await vi.waitFor(() => expect(requests.find((r) => r.method === 'PUT')).toBeDefined());
    expect(requests.find((r) => r.method === 'PUT')!.body).toMatchObject({ send_now: true });
  });

  it('has no Post now in the post slot', async () => {
    await open('post');
    expect(radio('Post now')).toBeUndefined();
  });

  it('says when posting is paused and when an admin checks the post first', async () => {
    meta = { paused: true, needs_approval: true, services: ON };
    await open('pre', 'Send for approval');
    expect(text()).toContain('Posting is paused by the station.');
    expect(text()).toContain('An admin checks this post first.');
    expect(button('Send for approval')).toBeTruthy();
  });

  it('blocks scheduling while an admin has Instagram turned off', async () => {
    meta = { paused: false, needs_approval: false, services: { ...ON, instagram: false } };
    await open();
    expect(text()).toContain('Instagram is temporarily not available.');
    expect(button('Schedule post').disabled).toBe(true);
  });

  it('has no paused or approval note by default', async () => {
    await open();
    expect(text()).not.toContain('Posting is paused by the station.');
    expect(text()).not.toContain('An admin checks this post first.');
  });

  describe('photo upload', () => {
    const photo = () => root.querySelector('input[type="file"]') as HTMLInputElement;
    const pick = async (file: File) => {
      Object.defineProperty(photo(), 'files', { value: [file], configurable: true });
      photo().dispatchEvent(new Event('change'));
      await settle();
    };
    const jpeg = (size = 1000, type = 'image/jpeg') =>
      new File([new Uint8Array(size)], 'brunch.jpg', { type });

    async function choose() {
      await open();
      radio('Upload a photo').click();
      await nextTick();
      expect(photo().accept).toBe('image/jpeg,image/png');
    }

    it('uploads a picked photo and schedules the post with its key', async () => {
      vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'], shouldAdvanceTime: true });
      const router = await open();
      radio('Upload a photo').click();
      await nextTick();
      expect(button('Schedule post').disabled).toBe(true);
      await pick(jpeg());
      expect((uploaded?.get('file') as File).name).toBe('brunch.jpg');
      expect(text()).toContain('Photo uploaded.');
      type('Headline on the image', 'Sunday brunch');
      await nextTick();
      vi.advanceTimersByTime(500);
      await nextTick();
      const src = (root.querySelector('img.preview') as HTMLImageElement).getAttribute('src')!;
      const query = new URLSearchParams(src.split('?')[1]);
      expect(query.get('image_source')).toBe('upload');
      expect(query.get('image_key')).toBe('shows/42/announcements/uploads/a.jpg');
      vi.useRealTimers();

      button('Schedule post').click();
      await vi.waitFor(() => expect(router.currentRoute.value.fullPath).toBe('/shows/42'));
      expect(requests.find((r) => r.method === 'PUT')?.body).toMatchObject({
        image_source: 'upload',
        image_key: 'shows/42/announcements/uploads/a.jpg',
      });
    });

    it('refuses a file that is not a JPEG or PNG', async () => {
      await choose();
      await pick(jpeg(1000, 'image/gif'));
      expect(text()).toContain('Use a JPEG or PNG photo.');
      expect(requests.some((r) => r.url.endsWith('/uploads'))).toBe(false);
    });

    it('refuses a photo over 10 MB', async () => {
      await choose();
      await pick(jpeg(10 * 1024 * 1024 + 1));
      expect(text()).toContain('The photo is too big. Max 10 MB.');
      expect(requests.some((r) => r.url.endsWith('/uploads'))).toBe(false);
    });

    it('shows why the upload failed', async () => {
      photoUpload = Response.json({ error: 'Not an image' }, { status: 400 });
      await choose();
      await pick(jpeg());
      expect(text()).toContain('Not an image');
      expect(button('Schedule post').disabled).toBe(true);
    });
  });
});

describe('who sees what', () => {
  it("keeps today's page for admins", async () => {
    await mount('admin', 9);
    await vi.waitFor(() => expect(text()).toContain("Today's show page"));
  });

  it("shows another host's episode as the same page read-only: tabs, no buttons", async () => {
    current = episode({ episode_status_label: 'Upcoming' });
    announcements = [
      announcement('pre', {
        state: 'sent',
        scheduled_at: FR_2310_1800,
        sent_at: FR_2310_1800,
        permalink: 'https://instagram.com/p/abc',
        caption: 'Secret caption',
      }),
      announcement('onair'),
    ];
    const router = await mount('host', 7);
    await vi.waitFor(() => expect(root.querySelector('h1')?.textContent).toBe('Brunchtime #31'));
    expect(root.querySelector('.when')?.textContent).toBe('So 04.01. · 12:00–14:00');
    for (const part of ['Upcoming', 'Host: lena', 'Read only · only its hosts can edit']) {
      expect(text()).toContain(part);
    }
    expect(frames().map((f) => f.title)).toEqual(['Social media', 'Recording & SoundCloud']);
    expect(text()).not.toContain("Today's show page");
    // The same four tab tiles as the owner's page; the tabs are the only buttons.
    expect(tabs()).toHaveLength(4);
    // It opens on the current step, as the owner's page does.
    expect(tabs()[1]).toMatchObject({ state: 'now', selected: true });
    // The Studio card read-only: no input asked for, nothing checked yet.
    expect(isShown(studioView())).toBe(true);
    expect(studioView()?.textContent).toContain('Audio inputNot chosen yet');
    expect(studioView()?.textContent).toContain('MusicNot checked yet');
    // Show time: the live panel without values.
    expect(panel()).toContain('Not on air · --:--:--');
    expect(root.querySelectorAll('button:not([role="tab"]), input, select, textarea')).toHaveLength(
      0
    );
    expect(text()).not.toContain('Edit');
    expect(root.querySelector('a')?.getAttribute('href')).toBe('/shows/brunchtime');
    expect(router.currentRoute.value.fullPath).toBe('/shows/42');
    // Social media from the status endpoint: the link of the sent post, no caption.
    await vi.waitFor(() =>
      expect(root.querySelector('.promotion a')?.getAttribute('href')).toBe(
        'https://instagram.com/p/abc'
      )
    );
    expect(cardStatus('Social media')).toBe('1 sent · 1 scheduled');
    // Only the sent post's picture; the armed story stays with its hosts.
    expect(root.querySelector('.promotion .pre img')?.getAttribute('src')).toContain(
      '/api/shows/42/announcements/pre/image.jpg?'
    );
    expect(root.querySelector('.promotion .onair img')).toBeNull();
    expect(text()).not.toContain('Secret caption');
    expect(requests.map((r) => r.url)).not.toContain('/api/shows/42/announcements');
    expect(requests.map((r) => r.url)).not.toContain('/api/shows/42/announcements/meta');
    // No tested or untested claim for another host's soundcheck.
    expect(studioView()?.textContent).not.toMatch(/tested/i);
  });

  it('shows another host on air with the public stream link', async () => {
    current = episode({ on_air: true });
    await mount('host', 7);
    await vi.waitFor(() => expect(shown('.on-air')).toBe(true));
    expect(root.querySelector('.onair-badge')?.textContent).toBe('On air');
    const listen = root.querySelector('.on-air .listen') as HTMLAnchorElement;
    expect(listen.textContent?.trim()).toBe('Listen live');
    expect(listen.getAttribute('href')).toBe('https://live.moafunk.de');
    expect(text()).not.toContain('Back to On Air');
    expect(root.querySelectorAll('button:not([role="tab"])')).toHaveLength(0);
  });

  it("shows what another host's browser saved: the soundcheck and the live panel, read-only", async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    try {
      // 12:42 in Berlin; on air since 12:01, the last save 10 s ago.
      vi.setSystemTime(new Date(`${FUTURE}T11:42:00Z`));
      const since = Date.parse(`${FUTURE}T11:01:00Z`);
      current = episode({
        on_air: true,
        studio_log: {
          soundcheck: {
            at: since - 60_000,
            input: 'Focusrite USB',
            checks: [
              { id: 'music', name: 'Music', state: 'ok', detail: 'Good · peak −9 dB' },
              { id: 'mic', name: 'Mic', state: 'skipped', detail: 'Not in this show' },
              { id: 'server', name: 'Server', state: 'ok', detail: 'Received your audio' },
              { id: 'return', name: 'Return feed', state: 'ok', detail: 'Your audio comes back' },
            ],
          },
          on_air: {
            at: Date.now() - 10_000,
            since,
            ended: false,
            input: 'Focusrite USB',
            gain: 1.2,
            connection: 'stable',
            upload_kbps: 201,
            buffer_s: 0.4,
            rtt_ms: 38,
            history: [190, 201],
            target_kbps: 192,
            quality: 'auto',
            moments: [{ at: 750, markedAt: since + 750_000 }],
          },
        },
        listener_samples: [
          { at: since + 60_000, count: 4 },
          { at: since + 120_000, count: 9 },
          { at: Date.now() - 5_000, count: 7 },
        ],
      });
      await mount('host', 7);
      expect(root.querySelector('.on-air-clock')?.textContent).toBe('00:41:00');
      expect(panel()).toContain('Input Focusrite USB');
      expect(panel()).toContain('Input gain120 %');
      expect(panel()).toContain('Upload201 kbps');
      expect(panel()).toContain('Auto · 192 kbps');
      expect(root.querySelector('.bar-listeners')?.textContent).toBe('7 listening');
      expect(panel()).toContain('Peak9');
      expect(panel()).toContain('00:12:30');
      // Read-only: no fader, quality buttons, reply or Mark moment.
      expect(
        root.querySelectorAll('button:not([role="tab"]), input, select, textarea')
      ).toHaveLength(0);
      // The Soundcheck tab: what the host went live with.
      expect(badges(1)).toEqual([
        'Music: Good · peak −9 dB',
        'Mic: Not in this show',
        'Server: Received your audio',
        'Return feed: Your audio comes back',
      ]);
      tab(1).click();
      await settle();
      expect(isShown(studioView())).toBe(true);
      expect(studioView()?.textContent).toContain('Soundcheck · saved at Go Live');
      expect(studioView()?.textContent).toContain('Audio inputFocusrite USB');
      expect(studioView()?.textContent).toContain('MicNot in this show');

      // No save for a minute: the bar says the values may be old.
      vi.setSystemTime(new Date(`${FUTURE}T11:43:00Z`));
      tab(2).click();
      await new Promise((resolve) => setTimeout(resolve, 1100));
      await settle();
      expect(panel()).toContain('Last update');
    } finally {
      vi.useRealTimers();
    }
  });

  it('keeps the values from the end after the show', async () => {
    const since = Date.parse(`${PAST}T11:01:00Z`);
    current = episode({
      date: PAST,
      episode_status: 'finished',
      studio_log: {
        on_air: {
          at: since + 7_000_000,
          since,
          ended: true,
          input: 'Screen audio',
          gain: 1,
          connection: 'tight',
          upload_kbps: 150,
          buffer_s: 2.5,
          rtt_ms: 80,
          history: [150],
          target_kbps: 128,
          quality: 128,
          moments: [],
        },
      },
      listener_samples: [{ at: since + 60_000, count: 3 }],
    });
    await mount('host', 7);
    tab(2).click();
    await settle();
    expect(root.querySelector('.onair-badge')?.textContent).toBe('Aired');
    expect(root.querySelector('.on-air-clock')?.textContent).toBe('01:56:40');
    expect(panel()).toContain('Input Screen audio');
    expect(panel()).toContain('128 kbps');
    expect(panel()).toContain('No moments marked.');
    // Nobody listens after the show; the peak stays.
    expect(root.querySelector('.bar-listeners')).toBeNull();
    expect(panel()).toContain('Peak3');
  });

  it.each([
    [true, 'Public track · Open on SoundCloud', true],
    [false, 'Private track', false],
  ])(
    "links another host's SoundCloud track only when it is public (%s)",
    async (isPublic, line, linked) => {
      current = episode({
        date: PAST,
        episode_status: 'finished',
        soundcloud_url: 'https://soundcloud.com/radio-moafunk/brunchtime-31',
        soundcloud_public: isPublic,
        latest_recording: {
          id: 1,
          version: 'v1',
          status: 'raw',
          duration_ms: 7120000,
          download_url: 'https://r2.example/v1.mp3',
        },
      });
      await mount('host', 7);
      await vi.waitFor(() => expect(text()).toContain(line));
      expect(!!root.querySelector('a[href^="https://soundcloud.com"]')).toBe(linked);
      // The recording file itself stays with the episode's hosts.
      expect(root.querySelector('.audio-player')).toBeNull();
      // The same tabs as the owner's page: a finished episode opens on Recording.
      expect(tabs().map((t) => t.state)).toEqual(['done', 'done', 'done', 'done']);
      expect(tabs()[3].selected).toBe(true);
      expect(shownCards()).toEqual(['Recording & SoundCloud']);
      expect(text()).not.toContain('Publish episode');
    }
  );
});
describe('flow', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('Episode matches the flow', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date('2026-10-20T10:00:00Z'));
    current = episode({ date: '2026-10-25' });
    announcements = [announcement('pre', { scheduled_at: FR_2310_1800 }), announcement('onair')];
    await mount();
    await expectFlow('episode', root);
  });

  it('Pre-announcement matches the flow', async () => {
    current = episode({ date: '2026-10-25', description: 'Guest mix with Juno Park.' });
    times = [
      { scheduled_at: FR_2310_1800, free: true, default: true },
      { scheduled_at: '2026-10-24T10:00:00Z', free: true },
      { scheduled_at: '2026-10-24T16:00:00Z', free: true },
    ];
    await mount('host', 1, undefined, '/shows/42/announce/pre');
    await vi.waitFor(() => expect(text()).toContain('Fr 23.10. · 18:00'));
    await expectFlow('announce', root);
  });

  it('Episode · Pre-recorded matches the flow', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date('2026-11-01T10:00:00Z'));
    current = episode({
      date: '2026-11-08',
      stream_mode: 'prerecorded',
      prerecorded_filename: 'brunchtime-0811.mp3',
      prerecorded_url: 'https://r2.example/brunchtime-0811.mp3',
      prerecorded_duration_secs: 7120,
      prerecorded_confirmed_at: '2026-11-01T10:00:00Z',
    });
    await mount();
    await expectFlow('episode-pre', root);
  });

  it('Episode (other host) matches the flow', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date('2026-10-20T10:00:00Z'));
    current = episode({
      title: 'Night Shift',
      date: '2026-10-27',
      start_time: '20:00',
      end_time: '22:00',
      series_slug: 'night-shift',
      series_name: 'Night Shift',
      series_description: 'Late-night club sounds, every Tuesday from 20:00.',
      host_user_id: 7,
      created_by: 7,
      host_username: 'jonas',
      episode_status_label: 'Upcoming',
    });
    await mount();
    await expectFlow('episode-view', root);
  });

  it('Episode · Pre-recorded, no file matches the flow', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date('2026-11-01T10:00:00Z'));
    current = episode({
      date: '2026-11-08',
      title: 'Brunchtime #32',
      description: 'Guest mix with Juno Park.',
      stream_mode: 'prerecorded',
    });
    await mount();
    await expectFlow('episode-pre-nofile', root);
  });
});
