import { afterEach, describe, expect, it, vi } from 'vitest';
import { createApp, h, nextTick, type App } from 'vue';
import { createMemoryHistory, createRouter, type Router } from 'vue-router';
import type { ShowDetail } from '../api';

// The Recording card's way into the Edit recording page (#883) and what it
// says about the edited file. The rest of the card is covered in hostEpisode.

vi.mock('../components/AudioPlayer.vue', () => ({
  __esModule: true,
  default: {
    props: ['src'],
    render(this: { src: string }) {
      return h('div', { class: 'audio-player', 'data-src': this.src });
    },
  },
}));

let app: App | null = null;
let root: HTMLElement;
let router: Router;

async function mount(
  show: Partial<ShowDetail>,
  phase: 'pre' | 'post' = 'post',
  onChanged = () => {}
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
        show: { id: 42, recording_url: 'https://r2.example/original.mp3', ...show } as ShowDetail,
        phase,
        state: 'now',
        recording: { state: 'ok', text: 'Recording ready · 2:00:00' },
        onChanged,
      }),
  });
  app.use(router);
  app.mount(root);
  await nextTick();
}

const text = () => root.textContent?.replace(/\s+/g, ' ') ?? '';
/** The card's key/value rows, e.g. { Edit: 'Edited · 1:47:41' }. */
const rows = () =>
  Object.fromEntries(
    [...root.querySelectorAll('dl > div')].map((d) => [
      d.querySelector('dt')?.textContent?.trim(),
      d.querySelector('dd')?.textContent?.trim(),
    ])
  );
const button = (name: string) =>
  [...root.querySelectorAll('button')].find((b) => b.textContent?.trim() === name);

afterEach(() => {
  app?.unmount();
  app = null;
  root.remove();
  vi.useRealTimers();
});

describe('Recording card · Edit recording', () => {
  it('opens the editor after the show', async () => {
    await mount({});
    button('Edit recording')!.click();
    await vi.waitFor(() => expect(router.currentRoute.value.path).toBe('/shows/42/edit-recording'));
    expect(rows().Edit).toBeUndefined();
  });

  it('has no editor before the show', async () => {
    await mount({}, 'pre');
    expect(button('Edit recording')).toBeUndefined();
  });

  it('plays and names the edited file', async () => {
    await mount({
      recording_edit_status: 'done',
      recording_edited_at: '2026-11-22 21:40:00',
      recording_edited_seconds: 6460.8,
      edited_recording_url: 'https://r2.example/edited.mp3',
    });
    expect(rows().Edit).toBe('Edited · 1:47:41');
    expect(root.querySelector('.audio-player')?.getAttribute('data-src')).toBe(
      'https://r2.example/edited.mp3'
    );
  });

  it('says when SoundCloud has the file from before the edit', async () => {
    const edited = {
      recording_edit_status: 'done',
      recording_edited_at: '2026-11-22 21:40:00',
      recording_edited_seconds: 6460.8,
      edited_recording_url: 'https://r2.example/edited.mp3',
      soundcloud_url: 'https://soundcloud.com/moafunk/brunchtime-31',
    };
    await mount({ ...edited, soundcloud_uploaded_at: '2026-11-22 20:00:00' });
    expect(text()).toContain('SoundCloud has the file from before the edit.');
    app!.unmount();
    root.remove();
    await mount({ ...edited, soundcloud_uploaded_at: '2026-11-22 22:00:00' });
    expect(text()).not.toContain('from before the edit');
  });

  it('looks again while the edited file is made, and says when it failed', async () => {
    vi.useFakeTimers();
    const changed = vi.fn();
    await mount({ recording_edit_status: 'rendering' }, 'post', changed);
    expect(rows().Edit).toBe('Making the edited file…');
    vi.advanceTimersByTime(5000);
    expect(changed).toHaveBeenCalledTimes(1);
    app!.unmount();
    root.remove();
    await mount({ recording_edit_status: 'failed' });
    expect(rows().Edit).toBe('Edit failed. Open Edit recording to try again.');
  });
});
