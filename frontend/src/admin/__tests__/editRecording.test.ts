import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApp, h, nextTick, type App } from 'vue';
import { createMemoryHistory, createRouter, RouterView, type Router } from 'vue-router';
import type { RecordingEditState, ShowDetail } from '../api';
import type { RecordingEdit } from '../components/recording-editor/edit';
import { expectFlow } from '../flow/expectFlow';

// The Edit recording page (#883). The waveform canvas is drawn by WaveBars,
// which jsdom cannot paint; a stand-in keeps the page's own markup.
vi.mock('../components/recording-editor/WaveBars.vue', () => ({
  __esModule: true,
  default: { render: () => h('canvas', { class: 'wave-bars' }) },
}));

const api = vi.hoisted(() => ({
  show: null as unknown,
  state: null as unknown,
  peaks: [] as unknown[],
  save: vi.fn(),
  apply: vi.fn(),
}));

vi.mock('../api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api')>()),
  showsApi: { get: vi.fn(async () => api.show) },
  recordingEditApi: {
    get: vi.fn(async () => api.state),
    peaks: vi.fn(async () => api.peaks.shift() ?? api.peaks[0]),
    save: api.save,
    apply: api.apply,
  },
}));

const DURATION = 7200;
const peaks = () => ({
  duration: DURATION,
  per_second: 20,
  peaks: new Array(DURATION * 20).fill(0.4),
});

function state(edit: RecordingEdit | null = null): RecordingEditState {
  return {
    source: { url: 'https://r2.example/set.mp3', duration: DURATION },
    edit,
    status: edit ? 'draft' : 'none',
    error: null,
    edited_url: null,
    rendered_at: null,
  };
}

const saved: RecordingEdit = {
  start: 369.6,
  end: 6830.4,
  gainDb: 2.5,
  sections: [
    { id: 'a', start: 1800, end: 2100, gainDb: 6 },
    { id: 'b', start: 4180, end: 4520, gainDb: -3 },
  ],
  snippets: [
    { slot: 1, start: 2712, length: 30 },
    { slot: 2, start: 4296, length: 15 },
  ],
};

let app: App | null = null;
let router: Router;
let root: HTMLElement;

/** `ready`: wait until the waveform and the tabs are there. */
async function mount(ready = true) {
  root = document.createElement('div');
  document.body.appendChild(root);
  const { default: Page } = await import('../pages/HostEditRecordingPage.vue');
  router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/shows/:id(\\d+)/edit-recording', component: Page },
      { path: '/shows/:id(\\d+)', component: { render: () => h('p', 'Episode page') } },
    ],
  });
  await router.push('/shows/42/edit-recording');
  await router.isReady();
  app = createApp({ render: () => h(RouterView) });
  app.use(router);
  app.mount(root);
  await vi.waitFor(() => expect(root.querySelector('h1')?.textContent).toBe('Edit recording'));
  if (ready) await vi.waitFor(() => expect(root.querySelector('[role="tablist"]')).not.toBeNull());
}

const text = () => root.textContent?.replace(/\s+/g, ' ') ?? '';
const button = (name: string) =>
  [...root.querySelectorAll('button')].find(
    (b) => (b.getAttribute('aria-label') ?? b.textContent?.trim()) === name
  ) as HTMLButtonElement;
const click = async (name: string) => {
  button(name).click();
  await nextTick();
};
/** A click on an element at `share` of its width. */
async function clickAt(el: Element, share: number) {
  vi.spyOn(el, 'getBoundingClientRect').mockReturnValue({ left: 0, width: 1000 } as DOMRect);
  el.dispatchEvent(new MouseEvent('click', { bubbles: true, clientX: share * 1000 }));
  await nextTick();
}
async function type(input: HTMLInputElement, value: string) {
  input.value = value;
  input.dispatchEvent(new Event('input'));
  input.dispatchEvent(new Event('blur'));
  await nextTick();
}

beforeEach(() => {
  // jsdom has no media playback.
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {});
  vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {});
  api.show = {
    id: 42,
    title: 'Brunchtime #31',
    series_name: 'Brunchtime',
    date: '2026-10-25',
  } as Partial<ShowDetail>;
  api.state = state(structuredClone(saved));
  api.peaks = [peaks()];
  api.save.mockReset().mockImplementation(async (_id: number, edit: RecordingEdit) => state(edit));
  api.apply.mockReset().mockResolvedValue({ status: 'rendering' });
});

afterEach(() => {
  app?.unmount();
  app = null;
  root.remove();
  vi.restoreAllMocks();
});

describe('Edit recording', () => {
  it('opens on Snippets with the saved edit', async () => {
    await mount();
    expect(text()).toContain('Brunchtime · ');
    expect(text()).toContain('original 2:00:00');
    expect(text()).toContain('2 of 3 placed');
    expect(text()).toContain('1:47:41 · +2.5 dB');
    expect(text()).toContain('2 sections');
    const inputs = [...root.querySelectorAll<HTMLInputElement>('.er-time-input')];
    expect(inputs.map((i) => i.value)).toEqual(['0:45:12.0', '1:11:36.0', '']);
  });

  it('waits while the server reads the recording', async () => {
    api.peaks = [{ status: 'processing' }, peaks()];
    vi.useFakeTimers({ toFake: ['setTimeout'] });
    try {
      const pending = mount(false);
      await vi.waitFor(() => expect(text()).toContain('Reading the recording for the waveform…'));
      await vi.advanceTimersByTimeAsync(2000);
      await pending;
      await vi.waitFor(() => expect(text()).toContain('2 of 3 placed'));
    } finally {
      vi.useRealTimers();
    }
  });

  it('says so when the episode has no recording', async () => {
    api.state = { ...state(), source: null };
    await mount(false);
    await vi.waitFor(() => expect(text()).toContain('This episode has no recording yet.'));
    expect(button('Apply edit')).toBeUndefined();
  });

  it('places the picked snippet where the waveform is clicked', async () => {
    await mount();
    // Snippet 3 is empty, so it is picked. 30 min view from the start of the set.
    await clickAt(root.querySelector('.er-main')!, 0.5);
    const third = root.querySelectorAll<HTMLInputElement>('.er-time-input')[2];
    expect(third.value).toBe('0:21:09.6');
    expect(text()).toContain('3 of 3 placed');
  });

  it('sets a typed snippet time and its length', async () => {
    await mount();
    const first = root.querySelectorAll<HTMLInputElement>('.er-time-input')[0];
    await type(first, '0:50:00');
    expect(first.value).toBe('0:50:00.0');
    await type(first, 'nonsense');
    expect(first.value).toBe('0:50:00.0');
    await click('Snippet 2 1 second longer');
    expect(text()).toContain('16 s');
    await click('Snippet 1 1 second longer');
    expect(button('Snippet 1 1 second longer').disabled).toBe(true);
  });

  it('cuts the set and sets its volume', async () => {
    await mount();
    await click('Start & end');
    const start = root.querySelector<HTMLInputElement>('#er-start')!;
    await type(start, '0:07:00');
    expect(start.value).toBe('0:07:00.0');
    await click('Start 0.1 seconds earlier');
    expect(start.value).toBe('0:06:59.9');
    await click('Whole set 0.5 dB louder');
    expect(text()).toContain('+3.0 dB');
    expect(text()).toContain('The set is 1:46:50.5 long.');
  });

  it('adds a volume section with two clicks', async () => {
    await mount();
    await click('Volume');
    await click('Add section');
    expect(text()).toContain('Click where the section starts.');
    const wave = root.querySelector('.er-main')!;
    await clickAt(wave, 0.8);
    await clickAt(wave, 0.9);
    expect(text()).toContain('3 sections');
    expect(text()).toContain('0:30:10 → 0:33:10');
    await click('Section 1 0.5 dB quieter');
    expect(text()).toContain('+5.5 dB');
  });

  it('applies the edit and goes back to the episode', async () => {
    await mount();
    await click('Whole set'); // the map: no change to the edit
    await click('Start & end');
    await click('Whole set 0.5 dB quieter');
    await click('Apply edit');
    await vi.waitFor(() => expect(router.currentRoute.value.path).toBe('/shows/42'));
    expect(api.save).toHaveBeenCalledWith(42, expect.objectContaining({ gainDb: 2 }));
    expect(api.apply).toHaveBeenCalledWith(42);
  });

  it('discards the changes since the last save', async () => {
    await mount();
    await click('Start & end');
    await click('Whole set 0.5 dB louder');
    expect(text()).toContain('+3.0 dB');
    await click('Discard');
    expect(text()).toContain('+2.5 dB');
    expect(button('Discard').disabled).toBe(true);
  });

  it('ends a fresh edit where the server says the recording ends', async () => {
    api.state = { ...state(), source: { url: 'https://r2.example/set.mp3', duration: 7199.2 } };
    await mount();
    await click('Start & end');
    expect(root.querySelector<HTMLInputElement>('#er-end')!.value).toBe('1:59:59.2');
    expect(button('Apply edit').disabled).toBe(false);
  });

  it('blocks Apply while the edit has a problem', async () => {
    await mount();
    const first = root.querySelectorAll<HTMLInputElement>('.er-time-input')[0];
    await click('Start & end');
    const end = root.querySelector<HTMLInputElement>('#er-end')!;
    await type(end, '0:30:00');
    expect(first.isConnected).toBe(false);
    expect(text()).toContain('Snippet 1 must be inside the set.');
    expect(button('Apply edit').disabled).toBe(true);
  });
});

describe('flow', () => {
  it('Edit recording · Snippets matches the flow', async () => {
    await mount();
    await expectFlow('edit-recording', root);
  });

  it('Edit recording · Start & end matches the flow', async () => {
    await mount();
    await click('Start & end');
    await expectFlow('edit-recording-start-end', root);
  });

  it('Edit recording · Volume matches the flow', async () => {
    await mount();
    await click('Volume');
    await expectFlow('edit-recording-volume', root);
  });
});
