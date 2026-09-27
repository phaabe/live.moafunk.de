import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApp, h, nextTick, type App } from 'vue';
import { createPinia } from 'pinia';
import { createMemoryHistory, createRouter, RouterView } from 'vue-router';
import { useAuthStore } from '../stores/auth';
import { useHostFlow } from '../composables/useHostFlow';
import { ensureFlowReady } from '../router';
import ShowDetailPage from '../pages/ShowDetailPage.vue';
import ShowsPage from '../pages/ShowsPage.vue';
import FlowLive from '../pages/flow/FlowLive.vue';
import type { ShowDetail, User } from '../api';

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

// Hardware access is outside this test; keep the page, button, flow and guard real.
vi.mock('../components/LiveSetupTest.vue', () => ({
  default: {
    emits: ['passed'],
    setup(_props: unknown, { emit }: { emit: (event: 'passed') => void }) {
      return () =>
        h(
          'button',
          { 'data-testid': 'pass-live-test', onClick: () => emit('passed') },
          'Audio hardware test'
        );
    },
  },
}));

let app: App | undefined;
let root: HTMLDivElement;
let requestedPaths: string[];
const flow = useHostFlow();

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-09-12T10:30:00Z'));
  flow.reset();
  requestedPaths = [];
  root = document.createElement('div');
  document.body.appendChild(root);
});

afterEach(() => {
  app?.unmount();
  app = undefined;
  root.remove();
  flow.reset();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

function makeShow(show_type = 'unheard'): ShowDetail {
  return {
    id: 39,
    title: 'Another host’s show',
    date: '2026-09-12',
    start_time: '12:23',
    end_time: '15:38',
    status: 'scheduled',
    show_type,
    stream_mode: 'live',
    host_user_id: 7,
    host_username: 'richard',
    host_has_logged_in: false,
    created_at: '2026-09-12T10:15:39Z',
    artists: [],
    available_artists: [],
    available_hosts: [
      { id: 8, username: 'host-8' },
      { id: 9, username: 'host-9' },
    ],
    artists_left: 0,
  };
}

async function mountPage(role: User['role'], show: ShowDetail, path = '/shows/39') {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, options?: RequestInit) => {
      requestedPaths.push(url);
      switch (url) {
        case '/api/shows/39/host': {
          if (options?.method === 'DELETE') {
            show.host_user_id = undefined;
            show.host_username = undefined;
            show.host_has_logged_in = null;
          } else {
            const { user_id } = JSON.parse(String(options?.body)) as { user_id: number };
            show.host_user_id = user_id;
            show.host_username = `host-${user_id}`;
            show.host_has_logged_in = user_id === 8;
          }
          return Response.json({
            success: true,
            host_user_id: show.host_user_id,
            host_username: show.host_username,
          });
        }
        case '/api/guests':
          return Response.json({
            user_id: 9,
            username: 'host-9',
            password: 'test-only',
            login_date: show.date,
          });
        case '/api/shows/39':
          return Response.json(show);
        case '/api/my-show':
          return Response.json({
            assigned: role !== 'host' || show.host_user_id === 1,
            shows: role !== 'host' || show.host_user_id === 1 ? [show] : [],
          });
        case '/api/shows-overview':
        case '/api/shows':
          return Response.json({ shows: [show], artists: [] });
        case '/api/soundcloud/status':
          return Response.json({ configured: false, authorized: false });
        default:
          throw new Error(`Unexpected API request: ${url}`);
      }
    })
  );
  const pinia = createPinia();
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { render: () => null } },
      { path: '/shows', component: ShowsPage },
      { path: '/shows/:id', component: ShowDetailPage },
      { path: '/stream/live', component: FlowLive, beforeEnter: ensureFlowReady },
      {
        path: '/stream/on-air',
        component: { render: () => h('div', 'On-air controls') },
        beforeEnter: ensureFlowReady,
      },
      { path: '/stream', component: { render: () => h('div', 'No show selected') } },
    ],
  });
  app = createApp({ render: () => h(RouterView) });
  app.use(pinia).use(router);
  useAuthStore(pinia).user = { id: 1, username: 'operator', role };
  await router.push(path);
  await router.isReady();
  app.mount(root);
  // Drain API response bodies and Vue render jobs without advancing the clock.
  await vi.waitFor(() => expect(root.textContent).toContain(show.title));
  await nextTick();
  return router;
}

function liveButton() {
  return [...root.querySelectorAll('button')].find((button) =>
    /Prepare live broadcast|Open live panel/.test(button.textContent ?? '')
  );
}

describe('broadcast entry from the show page', () => {
  it.each([
    ['admin', 'unheard'],
    ['superadmin', 'unheard'],
    ['admin', 'external'],
    ['superadmin', 'brunchtime'],
  ] as const)('%s reaches the live page for another host’s %s show', async (role, type) => {
    const router = await mountPage(role, makeShow(type));
    expect(liveButton()).toBeDefined();
    liveButton()!.click();
    await vi.waitFor(() => expect(router.currentRoute.value.path).toBe('/stream/live'));
    expect(root.textContent).toContain('Set Up Audio & Test');
    expect(flow.showId.value).toBe(39);
    expect(flow.uploadMode.value).toBe('live');
    expect(flow.canNavigateTo('live')).toBe(true);
    expect(requestedPaths).toContain('/api/my-show');
  });

  it('does not offer another host’s broadcast action to a host', async () => {
    const router = await mountPage('host', makeShow());
    expect(liveButton()).toBeUndefined();
    expect(router.currentRoute.value.path).toBe('/shows/39');
    expect(flow.showId.value).toBeUndefined();
  });

  it('updates the detail-page warning when the show enters the next hour', async () => {
    vi.setSystemTime(new Date('2026-09-12T09:22:58Z'));
    await mountPage('admin', makeShow());
    expect(root.querySelector('[role="alert"]')).toBeNull();
    await vi.advanceTimersByTimeAsync(2000);
    await nextTick();
    expect(root.querySelector('[role="alert"]')?.textContent).toContain('has no login recorded');
  });

  it('shows the overview login warning to hosts and removes it after start', async () => {
    vi.setSystemTime(new Date('2026-09-12T10:22:59Z'));
    await mountPage('host', makeShow(), '/shows');
    expect(requestedPaths).toContain('/api/shows-overview');
    expect(root.querySelector('[role="alert"]')?.textContent).toContain('has no login recorded');
    await vi.advanceTimersByTimeAsync(2000);
    await nextTick();
    expect(root.querySelector('[role="alert"]')).toBeNull();
  });
});

function buttonNamed(text: string, container: ParentNode = document) {
  const button = [...container.querySelectorAll('button')].find(
    (item) => item.textContent?.trim() === text
  );
  expect(button, `button ${text}`).toBeDefined();
  return button!;
}

async function openHostEditor() {
  root.querySelector<HTMLButtonElement>('[title="Edit host"]')!.click();
  await nextTick();
  return document.querySelector<HTMLElement>('[role="dialog"]')!;
}

describe('host login metadata refresh', () => {
  it.each([
    [false, 8, true],
    [true, 9, false],
  ] as const)(
    'refreshes the warning when switching login status from %s to host %s (%s)',
    async (initial, hostId, loggedIn) => {
      vi.setSystemTime(new Date('2026-09-12T10:00:00Z'));
      const show = { ...makeShow('external'), host_has_logged_in: initial };
      await mountPage('admin', show);
      const dialog = await openHostEditor();
      const select = dialog.querySelector<HTMLSelectElement>('.host-edit select')!;
      select.value = String(hostId);
      select.dispatchEvent(new Event('change', { bubbles: true }));
      await nextTick();
      buttonNamed('Reassign', dialog).click();
      await vi.waitFor(() =>
        expect(requestedPaths.filter((path) => path === '/api/shows/39')).toHaveLength(2)
      );
      await vi.waitFor(() => expect(!!root.querySelector('[role="alert"]')).toBe(!loggedIn));
      expect(dialog.textContent).toContain(`host-${hostId}`);
      expect(document.querySelector('[role="dialog"]')).toBe(dialog);
    }
  );

  it('clears the warning when the host is removed', async () => {
    vi.setSystemTime(new Date('2026-09-12T10:00:00Z'));
    await mountPage('admin', makeShow('external'));
    expect(root.querySelector('[role="alert"]')).not.toBeNull();
    const dialog = await openHostEditor();
    buttonNamed('Remove', dialog).click();
    await vi.waitFor(() => expect(root.querySelector('[role="alert"]')).toBeNull());
    expect(dialog.textContent).toContain('No host assigned');
  });

  it('does not restore a removed host from an older in-flight refresh', async () => {
    vi.setSystemTime(new Date('2026-09-12T10:00:00Z'));
    const show = makeShow('external');
    await mountPage('admin', show);
    const dialog = await openHostEditor();
    const oldSnapshot = Response.json(show);
    let resolve!: (response: Response) => void;
    const pending = new Promise<Response>((done) => {
      resolve = done;
    });
    vi.mocked(fetch).mockImplementationOnce(() => pending);
    window.dispatchEvent(new Event('focus'));
    buttonNamed('Remove', dialog).click();
    await vi.waitFor(() => expect(dialog.textContent).toContain('No host assigned'));
    resolve(oldSnapshot);
    await vi.advanceTimersByTimeAsync(0);
    await nextTick();
    expect(root.querySelector('[role="alert"]')).toBeNull();
    expect(dialog.textContent).toContain('No host assigned');
  });

  it('refreshes the warning after creating and assigning a guest', async () => {
    vi.setSystemTime(new Date('2026-09-12T10:00:00Z'));
    await mountPage('admin', { ...makeShow('external'), host_has_logged_in: true });
    const dialog = await openHostEditor();
    buttonNamed('Create guest', dialog).click();
    await nextTick();
    const input = dialog.querySelector<HTMLInputElement>('[placeholder="Guest username"]')!;
    input.value = 'host-9';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    await nextTick();
    buttonNamed('Create & assign', dialog).click();
    await vi.waitFor(() =>
      expect(root.querySelector('[role="alert"]')?.textContent).toContain('host-9')
    );
    expect(requestedPaths.filter((path) => path === '/api/shows/39')).toHaveLength(2);
  });

  it.each(['/shows/39', '/shows'])(
    'removes a stale warning after an external login on %s',
    async (path) => {
      vi.setSystemTime(new Date('2026-09-12T10:00:00Z'));
      const show = makeShow('external');
      await mountPage('admin', show, path);
      expect(root.querySelector('[role="alert"]')).not.toBeNull();
      show.host_has_logged_in = true;
      await vi.advanceTimersByTimeAsync(30_000);
      await nextTick();
      expect(root.querySelector('[role="alert"]')).toBeNull();
      show.host_has_logged_in = false;
      window.dispatchEvent(new Event('focus'));
      await vi.waitFor(() => expect(root.querySelector('[role="alert"]')).not.toBeNull());
    }
  );

  it('preserves title edits and the open schedule editor during a focus refresh', async () => {
    vi.setSystemTime(new Date('2026-09-12T10:00:00Z'));
    const show = makeShow('external');
    await mountPage('admin', show);
    buttonNamed('✎ Edit', root).click();
    await nextTick();
    const title = root.querySelector<HTMLInputElement>('.sh-title-input')!;
    title.value = 'Unsaved title';
    title.dispatchEvent(new Event('input', { bubbles: true }));
    const dialog = await openHostEditor();
    const duration = dialog.querySelector<HTMLSelectElement>('.duration-select')!;
    duration.value = '60';
    duration.dispatchEvent(new Event('change', { bubbles: true }));
    await nextTick();
    show.host_has_logged_in = true;
    window.dispatchEvent(new Event('focus'));
    await vi.waitFor(() => expect(root.querySelector('[role="alert"]')).toBeNull());
    expect(title.value).toBe('Unsaved title');
    expect(duration.value).toBe('60');
    expect(document.querySelector('[role="dialog"]')).toBe(dialog);
  });
});

describe('inline preparation selection', () => {
  it('selects a newly assigned show before enabling its live test', async () => {
    vi.setSystemTime(new Date('2026-09-12T10:00:00Z'));
    flow.selectShow({ ...makeShow(), id: 5 });
    flow.selectMode('live');
    flow.setLiveTestPassed(true);
    const show = makeShow('external');
    const router = await mountPage('host', show);
    expect(root.querySelector('[data-testid="pass-live-test"]')).toBeNull();
    show.host_user_id = 1;
    const originalFetch = fetch;
    let resolveShows!: (response: Response) => void;
    const showResponse = new Promise<Response>((resolve) => {
      resolveShows = resolve;
    });
    const myShowsRequested = vi.fn();
    vi.stubGlobal(
      'fetch',
      vi.fn((url: RequestInfo | URL, options?: RequestInit) => {
        if (url === '/api/my-show') {
          myShowsRequested();
          return showResponse;
        }
        return originalFetch(url, options);
      })
    );
    window.dispatchEvent(new Event('focus'));
    await vi.waitFor(() => expect(myShowsRequested).toHaveBeenCalled());
    expect(flow.showId.value).toBe(5);
    expect(root.querySelector('[data-testid="pass-live-test"]')).toBeNull();
    resolveShows(Response.json({ assigned: true, shows: [show] }));
    await vi.waitFor(() =>
      expect(root.querySelector('[data-testid="pass-live-test"]')).not.toBeNull()
    );
    expect(flow.showId.value).toBe(39);
    expect(flow.liveTestPassed.value).toBe(false);
    flow.setLiveTestPassed(true);
    root.querySelector<HTMLButtonElement>('[data-testid="pass-live-test"]')!.click();
    await vi.waitFor(() => expect(router.currentRoute.value.path).toBe('/stream/on-air'));
  });

  it('rejects a stale passed event if another show is now selected', async () => {
    vi.setSystemTime(new Date('2026-09-12T10:00:00Z'));
    const router = await mountPage('admin', makeShow('external'));
    await vi.waitFor(() =>
      expect(root.querySelector('[data-testid="pass-live-test"]')).not.toBeNull()
    );
    const button = root.querySelector<HTMLButtonElement>('[data-testid="pass-live-test"]')!;
    flow.selectShow({ ...makeShow(), id: 5 });
    flow.setLiveTestPassed(true);
    // Emit before Vue removes the stale panel from the DOM.
    button.click();
    await nextTick();
    expect(router.currentRoute.value.path).toBe('/shows/39');
    expect(root.querySelector('[data-testid="pass-live-test"]')).toBeNull();
  });
});
