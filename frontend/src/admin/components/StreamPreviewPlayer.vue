<script setup lang="ts">
// Broadcaster self-monitoring preview (#175) + stream spectrum analyzer (#274).
// Plays the Icecast `/test` mount through a Web-Audio graph so the host can
// hear AND see what listeners receive. Playback is opt-in (no autoplay) so it
// never starts feedback on its own.
//
// CORS: reading spectrum data from a cross-origin stream requires
// `Access-Control-Allow-Origin` on the mount. We try CORS mode first
// (crossorigin=anonymous + MediaElementSource → analyser → destination). If the
// mount lacks the header the CORS load errors out, and we rebuild a plain
// element without the graph — audible preview, flat spectrum, visible hint.
// (A MediaElementSource is permanent for its element, so the fallback needs a
// fresh element — that's why the <audio> is created programmatically.)
import { ref, shallowRef, onUnmounted, watch } from 'vue';
import { useSpectrum } from '@admin/composables';
import SpectrumBars from '@admin/components/SpectrumBars.vue';

const props = defineProps<{
  /** The Icecast `/test` mount URL (MP3). Component is only mounted when set. */
  src: string;
}>();

const playing = ref(false);
const starting = ref(false);
/** Monitor volume in % — element volume only; listeners are unaffected. */
const monitorVolume = ref(70);
/** True when CORS mode failed and we fell back to plain (no-spectrum) playback. */
const corsBlocked = ref(false);
const playError = ref<string | null>(null);

const analyser = shallowRef<AnalyserNode | null>(null);
const { bands } = useSpectrum(analyser);

let el: HTMLAudioElement | null = null;
let audioContext: AudioContext | null = null;
let sourceNode: MediaElementAudioSourceNode | null = null;

function teardownElement() {
  if (el) {
    el.onerror = null;
    el.pause();
    el.removeAttribute('src');
    el.load();
    el = null;
  }
  if (sourceNode) {
    sourceNode.disconnect();
    sourceNode = null;
  }
  if (analyser.value) {
    analyser.value.disconnect();
    analyser.value = null;
  }
}

async function startPlayback(withCors: boolean): Promise<void> {
  teardownElement();
  playError.value = null;

  el = new Audio();
  el.preload = 'none';
  if (withCors) el.crossOrigin = 'anonymous';
  el.volume = monitorVolume.value / 100;
  // Cache-bust so a stop→start rejoins the live edge instead of a stale buffer.
  el.src = props.src + (props.src.includes('?') ? '&' : '?') + '_=' + Date.now();

  el.onerror = () => {
    if (withCors) {
      // Most likely a missing Access-Control-Allow-Origin on the mount.
      console.warn('[StreamPreview] CORS load failed — falling back to plain playback');
      corsBlocked.value = true;
      startPlayback(false).catch(() => {});
    } else {
      playError.value = 'Could not play the relay stream.';
      playing.value = false;
    }
  };

  if (withCors) {
    try {
      audioContext = audioContext ?? new AudioContext();
      if (audioContext.state === 'suspended') await audioContext.resume();
      sourceNode = audioContext.createMediaElementSource(el);
      const node = audioContext.createAnalyser();
      node.fftSize = 2048;
      node.smoothingTimeConstant = 0.75;
      sourceNode.connect(node);
      node.connect(audioContext.destination);
      analyser.value = node;
    } catch (e) {
      console.warn('[StreamPreview] Web-Audio graph failed, plain playback only:', e);
    }
  }

  await el.play();
  playing.value = true;
}

async function toggle() {
  if (starting.value) return;
  if (playing.value) {
    teardownElement();
    playing.value = false;
    return;
  }
  starting.value = true;
  try {
    await startPlayback(!corsBlocked.value);
  } catch (e) {
    // A CORS failure usually surfaces via onerror (handled above); anything
    // else lands here.
    if (!playing.value && !corsBlocked.value) {
      playError.value = e instanceof Error ? e.message : 'Could not play the relay stream.';
    }
  } finally {
    starting.value = false;
  }
}

watch(monitorVolume, (v) => {
  if (el) el.volume = v / 100;
});

onUnmounted(() => {
  teardownElement();
  if (audioContext) {
    audioContext.close();
    audioContext = null;
  }
});
</script>

<template>
  <section class="preview-player">
    <div class="preview-header">
      <span class="preview-icon">🎧</span>
      <span class="preview-title">Listen to your stream</span>
      <button class="preview-toggle" :disabled="starting" @click="toggle">
        {{ playing ? '⏹ Stop' : starting ? '…' : '▶ Play' }}
      </button>
    </div>

    <SpectrumBars :bands="bands" variant="stream" />

    <div class="preview-fader">
      <label class="preview-fader-label" for="monitor-volume">Monitor</label>
      <input
        id="monitor-volume"
        v-model.number="monitorVolume"
        class="preview-fader-range"
        type="range"
        min="0"
        max="100"
        step="1"
      />
      <span class="preview-fader-value">{{ monitorVolume }}%</span>
    </div>

    <p v-if="playError" class="preview-error">{{ playError }}</p>
    <p v-else-if="corsBlocked" class="preview-warn">
      Spectrum unavailable — the mount doesn't send CORS headers
      (<code>Access-Control-Allow-Origin</code>). Audio preview still works.
    </p>
    <p class="preview-hint">
      Use <strong>headphones</strong> — the relay feed runs a few seconds behind live, so speakers
      will echo. It confirms your audio is reaching the server.
    </p>
  </section>
</template>

<style scoped>
.preview-player {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-sm);
}

.preview-header {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
}

.preview-icon {
  font-size: 1.2rem;
}

.preview-title {
  flex: 1 1 auto;
  font-weight: var(--font-weight-bold);
  color: var(--color-text);
}

.preview-toggle {
  background: none;
  border: 1px solid var(--color-border-light);
  border-radius: var(--radius-md);
  color: var(--color-text);
  padding: 4px 12px;
  font-family: var(--font-family);
  font-size: var(--font-size-sm);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.preview-toggle:hover:not(:disabled) {
  background: var(--color-surface-alt);
}

.preview-toggle:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.preview-fader {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
}

.preview-fader-label {
  font-family: var(--font-ui);
  font-size: var(--font-size-xs);
  color: var(--color-text-muted);
  white-space: nowrap;
}

.preview-fader-range {
  flex: 1 1 auto;
  accent-color: var(--color-primary);
}

.preview-fader-value {
  min-width: 42px;
  text-align: right;
  font-size: var(--font-size-xs);
  font-variant-numeric: tabular-nums;
  color: var(--color-text);
}

.preview-error {
  margin: 0;
  font-size: var(--font-size-xs);
  color: var(--color-error);
}

.preview-warn {
  margin: 0;
  font-size: var(--font-size-xs);
  color: var(--color-warning);
}

.preview-hint {
  font-size: var(--font-size-xs);
  color: var(--color-text-muted);
  margin: 0;
}
</style>
