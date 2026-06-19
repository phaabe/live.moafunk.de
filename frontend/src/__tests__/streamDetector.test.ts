import { describe, it, expect, vi, afterEach } from 'vitest';
import { checkStreamStatus, checkBackendLive } from '../streamDetector';

afterEach(() => {
  vi.restoreAllMocks();
});

function mockFetch(impl: () => Promise<Partial<Response>> | Partial<Response>) {
  vi.stubGlobal('fetch', vi.fn(impl as unknown as typeof fetch));
}

describe('checkStreamStatus (legacy NMS HEAD poll)', () => {
  it('is live on HTTP 200', async () => {
    mockFetch(() => ({ status: 200 }));
    expect(await checkStreamStatus('https://stream/x.m3u8')).toBe(true);
  });

  it('is offline on non-200', async () => {
    mockFetch(() => ({ status: 404 }));
    expect(await checkStreamStatus('https://stream/x.m3u8')).toBe(false);
  });

  it('is offline when fetch rejects', async () => {
    mockFetch(() => Promise.reject(new Error('network')));
    expect(await checkStreamStatus('https://stream/x.m3u8')).toBe(false);
  });
});

describe('checkBackendLive (Icecast mode, backend /api/stream/status)', () => {
  it('is live only when { active: true }', async () => {
    mockFetch(() => ({ ok: true, json: () => Promise.resolve({ active: true }) }));
    expect(await checkBackendLive('https://admin/api/stream/status')).toBe(true);
  });

  it('is offline when { active: false }', async () => {
    mockFetch(() => ({ ok: true, json: () => Promise.resolve({ active: false }) }));
    expect(await checkBackendLive('https://admin/api/stream/status')).toBe(false);
  });

  it('is offline when active is missing', async () => {
    mockFetch(() => ({ ok: true, json: () => Promise.resolve({ recording: true }) }));
    expect(await checkBackendLive('https://admin/api/stream/status')).toBe(false);
  });

  it('is offline on a non-ok response (no JSON parse)', async () => {
    const json = vi.fn();
    mockFetch(() => ({ ok: false, json }));
    expect(await checkBackendLive('https://admin/api/stream/status')).toBe(false);
    expect(json).not.toHaveBeenCalled();
  });

  it('is offline when fetch rejects', async () => {
    mockFetch(() => Promise.reject(new Error('network')));
    expect(await checkBackendLive('https://admin/api/stream/status')).toBe(false);
  });
});
