import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { liveStreamShowId } from '../composables/useStreamSocket';
import { reloadOnStaleChunk } from '../chunkReload';

// After a deploy an open tab asks for lazy files that are gone; reload once.
vi.mock('../composables/useStreamSocket', () => ({ liveStreamShowId: vi.fn(() => null) }));

const reload = vi.fn();

beforeEach(() => {
  vi.useFakeTimers();
  sessionStorage.clear();
  reload.mockReset();
  vi.mocked(liveStreamShowId).mockReturnValue(null);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('reloadOnStaleChunk', () => {
  it('reloads when a lazy file fails to load', () => {
    reloadOnStaleChunk(reload);
    expect(reload).toHaveBeenCalledOnce();
  });

  it('does not reload again within 10 s, so missing files cannot loop', () => {
    reloadOnStaleChunk(reload);
    vi.advanceTimersByTime(9_000);
    reloadOnStaleChunk(reload);
    expect(reload).toHaveBeenCalledOnce();

    vi.advanceTimersByTime(2_000);
    reloadOnStaleChunk(reload);
    expect(reload).toHaveBeenCalledTimes(2);
  });

  it('never reloads while this tab is live', () => {
    vi.mocked(liveStreamShowId).mockReturnValue(71);
    reloadOnStaleChunk(reload);
    expect(reload).not.toHaveBeenCalled();
  });

  it('does not reload without session storage', () => {
    vi.stubGlobal('sessionStorage', {
      getItem: () => {
        throw new Error('blocked');
      },
    });
    reloadOnStaleChunk(reload);
    expect(reload).not.toHaveBeenCalled();
  });
});
