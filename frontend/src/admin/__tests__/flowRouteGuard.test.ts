import { describe, it, expect, vi, beforeEach } from 'vitest';

// Mutable stub the guard reads through the dynamic `import('./composables')`.
const flowStub: { show: { value: unknown }; fetchMyShow: ReturnType<typeof vi.fn> } = {
  show: { value: null },
  fetchMyShow: vi.fn(),
};

vi.mock('../composables', () => ({
  useHostFlow: () => flowStub,
}));

import { ensureFlowReady } from '../router';

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
