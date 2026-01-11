<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch } from 'vue';
import WaveSurfer from 'wavesurfer.js';

const props = defineProps<{
  src: string;
  label?: string;
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

let wavesurfer: WaveSurfer | null = null;
let isDestroyed = false;

function formatTime(seconds: number): string {
  const mins = Math.floor(seconds / 60);
  const secs = Math.floor(seconds % 60);
  return `${mins}:${secs.toString().padStart(2, '0')}`;
}

function initWaveSurfer(): void {
  if (!waveformContainer.value || isDestroyed || isInitialized.value) return;
  
  if (!props.src) {
    console.warn('AudioPlayer: No src provided');
    return;
  }

  isLoading.value = true;
  isInitialized.value = true;

  // Destroy existing instance
  if (wavesurfer) {
    try {
      wavesurfer.destroy();
    } catch {
      // Ignore errors during destroy
    }
  }

  // Use MediaElement backend for streaming (buffers progressively, doesn't download entire file)
  wavesurfer = WaveSurfer.create({
    container: waveformContainer.value,
    waveColor: '#666666',
    progressColor: '#ffec44',
    cursorColor: '#ffec44',
    barWidth: 2,
    barGap: 1,
    barRadius: 2,
    height: 48,
    normalize: true,
    backend: 'MediaElement',
    mediaControls: false,
  });

  wavesurfer.load(props.src);

  wavesurfer.on('loading', () => {
    isLoading.value = true;
  });

  wavesurfer.on('ready', () => {
    if (isDestroyed) return;
    isLoading.value = false;
    if (wavesurfer) {
      duration.value = formatTime(wavesurfer.getDuration());
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
    initWaveSurfer();
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

watch(() => props.src, (newSrc, oldSrc) => {
  const newBasePath = getBasePath(newSrc);
  const oldBasePath = oldSrc ? getBasePath(oldSrc) : '';
  
  if (newBasePath !== oldBasePath) {
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
    <button 
      class="play-btn" 
      @click="togglePlay" 
      :disabled="isLoading"
      :aria-label="isPlaying ? 'Pause' : 'Play'"
    >
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
  0%, 100% { opacity: 1; }
  50% { opacity: 0.5; }
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

  .download-btn {
    display: none;
  }
}
</style>
