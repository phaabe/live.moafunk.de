import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApp, h, nextTick, type App } from 'vue';
import { createMemoryHistory, createRouter, type Router } from 'vue-router';
import type { ShowDetail } from '../api';
import type { SlotReading } from '../promotion';
import { expectFlow } from '../flow/expectFlow';

// The Recording tab (#1082, design option C): the player of the picked file,
// then the rows File, SoundCloud and Instagram, one open at a time. The
// episode page around it is covered in hostEpisode.

/** The flow's example: Brunchtime #31 on So 25.10., edited, private on SoundCloud. */
const SHOW: Partial<ShowDetail> = {
  id: 42,
  date: '2026-10-25',
  title: 'Brunchtime #31',
  episode_status: 'wrapup_open',
  recording_source: 'show',
  latest_recording: {
    id: 1,
    version: 'v1',
    status: 'raw',
    duration_ms: 7200000,
    download_url: 'https://r2.example/v1.mp3',
  },
  recording_edit_status: 'done',
  recording_edited_at: '2026-10-25 13:00:00',
  recording_edited_seconds: 6460,
  edited_recording_url: 'https://r2.example/edited.mp3',
  recording_download_url: 'https://r2.example/edited-dl.mp3',
  recording_original_download_url: 'https://r2.example/v1-dl.mp3',
  soundcloud_url: 'https://soundcloud.com/radio-moafunk/brunchtime-31',
  soundcloud_public: false,
  // 13:20 UTC is 14:20 in Berlin.
  soundcloud_uploaded_at: '2026-10-25 13:20:00',
  soundcloud_source: 'show',
  soundcloud_edited: true,
  soundcloud_current: true,
};
const EDIT = {
  start: 369.6,
  end: 6829.6,
  gainDb: 2.5,
  sections: [{ id: 'a', start: 1800, end: 2160, gainDb: 4 }],
};
const SNIPPETS = [
  { slot: 1, start: 2712, length: 30 },
  { slot: 2, start: 4296, length: 20 },
];
const READY = { state: 'ok', text: 'Recording ready · 2:00:00' } as const;

let app: App | null = null;
let root: HTMLElement;
let router: Router;
let requests: { url: string; method: string; body: unknown }[];
let snippets: typeof SNIPPETS;

beforeEach(() => {
  requests = [];
  snippets = structuredClone(SNIPPETS);
  // jsdom has no media playback.
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {});
  vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {});
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, options?: RequestInit) => {
      const method = options?.method ?? 'GET';
      const body = typeof options?.body === 'string' ? JSON.parse(options.body) : undefined;
      requests.push({ url, method, body });
      if (url === '/api/shows/42/recording/edit')
        return Response.json({
          source: { url: 'https://r2.example/v1.mp3', duration: 7200 },
          file: 'show',
          filename: 'v1.mp3',
          edit: EDIT,
          snippets,
          status: 'done',
          error: null,
          edited_url: 'https://r2.example/edited.mp3',
          rendered_at: '2026-10-25 13:00:00',
        });
      if (url === '/api/shows/42/recording/peaks')
        return Response.json({ duration: 7200, per_second: 1, peaks: [0.4, 0.6, 0.5] });
      if (url === '/api/shows/42/recording/source' && method === 'PUT') return Response.json(body);
      if (url === '/api/shows/42/recording/snippets' && method === 'PUT') {
        snippets = (body as { snippets: typeof SNIPPETS }).snippets;
        return Response.json({ edit: EDIT, snippets });
      }
      if (url === '/api/shows/42/soundcloud/upload' && method === 'POST')
        return Response.json({
          success: true,
          track_id: '556',
          track_url: 'https://soundcloud.com/radio-moafunk/brunchtime-31-2/s-Secret1',
        });
      // A private track has a secret link.
      if (url === '/api/shows/42/soundcloud/privacy' && method === 'POST')
        return Response.json({
          success: true,
          track_id: '556',
          track_url: `https://soundcloud.com/radio-moafunk/brunchtime-31-2${(body as { public: boolean }).public ? '' : '/s-Secret1'}`,
        });
      throw new Error(`Unexpected API request: ${method} ${url}`);
    })
  );
});

afterEach(() => {
  app?.unmount();
  app = null;
  root?.remove();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function mount(
  show: Partial<ShowDetail> = {},
  opts: {
    phase?: 'pre' | 'post';
    recording?: { state: 'ok' | 'processing' | 'failed'; text: string };
    announcements?: SlotReading[];
    onChanged?: () => void;
    onSocial?: () => void;
  } = {}
) {
  const { default: Card } = await import('../components/EpisodeRecordingCard.vue');
  router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/:p(.*)*', component: { render: () => null } }],
  });
  root = document.createElement('div');
  document.body.appendChild(root);
  app = createApp({
    render: () =>
      h(Card, {
        show: { ...SHOW, ...show } as ShowDetail,
        phase: opts.phase ?? 'post',
        state: 'now',
        recording: opts.recording ?? READY,
        announcements: opts.announcements,
        onChanged: opts.onChanged ?? (() => {}),
        onSocial: opts.onSocial ?? (() => {}),
      }),
  });
  app.use(router);
  app.mount(root);
  await nextTick();
}

const text = () => root.textContent?.replace(/\s+/g, ' ') ?? '';
const button = (name: string) =>
  [...root.querySelectorAll('button')].find(
    (b) => (b.getAttribute('aria-label') ?? b.textContent?.trim()) === name
  );
/** The one-line summary of a row: File, SoundCloud or Instagram. */
const summary = (row: string) =>
  [...root.querySelectorAll('.rec-toggle')]
    .find((b) => b.querySelector('.rec-name')?.textContent?.trim() === row)
    ?.querySelector('.rec-sum')
    ?.textContent?.replace(/\s+/g, ' ')
    .trim();
const openRow = async (row: string) => {
  [...root.querySelectorAll<HTMLButtonElement>('.rec-toggle')]
    .find((b) => b.querySelector('.rec-name')?.textContent?.trim() === row)!
    .click();
  await nextTick();
};
const openRows = () =>
  [...root.querySelectorAll('.rec-toggle[aria-expanded="true"] .rec-name')].map((n) =>
    n.textContent?.trim()
  );
const downloads = () =>
  Object.fromEntries(
    [...root.querySelectorAll('a[download]')].map((a) => [
      a.textContent?.trim(),
      a.getAttribute('href'),
    ])
  );
const playerReady = () => vi.waitFor(() => expect(button('Play')).toBeTruthy());

describe('Recording tab · flow', () => {
  it('matches the flow after the show, private on SoundCloud', async () => {
    await mount();
    await playerReady();
    expect(openRows()).toEqual(['SoundCloud']);
    await expectFlow('episode-recording', root);
  });

  it('matches the flow with the File row open', async () => {
    await mount();
    await playerReady();
    await openRow('File');
    expect(openRows()).toEqual(['File']);
    await expectFlow('episode-recording-file', root);
  });

  it('matches the flow with the Instagram row open', async () => {
    const scheduled: SlotReading = {
      slot: 'post',
      state: 'scheduled',
      scheduled_at: '2026-10-28T17:00:00Z',
      sent_at: null,
    };
    await mount({ soundcloud_public: true }, { announcements: [scheduled] });
    await playerReady();
    expect(openRows()).toEqual(['Instagram']);
    await vi.waitFor(() => expect(root.querySelectorAll('.sn-row')).toHaveLength(3));
    await expectFlow('episode-recording-instagram', root);
  });
});

describe('Recording tab · player', () => {
  it('shows the applied edit and plays the edited file', async () => {
    await mount();
    await playerReady();
    expect(root.querySelector('.rp-title')?.textContent).toBe('Show recording · edited · 1:47:40');
    expect(root.querySelectorAll('.rp-mark.cut')).toHaveLength(2);
    expect(root.querySelectorAll('.rp-mark.section')).toHaveLength(1);
    // Snippets are not marks of the edit: they sit in the Instagram row.
    expect(root.querySelectorAll('.rp-mark.snippet')).toHaveLength(0);
    expect([...root.querySelectorAll('.rp-flag')].map((f) => f.textContent)).toEqual([
      'START',
      'END',
    ]);
    // The edited file starts at the edit's start.
    expect(root.querySelector('.rp-time')?.textContent).toBe('0:00:00 / 1:47:40');
    expect(downloads()).toEqual({
      Download: 'https://r2.example/edited-dl.mp3',
      'Download original': 'https://r2.example/v1-dl.mp3',
    });
  });

  it('plays the file as it is without an applied edit', async () => {
    await mount({
      recording_edit_status: null,
      edited_recording_url: null,
      recording_download_url: 'https://r2.example/v1-dl.mp3',
      recording_original_download_url: null,
      soundcloud_url: undefined,
    });
    await playerReady();
    expect(root.querySelector('.rp-title')?.textContent).toBe('Show recording');
    expect(root.querySelectorAll('.rp-mark')).toHaveLength(0);
    expect(downloads()).toEqual({ Download: 'https://r2.example/v1-dl.mp3' });
    expect(summary('File')).toBe('Show recording · 2:00:00');
  });

  it('says why there is no show recording and opens File', async () => {
    await mount(
      { latest_recording: undefined, recording_edit_status: null, soundcloud_url: undefined },
      { recording: { state: 'failed', text: 'No usable recording · Shorter than 1 minute' } }
    );
    expect(text()).toContain(
      'No usable recording · Shorter than 1 minute. Upload your own file under File.'
    );
    expect(button('Play')).toBeUndefined();
    expect(openRows()).toEqual(['File']);
    expect(summary('File')).toBe('No usable show recording · upload your own file');
    expect(summary('SoundCloud')).toBe('Needs a file first');
    expect(button('Choose file')).toBeTruthy();
    expect(button('Upload to SoundCloud')).toBeUndefined();
  });

  it('waits while the recording processes', async () => {
    await mount(
      { latest_recording: undefined, recording_edit_status: null, soundcloud_url: undefined },
      { recording: { state: 'processing', text: 'Recording processing…' } }
    );
    expect(text()).toContain('Processing the recording. The waveform shows up when it is ready.');
    expect(openRows()).toEqual([]);
    expect(summary('SoundCloud')).toBe('After the recording is ready');
  });
});

describe('Recording tab · File', () => {
  it('picks the own file', async () => {
    const changed = vi.fn();
    await mount(
      { recording_filename: 'set.mp3', recording_url: 'https://r2.example/set.mp3' },
      { onChanged: changed }
    );
    await openRow('File');
    const own = [...root.querySelectorAll('label')]
      .find((l) => l.textContent?.trim() === 'Own file')!
      .querySelector('input')!;
    own.click();
    await vi.waitFor(() => expect(changed).toHaveBeenCalled());
    expect(requests.find((r) => r.url.endsWith('/recording/source'))).toEqual({
      url: '/api/shows/42/recording/source',
      method: 'PUT',
      body: { source: 'own' },
    });
  });

  it('names the own file, edits it, and has no own file to pick without one', async () => {
    await mount({ recording_source: 'own', recording_filename: 'set.mp3' });
    expect(summary('File')).toBe('Own file · set.mp3 · edited · 1:47:40');
    app!.unmount();
    root.remove();
    await mount({});
    await openRow('File');
    const own = [...root.querySelectorAll('label')]
      .find((l) => l.textContent?.trim() === 'Own file')!
      .querySelector('input')!;
    expect(own.disabled).toBe(true);
    const input = root.querySelector<HTMLInputElement>('input[type="file"]')!;
    const click = vi.spyOn(input, 'click').mockImplementation(() => {});
    button('Choose file')!.click();
    expect(click).toHaveBeenCalled();
  });

  it('opens Edit recording for the picked file', async () => {
    await mount();
    await openRow('File');
    button('Edit recording')!.click();
    await vi.waitFor(() => expect(router.currentRoute.value.path).toBe('/shows/42/edit-recording'));
  });

  it('looks again while the edited file is made, and says when it failed', async () => {
    vi.useFakeTimers();
    const changed = vi.fn();
    await mount({ recording_edit_status: 'rendering' }, { onChanged: changed });
    expect(summary('File')).toBe('Show recording · making the edited file…');
    vi.advanceTimersByTime(5000);
    expect(changed).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
    app!.unmount();
    root.remove();
    await mount({ recording_edit_status: 'failed' });
    await openRow('File');
    expect(text()).toContain('Edit failed. Open Edit recording to try again.');
  });
});

describe('Recording tab · SoundCloud', () => {
  it('names the file on SoundCloud and when it went up', async () => {
    await mount();
    expect(summary('SoundCloud')).toBe('Private · Show recording, edited · So 25.10. · 14:20');
    expect(button('Private')!.getAttribute('aria-pressed')).toBe('true');
    expect(text()).not.toContain('older file');
  });

  it('offers Upload again when SoundCloud has an older file', async () => {
    const changed = vi.fn();
    await mount({ soundcloud_current: false }, { onChanged: changed });
    expect(summary('SoundCloud')).toBe(
      'Private · Show recording, edited · So 25.10. · 14:20 · older file'
    );
    expect(text()).toContain('SoundCloud has an older file.');
    button('Upload again')!.click();
    await vi.waitFor(() => expect(changed).toHaveBeenCalled());
    expect(requests.filter((r) => r.url.endsWith('/soundcloud/upload'))).toHaveLength(1);
    expect(summary('SoundCloud')).toBe('Private');
    expect(root.querySelector('a[href$="brunchtime-31-2/s-Secret1"]')?.textContent).toBe(
      'Open on SoundCloud'
    );

    // The link follows the sharing: plain once public, secret again once private.
    button('Public')!.click();
    await vi.waitFor(() =>
      expect(root.querySelector('a[href$="brunchtime-31-2"]')?.textContent).toBe(
        'Open on SoundCloud'
      )
    );
    button('Private')!.click();
    await vi.waitFor(() =>
      expect(root.querySelector('a[href$="brunchtime-31-2/s-Secret1"]')).not.toBeNull()
    );
  });

  it('tells an older file by the time for tracks from before the file was stored', async () => {
    const legacy = { soundcloud_source: null, soundcloud_current: null, soundcloud_edited: false };
    await mount({ ...legacy, soundcloud_uploaded_at: '2026-10-25 12:00:00' });
    expect(text()).toContain('older file');
    app!.unmount();
    root.remove();
    await mount({ ...legacy, soundcloud_uploaded_at: '2026-10-25 13:20:00' });
    expect(text()).not.toContain('older file');
  });

  it('puts Upload to SoundCloud on the row before the first upload', async () => {
    await mount({ soundcloud_url: undefined, soundcloud_uploaded_at: undefined });
    expect(summary('SoundCloud')).toBe('No track yet');
    expect(button('Upload to SoundCloud')).toBeTruthy();
  });
});

describe('Recording tab · Instagram', () => {
  it('opens Instagram once the track is public, and leads to Social media', async () => {
    const social = vi.fn();
    await mount({ soundcloud_public: true }, { onSocial: social });
    expect(openRows()).toEqual(['Instagram']);
    expect(summary('Instagram')).toBe('Post-announcement not set up');
    button('Go to Social media')!.click();
    expect(social).toHaveBeenCalled();
  });

  it('places, moves and removes snippets and saves each change', async () => {
    await mount({ soundcloud_public: true });
    await vi.waitFor(() => expect(root.querySelectorAll('.sn-row')).toHaveLength(3));
    const times = () =>
      [...root.querySelectorAll<HTMLInputElement>('.sn-time')].map((i) => i.value);
    expect(times()).toEqual(['0:45:12.0', '1:11:36.0', '']);
    expect(text()).toContain('Click the waveform to place snippet 3.');

    // Snippet 3 is empty, so it is picked: a click on the whole set places it there.
    const map = root.querySelector('.sn-map')!;
    vi.spyOn(map, 'getBoundingClientRect').mockReturnValue({ left: 0, width: 1000 } as DOMRect);
    map.dispatchEvent(new MouseEvent('click', { bubbles: true, clientX: 500 }));
    await nextTick();
    expect(times()[2]).toBe('1:00:00.0');
    // The zoom shows 2 minutes around it; a click there places it to the second.
    expect(text()).toContain('Zoom · 0:59:15 – 1:01:15');
    const zoom = root.querySelector('.sn-zoom')!;
    vi.spyOn(zoom, 'getBoundingClientRect').mockReturnValue({ left: 0, width: 1200 } as DOMRect);
    zoom.dispatchEvent(new MouseEvent('click', { bubbles: true, clientX: 600 }));
    await nextTick();
    expect(times()[2]).toBe('1:00:15.0');

    button('Snippet 2 1 second longer')!.click();
    button('Remove snippet 1')!.click();
    await nextTick();
    expect(times()).toEqual(['', '1:11:36.0', '1:00:15.0']);
    expect(text()).toContain('Saving…');
    // One save for the row of clicks.
    await vi.waitFor(() => expect(text()).toContain('Saved.'), { timeout: 2000 });
    const saves = requests.filter((r) => r.url === '/api/shows/42/recording/snippets');
    expect(saves).toEqual([
      {
        url: '/api/shows/42/recording/snippets',
        method: 'PUT',
        body: {
          snippets: [
            { slot: 2, start: 4296, length: 21 },
            { slot: 3, start: 3615, length: 30 },
          ],
        },
      },
    ]);
  });

  it('types a snippet time and keeps the length within 5 to 30 s', async () => {
    await mount({ soundcloud_public: true });
    await vi.waitFor(() => expect(root.querySelectorAll('.sn-row')).toHaveLength(3));
    const first = root.querySelector<HTMLInputElement>('.sn-time')!;
    first.value = '0:50:00';
    first.dispatchEvent(new Event('input'));
    first.dispatchEvent(new Event('blur'));
    await nextTick();
    expect(first.value).toBe('0:50:00.0');
    first.value = 'nonsense';
    first.dispatchEvent(new Event('input'));
    first.dispatchEvent(new Event('blur'));
    await nextTick();
    expect(first.value).toBe('0:50:00.0');
    expect(button('Snippet 1 1 second longer')!.disabled).toBe(true);
    expect(button('Snippet 3 0.1 seconds later')!.disabled).toBe(true);
  });

  it('says snippets wait for the recording', async () => {
    await mount(
      { soundcloud_public: true, latest_recording: undefined },
      { recording: { state: 'processing', text: 'Recording processing' } }
    );
    await openRow('Instagram');
    expect(text()).toContain('Snippets can be placed once the recording is ready.');
    expect(root.querySelector('.sn')).toBeNull();
  });

  it('names the post time once the track is public: going public schedules it', async () => {
    const post: SlotReading = {
      slot: 'post',
      state: 'waiting_soundcloud',
      scheduled_at: null,
      sent_at: null,
    };
    await mount({}, { announcements: [post] });
    expect(summary('Instagram')).toBe('Post-announcement waits for a public track');
    app!.unmount();
    root.remove();
    // 3 days after So 25.10., 17:00 UTC is 18:00 in Berlin.
    const scheduled = {
      ...post,
      state: 'scheduled',
      scheduled_at: '2026-10-28T17:00:00Z',
    } as const;
    await mount({ soundcloud_public: true }, { announcements: [scheduled] });
    expect(summary('Instagram')).toBe('Post-announcement: Scheduled · Mi 28.10. · 18:00');
  });

  it('says the post goes out after the show, before it', async () => {
    await mount(
      { latest_recording: undefined, soundcloud_url: undefined, recording_edit_status: null },
      { phase: 'pre' }
    );
    expect(summary('Instagram')).toBe('Goes out after the show, once the track is public');
    expect(summary('SoundCloud')).toBe('After the show');
    expect(summary('File')).toBe('Show recording · starts when you go live');
    expect(button('Edit recording')).toBeUndefined();
  });
});
