import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { MyShowInfo } from '../../api';
import { useHostFlow } from '../useHostFlow';

const api = vi.hoisted(() => ({
  hostFlowApi: {
    getMyShows: vi.fn(),
    uploadFile: vi.fn(),
    confirm: vi.fn(),
    deleteUpload: vi.fn(),
  },
  streamApi: { stop: vi.fn() },
}));
vi.mock('../../api', () => api);

// 30 Oct 2026, 20:00 in Berlin (UTC+1).
const NOW = new Date('2026-10-30T19:00:00Z');

function makeShow(overrides: Partial<MyShowInfo> = {}): MyShowInfo {
  return {
    id: 48,
    title: 'UNHEARD',
    date: '2026-11-06',
    start_time: '20:00',
    end_time: '22:00',
    show_type: 'unheard',
    artists: [],
    ...overrides,
  };
}

const running = (overrides: Partial<MyShowInfo> = {}) =>
  makeShow({ date: '2026-10-30', start_time: '19:30', end_time: '21:00', ...overrides });

const uploaded = {
  prerecorded_key: 'shows/48/show.mp3',
  prerecorded_url: 'https://cdn.test/show.mp3',
  prerecorded_filename: 'show.mp3',
};

const flow = useHostFlow();

async function load(...shows: MyShowInfo[]) {
  api.hostFlowApi.getMyShows.mockResolvedValue({ assigned: shows.length > 0, shows });
  await flow.fetchMyShow();
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
  flow.reset();
  for (const fn of [...Object.values(api.hostFlowApi), api.streamApi.stop]) fn.mockReset();
  vi.spyOn(console, 'warn').mockImplementation(() => {});
});

afterEach(() => {
  flow.reset();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe('where the host starts', () => {
  it('a host without a show sees "not assigned" and can go nowhere else', async () => {
    await load();
    expect(flow.currentStep.value).toBe('not-assigned');
    expect(flow.canNavigateTo('not-assigned')).toBe(true);
    expect(flow.goToStep('select')).toBe(false);
    expect(flow.goToStep('mode')).toBe(false);
  });

  it('one upcoming show is picked for the host and starts at the mode choice', async () => {
    await load(makeShow());
    expect(flow.showId.value).toBe(48);
    expect(flow.currentStep.value).toBe('mode');
    expect(flow.loaded.value).toBe(true);
  });

  it('a show with a confirmed upload goes straight to On Air', async () => {
    await load(makeShow({ ...uploaded, prerecorded_confirmed_at: '2026-10-29T10:00:00Z' }));
    expect(flow.currentStep.value).toBe('on-air');
    expect(flow.uploadMode.value).toBe('prerecorded');
  });

  it('a running show without an upload asks how to broadcast', async () => {
    await load(running());
    expect(flow.isShowRunning(running())).toBe(true);
    expect(flow.currentStep.value).toBe('mode');
  });

  it('a running show with a confirmed upload goes straight to On Air', async () => {
    await load(running({ ...uploaded, prerecorded_confirmed_at: '2026-10-29T10:00:00Z' }));
    expect(flow.currentStep.value).toBe('on-air');
  });

  it('with several shows the host chooses, and a reload keeps the choice', async () => {
    const other = makeShow({ id: 49, title: 'Late Night' });
    await load(makeShow(), other);
    expect(flow.currentStep.value).toBe('select');
    expect(flow.show.value).toBeUndefined();

    flow.selectShow(other);
    await flow.fetchMyShow();
    expect(flow.showId.value).toBe(49);
    expect(flow.currentStep.value).toBe('mode');
  });

  it('shows why the shows could not load', async () => {
    api.hostFlowApi.getMyShows.mockRejectedValue(new Error('Session expired'));
    await flow.fetchMyShow();
    expect(flow.error.value).toBe('Session expired');
    expect(flow.loading.value).toBe(false);
    api.hostFlowApi.getMyShows.mockRejectedValue('weird');
    await flow.fetchMyShow();
    expect(flow.error.value).toBe('Failed to load show data');
  });
});

describe('prerecorded show', () => {
  it('upload shows progress, then the file to confirm, then opens On Air', async () => {
    await load(makeShow());
    flow.selectMode('prerecorded');
    expect(flow.currentStep.value).toBe('upload');
    expect(flow.goToStep('confirm')).toBe(false);

    let finish!: () => void;
    api.hostFlowApi.uploadFile.mockImplementation(async (_id, _file, onProgress) => {
      onProgress({ phase: 'uploading', percent: 40, chunkIndex: 2, totalChunks: 5 });
      await new Promise<void>((resolve) => (finish = resolve));
      return { success: true, key: uploaded.prerecorded_key, filename: 'show.mp3' };
    });
    const upload = flow.uploadFile(new File(['x'], 'show.mp3'));
    expect(flow.uploading.value).toBe(true);
    expect(flow.uploadProgress.value?.percent).toBe(40);
    finish();
    await upload;

    expect(flow.uploading.value).toBe(false);
    expect(flow.uploadProgress.value).toBeNull();
    expect(flow.currentStep.value).toBe('confirm');
    expect(flow.prerecordedFilename.value).toBe('show.mp3');
    expect(flow.hasUpload.value).toBe(true);
    expect(flow.canNavigateTo('on-air')).toBe(false);

    api.hostFlowApi.confirm.mockResolvedValue({ success: true, confirmed_at: '2026-10-30T19:01Z' });
    await flow.confirmUpload();
    expect(flow.isConfirmed.value).toBe(true);
    expect(flow.goToStep('on-air')).toBe(true);
  });

  it('a failed upload shows the error and stays on the upload step', async () => {
    await load(makeShow());
    flow.selectMode('prerecorded');
    api.hostFlowApi.uploadFile.mockRejectedValue(new Error('File too large'));
    await expect(flow.uploadFile(new File(['x'], 'show.wav'))).rejects.toThrow('File too large');
    expect(flow.error.value).toBe('File too large');
    expect(flow.currentStep.value).toBe('upload');
    expect(flow.uploading.value).toBe(false);
  });

  it('a failed confirm shows the error and keeps the show unconfirmed', async () => {
    await load(makeShow(uploaded));
    api.hostFlowApi.confirm.mockRejectedValue('nope');
    await expect(flow.confirmUpload()).rejects.toBe('nope');
    expect(flow.error.value).toBe('Confirmation failed');
    expect(flow.isConfirmed.value).toBe(false);
  });

  it('deleting the upload goes back to the upload step', async () => {
    await load(makeShow({ ...uploaded, prerecorded_confirmed_at: '2026-10-29T10:00:00Z' }));
    api.hostFlowApi.deleteUpload.mockResolvedValue({ success: true });
    await flow.deleteUpload();
    expect(flow.hasUpload.value).toBe(false);
    expect(flow.isConfirmed.value).toBe(false);
    expect(flow.currentStep.value).toBe('upload');
  });

  it('a failed delete shows the error and keeps the upload', async () => {
    await load(makeShow(uploaded));
    api.hostFlowApi.deleteUpload.mockRejectedValue(new Error('R2 down'));
    await expect(flow.deleteUpload()).rejects.toThrow('R2 down');
    expect(flow.error.value).toBe('R2 down');
    expect(flow.hasUpload.value).toBe(true);
  });

  it('every upload action needs a show', async () => {
    await expect(flow.uploadFile(new File(['x'], 'a.mp3'))).rejects.toThrow('No show selected');
    await expect(flow.confirmUpload()).rejects.toThrow('No show selected');
    await expect(flow.deleteUpload()).rejects.toThrow('No show selected');
  });
});

describe('live show', () => {
  it('On Air opens only after the audio test passed', async () => {
    await load(makeShow());
    flow.selectMode('live');
    expect(flow.currentStep.value).toBe('live');
    expect(flow.goToStep('upload')).toBe(false);
    expect(flow.goToStep('on-air')).toBe(false);
    flow.setLiveTestPassed();
    expect(flow.goToStep('on-air')).toBe(true);
  });

  it('stopping the stream goes back to the mode choice even when the stop fails', async () => {
    await load(makeShow(uploaded));
    flow.selectMode('live');
    flow.setLiveTestPassed();
    flow.setShowStarted();
    flow.setRecordStream(false);
    api.streamApi.stop.mockRejectedValue(new Error('offline'));
    api.hostFlowApi.deleteUpload.mockResolvedValue({ success: true });

    await flow.stopStreamAndRevert();
    expect(flow.currentStep.value).toBe('mode');
    expect(flow.uploadMode.value).toBeNull();
    expect(flow.showStarted.value).toBe(false);
    expect(flow.liveTestPassed.value).toBe(false);
    expect(flow.recordStream.value).toBe(true);
    expect(flow.hasUpload.value).toBe(false);
  });

  it('going back to the mode choice still works when the upload cannot be deleted', async () => {
    await load(makeShow(uploaded));
    api.hostFlowApi.deleteUpload.mockRejectedValue(new Error('R2 down'));
    await flow.revertToMode();
    expect(flow.currentStep.value).toBe('mode');
    expect(flow.hasUpload.value).toBe(true);
  });

  it('choosing another show clears the live test and the record choice', async () => {
    await load(makeShow());
    flow.selectMode('live');
    flow.setLiveTestPassed();
    flow.setRecordStream(false);
    flow.deselectShow();
    expect(flow.currentStep.value).toBe('select');
    expect(flow.show.value).toBeUndefined();
    expect(flow.liveTestPassed.value).toBe(false);
    expect(flow.recordStream.value).toBe(true);
  });
});
