import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import pageHtml from '../unheard-artists-form.html?raw';

const { extractPeaks, FakeCropper } = vi.hoisted(() => {
  // Stands in for Cropper.js: jsdom cannot draw, so the canvas only hands out a JPEG blob.
  class FakeCropper {
    static last: FakeCropper | null = null;
    zoomedBy: number[] = [];
    constructor() {
      FakeCropper.last = this;
    }
    getCroppedCanvas() {
      return {
        width: 1024,
        height: 1024,
        getContext: () => null,
        toBlob: (done: (b: Blob) => void) =>
          setTimeout(() => done(new Blob(['jpg'], { type: 'image/jpeg' }))),
      };
    }
    zoom(by: number) {
      this.zoomedBy.push(by);
    }
    destroy() {}
  }
  return { extractPeaks: vi.fn(), FakeCropper };
});
vi.mock('../waveformExtractor', () => ({ extractWaveformPeaksJson: extractPeaks }));
vi.mock('cropperjs', () => ({ default: FakeCropper }));

const MB = 1024 * 1024;

type Call = { path: string; init?: RequestInit };
let calls: Call[] = [];
let failWith: (path: string) => Response | undefined = () => undefined;

const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200 });

const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
  const u = new URL(url);
  const path = u.pathname + u.search;
  calls.push({ path, init });
  const failure = failWith(path);
  if (failure) return failure;
  if (path.startsWith('/api/submit/init')) return json({ session_id: 's1' });
  if (path.startsWith('/api/submit/file-chunk/init/')) return json({ file_session_id: 'f1' });
  if (path.startsWith('/api/submit/finalize/')) return json({ message: 'Thanks, we got it!' });
  return json({});
});

const el = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;

/** A file of any size without the bytes; slice() returns the byte range as text. */
function fakeFile(name: string, size: number, type = 'audio/mpeg'): File {
  const file = new File(['x'], name, { type });
  Object.defineProperty(file, 'size', { value: size });
  file.slice = (start = 0, end = size) => new Blob([`${start}-${end}`]);
  return file;
}

function choose(id: string, ...files: File[]): void {
  Object.defineProperty(el(id), 'files', { value: files, configurable: true });
}

function fillRequired(): void {
  el<HTMLInputElement>('artist-name').value = 'Nova';
  el<HTMLInputElement>('pronouns').value = 'she/her';
  el<HTMLTextAreaElement>('music-description').value = 'Dub techno';
  el<HTMLInputElement>('track1-name').value = 'First';
  el<HTMLInputElement>('track2-name').value = 'Second';
  choose('artist-pic', fakeFile('me.png', 2 * MB, 'image/png'));
  choose('track1-file', fakeFile('one.mp3', 10 * MB));
  choose('track2-file', fakeFile('two.wav', 20 * MB));
}

const message = () => el('form-message');
const submitBtn = () => el<HTMLButtonElement>('submit-btn');
const paths = () => calls.map((c) => c.path);

/** The artist picks a picture and the crop tool opens. */
function openCropper(): void {
  el('artist-pic').dispatchEvent(new Event('change'));
  el<HTMLImageElement>('artist-pic-cropper-img').onload?.(new Event('load'));
}

async function submit(): Promise<void> {
  el<HTMLFormElement>('uaf-form').dispatchEvent(new Event('submit', { cancelable: true }));
  await vi.waitFor(() => expect(message().style.display).toBe('block'));
}

function formField(call: Call, name: string): FormDataEntryValue | null {
  return (call.init?.body as FormData).get(name);
}

// jsdom's Blob has no text().
function readText(blob: Blob): Promise<string> {
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as string);
    reader.readAsText(blob);
  });
}

beforeEach(async () => {
  calls = [];
  failWith = () => undefined;
  extractPeaks.mockReset().mockResolvedValue('{"peaks":[0.5]}');
  vi.stubGlobal('fetch', fetchMock);
  // jsdom has neither FontFace nor matchMedia; the page touches both on load.
  vi.stubGlobal(
    'FontFace',
    class {
      load() {
        return Promise.reject(new Error('no fonts in jsdom'));
      }
    }
  );
  vi.stubGlobal('matchMedia', () => ({ matches: false }));
  URL.createObjectURL = () => 'blob:picture';
  URL.revokeObjectURL = () => {};
  FakeCropper.last = null;
  vi.spyOn(window, 'alert').mockImplementation(() => {});
  vi.spyOn(console, 'warn').mockImplementation(() => {});

  document.body.innerHTML = new DOMParser().parseFromString(pageHtml, 'text/html').body.innerHTML;
  vi.resetModules();
  await import('../unheard-artists-form');
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('artist form fields', () => {
  it('requires name, pronouns, description, picture and both tracks', () => {
    const required = [...document.querySelectorAll<HTMLInputElement>('[required]')].map(
      (f) => f.id
    );
    expect(required.sort()).toEqual(
      [
        'artist-name',
        'pronouns',
        'music-description',
        'artist-pic',
        'track1-name',
        'track1-file',
        'track2-name',
        'track2-file',
      ].sort()
    );
  });

  it('only offers images for the picture and audio files for tracks and voice', () => {
    expect(el<HTMLInputElement>('artist-pic').accept).toBe('image/*');
    for (const id of ['track1-file', 'track2-file', 'voice-message']) {
      const accept = el<HTMLInputElement>(id).accept.split(',');
      expect(accept).toContain('audio/*');
      expect(accept).toEqual(expect.arrayContaining(['.mp3', '.wav', '.flac', '.aiff']));
    }
  });

  it('stops before any upload when a track file is missing', async () => {
    fillRequired();
    choose('track2-file');
    await submit();
    expect(message().textContent).toBe('Track files are required.');
    expect(message().className).toContain('error');
    expect(calls).toEqual([]);
  });

  it('disables and clears the voice upload when the artist has no voice message', () => {
    const voice = el<HTMLInputElement>('voice-message');
    const none = el<HTMLInputElement>('no-voice-message');
    none.checked = true;
    none.dispatchEvent(new Event('change'));
    expect(voice.disabled).toBe(true);
    expect(voice.required).toBe(false);
    none.checked = false;
    none.dispatchEvent(new Event('change'));
    expect(voice.disabled).toBe(false);
  });
});

describe('size limits', () => {
  it('accepts a 500 MB track', async () => {
    fillRequired();
    choose('track1-file', fakeFile('big.wav', 500 * MB));
    await submit();
    expect(message().className).toContain('success');
  });

  it.each([
    ['track1-file', 'Track 1'],
    ['track2-file', 'Track 2'],
    ['voice-message', 'Voice message'],
    ['artist-pic', 'Artist picture'],
  ])('refuses %s over 500 MB and names it', async (id, label) => {
    fillRequired();
    choose(id, fakeFile('huge', 500 * MB + 1));
    await submit();
    expect(message().textContent).toBe(`${label} exceeds maximum size of 500MB`);
    expect(calls).toEqual([]);
  });

  it('refuses more than 1500 MB in total even when each file fits', async () => {
    fillRequired();
    choose('track1-file', fakeFile('a.wav', 500 * MB));
    choose('track2-file', fakeFile('b.wav', 500 * MB));
    choose('voice-message', fakeFile('v.wav', 500 * MB));
    await submit();
    expect(message().textContent).toBe('Total upload exceeds maximum size of 1500MB');
    expect(calls).toEqual([]);
  });

  it('warns as soon as the artist picks a track over 500 MB', () => {
    choose('track1-file', fakeFile('huge.wav', 501 * MB));
    el('track1-file').dispatchEvent(new Event('change'));
    expect(window.alert).toHaveBeenCalledWith('File exceeds maximum size of 500MB');
  });

  it('warns as soon as the artist picks a picture over 500 MB', () => {
    choose('artist-pic', fakeFile('huge.png', 501 * MB, 'image/png'));
    el('artist-pic').dispatchEvent(new Event('change'));
    expect(window.alert).toHaveBeenCalledWith('Artist picture exceeds maximum size of 500MB');
    expect(el('artist-pic-cropper').style.display).toBe('none');
  });
});

describe('submission', () => {
  it('sends the text, each file once, then finishes the submission once', async () => {
    fillRequired();
    el<HTMLInputElement>('instagram').value = 'https://instagram.com/nova';
    choose('voice-message', fakeFile('hi.m4a', 1 * MB, 'audio/mp4'));
    await submit();

    expect(paths()).toEqual([
      '/api/submit/init',
      '/api/submit/file/s1?field=track1',
      '/api/submit/file/s1?field=track2',
      '/api/submit/file/s1?field=voice',
      '/api/submit/finalize/s1',
    ]);
    const init = calls[0];
    expect(formField(init, 'artist-name')).toBe('Nova');
    expect(formField(init, 'track2-name')).toBe('Second');
    expect(formField(init, 'instagram')).toBe('https://instagram.com/nova');
    expect(formField(init, 'soundcloud')).toBeNull();
    expect(formField(init, 'no-voice-message')).toBeNull();
    expect((formField(init, 'artist-pic') as File).name).toBe('me.png');
    expect(formField(calls[1], 'peaks')).toBe('{"peaks":[0.5]}');

    expect(message().textContent).toBe('Thanks, we got it!');
    expect(message().className).toBe('form-message success');
    expect(el('progress-text').textContent).toBe('Upload complete!');
    expect(submitBtn().disabled).toBe(false);
    expect(submitBtn().textContent).toBe('Submit Application');
  });

  it('skips the voice upload and says so when the artist has no voice message', async () => {
    fillRequired();
    choose('voice-message', fakeFile('hi.m4a', 1 * MB, 'audio/mp4'));
    el<HTMLInputElement>('no-voice-message').checked = true;
    await submit();
    expect(formField(calls[0], 'no-voice-message')).toBe('on');
    expect(paths().some((p) => p.includes('field=voice'))).toBe(false);
  });

  it('shows a default thank-you when the server sends no message', async () => {
    fillRequired();
    failWith = (p) => (p.startsWith('/api/submit/finalize/') ? json({}) : undefined);
    await submit();
    expect(message().textContent).toBe("Thank you for your submission! We'll be in touch soon.");
  });

  it('clears the form two seconds after success', async () => {
    vi.useFakeTimers();
    fillRequired();
    await submit();
    vi.advanceTimersByTime(2000);
    expect(el<HTMLInputElement>('artist-name').value).toBe('');
    expect(el('upload-progress').style.display).toBe('none');
    expect(el('progress-fill').style.width).toBe('0%');
  });

  it('still uploads a track when its waveform cannot be read', async () => {
    fillRequired();
    extractPeaks.mockRejectedValue(new Error('decode failed'));
    await submit();
    expect(formField(calls[1], 'file')).not.toBeNull();
    expect(formField(calls[1], 'peaks')).toBeNull();
    expect(message().className).toContain('success');
  });
});

describe('artist picture', () => {
  it('sends the cropped and the branded picture named after the original', async () => {
    fillRequired();
    openCropper();
    expect(el('artist-pic-cropper').style.display).toBe('block');
    await submit();
    const cropped = formField(calls[0], 'artist-pic-cropped') as File;
    const branded = formField(calls[0], 'artist-pic-branded') as File;
    expect([cropped.name, cropped.type]).toEqual(['me.jpg', 'image/jpeg']);
    expect(branded.name).toBe('me.jpg');
  });

  it('zooms the picture with the + and - buttons', () => {
    fillRequired();
    openCropper();
    el('artist-pic-zoom-in').click();
    el('artist-pic-zoom-out').click();
    expect(FakeCropper.last?.zoomedBy).toEqual([0.1, -0.1]);
  });
});

describe('chunked upload', () => {
  it('sends a track over 100 MB in 50 MB chunks, in order, then finalizes it', async () => {
    fillRequired();
    choose('track1-file', fakeFile('long.wav', 120 * MB));
    await submit();

    expect(paths()).toEqual([
      '/api/submit/init',
      '/api/submit/file-chunk/init/s1?field=track1',
      '/api/submit/file-chunk/f1?field=track1&index=0',
      '/api/submit/file-chunk/f1?field=track1&index=1',
      '/api/submit/file-chunk/f1?field=track1&index=2',
      '/api/submit/file-chunk/finalize/f1?field=track1',
      '/api/submit/file/s1?field=track2',
      '/api/submit/finalize/s1',
    ]);
    expect(JSON.parse(calls[1].init?.body as string)).toEqual({
      filename: 'long.wav',
      total_size: 120 * MB,
      total_chunks: 3,
      peaks: '{"peaks":[0.5]}',
    });
    const ranges = await Promise.all(
      calls.slice(2, 5).map((c) => readText(formField(c, 'chunk') as Blob))
    );
    expect(ranges).toEqual([`0-${50 * MB}`, `${50 * MB}-${100 * MB}`, `${100 * MB}-${120 * MB}`]);
  });

  it('sends a track of exactly 100 MB in one request', async () => {
    fillRequired();
    choose('track1-file', fakeFile('edge.wav', 100 * MB));
    await submit();
    expect(paths()[1]).toBe('/api/submit/file/s1?field=track1');
  });

  it('stops at a failed chunk, shows the server error and lets the artist try again', async () => {
    fillRequired();
    choose('track1-file', fakeFile('long.wav', 120 * MB));
    failWith = (p) =>
      p.endsWith('index=1') ? new Response('Storage is full', { status: 507 }) : undefined;
    await submit();

    expect(paths().filter((p) => p.includes('index=1'))).toHaveLength(1);
    expect(paths().some((p) => p.includes('index=2') || p.includes('finalize'))).toBe(false);
    expect(message().textContent).toBe('Storage is full');
    expect(message().className).toBe('form-message error');
    expect(el('upload-progress').style.display).toBe('none');
    expect(submitBtn().disabled).toBe(false);
  });

  it('names the chunk when the server sends no error text', async () => {
    fillRequired();
    choose('track1-file', fakeFile('long.wav', 120 * MB));
    failWith = (p) => (p.endsWith('index=1') ? new Response('', { status: 502 }) : undefined);
    await submit();
    expect(message().textContent).toBe('Failed to upload Track 1 chunk 2/3');
  });

  // A failed chunk today fails the whole submission; the artist must upload every file again.
  // https://github.com/phaabe/live.moafunk.de/issues/923
  it.skip('retries a failed chunk before giving up', async () => {
    fillRequired();
    choose('track1-file', fakeFile('long.wav', 120 * MB));
    let failed = false;
    failWith = (p) => {
      if (!p.endsWith('index=1') || failed) return undefined;
      failed = true;
      return new Response('', { status: 502 });
    };
    await submit();
    expect(paths().filter((p) => p.endsWith('index=1'))).toHaveLength(2);
    expect(message().className).toContain('success');
  });
});

describe('errors shown to the artist', () => {
  it('shows the server text when the start of the submission fails', async () => {
    fillRequired();
    failWith = (p) =>
      p.startsWith('/api/submit/init')
        ? new Response('Artist name is too long', { status: 400 })
        : undefined;
    await submit();
    expect(message().textContent).toBe('Artist name is too long');
    expect(paths()).toEqual(['/api/submit/init']);
  });

  it.each([
    ['/api/submit/init', 'Failed to initialize submission'],
    ['/api/submit/file/', 'Failed to upload Track 1'],
    ['/api/submit/finalize/', 'Failed to finalize submission'],
  ])('falls back to a clear text when %s fails without one', async (prefix, text) => {
    fillRequired();
    failWith = (p) => (p.startsWith(prefix) ? new Response('', { status: 500 }) : undefined);
    await submit();
    expect(message().textContent).toBe(text);
    expect(submitBtn().textContent).toBe('Submit Application');
  });

  it('shows the network error when the connection drops', async () => {
    fillRequired();
    fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'));
    await submit();
    expect(message().textContent).toBe('Failed to fetch');
    expect(submitBtn().disabled).toBe(false);
  });
});
