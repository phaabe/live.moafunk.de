import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';
import { createApp, defineComponent, type App } from 'vue';
import type { RecordingArtist, RecordingVersionInfo, ShowWithArtists } from '../../api';
import { useRecordingSession, type UseRecordingSessionOptions } from '../useRecordingSession';

const api = vi.hoisted(() => ({
  recordingApi: {
    getShowWithArtists: vi.fn(),
    listRecordings: vi.fn(),
    start: vi.fn(),
    stop: vi.fn(),
    status: vi.fn(),
    addMarker: vi.fn(),
  },
  showsApi: { list: vi.fn() },
}));
vi.mock('../../api', () => api);

class FakeAudio {
  static all: FakeAudio[] = [];
  volume = 1;
  currentTime = 0;
  duration = NaN;
  paused = true;
  ontimeupdate: (() => void) | null = null;
  onended: (() => Promise<void>) | null = null;
  onerror: (() => void) | null = null;
  constructor(public src: string) {
    FakeAudio.all.push(this);
  }
  async play() {
    this.paused = false;
  }
  pause() {
    this.paused = true;
  }
}

const artist: RecordingArtist = {
  id: 7,
  name: 'robi',
  pronouns: 'they/them',
  voice_url: 'https://cdn.test/voice.mp3',
  voice_key: 'artists/7/voice.mp3',
  track1_url: 'https://cdn.test/t1.mp3',
  track1_key: 'artists/7/t1.mp3',
  track1_name: 'Song A',
  track2_name: '',
};
const show: ShowWithArtists = {
  id: 48,
  title: 'UNHEARD',
  date: '2026-10-30',
  status: 'scheduled',
  artists: [artist],
};

let app: App | undefined;
let onError: Mock;
let fetchMock: Mock;

function mountSession(options: UseRecordingSessionOptions = {}) {
  let result!: ReturnType<typeof useRecordingSession>;
  app = createApp(
    defineComponent({
      setup() {
        result = useRecordingSession({ onError, ...options });
        return () => null;
      },
    })
  );
  app.mount(document.createElement('div'));
  return result;
}

const flush = () => vi.advanceTimersByTimeAsync(0);

async function withShow(options: UseRecordingSessionOptions = {}) {
  const session = mountSession(options);
  session.selectedShowId.value = 48;
  await flush();
  return session;
}

const track1 = () => FakeAudio.all.at(-1)!;

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-10-30T20:00:00Z'));
  FakeAudio.all = [];
  onError = vi.fn();
  for (const fn of Object.values(api.recordingApi)) fn.mockReset();
  api.showsApi.list.mockReset();
  api.recordingApi.getShowWithArtists.mockResolvedValue(show);
  api.recordingApi.listRecordings.mockResolvedValue({ recordings: [] });
  api.recordingApi.start.mockResolvedValue({ success: true, started_at: '' });
  api.recordingApi.stop.mockResolvedValue({
    success: true,
    raw_key: 'recordings/48/2026-10-30_200000/raw.webm',
    markers_key: 'recordings/48/2026-10-30_200000/markers.json',
  });
  api.recordingApi.addMarker.mockResolvedValue({ success: true });
  fetchMock = vi.fn(async () => ({ ok: true, arrayBuffer: async () => new ArrayBuffer(1) }));
  vi.stubGlobal('fetch', fetchMock);
  vi.stubGlobal('Audio', FakeAudio);
  vi.spyOn(console, 'log').mockImplementation(() => {});
  vi.spyOn(console, 'warn').mockImplementation(() => {});
  vi.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
  app?.unmount();
  app = undefined;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe('recording clock', () => {
  it('starts the recording of the selected show and counts down 60 minutes', async () => {
    const onRecordingStarted = vi.fn();
    const s = await withShow({ onRecordingStarted });
    expect(await s.startRecording()).toBe(true);
    expect(api.recordingApi.start).toHaveBeenCalledWith(48);
    expect(onRecordingStarted).toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(61_000);
    expect(s.formattedDuration.value).toBe('01:01');
    expect(s.formattedCountdown.value).toBe('58:59');
    expect(s.countdownWarning.value).toBe(false);

    await vi.advanceTimersByTimeAsync(50 * 60_000 - 61_000);
    expect(s.countdownWarning.value).toBe(true);
    expect(s.countdownCritical.value).toBe(false);

    await vi.advanceTimersByTimeAsync(5 * 60_000);
    expect(s.countdownCritical.value).toBe(true);

    await vi.advanceTimersByTimeAsync(10 * 60_000);
    expect(s.formattedCountdown.value).toBe('00:00');
  });

  it('shows why the recording could not start, and the clock stays at zero', async () => {
    api.recordingApi.start.mockRejectedValue(new Error('Another recording is running'));
    const s = await withShow();
    expect(await s.startRecording()).toBe(false);
    expect(onError).toHaveBeenCalledWith('Another recording is running');
    expect(s.isRecording.value).toBe(false);
    await vi.advanceTimersByTimeAsync(5000);
    expect(s.formattedDuration.value).toBe('00:00');
  });

  it('needs a show before it can record', async () => {
    const s = mountSession();
    expect(await s.startRecording()).toBe(false);
    expect(onError).toHaveBeenCalledWith('No show selected');
  });

  it('picks up a running recording after a page reload', async () => {
    api.recordingApi.status.mockResolvedValue({
      is_recording: true,
      show_id: 48,
      started_at: '2026-10-30T19:50:00Z',
      marker_count: 4,
    });
    const s = mountSession();
    await s.checkRecordingStatus();
    await flush();
    expect(s.isRecording.value).toBe(true);
    expect(s.markerCount.value).toBe(4);
    expect(s.formattedDuration.value).toBe('10:00');
    expect(s.selectedShow.value?.id).toBe(48);
    await vi.advanceTimersByTimeAsync(1000);
    expect(s.formattedDuration.value).toBe('10:01');
  });

  it('stays idle when the status check fails', async () => {
    api.recordingApi.status.mockRejectedValue(new Error('offline'));
    const s = mountSession();
    await s.checkRecordingStatus();
    expect(s.isRecording.value).toBe(false);
  });
});

describe('stopping the recording', () => {
  it('stops the tracks and the clock and returns the version', async () => {
    const onRecordingStopped = vi.fn();
    const s = await withShow({ onRecordingStopped });
    await s.startRecording();
    await s.toggleTrack(artist, 'track1');
    await vi.advanceTimersByTimeAsync(3000);

    expect(await s.stopRecording()).toBe('2026-10-30_200000');
    expect(onRecordingStopped).toHaveBeenCalledWith('2026-10-30_200000');
    expect(track1().paused).toBe(true);
    expect(s.isAnyTrackPlaying()).toBe(false);
    expect(s.isRecording.value).toBe(false);
    const stoppedAt = s.formattedDuration.value;
    await vi.advanceTimersByTimeAsync(5000);
    expect(s.formattedDuration.value).toBe(stoppedAt);
  });

  it('sends one stop for a double click', async () => {
    const s = await withShow();
    await s.startRecording();
    const first = s.stopRecording();
    const second = s.stopRecording();
    expect(await second).toBeNull();
    expect(await first).toBe('2026-10-30_200000');
    expect(api.recordingApi.stop).toHaveBeenCalledTimes(1);
  });

  it('keeps recording when stop fails, so the host can try again', async () => {
    api.recordingApi.stop.mockRejectedValueOnce(new Error('Upload to R2 failed'));
    const s = await withShow();
    await s.startRecording();
    expect(await s.stopRecording()).toBeNull();
    expect(onError).toHaveBeenCalledWith('Upload to R2 failed');
    expect(s.isRecording.value).toBe(true);
    expect(s.isStopping.value).toBe(false);
    expect(await s.stopRecording()).toBe('2026-10-30_200000');
  });

  it('uses the whole key as version when the key has an unknown shape', async () => {
    api.recordingApi.stop.mockResolvedValue({
      success: true,
      raw_key: 'odd.webm',
      markers_key: '',
    });
    const s = await withShow();
    await s.startRecording();
    expect(await s.stopRecording()).toBe('odd.webm');
  });
});

describe('track markers', () => {
  it('turns a track that plays to the end into a marker at its start offset', async () => {
    const onTrackStarted = vi.fn();
    const onTrackEnded = vi.fn();
    const s = await withShow({ onTrackStarted, onTrackEnded });
    await s.startRecording();
    await vi.advanceTimersByTimeAsync(30_000);

    s.setTrackVolume(7, 'track1', 0.8);
    await s.toggleTrack(artist, 'track1');
    expect(track1().src).toBe(artist.track1_url);
    expect(track1().volume).toBe(0.8);
    expect(track1().paused).toBe(false);
    expect(onTrackStarted).toHaveBeenCalledWith(7, 'track1');

    await vi.advanceTimersByTimeAsync(90_000);
    await track1().onended!();
    expect(api.recordingApi.addMarker).toHaveBeenCalledWith({
      artist_id: 7,
      track_type: 'track1',
      track_key: 'artists/7/t1.mp3',
      duration_ms: 90_000,
      offset_ms: 30_000,
      volume: 80,
    });
    expect(s.markerCount.value).toBe(1);
    expect(onTrackEnded).toHaveBeenCalledWith(7, 'track1', 90_000);
    expect(s.getTrackState(7, 'track1').playing).toBe(false);
  });

  it('turns a track stopped early into a marker for the part that played', async () => {
    const s = await withShow();
    await s.startRecording();
    await vi.advanceTimersByTimeAsync(5000);
    await s.toggleTrack(artist, 'voice_message');
    const audio = track1();
    await vi.advanceTimersByTimeAsync(12_000);
    await s.toggleTrack(artist, 'voice_message');

    expect(api.recordingApi.addMarker).toHaveBeenCalledWith(
      expect.objectContaining({
        track_type: 'voice_message',
        track_key: 'artists/7/voice.mp3',
        duration_ms: 12_000,
        offset_ms: 5000,
        volume: 100,
      })
    );
    expect(s.markerCount.value).toBe(1);
    expect(audio.paused).toBe(true);
    expect(audio.currentTime).toBe(0);
    expect(s.getTrackState(7, 'voice_message').playing).toBe(false);
  });

  it('makes no marker outside a recording', async () => {
    const s = await withShow();
    await s.toggleTrack(artist, 'track1');
    await track1().onended!();
    await s.toggleTrack(artist, 'voice_message');
    await s.toggleTrack(artist, 'voice_message');
    expect(api.recordingApi.addMarker).not.toHaveBeenCalled();
    expect(s.markerCount.value).toBe(0);
  });

  it('keeps the recording running when a marker cannot be saved', async () => {
    api.recordingApi.addMarker.mockRejectedValue(new Error('500'));
    const s = await withShow();
    await s.startRecording();
    await s.toggleTrack(artist, 'track1');
    await track1().onended!();
    await s.toggleTrack(artist, 'voice_message');
    await s.toggleTrack(artist, 'voice_message');
    expect(s.markerCount.value).toBe(0);
    expect(s.isRecording.value).toBe(true);
  });

  // Not built yet: https://github.com/phaabe/live.moafunk.de/issues/928
  it.skip('tells the host when a track marker could not be saved', async () => {
    api.recordingApi.addMarker.mockRejectedValue(new Error('500'));
    const s = await withShow();
    await s.startRecording();
    await s.toggleTrack(artist, 'track1');
    await track1().onended!();
    expect(onError).toHaveBeenCalled();
  });

  it('shows an error when a track cannot play', async () => {
    const s = await withShow();
    await s.toggleTrack(artist, 'track1');
    track1().onerror!();
    expect(onError).toHaveBeenCalledWith('Failed to play track');
    expect(s.getTrackState(7, 'track1').playing).toBe(false);
  });

  it('says so when the artist has no such track', async () => {
    const s = await withShow();
    await s.toggleTrack(artist, 'track2');
    expect(onError).toHaveBeenCalledWith('Track not available');
    expect(FakeAudio.all).toHaveLength(0);
  });

  it('shows playback progress', async () => {
    const s = await withShow();
    await s.toggleTrack(artist, 'track1');
    track1().duration = 200;
    track1().currentTime = 50;
    track1().ontimeupdate!();
    expect(s.getTrackState(7, 'track1').progress).toBe(25);
    expect(s.isAnyTrackPlaying()).toBe(true);
  });

  it('keeps the volume between 0 and 1, also on a playing track', async () => {
    const s = await withShow();
    await s.toggleTrack(artist, 'track1');
    s.setTrackVolume(7, 'track1', 1.5);
    expect(track1().volume).toBe(1);
    s.setTrackVolume(7, 'track1', -1);
    expect(track1().volume).toBe(0);
  });

  it('names the tracks', () => {
    const s = mountSession();
    expect(s.getTrackName(artist, 'voice_message')).toBe('Voice');
    expect(s.getTrackName(artist, 'track1')).toBe('Song A');
    expect(s.getTrackName(artist, 'track2')).toBe('Track 2');
    expect(s.getTrackUrl(artist, 'track2')).toBeUndefined();
  });
});

describe('show and recordings', () => {
  it('loads the show’s tracks into the browser cache before the show', async () => {
    fetchMock.mockImplementation(async (url: string) => ({
      ok: url !== artist.track1_url,
      arrayBuffer: async () => new ArrayBuffer(1),
    }));
    const s = await withShow();
    expect(fetchMock).toHaveBeenCalledWith(artist.voice_url, { cache: 'force-cache' });
    expect(s.preloadProgress.value).toBe(100);
    expect(s.preloadingTracks.value).toBe(false);
    expect([...s.preloadedTracks.value]).toEqual([artist.voice_url]);
  });

  it('survives a track that fails to download', async () => {
    fetchMock.mockRejectedValue(new Error('offline'));
    const s = await withShow();
    expect(s.preloadProgress.value).toBe(100);
    expect(s.preloadedTracks.value.size).toBe(0);
  });

  it('groups the show’s recordings by state', async () => {
    const rec = (id: number, status: RecordingVersionInfo['status']): RecordingVersionInfo => ({
      id,
      show_id: 48,
      version: `v${id}`,
      status,
      marker_count: 0,
      created_at: '2026-10-30T20:00:00Z',
    });
    api.recordingApi.listRecordings.mockResolvedValue({
      recordings: [rec(1, 'raw'), rec(2, 'failed'), rec(3, 'finalized'), rec(4, 'finalizing')],
    });
    const s = await withShow();
    expect(s.rawRecordings.value.map((r) => r.id)).toEqual([1, 2]);
    expect(s.finalizedRecordings.value.map((r) => r.id)).toEqual([3]);
    expect(s.finalizingRecordings.value.map((r) => r.id)).toEqual([4]);
  });

  it('shows load errors', async () => {
    api.showsApi.list.mockRejectedValue(new Error('Network down'));
    api.recordingApi.getShowWithArtists.mockRejectedValue(new Error('Show not found'));
    api.recordingApi.listRecordings.mockRejectedValue(new Error('Recordings unavailable'));
    const s = await withShow();
    await s.loadShows();
    expect(onError).toHaveBeenCalledWith('Network down');
    expect(onError).toHaveBeenCalledWith('Show not found');
    expect(onError).toHaveBeenCalledWith('Recordings unavailable');
    expect(s.selectedShow.value).toBeNull();
    expect(s.recordings.value).toEqual([]);
  });

  it('lists the shows', async () => {
    api.showsApi.list.mockResolvedValue({ shows: [{ id: 48 }], artists: [] });
    const s = mountSession();
    await s.loadShows();
    expect(s.shows.value).toEqual([{ id: 48 }]);
    expect(s.loadingShows.value).toBe(false);
  });

  it('clears the show when the host deselects it', async () => {
    const s = await withShow();
    expect(s.artists.value).toHaveLength(1);
    s.selectedShowId.value = null;
    await flush();
    expect(s.artists.value).toEqual([]);
    expect(s.trackStates.value.size).toBe(0);
  });
});

describe('leaving the page', () => {
  it('stops the tracks and the clock', async () => {
    const s = await withShow();
    await s.startRecording();
    await s.toggleTrack(artist, 'track1');
    await vi.advanceTimersByTimeAsync(2000);
    app!.unmount();
    app = undefined;
    expect(track1().paused).toBe(true);
    const at = s.formattedDuration.value;
    await vi.advanceTimersByTimeAsync(5000);
    expect(s.formattedDuration.value).toBe(at);
  });
});
