import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApp, h, nextTick } from 'vue';
import { createPinia, setActivePinia } from 'pinia';
import { RouterView } from 'vue-router';
import router from '../router';
import { useAuthStore } from '../stores/auth';
import { useRecovery } from '../composables/useRecovery';

const fetchMock = vi.fn();

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock);
  fetchMock.mockReset();
});
afterEach(() => {
  vi.unstubAllGlobals();
});

describe('recovery requests', () => {
  it('keeps the verified address until the replacement is verified', async () => {
    fetchMock
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            enabled: true,
            eligible: true,
            email: 'old@example.com',
          })
        )
      )
      .mockResolvedValueOnce(new Response('{"success":true}'));
    const recovery = useRecovery();
    await recovery.load();
    expect(await recovery.enroll('new@example.com', 'current-password')).toBe(true);
    expect(recovery.status.value?.email).toBe('old@example.com');
    expect(recovery.message.value).toContain('verify');
    const [url, options] = fetchMock.mock.calls[1];
    expect(url).toBe('/api/auth/recovery-email');
    expect(JSON.parse(options.body)).toEqual({
      email: 'new@example.com',
      current_password: 'current-password',
    });
  });

  it('shows the same server response for a reset request', async () => {
    const message = 'If this address is eligible, a reset link will be sent.';
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ success: true, message })));
    const recovery = useRecovery();
    await recovery.request('unknown@example.com');
    expect(recovery.message.value).toBe(message);
  });

  it('shows link errors and allows another attempt', async () => {
    fetchMock.mockResolvedValueOnce(new Response('{"error":"Link expired"}', { status: 400 }));
    const recovery = useRecovery();
    expect(await recovery.complete('expired', 'new-password')).toBe(false);
    expect(recovery.error.value).toBe('Link expired');
    expect(recovery.loading.value).toBe(false);
    fetchMock.mockResolvedValueOnce(new Response('{"success":true}'));
    expect(await recovery.complete('fresh', 'new-password')).toBe(true);
    expect(recovery.error.value).toBe('');
  });

  it('clears the displayed email only after successful removal', async () => {
    const recovery = useRecovery();
    recovery.status.value = { enabled: true, eligible: true, email: 'old@example.com' };
    fetchMock.mockResolvedValueOnce(new Response('{"error":"Wrong password"}', { status: 403 }));
    await recovery.remove('wrong-password');
    expect(recovery.status.value.email).toBe('old@example.com');
    fetchMock.mockResolvedValueOnce(new Response('{"success":true}'));
    await recovery.remove('current-password');
    expect(recovery.status.value.email).toBeNull();
  });
});

describe('public recovery pages', () => {
  it('bypasses forced-password redirection and consumes verification only on submit', async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const app = createApp({ render: () => h(RouterView) });
    app.use(pinia);
    app.use(router);
    const store = app.runWithContext(() => useAuthStore());
    store.initialized = true;
    store.user = { id: 1, username: 'host', role: 'host', must_change_password: true };
    const container = document.createElement('div');
    document.body.append(container);
    app.mount(container);
    try {
      for (const name of ['forgot-password', 'reset-password', 'verify-email']) {
        await router.push({ name, query: { token: 'test-link' } });
        expect(router.currentRoute.value.name).toBe(name);
      }
      await nextTick();
      expect(fetchMock).not.toHaveBeenCalled();
      fetchMock.mockResolvedValue(new Response('{"success":true}'));
      container.querySelector('form')?.dispatchEvent(new Event('submit', { cancelable: true }));
      await vi.waitFor(() => expect(container.textContent).toContain('Recovery email verified.'));
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(fetchMock.mock.calls[0][0]).toBe('/api/auth/recovery-email/verify');
      // RouterView can reuse the component across recovery pages and new links.
      await router.push('/reset-password?token=first-link');
      await nextTick();
      expect(container.querySelector('form')).not.toBeNull();
      expect(container.querySelector('[role="status"]')).toBeNull();
      const input = container.querySelector('input');
      if (!input) throw new Error('Reset password input missing');
      input.value = 'too-short';
      input.dispatchEvent(new Event('input'));
      container.querySelector('form')?.dispatchEvent(new Event('submit', { cancelable: true }));
      await nextTick();
      expect(container.querySelector('[role="alert"]')).not.toBeNull();
      await router.push('/reset-password?token=second-link');
      await nextTick();
      expect(container.querySelector('[role="alert"]')).toBeNull();
      expect(container.querySelector('input')?.value).toBe('');
      expect(fetchMock).toHaveBeenCalledTimes(1);
      await router.push('/users');
      expect(router.currentRoute.value.name).toBe('change-password');
    } finally {
      app.unmount();
      container.remove();
    }
  });

  it('lets logged-out users reset and returns a cached logged-in user to normal login', async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const app = createApp({ render: () => h(RouterView) });
    app.use(pinia);
    app.use(router);
    const store = app.runWithContext(() => useAuthStore());
    store.initialized = true;
    const container = document.createElement('div');
    document.body.append(container);
    app.mount(container);
    try {
      await router.push('/reset-password?token=reset-link');
      expect(router.currentRoute.value.name).toBe('reset-password');
      await nextTick();
      expect(fetchMock).not.toHaveBeenCalled();
      store.user = { id: 1, username: 'host', role: 'host', must_change_password: true };
      for (const input of container.querySelectorAll('input')) {
        input.value = 'new-password';
        input.dispatchEvent(new Event('input'));
      }
      fetchMock.mockResolvedValue(new Response('{"success":true}'));
      container.querySelector('form')?.dispatchEvent(new Event('submit', { cancelable: true }));
      await vi.waitFor(() => expect(container.textContent).toContain('Password changed.'));
      expect(store.user).toBeNull();
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(fetchMock.mock.calls[0][0]).toBe('/api/auth/password-reset/complete');
      await router.push('/login');
      expect(router.currentRoute.value.name).toBe('login');
    } finally {
      app.unmount();
      container.remove();
    }
  });
});
