import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApp, defineComponent, h, nextTick, ref, type App } from 'vue';
import { expectFlow } from '../flow/expectFlow';

// The on-air bar (#1063, flow screens `onair-bar`, `onair-bar-warn`,
// `onair-bar-end`). The live session is a stand-in: its own tests are in
// liveSession.test.ts.

const m = vi.hoisted(() => ({
  push: vi.fn(),
  session: null as null | { value: unknown },
  panelShown: null as null | { value: boolean },
}));

vi.mock('vue-router', () => ({ useRouter: () => ({ push: m.push }) }));
vi.mock('@admin/liveSession', async () => {
  const { ref, shallowRef } = await import('vue');
  m.session = shallowRef(null);
  m.panelShown = ref(false);
  return {
    useLiveSession: () => ({ session: m.session, panelShown: m.panelShown, ended: ref(null) }),
  };
});

const DEVICE_ENDED = 'Audio device is no longer active. Re-select your device.';

function fakeSession(warnings: { text: string; urgent: boolean }[] = []) {
  return {
    showId: 7,
    panelPath: '/shows/7',
    title: 'Brunchtime',
    slot: '12:00–14:00',
    elapsed: ref('00:42:10'),
    warnings: ref(warnings),
    end: vi.fn(async () => 7),
  };
}

let app: App | null = null;
let root: HTMLElement;

async function mountBar() {
  const { default: OnAirBar } = await import('../components/live-panel/OnAirBar.vue');
  root = document.createElement('div');
  document.body.appendChild(root);
  app = createApp(OnAirBar);
  app.component(
    'RouterLink',
    defineComponent({
      props: { to: { type: String, required: true } },
      setup(props, { slots }) {
        return () => h('a', { 'data-to': props.to }, slots.default?.());
      },
    })
  );
  app.mount(root);
  await nextTick();
}

const bar = () => root.querySelector('.onair-bar');
const buttons = () => [...root.querySelectorAll('button')].map((b) => b.textContent?.trim());
const press = async (label: string) => {
  [...root.querySelectorAll('button')].find((b) => b.textContent?.trim() === label)!.click();
  await nextTick();
};

beforeEach(async () => {
  m.push.mockReset();
  // Load the stand-in so its refs exist before a test sets them.
  await import('@admin/liveSession');
  m.session!.value = null;
  m.panelShown!.value = false;
});

afterEach(() => {
  app?.unmount();
  app = null;
  root?.remove();
});

describe('On-air bar', () => {
  it('shows only while this tab is live, and not while the live panel is on screen', async () => {
    await mountBar();
    expect(bar()).toBeNull();
    m.session!.value = fakeSession();
    await nextTick();
    expect(bar()).not.toBeNull();
    m.panelShown!.value = true;
    await nextTick();
    expect(bar()).toBeNull();
  });

  it('shows the on-air time, the episode, and a link back to the live panel', async () => {
    m.session!.value = fakeSession();
    await mountBar();
    expect(root.querySelector('.pill')!.textContent).toBe('On air · 00:42:10');
    expect(bar()!.textContent).toContain('Brunchtime · 12:00–14:00');
    expect(root.querySelector('a')!.getAttribute('data-to')).toBe('/shows/7');
    expect(bar()!.classList).not.toContain('warn');
    await expectFlow('onair-bar', root);
  });

  it('shows the live panel’s warnings and turns amber', async () => {
    m.session!.value = fakeSession([{ text: DEVICE_ENDED, urgent: true }]);
    await mountBar();
    expect(bar()!.classList).toContain('warn');
    expect(root.querySelector('[role="alert"]')!.textContent?.trim()).toBe(DEVICE_ENDED);
    await expectFlow('onair-bar-warn', root);
  });

  it('reads the reconnect line politely: it changes every second', async () => {
    const text = 'Reconnecting · 4 s. Your audio waits in this browser.';
    m.session!.value = fakeSession([{ text, urgent: false }]);
    await mountBar();
    expect(root.querySelector('[role="status"]')!.textContent?.trim()).toBe(text);
    expect(root.querySelector('[role="alert"]')).toBeNull();
  });

  it('End Episode asks once; Keep on air goes back and ends nothing', async () => {
    const live = fakeSession();
    m.session!.value = live;
    await mountBar();
    await press('End Episode');
    expect(bar()!.textContent).toContain('End the episode?');
    expect(root.querySelector('a')).toBeNull();
    await expectFlow('onair-bar-end', root);
    await press('Keep on air');
    expect(bar()!.textContent).not.toContain('End the episode?');
    expect(buttons()).toEqual(['End Episode']);
    expect(live.end).not.toHaveBeenCalled();
  });

  it('End Episode, asked and confirmed, ends the session and opens the episode', async () => {
    const live = fakeSession();
    m.session!.value = live;
    await mountBar();
    await press('End Episode');
    await press('End Episode');
    await vi.waitFor(() => expect(m.push).toHaveBeenCalled());
    expect(live.end).toHaveBeenCalledOnce();
    expect(m.push).toHaveBeenCalledWith({ path: '/shows/7', state: { episodeEnded: 7 } });
  });
});
