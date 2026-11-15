import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApp, h, nextTick, ref, type App } from 'vue';

// The Studio's soundcheck controls (LiveSetupTest with `studio`): input
// level meter, "Audio input" and "Test broadcast sounds right".

const mocks = vi.hoisted(() => ({
  passed: null as null | { value: boolean },
  setPassed: vi.fn(),
  connect: vi.fn(),
  stopStream: vi.fn(),
}));

vi.mock('@/config', () => ({
  config: { stream: { icecastTestUrl: 'https://test.example/test.mp3' } },
}));
vi.mock('@admin/components/DbMeter.vue', () => ({
  __esModule: true,
  default: { props: ['label'], render: (p: { label: string }) => h('div', p.label) },
}));
vi.mock('@admin/components/StreamPreviewPlayer.vue', () => ({
  __esModule: true,
  default: { render: () => h('div', 'Test player') },
}));
vi.mock('@admin/composables', () => ({
  STREAM_AUDIO_BITS_PER_SECOND: 128000,
  BROWSER_UNSUPPORTED_MESSAGE: 'unsupported',
  streamRecordingMimeType: () => 'audio/webm',
  useHostFlow: () => ({ liveTestPassed: mocks.passed, setLiveTestPassed: mocks.setPassed }),
  useAudioCapture: () => ({
    devices: ref([{ deviceId: 'usb', label: 'Focusrite USB' }]),
    selectedDeviceId: ref('usb'),
    isCapturing: ref(true),
    error: ref(null),
    mediaStream: ref(null),
    processedStream: ref(null),
    refreshDevices: vi.fn(async () => {}),
    listDevices: vi.fn(),
    captureDevice: vi.fn(async () => true),
    captureScreenAudio: vi.fn(async () => true),
    resumeAudioContext: vi.fn(),
  }),
  useStreamSocket: () => ({ connect: mocks.connect, stopStream: mocks.stopStream, send: vi.fn() }),
  useUploadHealth: () => ({ verdict: ref('good'), verdictText: ref(''), uploadKbps: ref(0) }),
}));

import LiveSetupTest from '../components/LiveSetupTest.vue';

let app: App | null = null;
let root: HTMLElement;

async function mount() {
  root = document.createElement('div');
  document.body.appendChild(root);
  app = createApp({ render: () => h(LiveSetupTest, { studio: true }) });
  app.mount(root);
  for (let i = 0; i < 3; i++) await nextTick();
}

const testBox = () =>
  [...root.querySelectorAll('label')]
    .find((l) => l.textContent?.includes('Test broadcast sounds right'))!
    .querySelector('input')!;

beforeEach(() => {
  mocks.passed = ref(false);
  mocks.setPassed.mockReset().mockImplementation((v = true) => (mocks.passed!.value = v));
  mocks.connect.mockReset().mockResolvedValue(undefined);
  mocks.stopStream.mockReset();
  Object.defineProperty(navigator, 'mediaDevices', {
    value: { addEventListener: vi.fn(), removeEventListener: vi.fn() },
    configurable: true,
  });
});

afterEach(() => {
  app?.unmount();
  app = null;
  root?.remove();
});

describe('Studio soundcheck', () => {
  it('shows the flow controls, with screen audio as an input', async () => {
    await mount();
    expect(root.textContent).toContain('Input level');
    expect(root.textContent).toContain('Audio input');
    const options = [...root.querySelectorAll('option')].map((o) => o.textContent?.trim());
    expect(options).toEqual(['Select audio input', 'Focusrite USB', 'Screen audio']);
    expect(root.textContent).not.toContain('Share screen audio instead');
    expect(root.textContent).not.toContain('Yes, ready to go live');
  });

  it('can be ticked only after a test broadcast', async () => {
    await mount();
    expect(testBox().disabled).toBe(true);
    expect(root.textContent).toContain('Start test');
  });

  it('keeps an earlier passed test and can be unticked', async () => {
    mocks.passed!.value = true;
    await mount();
    expect(testBox().checked).toBe(true);
    testBox().click();
    await nextTick();
    expect(mocks.setPassed).toHaveBeenCalledWith(false);
  });
});
