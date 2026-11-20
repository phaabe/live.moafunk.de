<script setup lang="ts">
// Broadcaster self-monitoring preview (#175) + stream spectrum analyzer (#274).
// Plays the Icecast `/test` mount through a Web-Audio graph so the host can
// hear AND see what listeners receive. Playback is opt-in (no autoplay) so it
// never starts feedback on its own. The player itself (and its CORS fallback)
// lives in useStreamMonitor, which On Air's audio card uses too (#1053).
import { useStreamMonitor } from '@admin/composables/useStreamMonitor';
import SpectrumBars from '@admin/components/SpectrumBars.vue';

const props = defineProps<{
  /** The Icecast `/test` mount URL (MP3). Component is only mounted when set. */
  src: string;
}>();

const { playing, starting, monitorVolume, corsBlocked, playError, bands, toggle } =
  useStreamMonitor(() => props.src);
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
      Spectrum unavailable — usually a missing <code>Access-Control-Allow-Origin</code> header on
      the mount. Audio preview still works.
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
