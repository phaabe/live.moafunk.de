import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// The Studio opens 30 min before the start and closes at the end time
// unless the episode is still on air. On Air needs a live broadcast in
// this tab. Times: 2026-11-15 12:00–14:00 Berlin = 11:00–13:00 UTC.

type Show = { id: number; date: string; start_time?: string; end_time?: string } & Record<
  string,
  unknown
>;

const stub = vi.hoisted(() => ({
  show: { value: null as Show | null },
  showId: { value: null as number | null },
  shows: { value: [] as Show[] },
  uploadMode: { value: null as string | null },
  liveShow: null as number | null,
  onAir: false,
  fetchMyShow: vi.fn(),
  selectShow: vi.fn(),
  selectMode: vi.fn(),
  setLiveTestPassed: vi.fn(),
}));

vi.mock('../composables', () => ({
  useHostFlow: () => stub,
  liveStreamShowId: () => stub.liveShow,
}));
vi.mock('../api', () => ({
  showsApi: { get: vi.fn(async () => ({ on_air: stub.onAir })) },
  setUnauthorizedHandler: vi.fn(),
}));

import { onAirGate, studioGate } from '../router';

const episode = (overrides: Partial<Show> = {}): Show => ({
  id: 7,
  date: '2026-11-15',
  start_time: '12:00',
  end_time: '14:00',
  ...overrides,
});

function select(show: Show) {
  stub.show.value = show;
  stub.showId.value = show.id;
}

async function gate(guard: typeof studioGate, at: string, query: Record<string, string> = {}) {
  vi.setSystemTime(new Date(at));
  const next = vi.fn();
  await guard({ query }, {}, next);
  return next;
}

beforeEach(() => {
  vi.useFakeTimers();
  select(episode());
  stub.shows.value = [];
  stub.uploadMode.value = null;
  stub.liveShow = null;
  stub.onAir = false;
  stub.fetchMyShow = vi.fn();
  stub.selectShow = vi.fn((show: Show) => select(show));
  stub.selectMode = vi.fn();
  stub.setLiveTestPassed = vi.fn();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('Studio gate', () => {
  it('sends the host to the episode until 30 min before the start', async () => {
    expect(await gate(studioGate, '2026-11-15T10:29:00Z')).toHaveBeenCalledWith('/shows/7');
  });

  it('opens 30 min before the start, in live mode', async () => {
    const next = await gate(studioGate, '2026-11-15T10:30:00Z');
    expect(next).toHaveBeenCalledWith();
    expect(stub.selectMode).toHaveBeenCalledWith('live');
  });

  it('closes after the end time unless the episode is still on air', async () => {
    expect(await gate(studioGate, '2026-11-15T13:05:00Z')).toHaveBeenCalledWith('/shows/7');
    stub.onAir = true;
    expect(await gate(studioGate, '2026-11-15T13:05:00Z')).toHaveBeenCalledWith();
  });

  it('has no Studio for a pre-recorded episode', async () => {
    select(episode({ prerecorded_confirmed_at: '2026-11-10T10:00:00Z' }));
    expect(await gate(studioGate, '2026-11-15T11:30:00Z')).toHaveBeenCalledWith('/shows/7');
  });

  it('goes back to On Air while this tab is live', async () => {
    stub.liveShow = 7;
    expect(await gate(studioGate, '2026-11-15T11:30:00Z')).toHaveBeenCalledWith({
      path: '/stream/on-air',
      query: { show: '7' },
    });
  });

  it('selects the episode named in the address, so a reload keeps it', async () => {
    stub.show.value = null;
    stub.showId.value = null;
    stub.fetchMyShow = vi.fn(async () => {
      stub.shows.value = [episode({ id: 6, date: '2026-11-08' }), episode()];
    });
    const next = await gate(studioGate, '2026-11-15T11:30:00Z', { show: '7' });
    expect(stub.selectShow).toHaveBeenCalledWith(expect.objectContaining({ id: 7 }));
    expect(stub.setLiveTestPassed).toHaveBeenCalledWith(false);
    expect(next).toHaveBeenCalledWith();
  });

  it('sends an episode that is not the user’s to its page', async () => {
    expect(await gate(studioGate, '2026-11-15T11:30:00Z', { show: '99' })).toHaveBeenCalledWith(
      '/shows/99'
    );
  });
});

describe('On Air gate', () => {
  it('sends a live episode to the Studio while nothing is live', async () => {
    stub.uploadMode.value = 'live';
    expect(await gate(onAirGate, '2026-11-15T11:30:00Z')).toHaveBeenCalledWith({
      path: '/stream/live',
      query: { show: '7' },
    });
  });

  it('shows On Air while this tab is live', async () => {
    stub.uploadMode.value = 'live';
    stub.liveShow = 7;
    expect(await gate(onAirGate, '2026-11-15T11:30:00Z')).toHaveBeenCalledWith();
  });

  it('keeps the pre-recorded waiting room', async () => {
    stub.uploadMode.value = 'prerecorded';
    expect(await gate(onAirGate, '2026-11-15T09:00:00Z')).toHaveBeenCalledWith();
  });
});
