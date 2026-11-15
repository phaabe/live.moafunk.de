<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch } from 'vue';
import WaveSurfer from 'wavesurfer.js';

const props = withDefaults(defineProps<{
  src: string;
  showVolume?: boolean;
  initialVolume?: number;
}>(), {
  showVolume: false,
  initialVolume: 100,
});

const emit = defineEmits<{
  (e: 'volumeChange', volume: number): void;
}>();

const globalAudioBus: EventTarget = typeof window !== 'undefined'
  ? ((window as any).__audioPlayerBus ||= new EventTarget())
  : new EventTarget();
const playerId = `audio-player-${Math.random().toString(36).slice(2)}-${Date.now()}`;

const waveformContainer = ref<HTMLElement | null>(null);
const isPlaying = ref(false);
const currentTime = ref('0:00');
const duration = ref('0:00');
const isLoading = ref(false);
const isInitialized = ref(false);
const volume = ref(props.initialVolume);

let wavesurfer: WaveSurfer | null = null;
let isDestroyed = false;
// Bumped when the file changes, so a load that is still reading the header stops.
let loadId = 0;

/** Longer files stream: playback starts at once, flat bars stand in for the waveform. */
const WAVEFORM_MAX_SECS = 10 * 60;

/** Flat bars for a streamed file: enough values for one bar per 3 px up to 12 000 px. */
function flatPeaks(): Float32Array[] {
  return [new Float32Array(4000).fill(0.1)];
}

/**
 * Read the length from the file header only (a small range request).
 * `wavesurfer.load(url)` without peaks downloads and decodes the whole file
 * before it can play, which takes too long for a 2 h set.
 */
function readDuration(src: string): Promise<number> {
  const probe = document.createElement('audio');
  probe.preload = 'metadata';
  return new Promise<number>((resolve, reject) => {
    probe.addEventListener('loadedmetadata', () => resolve(probe.duration), { once: true });
    probe.addEventListener(
      'error',
      () => reject(probe.error ?? new Error('Audio file failed to load')),
      { once: true }
    );
    probe.src = src;
  }).finally(() => {
    // Stop the probe. Wavesurfer plays through its own audio element.
    probe.removeAttribute('src');
    probe.load();
  });
}

function formatTime(seconds: number): string {
  const mins = Math.floor(seconds / 60);
  const secs = Math.floor(seconds % 60);
  return `${mins}:${secs.toString().padStart(2, '0')}`;
}

async function initWaveSurfer(): Promise<void> {
  if (!waveformContainer.value || isDestroyed || isInitialized.value) return;

  if (!props.src) {
    console.warn('AudioPlayer: No src provided');
    return;
  }

  isLoading.value = true;
  isInitialized.value = true;
  const id = ++loadId;
  const src = props.src;

  let fileSecs: number;
  try {
    fileSecs = await readDuration(src);
  } catch (err) {
    if (isDestroyed || id !== loadId) return;
    console.error('AudioPlayer: could not read the audio file:', err);
    isLoading.value = false;
    isInitialized.value = false;
    return;
  }
  if (isDestroyed || id !== loadId || !waveformContainer.value) return;
  // NaN or Infinity (unknown length) also streams.
  const stream = !(fileSecs <= WAVEFORM_MAX_SECS);

  // Destroy existing instance
  if (wavesurfer) {
    try {
      wavesurfer.destroy();
    } catch {
      // Ignore errors during destroy
    }
  }

  wavesurfer = WaveSurfer.create({
    container: waveformContainer.value,
    waveColor: '#666666',
    progressColor: '#ffec44',
    cursorColor: '#ffec44',
    barWidth: 2,
    barGap: 1,
    barRadius: 2,
    height: 48,
    // A real waveform fills the height; the flat bars stay thin.
    normalize: !stream,
    backend: 'MediaElement',
    mediaControls: false,
  });

  wavesurfer.on('loading', () => {
    isLoading.value = true;
  });

  wavesurfer.on('ready', () => {
    if (isDestroyed) return;
    isLoading.value = false;
    if (wavesurfer) {
      duration.value = formatTime(wavesurfer.getDuration());
      // Apply initial volume (0-200 maps to 0-2)
      wavesurfer.setVolume(volume.value / 100);
      // Auto-play after lazy init
      globalAudioBus.dispatchEvent(new CustomEvent('audio-play', { detail: { id: playerId } }));
      wavesurfer.play();
    }
  });

  wavesurfer.on('audioprocess', () => {
    if (isDestroyed) return;
    if (wavesurfer) {
      currentTime.value = formatTime(wavesurfer.getCurrentTime());
    }
  });

  wavesurfer.on('seeking', () => {
    if (isDestroyed) return;
    if (wavesurfer) {
      currentTime.value = formatTime(wavesurfer.getCurrentTime());
    }
  });

  wavesurfer.on('play', () => {
    if (isDestroyed) return;
    isPlaying.value = true;
  });

  wavesurfer.on('pause', () => {
    if (isDestroyed) return;
    isPlaying.value = false;
  });

  wavesurfer.on('finish', () => {
    if (isDestroyed) return;
    isPlaying.value = false;
  });

  wavesurfer.on('error', (err) => {
    if (isDestroyed) return;
    console.error('WaveSurfer error:', err);
    isLoading.value = false;
  });

  if (stream) {
    // With peaks and a length, wavesurfer skips the download and decode:
    // the audio element plays the file while it streams.
    wavesurfer.load(src, flatPeaks(), fileSecs);
  } else {
    // Short file: download and decode it all to draw the real waveform.
    wavesurfer.load(src);
  }
}

function getBasePath(url: string): string {
  try {
    const urlObj = new URL(url);
    return urlObj.origin + urlObj.pathname;
  } catch {
    return url.split('?')[0];
  }
}

function togglePlay(): void {
  // Lazy init: only create wavesurfer when user clicks play
  if (!isInitialized.value) {
    void initWaveSurfer();
    return;
  }

  if (wavesurfer) {
    if (!wavesurfer.isPlaying()) {
      globalAudioBus.dispatchEvent(new CustomEvent('audio-play', { detail: { id: playerId } }));
    }
    wavesurfer.playPause();
  }
}

function handleGlobalPlay(event: Event): void {
  const detail = (event as CustomEvent<{ id: string }>).detail;
  if (detail?.id !== playerId && wavesurfer?.isPlaying()) {
    wavesurfer.pause();
  }
}

function handleVolumeChange(event: Event): void {
  const target = event.target as HTMLInputElement;
  const newVolume = Number(target.value);
  volume.value = newVolume;
  if (wavesurfer) {
    wavesurfer.setVolume(newVolume / 100);
  }
  emit('volumeChange', newVolume);
}

watch(() => props.initialVolume, (newVolume) => {
  volume.value = newVolume;
  if (wavesurfer) {
    wavesurfer.setVolume(newVolume / 100);
  }
});

watch(() => props.src, (newSrc, oldSrc) => {
  const newBasePath = getBasePath(newSrc);
  const oldBasePath = oldSrc ? getBasePath(oldSrc) : '';

  if (newBasePath !== oldBasePath) {
    loadId++;
    isInitialized.value = false;
    isLoading.value = false;
    if (wavesurfer) {
      try {
        wavesurfer.destroy();
      } catch {
        // Ignore errors
      }
      wavesurfer = null;
    }
  }
});

onMounted(() => {
  globalAudioBus.addEventListener('audio-play', handleGlobalPlay);
});

onUnmounted(() => {
  isDestroyed = true;
  globalAudioBus.removeEventListener('audio-play', handleGlobalPlay);
  if (wavesurfer) {
    try {
      wavesurfer.destroy();
    } catch {
      // Ignore errors during destroy
    }
    wavesurfer = null;
  }
});
</script>

<template>
  <div class="audio-player">
    <button class="play-btn" @click="togglePlay" :disabled="isLoading" :aria-label="isPlaying ? 'Pause' : 'Play'">
      <span v-if="isLoading" class="loading-icon">⏳</span>
      <span v-else-if="isPlaying" class="pause-icon">❚❚</span>
      <span v-else class="play-icon">▶</span>
    </button>

    <div class="waveform-wrapper">
      <div v-if="!isInitialized" class="waveform-placeholder" @click="togglePlay">
        <div class="placeholder-bars"></div>
      </div>
      <div v-show="isInitialized" ref="waveformContainer" class="waveform"></div>
    </div>

    <div class="time-display">
      <span class="current-time">{{ currentTime }}</span>
      <span class="separator">/</span>
      <span class="duration">{{ duration }}</span>
    </div>

    <div v-if="showVolume" class="volume-control">
      <input type="range" min="0" max="200" :value="volume" @input="handleVolumeChange" class="volume-slider"
        :title="`Volume: ${volume}%`" />
      <span class="volume-label">{{ volume }}%</span>
    </div>

    <a :href="src" download class="download-btn" title="Download">
      ⬇
    </a>
  </div>
</template>

<style scoped>
.audio-player {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  padding: var(--spacing-sm);
  background-color: var(--color-surface);
  border-radius: var(--radius-md);
  border: 1px solid var(--color-border);
}

.play-btn {
  width: 40px;
  height: 40px;
  border-radius: 50%;
  border: 1px solid var(--color-border);
  background-color: transparent;
  color: var(--color-text);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;
  flex-shrink: 0;
  transition: all var(--transition-fast);
}

.play-btn:hover:not(:disabled) {
  border-color: #ffec44;
  color: #ffec44;
}

.play-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.play-icon {
  margin-left: 2px;
}

.pause-icon {
  font-size: 12px;
  letter-spacing: 2px;
}

.loading-icon {
  animation: pulse 1s ease-in-out infinite;
}

@keyframes pulse {

  0%,
  100% {
    opacity: 1;
  }

  50% {
    opacity: 0.5;
  }
}

.waveform-wrapper {
  flex: 1;
  min-width: 0;
  overflow: hidden;
}

.waveform {
  width: 100%;
  cursor: pointer;
}

.time-display {
  display: flex;
  align-items: center;
  gap: 2px;
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
  flex-shrink: 0;
  font-variant-numeric: tabular-nums;
}

.current-time {
  color: #ffec44;
}

.separator {
  opacity: 0.5;
}

.volume-control {
  display: flex;
  align-items: center;
  gap: var(--spacing-xs);
  flex-shrink: 0;
}

.volume-slider {
  width: 60px;
  height: 4px;
  -webkit-appearance: none;
  appearance: none;
  background: var(--color-surface-alt);
  border-radius: 2px;
  outline: none;
  cursor: pointer;
}

.volume-slider::-webkit-slider-thumb {
  -webkit-appearance: none;
  appearance: none;
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background: var(--color-text-muted);
  cursor: pointer;
  transition: all 0.1s ease;
}

.volume-slider::-webkit-slider-thumb:hover {
  background: #ffec44;
  transform: scale(1.2);
}

.volume-slider::-moz-range-thumb {
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background: var(--color-text-muted);
  cursor: pointer;
  border: none;
}

.volume-slider::-moz-range-thumb:hover {
  background: #ffec44;
}

.volume-label {
  font-size: 0.7em;
  color: var(--color-text-muted);
  min-width: 35px;
  text-align: right;
  font-variant-numeric: tabular-nums;
}

.download-btn {
  color: var(--color-text-muted);
  text-decoration: none;
  font-size: 16px;
  padding: var(--spacing-xs);
  transition: color var(--transition-fast);
  flex-shrink: 0;
}

.download-btn:hover {
  color: #ffec44;
}

/* Mobile: compact mode - hide waveform, show only play button and duration */
@media (max-width: 768px) {
  .audio-player {
    padding: var(--spacing-xs);
    gap: var(--spacing-xs);
  }

  .play-btn {
    width: 32px;
    height: 32px;
    font-size: 12px;
  }

  .waveform-wrapper {
    display: none;
  }

  .time-display {
    font-size: 0.8em;
  }

  .volume-control {
    display: none;
  }

  .download-btn {
    display: none;
  }
}
</style>
