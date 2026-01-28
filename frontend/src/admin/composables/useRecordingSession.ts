import { ref, computed, watch, onUnmounted } from 'vue';
import {
  recordingApi,
  showsApi,
  type Show,
  type RecordingArtist,
  type ShowWithArtists,
  type RecordingVersionInfo,
} from '../api';

export type TrackType = 'voice_message' | 'track1' | 'track2';

export interface TrackState {
  playing: boolean;
  progress: number;
  volume: number; // 0-1
  audio: HTMLAudioElement | null;
  startTime: number | null; // When playback started (for marker offset calculation)
  trackKey: string | null; // S3 key of the track being played
}

export interface UseRecordingSessionOptions {
  onError?: (error: string) => void;
  onRecordingStarted?: () => void;
  onRecordingStopped?: (version: string) => void;
  onTrackStarted?: (artistId: number, trackType: TrackType) => void;
  onTrackEnded?: (artistId: number, trackType: TrackType, durationMs: number) => void;
}

export function useRecordingSession(options: UseRecordingSessionOptions = {}) {
  const { onError, onRecordingStarted, onRecordingStopped, onTrackStarted, onTrackEnded } = options;

  // Show selection
  const shows = ref<Show[]>([]);
  const selectedShowId = ref<number | null>(null);
  const selectedShow = ref<ShowWithArtists | null>(null);
  const loadingShows = ref(false);
  const loadingShowDetails = ref(false);

  // Track preloading
  const preloadingTracks = ref(false);
  const preloadProgress = ref(0); // 0-100
  const preloadedTracks = ref<Set<string>>(new Set()); // URLs that have been preloaded

  // Recording state
  const isRecording = ref(false);
  const isStopping = ref(false);
  const recordingStartTime = ref<number | null>(null);
  const recordingDuration = ref(0);
  const markerCount = ref(0);

  // Recording versions list
  const recordings = ref<RecordingVersionInfo[]>([]);
  const loadingRecordings = ref(false);

  // Track playback state
  const trackStates = ref<Map<string, TrackState>>(new Map());

  // Timer interval
  let recordingInterval: ReturnType<typeof setInterval> | null = null;

  // Max recording duration: 60 minutes
  const MAX_RECORDING_MS = 60 * 60 * 1000;

  // Computed
  const formattedDuration = computed(() => {
    const seconds = Math.floor(recordingDuration.value / 1000);
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return `${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
  });

  // Countdown from 60 minutes
  const remainingMs = computed(() => Math.max(0, MAX_RECORDING_MS - recordingDuration.value));

  const formattedCountdown = computed(() => {
    const totalSeconds = Math.floor(remainingMs.value / 1000);
    const mins = Math.floor(totalSeconds / 60);
    const secs = totalSeconds % 60;
    return `${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
  });

  // Warning states for countdown
  const countdownWarning = computed(() => remainingMs.value <= 10 * 60 * 1000); // 10 min warning
  const countdownCritical = computed(() => remainingMs.value <= 5 * 60 * 1000); // 5 min critical

  const artists = computed(() => selectedShow.value?.artists ?? []);

  // Load all shows
  async function loadShows(): Promise<void> {
    loadingShows.value = true;
    try {
      const response = await showsApi.list();
      shows.value = response.shows;
    } catch (e) {
      onError?.(e instanceof Error ? e.message : 'Failed to load shows');
    } finally {
      loadingShows.value = false;
    }
  }

  // Load recordings list for selected show
  async function loadRecordings(): Promise<void> {
    if (!selectedShowId.value) {
      recordings.value = [];
      return;
    }
    loadingRecordings.value = true;
    try {
      const response = await recordingApi.listRecordings(selectedShowId.value);
      recordings.value = response.recordings;
    } catch (e) {
      onError?.(e instanceof Error ? e.message : 'Failed to load recordings');
      recordings.value = [];
    } finally {
      loadingRecordings.value = false;
    }
  }

  // Computed helpers for recordings
  const rawRecordings = computed(() =>
    recordings.value.filter((r) => r.status === 'raw' || r.status === 'failed')
  );

  const finalizedRecordings = computed(() =>
    recordings.value.filter((r) => r.status === 'finalized')
  );

  const finalizingRecordings = computed(() =>
    recordings.value.filter((r) => r.status === 'finalizing')
  );

  // Load show details with artists
  async function loadShowDetails(showId: number): Promise<void> {
    loadingShowDetails.value = true;
    try {
      selectedShow.value = await recordingApi.getShowWithArtists(showId);
      // Initialize track states for all artists
      initializeTrackStates();
      // Preload all tracks in background
      preloadAllTracks();
    } catch (e) {
      onError?.(e instanceof Error ? e.message : 'Failed to load show details');
      selectedShow.value = null;
    } finally {
      loadingShowDetails.value = false;
    }
  }

  // Preload all artist tracks into browser cache
  async function preloadAllTracks(): Promise<void> {
    if (!selectedShow.value) return;

    // Collect all track URLs
    const trackUrls: string[] = [];
    for (const artist of selectedShow.value.artists) {
      if (artist.voice_url) trackUrls.push(artist.voice_url);
      if (artist.track1_url) trackUrls.push(artist.track1_url);
      if (artist.track2_url) trackUrls.push(artist.track2_url);
    }

    if (trackUrls.length === 0) return;

    preloadingTracks.value = true;
    preloadProgress.value = 0;
    preloadedTracks.value.clear();

    let loaded = 0;

    // Preload tracks in parallel (but limit concurrency to avoid overwhelming the browser)
    const concurrency = 3;
    const chunks: string[][] = [];
    for (let i = 0; i < trackUrls.length; i += concurrency) {
      chunks.push(trackUrls.slice(i, i + concurrency));
    }

    for (const chunk of chunks) {
      await Promise.all(
        chunk.map(async (url) => {
          try {
            // Use fetch with cache to preload the audio file
            const response = await fetch(url, { cache: 'force-cache' });
            if (response.ok) {
              // Read the response to ensure it's fully downloaded
              await response.arrayBuffer();
              preloadedTracks.value.add(url);
            }
          } catch (e) {
            console.warn('Failed to preload track:', url, e);
          } finally {
            loaded++;
            preloadProgress.value = Math.round((loaded / trackUrls.length) * 100);
          }
        })
      );
    }

    preloadingTracks.value = false;
    console.log(
      `[RecordingSession] Preloaded ${preloadedTracks.value.size}/${trackUrls.length} tracks`
    );
  }

  // Initialize track states for current show's artists
  function initializeTrackStates(): void {
    trackStates.value.clear();
    if (!selectedShow.value) return;

    for (const artist of selectedShow.value.artists) {
      trackStates.value.set(`${artist.id}-voice_message`, {
        playing: false,
        progress: 0,
        volume: 1,
        audio: null,
        startTime: null,
        trackKey: null,
      });
      trackStates.value.set(`${artist.id}-track1`, {
        playing: false,
        progress: 0,
        volume: 1,
        audio: null,
        startTime: null,
        trackKey: null,
      });
      trackStates.value.set(`${artist.id}-track2`, {
        playing: false,
        progress: 0,
        volume: 1,
        audio: null,
        startTime: null,
        trackKey: null,
      });
    }
  }

  // Watch for show selection changes
  watch(selectedShowId, (newId) => {
    if (newId !== null) {
      loadShowDetails(newId);
      loadRecordings();
    } else {
      selectedShow.value = null;
      trackStates.value.clear();
      recordings.value = [];
    }
  });

  // Start recording session
  async function startRecording(): Promise<boolean> {
    if (!selectedShow.value) {
      onError?.('No show selected');
      return false;
    }

    try {
      await recordingApi.start(selectedShow.value.id);
      isRecording.value = true;
      recordingStartTime.value = Date.now();
      recordingDuration.value = 0;
      markerCount.value = 0;

      // Start duration timer
      recordingInterval = setInterval(() => {
        if (recordingStartTime.value) {
          recordingDuration.value = Date.now() - recordingStartTime.value;
        }
      }, 100);

      onRecordingStarted?.();
      return true;
    } catch (e) {
      onError?.(e instanceof Error ? e.message : 'Failed to start recording');
      return false;
    }
  }

  // Stop recording session
  async function stopRecording(): Promise<string | null> {
    // Prevent double-clicks
    if (isStopping.value || !isRecording.value) {
      return null;
    }
    isStopping.value = true;

    try {
      // Stop all playing tracks first
      stopAllTracks();

      const response = await recordingApi.stop();
      isRecording.value = false;
      recordingStartTime.value = null;

      if (recordingInterval) {
        clearInterval(recordingInterval);
        recordingInterval = null;
      }

      // Extract version from raw_key: recordings/{show_id}/{version}/raw.webm
      const versionMatch = response.raw_key.match(/recordings\/\d+\/([^/]+)\//);
      const version = versionMatch ? versionMatch[1] : response.raw_key;

      onRecordingStopped?.(version);
      return version;
    } catch (e) {
      onError?.(e instanceof Error ? e.message : 'Failed to stop recording');
      return null;
    } finally {
      isStopping.value = false;
    }
  }

  // Check recording status (for resuming after page reload)
  async function checkRecordingStatus(): Promise<void> {
    try {
      const status = await recordingApi.status();
      if (status.is_recording) {
        isRecording.value = true;
        markerCount.value = status.marker_count ?? 0;

        if (status.started_at) {
          recordingStartTime.value = new Date(status.started_at).getTime();
          recordingDuration.value = Date.now() - recordingStartTime.value;

          // Resume duration timer
          recordingInterval = setInterval(() => {
            if (recordingStartTime.value) {
              recordingDuration.value = Date.now() - recordingStartTime.value;
            }
          }, 100);
        }

        if (status.show_id) {
          selectedShowId.value = status.show_id;
        }
      }
    } catch (e) {
      console.error('Failed to check recording status:', e);
    }
  }

  // Track key helper (for local state management)
  function getTrackKey(artistId: number, trackType: TrackType): string {
    return `${artistId}-${trackType}`;
  }

  // Get track URL from artist (for playback)
  function getTrackUrl(artist: RecordingArtist, trackType: TrackType): string | undefined {
    switch (trackType) {
      case 'voice_message':
        return artist.voice_url;
      case 'track1':
        return artist.track1_url;
      case 'track2':
        return artist.track2_url;
    }
  }

  // Get track S3 key from artist (for marker/finalize)
  function getTrackS3Key(artist: RecordingArtist, trackType: TrackType): string | undefined {
    switch (trackType) {
      case 'voice_message':
        return artist.voice_key;
      case 'track1':
        return artist.track1_key;
      case 'track2':
        return artist.track2_key;
    }
  }

  // Get track name from artist
  function getTrackName(artist: RecordingArtist, trackType: TrackType): string {
    switch (trackType) {
      case 'voice_message':
        return 'Voice';
      case 'track1':
        return artist.track1_name || 'Track 1';
      case 'track2':
        return artist.track2_name || 'Track 2';
    }
  }

  // Get track state
  function getTrackState(artistId: number, trackType: TrackType): TrackState {
    const key = getTrackKey(artistId, trackType);
    return (
      trackStates.value.get(key) ?? {
        playing: false,
        progress: 0,
        volume: 1,
        audio: null,
        startTime: null,
        trackKey: null,
      }
    );
  }

  // Set track volume
  function setTrackVolume(artistId: number, trackType: TrackType, volume: number): void {
    const key = getTrackKey(artistId, trackType);
    const state = trackStates.value.get(key);
    if (state) {
      state.volume = Math.max(0, Math.min(1, volume));
      if (state.audio) {
        state.audio.volume = state.volume;
      }
    }
  }

  // Play or stop a track
  async function toggleTrack(artist: RecordingArtist, trackType: TrackType): Promise<void> {
    const key = getTrackKey(artist.id, trackType);
    const url = getTrackUrl(artist, trackType);
    const s3Key = getTrackS3Key(artist, trackType);

    if (!url) {
      onError?.('Track not available');
      return;
    }

    const state = trackStates.value.get(key);
    if (!state) return;

    // If already playing, stop it and send marker for partial playback
    if (state.playing && state.audio) {
      const durationMs = state.startTime ? Date.now() - state.startTime : 0;

      // Send marker for partial playback if recording
      if (isRecording.value && recordingStartTime.value && state.startTime && state.trackKey) {
        const offsetMs = state.startTime - recordingStartTime.value;
        try {
          await recordingApi.addMarker({
            artist_id: artist.id,
            track_type: trackType,
            track_key: state.trackKey,
            duration_ms: durationMs,
            offset_ms: Math.max(0, offsetMs),
          });
          markerCount.value++;
        } catch (e) {
          console.error('Failed to add marker for stopped track:', e);
        }
      }

      state.audio.pause();
      state.audio.currentTime = 0;
      state.playing = false;
      state.progress = 0;
      state.startTime = null;
      state.trackKey = null;
      state.audio = null;
      return;
    }

    // Create audio element and play
    const audio = new Audio(url);
    audio.volume = state.volume; // Apply saved volume
    state.audio = audio;
    state.playing = true;

    // Track playback time for marker - store in state for manual stop
    const playStartTime = Date.now();
    state.startTime = playStartTime;
    state.trackKey = s3Key || null;

    audio.ontimeupdate = () => {
      if (audio.duration) {
        state.progress = (audio.currentTime / audio.duration) * 100;
      }
    };

    audio.onended = async () => {
      const durationMs = Date.now() - playStartTime;
      state.playing = false;
      state.progress = 0;
      state.startTime = null;
      state.trackKey = null;
      state.audio = null;

      onTrackEnded?.(artist.id, trackType, durationMs);

      // Add marker if recording - calculate offset from when track STARTED playing
      if (isRecording.value && recordingStartTime.value && s3Key) {
        try {
          // offset_ms is when the track started relative to recording start
          const offsetMs = playStartTime - recordingStartTime.value;
          await recordingApi.addMarker({
            artist_id: artist.id,
            track_type: trackType,
            track_key: s3Key,
            duration_ms: durationMs,
            offset_ms: Math.max(0, offsetMs), // Ensure non-negative
          });
          markerCount.value++;
        } catch (e) {
          console.error('Failed to add marker:', e);
        }
      }
    };

    audio.onerror = () => {
      onError?.('Failed to play track');
      state.playing = false;
      state.progress = 0;
      state.startTime = null;
      state.trackKey = null;
      state.audio = null;
    };

    onTrackStarted?.(artist.id, trackType);

    await audio.play();
  }

  // Stop all playing tracks
  function stopAllTracks(): void {
    for (const [, state] of trackStates.value) {
      if (state.audio) {
        state.audio.pause();
        state.audio.currentTime = 0;
      }
      state.playing = false;
      state.progress = 0;
      state.audio = null;
    }
  }

  // Check if any track is currently playing
  function isAnyTrackPlaying(): boolean {
    for (const [, state] of trackStates.value) {
      if (state.playing) return true;
    }
    return false;
  }

  // Cleanup on unmount
  onUnmounted(() => {
    if (recordingInterval) {
      clearInterval(recordingInterval);
    }
    stopAllTracks();
  });

  return {
    // Show state
    shows,
    selectedShowId,
    selectedShow,
    loadingShows,
    loadingShowDetails,
    artists,

    // Recording state
    isRecording,
    isStopping,
    recordingStartTime,
    recordingDuration,
    formattedDuration,
    formattedCountdown,
    remainingMs,
    countdownWarning,
    countdownCritical,
    markerCount,

    // Recordings list
    recordings,
    loadingRecordings,
    rawRecordings,
    finalizedRecordings,
    finalizingRecordings,

    // Track preloading
    preloadingTracks,
    preloadProgress,
    preloadedTracks,

    // Track state
    trackStates,

    // Actions
    loadShows,
    loadShowDetails,
    loadRecordings,
    preloadAllTracks,
    startRecording,
    stopRecording,
    checkRecordingStatus,
    toggleTrack,
    setTrackVolume,
    stopAllTracks,
    isAnyTrackPlaying,

    // Helpers
    getTrackKey,
    getTrackUrl,
    getTrackName,
    getTrackState,
  };
}
