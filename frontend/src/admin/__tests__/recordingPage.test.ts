import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApp, nextTick, ref, type App } from 'vue';

// The admin Recording page must not end a broadcast that another page of this
// tab started (https://github.com/phaabe/live.moafunk.de/issues/1062).

const mocks = vi.hoisted(() => ({
  liveShowId: null as number | null,
  useStreamSocket: vi.fn(),
  useAudioCapture: vi.fn(),
  stopStream: vi.fn(),
  stopCapture: vi.fn(),
}));

// The real page state; the whole index needs FontFace, which jsdom lacks.
vi.mock('../composables', async () => {
  return {
    ...(await vi.importActual('../composables/useFlash')),
    ...(await vi.importActual('../composables/useRecordingSession')),
    ...(await vi.importActual('../composables/useFinalizeProgress')),
    liveStreamShowId: () => mocks.liveShowId,
    useStreamSocket: mocks.useStreamSocket,
    useAudioCapture: mocks.useAudioCapture,
    useAudioMeter: () => ({ level: ref(0) }),
  };
});

import RecordingPage from '../pages/RecordingPage.vue';

let app: App | undefined;
let root: HTMLDivElement;

beforeEach(() => {
  mocks.liveShowId = null;
  mocks.useStreamSocket.mockReset().mockReturnValue({
    state: ref('live'),
    connect: vi.fn(),
    send: vi.fn(),
    stopStream: mocks.stopStream,
  });
  // The tab's capture is running (the live broadcast's input).
  mocks.useAudioCapture.mockReset().mockReturnValue({
    isCapturing: ref(true),
    devices: ref([]),
    inputVolume: ref(1),
    processedStream: ref(null),
    refreshDevices: vi.fn(),
    stopCapture: mocks.stopCapture,
  });
  mocks.stopStream.mockReset();
  mocks.stopCapture.mockReset();
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => Response.json({ shows: [], active: true, is_recording: false }))
  );
  root = document.createElement('div');
  document.body.appendChild(root);
});

afterEach(() => {
  app?.unmount();
  app = undefined;
  root.remove();
  vi.unstubAllGlobals();
});

async function mount() {
  app = createApp(RecordingPage);
  app.component('RouterLink', { render: () => null });
  app.mount(root);
  for (let i = 0; i < 4; i++) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    await nextTick();
  }
}

describe('Recording page', () => {
  it('leaves a broadcast from another page alone, also when it closes', async () => {
    mocks.liveShowId = 7;
    await mount();

    // No call: any useStreamSocket() call replaces the live panel's callbacks.
    expect(mocks.useStreamSocket).not.toHaveBeenCalled();
    expect(mocks.useAudioCapture).toHaveBeenCalledWith({});
    expect(root.textContent).toContain('This tab is already live from another page.');
    expect(root.textContent).not.toContain('Stop Stream');
    expect(root.querySelector('.volume-slider')).toBeNull();

    app!.unmount();
    app = undefined;
    expect(mocks.stopStream).not.toHaveBeenCalled();
    expect(mocks.stopCapture).not.toHaveBeenCalled();
  });

  it('stops its own stream when it closes', async () => {
    await mount();

    expect(mocks.useStreamSocket).toHaveBeenCalledOnce();
    expect(mocks.useAudioCapture.mock.calls[0][0].onData).toBeTypeOf('function');
    expect(root.textContent).toContain('Stop Stream');

    app!.unmount();
    app = undefined;
    expect(mocks.stopStream).toHaveBeenCalledOnce();
    expect(mocks.stopCapture).toHaveBeenCalledOnce();
  });
});
