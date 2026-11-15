import { describe, it, expect, vi, beforeEach } from 'vitest';

// Mutable stub the guard reads through the dynamic `import('./composables')`.
const flowStub: { show: { value: unknown }; fetchMyShow: ReturnType<typeof vi.fn> } = {
  show: { value: null },
  fetchMyShow: vi.fn(),
};

vi.mock('../composables', () => ({
  useHostFlow: () => flowStub,
}));

import { createPinia, setActivePinia } from 'pinia';
import { useAuthStore } from '../stores/auth';
import { ensureFlowReady, sendHostToEpisode } from '../router';

describe('ensureFlowReady (flow step guard)', () => {
  beforeEach(() => {
    flowStub.show.value = null;
    flowStub.fetchMyShow = vi.fn();
  });

  it('lets navigation through when a show is already selected (no refetch)', async () => {
    flowStub.show.value = { id: 1, date: '2026-06-20', start_time: '20:00' };
    const next = vi.fn();

    await ensureFlowReady({}, {}, next);

    expect(flowStub.fetchMyShow).not.toHaveBeenCalled();
    expect(next).toHaveBeenCalledWith();
  });

  it('refetches when no show is selected, then proceeds once it resolves', async () => {
    flowStub.fetchMyShow = vi.fn().mockImplementation(async () => {
      flowStub.show.value = { id: 2, date: '2026-06-20', start_time: '21:00' };
    });
    const next = vi.fn();

    await ensureFlowReady({}, {}, next);

    expect(flowStub.fetchMyShow).toHaveBeenCalledOnce();
    expect(next).toHaveBeenCalledWith();
  });

  it('redirects to the smart /stream entry when no show can be resolved', async () => {
    flowStub.fetchMyShow = vi.fn().mockResolvedValue(undefined); // show stays null
    const next = vi.fn();

    await ensureFlowReady({}, {}, next);

    expect(flowStub.fetchMyShow).toHaveBeenCalledOnce();
    expect(next).toHaveBeenCalledWith('/stream');
  });
});

describe('sendHostToEpisode (old upload and confirm steps)', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    flowStub.show.value = null;
    flowStub.fetchMyShow = vi.fn().mockImplementation(async () => {
      flowStub.show.value = { id: 42, date: '2026-10-25', start_time: '12:00' };
    });
  });

  it('sends a host to their episode', async () => {
    useAuthStore().user = { id: 1, username: 'host', role: 'host' };
    const next = vi.fn();

    await sendHostToEpisode({}, {}, next);

    expect(next).toHaveBeenCalledWith('/shows/42');
  });

  it('sends a host without an episode to My Shows', async () => {
    useAuthStore().user = { id: 1, username: 'host', role: 'host' };
    flowStub.fetchMyShow = vi.fn().mockResolvedValue(undefined);
    const next = vi.fn();

    await sendHostToEpisode({}, {}, next);

    expect(next).toHaveBeenCalledWith('/shows');
  });

  it.each(['admin', 'guest'] as const)('keeps the step for a %s', async (role) => {
    useAuthStore().user = { id: 1, username: role, role };
    const next = vi.fn();

    await sendHostToEpisode({}, {}, next);

    expect(next).toHaveBeenCalledWith();
  });
});
