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

// jsdom cannot decode audio; the upload goes on without peaks.
vi.mock('../../pages/waveformExtractor', () => ({
  extractWaveformPeaksJson: async () => undefined,
}));

import HostEpisodePage from '../pages/HostEpisodePage.vue';
import HostAnnouncePage from '../pages/HostAnnouncePage.vue';
import type { Announcement, AnnouncementSlot, AnnouncementTime } from '../api';

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
    sent_at: null,
    permalink: null,
    error: null,
    send_now: false,
    ...overrides,
  };
}

/** Fr 23.10. · 18:00 Berlin (summer time). */
const FR_2310_1800 = '2026-10-23T16:00:00Z';
/** Mo 26.10. · 18:00 Berlin (winter time). */
const MO_2610_1800 = '2026-10-26T17:00:00Z';
/** Mi 28.10. · 18:00 Berlin: the post default, 3 days after So 25.10. */
const MI_2810_1800 = '2026-10-28T17:00:00Z';

let app: App | undefined;
let root: HTMLDivElement;
let current: ShowDetail;
let announcements: Announcement[];
let times: AnnouncementTime[];
let saveError: Response | undefined;
let requests: { url: string; method: string; body: unknown }[];
let uploaded: FormData | undefined;
let soundcloud: Response;
let meta: { paused: boolean; needs_approval: boolean };
let photoUpload: Response | undefined;

beforeEach(() => {
  current = episode();
  announcements = [];
  times = [];
  saveError = undefined;
  requests = [];
  uploaded = undefined;
  meta = { paused: false, needs_approval: false };
  photoUpload = undefined;
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
      if (url === '/api/series/brunchtime')
        return Response.json({ ...BRUNCHTIME_SERIES, episodes: [] });
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
      if (url === '/api/shows/42/announcements') return Response.json(announcements);
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
    // The Promotion card names Instagram and the SoundCloud track.
    for (const part of LEFT_OUT.filter((p) => p !== 'SoundCloud' && p !== 'Instagram')) {
      expect(text()).not.toContain(part);
    }
    expect(button('Open Studio').disabled).toBe(true);
    expect(root.querySelector('a')?.getAttribute('href')).toBe('/shows/brunchtime');
  });

  it('opens the Studio with this episode selected from 30 min before the start', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    try {
      // 11:45 in Berlin on the show day: 15 min before the 12:00 start.
      vi.setSystemTime(new Date(`${FUTURE}T10:45:00Z`));
      const router = await mount();
      expect(button('Open Studio').disabled).toBe(false);
      button('Open Studio').click();
      await vi.waitFor(() =>
        expect(router.currentRoute.value.fullPath).toBe('/stream/live?show=42')
      );
    } finally {
      vi.useRealTimers();
    }
  });

  it('while on air, says so and leads back to On Air', async () => {
    current = episode({ on_air: true });
    const router = await mount();
    expect(text()).toContain('On air · until 14:00');
    expect(text()).not.toContain('Upcoming · Live');
    expect(text()).not.toContain('Open Studio');
    button('Back to On Air').click();
    await vi.waitFor(() =>
      expect(router.currentRoute.value.fullPath).toBe('/stream/on-air?show=42')
    );
  });

  it('saves the title', async () => {
    await mount();
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
  });

  it('moves the episode to a free slot and switches its mode with one Save', async () => {
    await mount();
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

  it('locks date, time and mode once the episode has started', async () => {
    current = episode({ date: PAST, on_air: true });
    await mount();
    for (const el of root.querySelectorAll('select, input[type="radio"]')) {
      expect((el as HTMLInputElement).disabled).toBe(true);
    }
    expect((root.querySelector('input[type="text"]') as HTMLInputElement).disabled).toBe(false);
  });

  it('asks for the set of a pre-recorded episode that has no file yet', async () => {
    current = episode({ stream_mode: 'prerecorded' });
    const router = await mount();
    expect(text()).toContain('Pre-recorded · no file yet · plays at 12:00');
    expect(text()).toContain('Upload your set before air time.');
    button('Upload your set').click();
    await vi.waitFor(() =>
      expect(router.currentRoute.value.fullPath).toBe('/shows/brunchtime/book?mode=prerecorded')
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
    expect(button('Upload to SoundCloud').disabled).toBe(true);
    pick('brunchtime-2510.mp3');
    await vi.waitFor(() => expect(text()).toContain('brunchtime-2510.mp3'));
    expect((uploaded?.get('file') as File).name).toBe('brunchtime-2510.mp3');
    // Reloaded, so the button sees the new recording.
    expect(requests.filter((r) => r.url === '/api/shows/42').length).toBe(2);
    expect(button('Upload to SoundCloud').disabled).toBe(false);
  });

  it('sends the recording to SoundCloud and shows the track link', async () => {
    current = episode({ recording_filename: 'set.mp3', recording_url: 'https://r2/set.mp3' });
    await mount();
    button('Upload to SoundCloud').click();
    await vi.waitFor(() =>
      expect(root.querySelector('a[href^="https://soundcloud.com"]')?.textContent).toBe(
        'https://soundcloud.com/radio-moafunk/brunchtime-31'
      )
    );
    expect(requests.filter((r) => r.url.endsWith('/soundcloud/upload'))).toHaveLength(1);
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

describe('Episode · Promotion card', () => {
  const row = (name: string) =>
    [...root.querySelectorAll('.promotion li')].find((li) => li.textContent?.includes(name));
  const onair = () => root.querySelector('.promotion input[type="checkbox"]') as HTMLInputElement;
  const buttons = () => [...root.querySelectorAll('.promotion button')].map((b) => b.textContent);

  it('shows a scheduled pre-announcement with Edit and Cancel', async () => {
    announcements = [announcement('pre', { scheduled_at: FR_2310_1800 })];
    const router = await mount();
    expect(text()).toContain('Promotion · Instagram');
    expect(text()).toContain('@brunchtime is invited as collaborator.');
    expect(row('Pre')?.textContent).toContain('Scheduled · Fr 23.10. · 18:00');
    button('Edit pre-announcement').click();
    await vi.waitFor(() =>
      expect(router.currentRoute.value.fullPath).toBe('/shows/42/announce/pre')
    );
  });

  it('cancels the pre-announcement after one confirm', async () => {
    announcements = [announcement('pre', { scheduled_at: FR_2310_1800 })];
    await mount();
    vi.spyOn(window, 'confirm').mockReturnValueOnce(true);
    button('Cancel pre-announcement').click();
    await vi.waitFor(() => expect(row('Pre')?.textContent).toContain('Cancelled'));
    expect(requests.find((r) => r.method === 'DELETE')?.url).toBe(
      '/api/shows/42/announcements/pre'
    );
    expect(buttons()).toContain('Set up pre-announcement');
  });

  it('offers to set up a pre slot that is not set up', async () => {
    await mount();
    expect(row('Pre')?.textContent).toContain('Not set up');
    expect(buttons()).toEqual(['Set up pre-announcement']);
    expect(row('Post')?.textContent).toContain(
      'Set up in Wrap-up when the SoundCloud track is public'
    );
  });

  it('switches the armed on-air story off and on again', async () => {
    announcements = [announcement('onair')];
    await mount();
    expect(onair().checked).toBe(true);
    onair().click();
    await vi.waitFor(() =>
      expect(requests.find((r) => r.method === 'DELETE')?.url).toBe(
        '/api/shows/42/announcements/onair'
      )
    );
    await vi.waitFor(() => expect(onair().disabled).toBe(false));
    expect(onair().checked).toBe(false);
    onair().click();
    await vi.waitFor(() =>
      expect(requests.find((r) => r.method === 'PUT')?.body).toMatchObject({
        image_source: 'cover',
        collaborator: false,
      })
    );
    await vi.waitFor(() => expect(onair().checked).toBe(true));
  });

  it('unchecks the on-air switch again when switching it on is refused', async () => {
    saveError = Response.json({ error: 'Refused' }, { status: 400 });
    await mount();
    expect(onair().checked).toBe(false);
    onair().click();
    await vi.waitFor(() => expect(document.body.textContent).toContain('Refused'));
    expect(onair().checked).toBe(false);
  });

  it('locks the on-air switch while on air', async () => {
    current = episode({ on_air: true });
    announcements = [announcement('onair')];
    await mount();
    expect(onair().disabled).toBe(true);
  });

  it('shows a sent post with its link and no Edit', async () => {
    announcements = [
      announcement('pre', {
        state: 'sent',
        scheduled_at: FR_2310_1800,
        sent_at: FR_2310_1800,
        permalink: 'https://instagram.com/p/abc',
      }),
    ];
    await mount();
    expect(row('Pre')?.textContent).toContain('Sent · Fr 23.10. · 18:00');
    expect(root.querySelector('.promotion a')?.getAttribute('href')).toBe(
      'https://instagram.com/p/abc'
    );
    expect(buttons()).toEqual([]);
  });

  it('shows why a post failed', async () => {
    announcements = [announcement('pre', { state: 'failed', error: 'Token expired' })];
    await mount();
    expect(row('Pre')?.textContent).toContain('Failed · Token expired');
    expect(buttons()).toContain('Edit pre-announcement');
  });

  it('shows a post slot that waits for SoundCloud', async () => {
    announcements = [announcement('post')];
    await mount();
    expect(row('Post')?.textContent).toContain('Waiting for SoundCloud');
  });

  it('says when an admin has paused all posting', async () => {
    meta = { paused: true, needs_approval: false };
    await mount();
    expect(root.querySelector('.promotion .notice')?.textContent).toBe(
      'Posting is paused by the station.'
    );
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
    expect(row('Pre')?.textContent).toContain('Scheduled · Fr 23.10. · 18:00');
    expect(row('Pre')?.textContent).toContain(
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
    expect(row('Pre')?.textContent).toContain(
      'Moved from Fr 23.10. · 18:00: the station already posts that day.'
    );
  });

  it('has no moved note when the post kept its time', async () => {
    announcements = [
      announcement('pre', { scheduled_at: FR_2310_1800, requested_at: '2026-10-23T16:00:00.000Z' }),
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
        scheduled_at: FR_2310_1800,
      },
    });
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
    meta = { paused: false, needs_approval: true };
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
    meta = { paused: true, needs_approval: true };
    await open('pre', 'Send for approval');
    expect(text()).toContain('Posting is paused by the station.');
    expect(text()).toContain('An admin checks this post first.');
    expect(button('Send for approval')).toBeTruthy();
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

describe('Wrap-up · Post-announcement', () => {
  const wrapup = (overrides: Partial<ShowDetail> = {}) =>
    episode({ date: '2026-10-25', episode_status: 'wrapup_open', ...overrides });
  const postRow = () => root.querySelector('.post-slot li')?.textContent ?? '';

  it('waits for the public track and does not hold up Publish', async () => {
    current = wrapup({ live_recorded: false });
    const router = await mount();
    // Default: 3 days after the episode at 18:00.
    expect(postRow()).toContain('Mi 28.10. · 18:00 · waits for the public track');
    expect(button('Publish').disabled).toBe(false);
    button('Set up post-announcement').click();
    await vi.waitFor(() =>
      expect(router.currentRoute.value.fullPath).toBe('/shows/42/announce/post')
    );
  });

  it('shows a set-up post with Edit and Cancel once the track is public', async () => {
    current = wrapup({ soundcloud_public: true });
    announcements = [announcement('post', { scheduled_at: MO_2610_1800 })];
    await mount();
    expect(postRow()).toContain('Scheduled · Mo 26.10. · 18:00');
    expect(button('Edit post-announcement')).toBeTruthy();
    expect(button('Cancel post-announcement')).toBeTruthy();
  });

  it.each([
    ['no post slot', [], false, 'Instagram · no post-announcement'],
    [
      'a private track',
      [announcement('post')],
      false,
      'Instagram · waits for the public SoundCloud track',
    ],
  ])('says on Done when there is %s', async (_name, list, isPublic, pill) => {
    current = wrapup({ episode_status: 'finished', soundcloud_public: isPublic });
    announcements = list;
    await mount();
    expect(text()).toContain(pill);
  });
});

describe('who sees what', () => {
  it("keeps today's page for admins", async () => {
    await mount('admin', 9);
    await vi.waitFor(() => expect(text()).toContain("Today's show page"));
  });

  it("shows another host's episode read-only", async () => {
    current = episode({ host_username: 'lena', episode_status_label: 'Upcoming' });
    const router = await mount('host', 7);
    await vi.waitFor(() => expect(text()).toContain('So 04.01. · Brunchtime'));
    expect(text()).toContain('Upcoming · Live · 12:00–14:00');
    expect(text()).toContain('lena');
    expect(text()).not.toContain("Today's show page");
    expect(root.querySelectorAll('button, input')).toHaveLength(0);
    expect(root.querySelector('a')?.getAttribute('href')).toBe('/dashboard');
    expect(router.currentRoute.value.fullPath).toBe('/shows/42');
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
    current = episode({
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

  it('Wrap-up matches the flow', async () => {
    current = episode({
      date: '2026-10-25',
      episode_status: 'wrapup_open',
      latest_recording: recordingReady,
    });
    await mount();
    await expectFlow('wrapup', root);
  });

  it('Published matches the flow', async () => {
    current = episode({
      date: '2026-10-25',
      episode_status: 'finished',
      latest_recording: recordingReady,
      soundcloud_public: true,
    });
    announcements = [announcement('post', { scheduled_at: MI_2810_1800 })];
    await mount();
    await expectFlow('done', root);
  });
});
