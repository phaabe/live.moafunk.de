import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApp, h, nextTick, ref, type App, type Component, type Ref } from 'vue';

// The live panel's parts (#1053) that the On Air page tests do not reach:
// folding on phones, the chat's "new" count, and the time helpers.

const chat = vi.hoisted(() => ({
  messages: null as null | Ref<
    { id: number; author: string; text: string; ts: number; host: boolean }[]
  >,
}));
vi.mock('@admin/composables', () => ({
  useLiveChat: () => ({
    messages: chat.messages,
    connected: ref(true),
    error: ref(null),
    send: vi.fn(() => true),
  }),
}));

import LivePanelCard from '../components/live-panel/LivePanelCard.vue';
import LiveChatCard from '../components/LiveChatCard.vue';
import { isMarkKey } from '../components/live-panel/LiveMomentsCard.vue';
import { formatHms, formatLeft, slotWindow } from '../components/live-panel/onAir';
import { addListenerSample, LISTENER_WINDOW_MS } from '../composables/useListeners';

let app: App | null = null;
let root: HTMLElement;
let phone = false;

function mount(component: Component, props: Record<string, unknown> = {}) {
  root = document.createElement('div');
  document.body.appendChild(root);
  app = createApp({ render: () => h(component, props, { default: () => h('p', 'Body') }) });
  app.mount(root);
}

beforeEach(() => {
  phone = false;
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: phone && query.includes('max-width'),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }));
  chat.messages = ref([]);
});

afterEach(() => {
  app?.unmount();
  app = null;
  root?.remove();
  vi.unstubAllGlobals();
});

const body = () => root.querySelector('.panel-body') as HTMLElement;
const toggle = () => root.querySelector('.panel-toggle') as HTMLButtonElement | null;

describe('a panel card', () => {
  it('is always open on a wide screen, with no toggle', () => {
    mount(LivePanelCard, { title: 'Connection', icon: 'wifi', status: 'Stable', tone: 'ok' });
    expect(root.querySelector('h2')!.textContent).toBe('Connection');
    expect(root.querySelector('.panel-status')!.textContent).toBe('Stable');
    expect(toggle()).toBeNull();
    expect(body().style.display).toBe('');
  });

  it('folds on a phone; a tap on the title opens it', async () => {
    phone = true;
    mount(LivePanelCard, { title: 'Connection', icon: 'wifi' });
    expect(body().style.display).toBe('none');
    expect(toggle()!.getAttribute('aria-expanded')).toBe('false');
    toggle()!.click();
    await nextTick();
    expect(body().style.display).toBe('');
    expect(toggle()!.getAttribute('aria-expanded')).toBe('true');
  });

  it('starts open on a phone when asked, and a problem keeps it open', async () => {
    phone = true;
    mount(LivePanelCard, { title: 'Audio', icon: 'mic', phoneOpen: true });
    expect(body().style.display).toBe('');
    app!.unmount();
    root.remove();

    mount(LivePanelCard, { title: 'Audio', icon: 'mic', alert: true });
    expect(body().style.display).toBe('');
    expect(toggle()).toBeNull();
    expect(root.querySelector('section')!.classList).toContain('alert');
  });
});

describe('the live chat', () => {
  const message = (id: number) => ({
    id,
    author: 'tom',
    text: `Hi ${id}`,
    ts: 1_700_000_000,
    host: false,
  });
  const status = () => root.querySelector('.panel-status')!.textContent;

  it('names the bridge while connected', () => {
    mount(LiveChatCard, { active: true });
    expect(status()).toBe('Telegram');
    expect(root.querySelector('label')!.textContent).toBe('Reply as host');
    expect(root.querySelector('button[aria-label="Send"]')).not.toBeNull();
  });

  it('counts new messages while folded on a phone, not the history', async () => {
    phone = true;
    mount(LiveChatCard, { active: true });
    chat.messages!.value = [message(1), message(2), message(3)];
    await nextTick();
    expect(status()).toBe('Telegram');

    chat.messages!.value = [...chat.messages!.value, message(4), message(5)];
    await nextTick();
    expect(status()).toBe('2 new');

    toggle()!.click();
    await nextTick();
    expect(status()).toBe('Telegram');
    expect(root.querySelectorAll('li')).toHaveLength(5);
  });
});

describe('the M key', () => {
  const key = (target: Element, init: KeyboardEventInit = {}) => {
    const event = new KeyboardEvent('keydown', { key: 'm', ...init });
    Object.defineProperty(event, 'target', { value: target });
    return event;
  };

  it('marks from the page, not from a field or with a modifier', () => {
    const div = document.createElement('div');
    expect(isMarkKey(key(div))).toBe(true);
    expect(isMarkKey(key(div, { key: 'M' }))).toBe(true);
    expect(isMarkKey(key(div, { key: 'n' }))).toBe(false);
    expect(isMarkKey(key(div, { metaKey: true }))).toBe(false);
    expect(isMarkKey(key(div, { altKey: true }))).toBe(false);
    for (const tag of ['input', 'textarea', 'select']) {
      expect(isMarkKey(key(document.createElement(tag)))).toBe(false);
    }
    const editable = document.createElement('div');
    editable.setAttribute('contenteditable', 'true');
    const inside = document.createElement('span');
    editable.appendChild(inside);
    expect(isMarkKey(key(inside))).toBe(false);
  });
});

describe('time helpers', () => {
  it('formats time on air and time left', () => {
    expect(formatHms(2530)).toBe('00:42:10');
    expect(formatHms(-5)).toBe('00:00:00');
    expect(formatLeft(4670)).toBe('1:17:50');
    expect(formatLeft(252)).toBe('4:12');
    expect(formatLeft(9)).toBe('0:09');
  });

  it('puts an end at or before the start on the next day', () => {
    const utc = (date: string, time: string) => new Date(`${date}T${time}:00Z`);
    expect(slotWindow({ date: '2026-11-15', start_time: '12:00', end_time: '14:00' }, utc)).toEqual(
      {
        start: new Date('2026-11-15T12:00:00Z'),
        end: new Date('2026-11-15T14:00:00Z'),
      }
    );
    expect(
      slotWindow({ date: '2026-11-15', start_time: '23:00', end_time: '01:00' }, utc).end
    ).toEqual(new Date('2026-11-16T01:00:00Z'));
    expect(slotWindow({ date: '2026-11-15' }, utc)).toEqual({ start: null, end: null });
    expect(slotWindow(undefined, utc)).toEqual({ start: null, end: null });
  });
});

describe('listener history', () => {
  it('keeps the last 40 minutes', () => {
    const t = 1_700_000_000_000;
    let samples = addListenerSample([], { at: t, count: 12 });
    samples = addListenerSample(samples, { at: t + 20 * 60_000, count: 30 });
    samples = addListenerSample(samples, { at: t + LISTENER_WINDOW_MS, count: 37 });
    expect(samples.map((s) => s.count)).toEqual([30, 37]);
  });
});
