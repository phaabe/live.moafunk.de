import { describe, it, expect, vi } from 'vitest';
import { shouldReload, startVersionWatcher } from '../src/versionWatcher';

describe('shouldReload', () => {
  it('reloads when the deployed build changed and audio is idle', () => {
    expect(shouldReload('abc123', 'def456', false)).toBe(true);
  });

  it('defers (no reload) while audio is actively playing', () => {
    expect(shouldReload('abc123', 'def456', true)).toBe(false);
  });

  it('does not reload when the version is unchanged', () => {
    expect(shouldReload('abc123', 'abc123', false)).toBe(false);
  });

  it('does not reload when the latest version is missing (fetch failed)', () => {
    expect(shouldReload('abc123', null, false)).toBe(false);
    expect(shouldReload('abc123', undefined, false)).toBe(false);
  });
});

describe('startVersionWatcher', () => {
  it('reloads once a changed version is seen while idle, not before', async () => {
    vi.useFakeTimers();
    const reload = vi.fn();
    let deployed = 'build-1';
    const fetchVersion = vi.fn(async () => deployed);

    startVersionWatcher({
      isPlaying: () => false,
      intervalMs: 1000,
      reload,
      // currentBuildId() is 'dev' in tests; serve a matching id first so nothing reloads.
      fetchVersion: async () => {
        const v = await fetchVersion();
        return v === 'build-1' ? 'dev' : v;
      },
    });

    await vi.advanceTimersByTimeAsync(1000);
    expect(reload).not.toHaveBeenCalled();

    deployed = 'build-2'; // a new deploy lands
    await vi.advanceTimersByTimeAsync(1000);
    expect(reload).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
  });

  it('never reloads while playback is active even if the version changed', async () => {
    vi.useFakeTimers();
    const reload = vi.fn();
    startVersionWatcher({
      isPlaying: () => true,
      intervalMs: 1000,
      reload,
      fetchVersion: async () => 'a-different-build',
    });
    await vi.advanceTimersByTimeAsync(3000);
    expect(reload).not.toHaveBeenCalled();
    vi.useRealTimers();
  });
});
