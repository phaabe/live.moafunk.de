<script setup lang="ts">
// Broadcaster self-monitoring preview (#175). Plays the Icecast `/test` mount so
// the host can confirm their audio is actually reaching the relay before/while
// going live. Plain <audio controls> — playback is opt-in (no autoplay) so it
// never starts feedback on its own.
defineProps<{
  /** The Icecast `/test` mount URL (MP3). Component is only mounted when set. */
  src: string;
}>();
</script>

<template>
  <section class="preview-player">
    <div class="preview-header">
      <span class="preview-icon">🎧</span>
      <span class="preview-title">Listen to your stream</span>
    </div>
    <audio class="preview-audio" controls preload="none" :src="src">
      Your browser can’t play this stream.
    </audio>
    <p class="preview-hint">
      Use <strong>headphones</strong> — this is the relay feed and runs a few seconds behind live,
      so playing it on speakers will echo. It confirms your audio is reaching the server.
    </p>
  </section>
</template>

<style scoped>
.preview-player {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: var(--spacing-md) var(--spacing-lg);
  margin-bottom: var(--spacing-xl);
}

.preview-header {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  margin-bottom: var(--spacing-sm);
}

.preview-icon {
  font-size: 1.2rem;
}

.preview-title {
  font-weight: var(--font-weight-bold);
  color: var(--color-text);
}

.preview-audio {
  width: 100%;
}

.preview-hint {
  font-size: var(--font-size-xs);
  color: var(--color-text-muted);
  margin: var(--spacing-sm) 0 0;
}
</style>
