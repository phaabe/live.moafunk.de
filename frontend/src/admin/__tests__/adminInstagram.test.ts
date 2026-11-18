import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApp, h, nextTick, type App } from 'vue';
import { createPinia, setActivePinia } from 'pinia';
import { createMemoryHistory, createRouter, RouterView } from 'vue-router';
import { useAuthStore } from '../stores/auth';
import appRouter from '../router';
import AdminInstagramPage from '../pages/AdminInstagramPage.vue';
import { parseSlots, statusPill, weekOf } from '../instagramQueue';
import type { AnnouncementSettings, InstagramStatus, QueuedAnnouncement } from '../api';
import { expectFlow } from '../flow/expectFlow';

// The flow's example week: Wednesday 21.10.2026, 12:00 Berlin.
const NOW = new Date('2026-10-21T10:00:00Z');

const STATUS: InstagramStatus = {
  account: 'prod',
  account_username: 'moafunk_radio',
  token_expires_at: '2027-01-15T12:00:00Z',
  token_refreshed_at: '2026-11-16T03:00:00Z',
  quota_used: 3,
  quota_total: 25,
};

const SETTINGS: AnnouncementSettings = {
  limit_per_episode: 3,
  feed_per_day: 1,
  feed_per_week: 5,
  stories_per_day: 6,
  pre_days_before: 2,
  post_days_after: 3,
  slots: ['12:00', '18:00'],
  approval_mode: 'first_two',
  paused: false,
};

const row = (r: Partial<QueuedAnnouncement> & Pick<QueuedAnnouncement, 'id' | 'slot' | 'state'>) =>
  ({
    show_id: 42,
    show_title: 'Brunchtime #42',
    series_name: 'Brunchtime',
    host_handle: 'lena',
    scheduled_at: null,
    sent_at: null,
    permalink: null,
    error: null,
    ...r,
  }) as QueuedAnnouncement;

const ROWS: QueuedAnnouncement[] = [
  row({ id: 1, slot: 'pre', state: 'pending_approval', scheduled_at: '2026-10-23T16:00:00Z' }),
  row({ id: 2, slot: 'onair', state: 'armed', scheduled_at: '2026-10-25T11:00:00Z' }),
  row({ id: 3, slot: 'post', state: 'waiting_soundcloud', scheduled_at: '2026-10-26T17:00:00Z' }),
  row({
    id: 4,
    show_id: 39,
    series_name: 'Night Shift',
    host_handle: 'jonas',
    slot: 'onair',
    state: 'failed',
    scheduled_at: '2026-10-21T18:14:00Z',
    error: 'image URL not reachable',
  }),
];

let app: App | undefined;
let root: HTMLDivElement;
let rows: QueuedAnnouncement[];
let status: InstagramStatus;
let calls: { method: string; url: string; body?: unknown }[];

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(NOW);
  rows = ROWS;
  status = STATUS;
  calls = [];
  root = document.createElement('div');
  document.body.appendChild(root);
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, options?: RequestInit) => {
      const method = options?.method ?? 'GET';
      const body = options?.body ? JSON.parse(options.body as string) : undefined;
      calls.push({ method, url, body });
      if (url === '/api/admin/instagram/status') return Response.json(status);
      if (url === '/api/settings/announcements')
        return Response.json(method === 'PUT' ? body : SETTINGS);
      if (url.startsWith('/api/admin/announcements?')) return Response.json(rows);
      const act = url.match(/^\/api\/admin\/announcements\/(\d+)\/(approve|reject|retry)$/);
      if (act && method === 'POST') {
        const next = { approve: 'scheduled', reject: 'rejected', retry: 'scheduled' }[act[2]];
        const found = rows.find((r) => r.id === Number(act[1]))!;
        return Response.json({ ...found, state: next, error: null });
      }
      throw new Error(`Unexpected API request: ${method} ${url}`);
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

async function mount(): Promise<void> {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/admin/instagram', component: AdminInstagramPage },
      { path: '/shows/:id', component: { render: () => h('div', 'Episode') } },
    ],
  });
  app = createApp({ render: () => h(RouterView) });
  app.use(createPinia()).use(router);
  await router.push('/admin/instagram');
  await router.isReady();
  app.mount(root);
  await vi.waitFor(() => expect(root.querySelectorAll('.queue-row').length).toBeGreaterThan(0));
  await nextTick();
}

function button(text: string, scope: ParentNode = root): HTMLButtonElement {
  const found = [...scope.querySelectorAll('button')].find((b) => b.textContent?.trim() === text);
  if (!found) throw new Error(`No button "${text}"`);
  return found;
}

const rowEl = (i: number) => root.querySelectorAll('.queue-row')[i];

function field(name: string, value: string) {
  const el = root.querySelector(`input[name="${name}"]`) as HTMLInputElement;
  el.value = value;
  el.dispatchEvent(new Event('input'));
}

describe('Instagram queue', () => {
  it('matches the flow', async () => {
    await mount();
    await expectFlow('admin-instagram', root);
  });

  it('loads this week and lists every state with its actions', async () => {
    await mount();
    expect(calls).toContainEqual({
      method: 'GET',
      url: '/api/admin/announcements?from=2026-10-19&to=2026-10-25',
      body: undefined,
    });
    const text = [...root.querySelectorAll('.queue-row a.board-row')].map((a) =>
      [...a.children].map((c) => c.textContent).join(' | ')
    );
    expect(text).toEqual([
      'Fr 23.10. · 18:00 · Pre | Brunchtime · lena · Needs approval',
      'So 25.10. · at go-live · On air | Brunchtime · lena · Armed',
      'Mo 26.10. · 18:00 · Post | Brunchtime · lena · Waiting for SoundCloud',
      'Mi 21.10. · 20:14 · On air | Night Shift · jonas · Failed · image URL not reachable',
    ]);
    expect(rowEl(0).querySelector('a')?.getAttribute('href')).toBe('/shows/42');
    const actions = (i: number) =>
      [...rowEl(i).querySelectorAll('.actions button')].map((b) => b.textContent?.trim());
    expect(actions(0)).toEqual(['Approve', 'Reject']);
    expect(actions(1)).toEqual([]);
    expect(actions(3)).toEqual(['Retry']);
    expect(root.textContent).toContain('Feed posts 2 / 5 · stories today 0 / 6');
  });

  it('links a sent post to Instagram', async () => {
    rows = [
      row({
        id: 5,
        slot: 'pre',
        state: 'sent',
        sent_at: '2026-10-20T10:00:00Z',
        permalink: 'https://www.instagram.com/p/abc/',
      }),
    ];
    await mount();
    const open = rowEl(0).querySelector('.actions a') as HTMLAnchorElement;
    expect(open.textContent?.trim()).toBe('Open');
    expect(open.href).toBe('https://www.instagram.com/p/abc/');
    expect(rowEl(0).textContent).toContain('Di 20.10. · 12:00 · Pre');
    expect(rowEl(0).textContent).toContain('Posted');
  });

  it.each([
    [0, 'Approve', '/api/admin/announcements/1/approve', 'Scheduled'],
    [0, 'Reject', '/api/admin/announcements/1/reject', 'Rejected'],
    [3, 'Retry', '/api/admin/announcements/4/retry', 'Scheduled'],
  ])('row %i: %s posts to the API and shows the new state', async (i, label, url, state) => {
    await mount();
    button(label, rowEl(i)).click();
    await vi.waitFor(() => expect(rowEl(i).textContent).toContain(state));
    expect(calls).toContainEqual({ method: 'POST', url, body: undefined });
    expect(rowEl(i).querySelectorAll('.actions button')).toHaveLength(0);
  });

  it('saves the policy', async () => {
    await mount();
    field('feed_per_week', '4');
    field('post_days_after', '5');
    field('slots', '12:00,18:00, 20:30');
    (root.querySelectorAll('input[name="approval"]')[1] as HTMLInputElement).click();
    await nextTick();
    button('Save policy').click();
    await vi.waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true));
    expect(calls.find((c) => c.method === 'PUT')).toEqual({
      method: 'PUT',
      url: '/api/settings/announcements',
      body: {
        ...SETTINGS,
        feed_per_week: 4,
        post_days_after: 5,
        slots: ['12:00', '18:00', '20:30'],
        approval_mode: 'always',
      },
    });
    await vi.waitFor(() =>
      expect((root.querySelector('input[name="slots"]') as HTMLInputElement).value).toBe(
        '12:00, 18:00, 20:30'
      )
    );
  });

  it.each([
    ['slots', '25:00', 'Slots: enter times like 12:00, 18:00.'],
    ['slots', '', 'Slots: enter times like 12:00, 18:00.'],
    ['feed_per_day', '-1', 'Feed posts per day: enter a whole number, 0 or more.'],
    ['stories_per_day', '', 'Stories per day: enter a whole number, 0 or more.'],
    ['post_days_after', '-1', 'Post-announcement days after: enter a whole number, 0 or more.'],
    ['post_days_after', '1.5', 'Post-announcement days after: enter a whole number, 0 or more.'],
    ['limit_per_episode', '4', 'Announcements per episode: at most 3.'],
  ])('refuses %s = "%s"', async (name, value, message) => {
    await mount();
    field(name, value);
    await nextTick();
    button('Save policy').click();
    await vi.waitFor(() => expect(root.querySelector('[role="alert"]')?.textContent).toBe(message));
    expect(calls.some((c) => c.method === 'PUT')).toBe(false);
  });

  it('pauses and resumes all posting', async () => {
    await mount();
    button('Pause all posting').click();
    await vi.waitFor(() => expect(button('Resume posting')).toBeTruthy());
    expect(calls.at(-1)).toEqual({
      method: 'PUT',
      url: '/api/settings/announcements',
      body: { ...SETTINGS, paused: true },
    });
    expect(root.textContent).toContain('All posting is paused.');
    button('Resume posting').click();
    await vi.waitFor(() => expect(button('Pause all posting')).toBeTruthy());
    expect(calls.at(-1)?.body).toEqual({ ...SETTINGS, paused: false });
  });

  it('shows the test account on staging and warns before the token expires', async () => {
    status = {
      ...STATUS,
      account: 'dev',
      account_username: 'moafunk_tester',
      token_expires_at: '2026-10-30T12:00:00Z',
    };
    await mount();
    const pill = root.querySelector('.pill')!;
    expect(pill.textContent).toBe('Posts to @moafunk_tester (test account) · token expires 30.10.');
    expect(pill.classList).toContain('warn');
    expect(root.textContent).toContain('Token refreshed 16.11. · API quota 3 / 25');
  });
});

describe('Instagram queue helpers', () => {
  it('names the account and the token state', () => {
    const at = (expires: string | null) =>
      statusPill({ ...STATUS, token_expires_at: expires }, NOW);
    expect(at('2027-01-15T12:00:00Z')).toEqual({
      text: 'Posts to @moafunk_radio · token valid to 15.01.',
      tone: 'ok',
    });
    expect(at('2026-11-03T12:00:00Z').tone).toBe('warn');
    expect(at('2026-11-05T12:00:00Z').tone).toBe('ok');
    expect(at('2026-10-20T12:00:00Z')).toEqual({
      text: 'Posts to @moafunk_radio · token expired 20.10.',
      tone: 'failed',
    });
    expect(at(null).tone).toBe('failed');
  });

  it('writes the week across a month end', () => {
    expect(weekOf('2026-10-21').title).toBe('This week · 19.–25.10.');
    expect(weekOf('2026-10-29')).toEqual({
      from: '2026-10-26',
      to: '2026-11-01',
      title: 'This week · 26.10.–01.11.',
    });
  });

  it('parses slots', () => {
    expect(parseSlots(' 12:00 , 18:00 ')).toEqual(['12:00', '18:00']);
    expect(parseSlots('9:00')).toBeNull();
    expect(parseSlots('')).toBeNull();
  });
});

describe('Instagram queue route', () => {
  beforeEach(async () => {
    setActivePinia(createPinia());
    useAuthStore().initialized = true;
    await appRouter.replace('/login');
  });

  it('sends a host to Dashboard', async () => {
    useAuthStore().user = { id: 1, username: 'lena', role: 'host' };
    await appRouter.push('/admin/instagram');
    expect(appRouter.currentRoute.value.fullPath).toBe('/dashboard');
  });

  it('opens for an admin', async () => {
    useAuthStore().user = { id: 2, username: 'admin', role: 'admin' };
    await appRouter.push('/admin/instagram');
    expect(appRouter.currentRoute.value.fullPath).toBe('/admin/instagram');
  });
});
