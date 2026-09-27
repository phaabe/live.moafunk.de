import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  createApp,
  defineComponent,
  effectScope,
  h,
  KeepAlive,
  nextTick,
  ref,
  type EffectScope,
} from 'vue';
import { useMetadataRefresh } from '../useMetadataRefresh';

let scope: EffectScope;
beforeEach(() => {
  vi.useFakeTimers();
  scope = effectScope();
});
afterEach(() => {
  scope.stop();
  vi.useRealTimers();
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

describe('metadata refresh lifecycle', () => {
  it('ignores an older response after a newer refresh', async () => {
    const old = deferred<string>();
    let metadata = '';
    const load = vi.fn().mockReturnValueOnce(old.promise).mockResolvedValueOnce('new host');
    const refresh = scope.run(() =>
      useMetadataRefresh(load, (value: string) => {
        metadata = value;
      })
    )!;
    const pending = refresh.refresh();
    await refresh.refresh();
    old.resolve('old host');
    await pending;
    expect(metadata).toBe('new host');
  });

  it('stops timers and focus requests and ignores in-flight responses on disposal', async () => {
    const response = deferred<string>();
    const load = vi.fn(() => response.promise);
    const apply = vi.fn();
    const refresh = scope.run(() => useMetadataRefresh(load, apply))!;
    const pending = refresh.refresh();
    scope.stop();
    response.resolve('late result');
    await pending;
    window.dispatchEvent(new Event('focus'));
    await vi.advanceTimersByTimeAsync(60_000);
    await refresh.refresh();
    expect(apply).not.toHaveBeenCalled();
    expect(load).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('retains metadata and exposes errors, then recovers on focus', async () => {
    let metadata = 'last known';
    const load = vi.fn().mockRejectedValueOnce(new Error('Offline')).mockResolvedValue('refreshed');
    const refresh = scope.run(() =>
      useMetadataRefresh(load, (value: string) => {
        metadata = value;
      })
    )!;
    await refresh.refresh();
    expect(metadata).toBe('last known');
    expect(refresh.error.value).toBe('Offline');
    window.dispatchEvent(new Event('focus'));
    await vi.advanceTimersByTimeAsync(0);
    expect(metadata).toBe('refreshed');
    expect(refresh.error.value).toBeNull();
  });
});

describe('metadata refresh with KeepAlive', () => {
  it('pauses on deactivation, rejects pending responses, and refreshes on reactivation', async () => {
    const old = deferred<string>();
    const load = vi.fn().mockReturnValueOnce(old.promise).mockResolvedValue('fresh');
    const applied: string[] = [];
    const visible = ref(true);
    const Cached = defineComponent({
      setup() {
        useMetadataRefresh(load, (value: string) => {
          applied.push(value);
        });
        return () => h('div', 'cached detail');
      },
    });
    const root = document.createElement('div');
    const app = createApp({
      render: () =>
        h(KeepAlive, null, { default: () => (visible.value ? h(Cached) : h('div', 'logged out')) }),
    });
    app.mount(root);
    try {
      window.dispatchEvent(new Event('focus'));
      expect(load).toHaveBeenCalledTimes(1);
      visible.value = false;
      await nextTick();
      window.dispatchEvent(new Event('focus'));
      await vi.advanceTimersByTimeAsync(60_000);
      expect(load).toHaveBeenCalledTimes(1);
      expect(vi.getTimerCount()).toBe(0);
      visible.value = true;
      await nextTick();
      await vi.advanceTimersByTimeAsync(0);
      expect(load).toHaveBeenCalledTimes(2);
      expect(applied).toEqual(['fresh']);
      old.resolve('stale before logout');
      await vi.advanceTimersByTimeAsync(0);
      expect(applied).toEqual(['fresh']);
      await vi.advanceTimersByTimeAsync(30_000);
      expect(load).toHaveBeenCalledTimes(3);
      window.dispatchEvent(new Event('focus'));
      await vi.advanceTimersByTimeAsync(0);
      expect(load).toHaveBeenCalledTimes(4);
    } finally {
      app.unmount();
      root.remove();
    }
    window.dispatchEvent(new Event('focus'));
    await vi.advanceTimersByTimeAsync(60_000);
    expect(load).toHaveBeenCalledTimes(4);
    expect(vi.getTimerCount()).toBe(0);
  });
});
