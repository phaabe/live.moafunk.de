import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApp, nextTick, type App } from 'vue';
import { createPinia, setActivePinia } from 'pinia';
import { api, type User } from '../api';
import router, { returnPath } from '../router';
import { useAuthStore } from '../stores/auth';
import LoginPage from '../pages/LoginPage.vue';

// An expired session (401) opens the login page; login leads back to the page.
// https://github.com/phaabe/live.moafunk.de/issues/924

const HOST: User = { id: 1, username: 'lena', role: 'host' };
const fetchMock = vi.fn();
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
const expired = () => json({ error: 'Not authenticated' }, 401);

let app: App;
let root: HTMLDivElement;
let auth: ReturnType<typeof useAuthStore>;

beforeEach(async () => {
  fetchMock.mockReset().mockImplementation(async () => json({ ok: true }));
  vi.stubGlobal('fetch', fetchMock);
  const pinia = createPinia();
  setActivePinia(pinia);
  app = createApp(LoginPage).use(pinia).use(router);
  auth = app.runWithContext(() => useAuthStore());
  auth.initialized = true;
  // The app router is shared: start each test on the login page, logged out.
  await router.replace('/login');
  root = document.createElement('div');
  document.body.append(root);
  // Mounted, so unmount() takes this app out of the router: guards run in the first app's context.
  app.mount(root);
});

afterEach(() => {
  app.unmount();
  root.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

const route = () => router.currentRoute.value.fullPath;

async function openAsHost(path: string) {
  auth.user = { ...HOST };
  await router.push(path);
  expect(route()).toBe(path);
}

/** Submit the login page. */
async function logIn(response: Response) {
  const [username, password] = [...root.querySelectorAll('input')];
  username.value = 'lena';
  username.dispatchEvent(new Event('input'));
  password.value = 'secret';
  password.dispatchEvent(new Event('input'));
  await nextTick();
  fetchMock.mockResolvedValueOnce(response);
  root.querySelector('form')?.dispatchEvent(new Event('submit'));
}

describe('expired session', () => {
  it('opens the login page and forgets the user', async () => {
    await openAsHost('/shows/42?tab=artists');
    fetchMock.mockResolvedValueOnce(expired());
    await expect(api.get('/api/my-show')).rejects.toThrow('Not authenticated');
    await vi.waitFor(() =>
      expect(router.currentRoute.value).toMatchObject({
        name: 'login',
        query: { redirect: '/shows/42?tab=artists' },
      })
    );
    expect(auth.user).toBeNull();
  });

  it('returns to that page after login', async () => {
    await openAsHost('/shows/42?tab=artists');
    fetchMock.mockResolvedValueOnce(expired());
    await api.get('/api/my-show').catch(() => undefined);
    await vi.waitFor(() => expect(router.currentRoute.value.name).toBe('login'));
    await logIn(json({ user: HOST }));
    await vi.waitFor(() => expect(route()).toBe('/shows/42?tab=artists'));
    expect(auth.user).toEqual(HOST);
  });

  it('navigates once when parallel calls all get a 401', async () => {
    await openAsHost('/dashboard');
    const push = vi.spyOn(router, 'push');
    fetchMock.mockImplementation(async () => expired());
    await Promise.allSettled([api.get('/api/a'), api.get('/api/b'), api.post('/api/c')]);
    await vi.waitFor(() => expect(route()).toBe('/login?redirect=/dashboard'));
    expect(push).toHaveBeenCalledTimes(1);
  });

  it('sends a 401 on the auth check at page load to login, with the page', async () => {
    auth.initialized = false;
    fetchMock.mockImplementation(async () => expired());
    await router.push('/dashboard');
    expect(route()).toBe('/login?redirect=/dashboard');
    expect(auth.user).toBeNull();
  });

  it('shows a wrong password and stays on the login page', async () => {
    await router.replace('/login?redirect=/dashboard');
    const push = vi.spyOn(router, 'push');
    await logIn(json({ error: 'Invalid username or password' }, 401));
    await vi.waitFor(() => expect(root.textContent).toContain('Invalid username or password'));
    expect(route()).toBe('/login?redirect=/dashboard');
    expect(push).not.toHaveBeenCalled();
  });

  it.each([
    ['logout works', json({ success: true })],
    ['logout works when the session already expired', expired()],
  ])('%s', async (_name, response) => {
    await openAsHost('/dashboard');
    fetchMock.mockResolvedValueOnce(response);
    await auth.logout();
    await vi.waitFor(() => expect(route()).toBe('/login'));
    expect(auth.user).toBeNull();
  });
});

describe('login landing (flow: Login -> Dashboard)', () => {
  it('lands a host on Dashboard without a page to return to', async () => {
    await logIn(json({ user: HOST }));
    await vi.waitFor(() => expect(route()).toBe('/dashboard'));
  });

  it.each(['https://evil.example/', '//evil.example', '/login'])(
    'ignores the redirect %s and lands on Dashboard',
    async (redirect) => {
      await router.replace({ name: 'login', query: { redirect } });
      await logIn(json({ user: HOST }));
      await vi.waitFor(() => expect(route()).toBe('/dashboard'));
    }
  );
});

describe('returnPath', () => {
  it.each([
    ['/shows/42?tab=artists', '/shows/42?tab=artists'],
    ['/stream/live?show=7', '/stream/live?show=7'],
    ['/dashboard', '/dashboard'],
  ])('accepts the app page %s', (value, path) => {
    expect(returnPath(router, value)).toBe(path);
  });

  it.each([
    ['nothing', undefined],
    ['a list', ['/dashboard', '/schedule']],
    ['an empty text', ''],
    ['a relative path', 'dashboard'],
    ['the login page', '/login'],
    ['the login page with its own redirect', '/login?redirect=/dashboard'],
    ['a recovery page', '/reset-password?token=t'],
    ['an unknown page', '/nope'],
    ['another site', 'https://evil.example/dashboard'],
    ['a protocol-relative URL', '//evil.example/dashboard'],
    ['a backslash URL', '/\\evil.example'],
    ['a script URL', 'javascript:alert(1)'],
  ])('rejects %s', (_name, value) => {
    expect(returnPath(router, value)).toBeNull();
  });
});
