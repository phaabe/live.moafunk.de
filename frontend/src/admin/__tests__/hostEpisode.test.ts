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

import HostEpisodePage from '../pages/HostEpisodePage.vue';

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

let app: App | undefined;
let root: HTMLDivElement;
let current: ShowDetail;
let requests: { url: string; method: string; body: unknown }[];

beforeEach(() => {
  current = episode();
  requests = [];
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
      if (url === '/api/my-show')
        return Response.json({ assigned: true, shows: [{ ...current, artists: [] }] });
      throw new Error(`Unexpected API request: ${method} ${url}`);
    })
  );
});

afterEach(() => {
  app?.unmount();
  app = undefined;
  root.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

async function mount(
  role: User['role'] = 'host',
  userId = 1,
  state?: Record<string, number>
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
      { path: '/shows/:slug', component: stub('Show page') },
      { path: '/stream/live', component: stub('Studio') },
    ],
  });
  const pinia = createPinia();
  app = createApp({ render: () => h(RouterView) });
  app.use(pinia).use(router);
  useAuthStore(pinia).user = { id: userId, username: 'lena', role };
  await router.push(state ? { path: '/shows/42', state } : '/shows/42');
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

const LEFT_OUT = [
  'Announcements',
  'Create guest link',
  'SoundCloud',
  'Instagram',
  'Social Media',
  'Pre-recorded ▾',
];

describe('Episode', () => {
  it('shows the flow screen for an upcoming live episode', async () => {
    await mount();
    for (const part of [
      '< Brunchtime',
      'So 04.01. · Brunchtime',
      'Upcoming · Live · 12:00–14:00',
      'Next step',
      'Test your audio before air time. The Studio opens 30 min before the start.',
      'Open Studio',
      'Details',
      'Title',
      'Cancel booking',
    ]) {
      expect(text()).toContain(part);
    }
    for (const part of LEFT_OUT) expect(text()).not.toContain(part);
    expect(button('Open Studio').disabled).toBe(true);
    expect(root.querySelector('a')?.getAttribute('href')).toBe('/shows/brunchtime');
  });

  it('opens the Studio with this episode selected from 30 min before the start', async () => {
    current = episode({ on_air: true });
    const router = await mount();
    expect(button('Open Studio').disabled).toBe(false);
    button('Open Studio').click();
    await vi.waitFor(() => expect(router.currentRoute.value.fullPath).toBe('/stream/live?show=42'));
  });

  it('saves the title', async () => {
    await mount();
    const input = root.querySelector('input[type="text"]') as HTMLInputElement;
    expect(input.value).toBe('Brunchtime #31');
    input.value = 'Brunchtime #31 · Guest special';
    input.dispatchEvent(new Event('input'));
    input.dispatchEvent(new Event('change'));
    await vi.waitFor(() =>
      expect(requests.find((r) => r.method === 'PUT')?.body).toEqual({
        title: 'Brunchtime #31 · Guest special',
      })
    );
  });

  it('asks once before it cancels the booking, then opens the Show page', async () => {
    const router = await mount();
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

  it('shows the flow screen and waits for the end before the wrap-up', async () => {
    current = prerecorded();
    await mount();
    for (const part of [
      '< Brunchtime',
      'Pre-recorded & confirmed · plays at 12:00',
      'Next step',
      'Nothing to do. The set plays at air time. Wrap-up opens after 14:00.',
      'brunchtime-0811.mp3 · 1:58:40',
      'Replace file',
      'Open Wrap-up',
    ]) {
      expect(text()).toContain(part);
    }
    for (const part of [...LEFT_OUT, 'Cancel booking', 'Open Studio']) {
      expect(text()).not.toContain(part);
    }
    expect(button('Open Wrap-up').disabled).toBe(true);
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

describe('Wrap-up and Done', () => {
  it('opens on the wrap-up of an ended episode and publishes it', async () => {
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
    expect(text()).toContain('< Episode');
    expect(text()).toContain('Wrap-up');
    expect(text()).toContain('Recording ready · 1:58:40');
    for (const part of LEFT_OUT) expect(text()).not.toContain(part);

    button('Publish').click();
    await vi.waitFor(() => expect(text()).toContain('Episode published'));
    expect(text()).not.toContain('SoundCloud');
    button('Back to Schedule').click();
    await vi.waitFor(() => expect(router.currentRoute.value.fullPath).toBe('/schedule'));
  });

  it('switches between the wrap-up and the episode on one path', async () => {
    current = episode({ date: PAST, episode_status: 'wrapup_open' });
    await mount();
    button('< Episode').click();
    await nextTick();
    expect(text()).toContain('Upcoming · Live');
  });

  it('waits while the recording converts', async () => {
    current = episode({
      date: PAST,
      episode_status: 'wrapup_open',
      latest_recording: { id: 1, version: 'v1', status: 'raw' },
    });
    await mount();
    expect(text()).toContain('Recording processing…');
    expect(button('Publish').disabled).toBe(true);
  });

  it.each([
    [
      'a failed recording',
      { id: 1, version: 'v1', status: 'failed' as const, error_message: 'Shorter than 1 minute' },
      'No usable recording · Shorter than 1 minute',
    ],
    ['no recording long after the end', undefined, 'No usable recording · Nothing was recorded.'],
  ])('lets %s be published', async (_name, latest_recording, pill) => {
    current = episode({ date: PAST, episode_status: 'wrapup_open', latest_recording });
    await mount();
    expect(text()).toContain(pill);
    expect(button('Publish').disabled).toBe(false);
  });

  it('opens the wrap-up after End Episode, while the recording is still saved', async () => {
    // Ended before its scheduled end, and the session is still closing.
    current = episode({ on_air: true });
    await mount('host', 1, { episodeEnded: 42 });
    expect(text()).toContain('Wrap-up');
    expect(text()).toContain('Recording processing…');
    expect(button('Publish').disabled).toBe(true);
  });

  it('counts the recording wait from an early End Episode', async () => {
    // Ended 20 min ago, long before its scheduled end; no recording was saved.
    current = episode({
      episode_status: 'wrapup_open',
      live_ended_at: new Date(Date.now() - 20 * 60 * 1000).toISOString(),
    });
    await mount('host', 1, { episodeEnded: 42 });
    expect(text()).toContain('No usable recording · Nothing was recorded.');
    expect(button('Publish').disabled).toBe(false);
  });

  it('still waits right after an early End Episode', async () => {
    current = episode({
      episode_status: 'wrapup_open',
      live_ended_at: new Date(Date.now() - 60 * 1000).toISOString(),
    });
    await mount('host', 1, { episodeEnded: 42 });
    expect(text()).toContain('Recording processing…');
    expect(button('Publish').disabled).toBe(true);
  });

  it('says when "Record this show" was off', async () => {
    current = episode({ episode_status: 'wrapup_open', live_recorded: false });
    await mount('host', 1, { episodeEnded: 42 });
    expect(text()).toContain('No usable recording · Record this show was off.');
    expect(button('Publish').disabled).toBe(false);
  });

  it('shows Done for a published episode', async () => {
    current = episode({ date: PAST, episode_status: 'finished' });
    await mount();
    expect(text()).toContain('Episode published');
    expect(text()).toContain('Back to Schedule');
  });
});

describe('who sees what', () => {
  it("keeps today's page for admins and for another host's show", async () => {
    await mount('admin', 9);
    await vi.waitFor(() => expect(text()).toContain("Today's show page"));
    app?.unmount();
    await mount('host', 7);
    await vi.waitFor(() => expect(text()).toContain("Today's show page"));
  });
});

describe('flow', () => {
  afterEach(() => {
    vi.useRealTimers();
  });
  const recordingReady: ShowDetail['latest_recording'] = {
    id: 1,
    version: 'v1',
    status: 'raw',
    duration_ms: 7120000,
    download_url: 'https://r2.example/v1.mp3',
  };

  it('Episode matches the flow', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date('2026-10-20T10:00:00Z'));
    current = episode({ date: '2026-10-25' });
    await mount();
    await expectFlow('episode', root);
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

  it('Wrap-up matches the flow', async () => {
    current = episode({
      date: PAST,
      episode_status: 'wrapup_open',
      latest_recording: recordingReady,
    });
    await mount();
    await expectFlow('wrapup', root);
  });

  it('Published matches the flow', async () => {
    current = episode({ date: PAST, episode_status: 'finished', latest_recording: recordingReady });
    await mount();
    await expectFlow('done', root);
  });
});
