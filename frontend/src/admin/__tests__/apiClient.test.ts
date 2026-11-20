import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import {
  api,
  authApi,
  hostFlowApi,
  recordingApi,
  seriesApi,
  setUnauthorizedHandler,
  streamApi,
} from '../api';
import router, { expireSession } from '../router';
import { useAuthStore } from '../stores/auth';

const MB = 1024 * 1024;
const fetchMock = vi.fn();

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });

/** A file of any size without the bytes; slice() returns the byte range as text. */
function fakeFile(name: string, size: number): File {
  const file = new File(['x'], name, { type: 'audio/mpeg' });
  Object.defineProperty(file, 'size', { value: size });
  file.slice = (start = 0, end = size) => new Blob([`${start}-${end}`]);
  return file;
}

// jsdom's Blob has no text().
function readText(blob: Blob): Promise<string> {
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as string);
    reader.readAsText(blob);
  });
}

const requests = () =>
  fetchMock.mock.calls.map(([url, init]: [string, RequestInit]) => ({
    url,
    method: init.method ?? 'GET',
    body: init.body,
  }));

beforeEach(() => {
  setActivePinia(createPinia());
  fetchMock.mockReset().mockImplementation(async () => json({ ok: true }));
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('api client errors', () => {
  it('passes the server error text on to the page', async () => {
    fetchMock.mockResolvedValueOnce(json({ error: 'This slot was just taken' }, 409));
    await expect(api.post('/api/x', { a: 1 })).rejects.toThrow('This slot was just taken');
  });

  it('uses the details when the server sends no error text', async () => {
    fetchMock.mockResolvedValueOnce(json({ details: 'Cover is too small' }, 400));
    await expect(api.get('/api/x')).rejects.toThrow('Cover is too small');
  });

  it('names the HTTP status when the error is not JSON (proxy page)', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response('<html>502</html>', { status: 502, statusText: 'Bad Gateway' })
    );
    await expect(api.get('/api/x')).rejects.toThrow('HTTP 502: Bad Gateway');
  });

  it('says "Unknown error" for an empty JSON error', async () => {
    fetchMock.mockResolvedValueOnce(json({}, 500));
    await expect(api.get('/api/x')).rejects.toThrow('Unknown error');
  });

  it('rejects an expired session with the server text', async () => {
    fetchMock.mockResolvedValueOnce(json({ error: 'Not authenticated' }, 401));
    await expect(authApi.me()).rejects.toThrow('Not authenticated');
  });

  // https://github.com/phaabe/live.moafunk.de/issues/924
  it('sends the user to the login page when the session has expired', async () => {
    const auth = useAuthStore();
    auth.initialized = true;
    auth.user = { id: 1, username: 'lena', role: 'host' };
    await router.push('/shows/42');
    fetchMock.mockResolvedValueOnce(json({ error: 'Not authenticated' }, 401));
    await hostFlowApi.getMyShows().catch(() => undefined);
    await vi.waitFor(() => expect(window.location.hash).toBe('#/login?redirect=/shows/42'));
    expect(auth.user).toBeNull();
  });

  it('resolves with nothing on 204 No Content', async () => {
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }));
    await expect(api.delete('/api/x')).resolves.toBeUndefined();
  });

  it('sends the login cookie and JSON with every call', async () => {
    await authApi.login('host', 'pw');
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/auth/login');
    expect(init).toMatchObject({
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
    });
    expect(JSON.parse(init.body)).toEqual({ username: 'host', password: 'pw' });
  });
});

describe('expired session', () => {
  const expired = vi.fn();
  const notAuthenticated = () => json({ error: 'Not authenticated' }, 401);

  beforeEach(() => {
    expired.mockClear();
    setUnauthorizedHandler(expired);
  });

  afterEach(() => {
    setUnauthorizedHandler(expireSession);
  });

  it('does not treat a wrong password at login as an expired session', async () => {
    fetchMock.mockResolvedValueOnce(json({ error: 'Invalid username or password' }, 401));
    await expect(authApi.login('lena', 'wrong')).rejects.toThrow('Invalid username or password');
    expect(expired).not.toHaveBeenCalled();
  });

  it.each([
    ['a JSON call', () => hostFlowApi.getMyShows()],
    ['the auth check', () => authApi.me()],
    ['a series page', () => seriesApi.get('brunchtime')],
    ['a set upload', () => hostFlowApi.uploadFile(7, fakeFile('set.mp3', MB))],
  ])('ends the session on a 401 from %s', async (_name, call) => {
    fetchMock.mockResolvedValueOnce(notAuthenticated());
    await call().catch(() => undefined);
    expect(expired).toHaveBeenCalledTimes(1);
  });

  it.each([403, 500])('keeps the session on a %s', async (status) => {
    fetchMock.mockResolvedValueOnce(json({ error: 'No' }, status));
    await hostFlowApi.getMyShows().catch(() => undefined);
    expect(expired).not.toHaveBeenCalled();
  });
});

describe('show day calls', () => {
  const marker = {
    artist_id: 3,
    track_type: 'track1' as const,
    track_key: 'artists/3/track1.mp3',
    duration_ms: 180000,
    offset_ms: 60000,
    volume: 0.8,
  };

  it.each([
    ['my shows', () => hostFlowApi.getMyShows(), 'GET', '/api/my-show', undefined],
    ['confirm', () => hostFlowApi.confirm(7), 'POST', '/api/my-show/confirm?show_id=7', undefined],
    ['go live', () => hostFlowApi.goLive(7), 'POST', '/api/my-show/go-live?show_id=7', undefined],
    [
      'delete set',
      () => hostFlowApi.deleteUpload(7),
      'DELETE',
      '/api/my-show/upload?show_id=7',
      undefined,
    ],
    ['stream status', () => streamApi.status(), 'GET', '/api/stream/status', undefined],
    ['stream metrics', () => streamApi.metrics(), 'GET', '/api/stream/metrics', undefined],
    ['stop stream', () => streamApi.stop(), 'POST', '/api/stream/stop', undefined],
    [
      'show with artists',
      () => recordingApi.getShowWithArtists(7),
      'GET',
      '/api/shows/7/with-artists',
      undefined,
    ],
    [
      'start recording',
      () => recordingApi.start(7),
      'POST',
      '/api/recording/start',
      { show_id: 7 },
    ],
    ['stop recording', () => recordingApi.stop(), 'POST', '/api/recording/stop', undefined],
    ['recording status', () => recordingApi.status(), 'GET', '/api/recording/status', undefined],
    ['add marker', () => recordingApi.addMarker(marker), 'POST', '/api/recording/marker', marker],
    [
      'recordings',
      () => recordingApi.listRecordings(7),
      'GET',
      '/api/shows/7/recordings',
      undefined,
    ],
  ])('%s → %s %s', async (_name, call, method, url, body) => {
    await call();
    const [req] = requests();
    expect(req.url).toBe(url);
    expect(req.method).toBe(method);
    expect(req.body === undefined ? undefined : JSON.parse(req.body as string)).toEqual(body);
  });
});

describe('host set upload', () => {
  it('sends a set up to 50 MB in one request', async () => {
    fetchMock.mockResolvedValueOnce(json({ success: true, key: 'k', filename: 'set.mp3' }));
    const progress: number[] = [];
    const result = await hostFlowApi.uploadFile(7, fakeFile('set.mp3', 50 * MB), (p) =>
      progress.push(p.percent)
    );
    expect(result.key).toBe('k');
    const [req] = requests();
    expect([req.method, req.url]).toEqual(['POST', '/api/my-show/upload?show_id=7']);
    expect(((req.body as FormData).get('file') as File).name).toBe('set.mp3');
    expect(progress).toEqual([0, 100]);
  });

  it.each([
    [json({ error: 'Show is not yours' }, 403), 'Show is not yours'],
    [new Response('gateway timeout', { status: 504 }), 'Upload failed'],
  ])('shows why a small set failed', async (response, text) => {
    fetchMock.mockResolvedValueOnce(response);
    await expect(hostFlowApi.uploadFile(7, fakeFile('set.mp3', MB))).rejects.toThrow(text);
  });

  it('sends a bigger set in 50 MB chunks, in order, then finalizes it', async () => {
    fetchMock
      .mockResolvedValueOnce(json({ success: true, session_id: 'up1' }))
      .mockResolvedValueOnce(json({ success: true }))
      .mockResolvedValueOnce(json({ success: true }))
      .mockResolvedValueOnce(json({ success: true }))
      .mockResolvedValueOnce(json({ success: true, key: 'sets/7.mp3', filename: 'set.mp3' }));
    const progress: string[] = [];
    const result = await hostFlowApi.uploadFile(7, fakeFile('set.mp3', 120 * MB), (p) =>
      progress.push(`${p.phase} ${p.percent}`)
    );

    expect(result.key).toBe('sets/7.mp3');
    const reqs = requests();
    expect(reqs.map((r) => `${r.method} ${r.url}`)).toEqual([
      'POST /api/my-show/upload/init?show_id=7',
      'POST /api/my-show/upload/chunk/up1?index=0&show_id=7',
      'POST /api/my-show/upload/chunk/up1?index=1&show_id=7',
      'POST /api/my-show/upload/chunk/up1?index=2&show_id=7',
      'POST /api/my-show/upload/finalize/up1?show_id=7',
    ]);
    expect(JSON.parse(reqs[0].body as string)).toEqual({
      filename: 'set.mp3',
      total_size: 120 * MB,
      total_chunks: 3,
    });
    const ranges = await Promise.all(
      reqs.slice(1, 4).map((r) => readText((r.body as FormData).get('chunk') as Blob))
    );
    expect(ranges).toEqual([`0-${50 * MB}`, `${50 * MB}-${100 * MB}`, `${100 * MB}-${120 * MB}`]);
    expect(progress).toEqual([
      'uploading 0',
      'uploading 33',
      'uploading 67',
      'uploading 100',
      'finalizing 0',
      'finalizing 100',
    ]);
  });

  it('stops at a failed chunk and names it', async () => {
    fetchMock
      .mockResolvedValueOnce(json({ success: true, session_id: 'up1' }))
      .mockResolvedValueOnce(json({ success: true }))
      .mockResolvedValueOnce(json({}, 502));
    await expect(hostFlowApi.uploadFile(7, fakeFile('set.mp3', 120 * MB))).rejects.toThrow(
      'Failed to upload chunk 2/3'
    );
    expect(requests()).toHaveLength(3);
  });

  it.each([
    ['start', [json({ error: 'Upload session expired' }, 401)], 'Upload session expired'],
    [
      'finish',
      [
        json({ session_id: 'up1' }),
        json({}),
        json({}),
        json({}),
        json({ message: 'Not an audio file' }, 422),
      ],
      'Not an audio file',
    ],
  ])('passes the server text on when the %s of a chunked upload fails', async (_s, rs, text) => {
    for (const r of rs) fetchMock.mockResolvedValueOnce(r);
    await expect(hostFlowApi.uploadFile(7, fakeFile('set.mp3', 120 * MB))).rejects.toThrow(text);
  });
});
