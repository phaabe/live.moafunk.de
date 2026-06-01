<script setup lang="ts">
import { computed } from 'vue';
import type { ShowDetail } from '../../api';
import { BaseButton } from '@shared/components';
import AudioPlayer from '../AudioPlayer.vue';

const props = defineProps<{
  show: ShowDetail;
  /** Selected media mode (owned by the page so it survives reloads). */
  mode: 'live' | 'upload';
  /** Whether the current viewer (assigned host) may change media. */
  canManage: boolean;
}>();

const emit = defineEmits<{
  /** Go to the live user story. */
  'select-live': [];
  /** Go to the upload user story. */
  'select-upload': [];
  'mark-uploaded': [];
}>();

const hasFile = computed(() => !!props.show.prerecorded_key);
const confirmed = computed(() => !!props.show.prerecorded_confirmed_at);

const statusText = computed(() => {
  if (!hasFile.value) return 'No file';
  return confirmed.value ? 'Confirmed ✓' : 'Uploaded';
});
</script>

<template>
  <div class="card media-card">
    <h2 class="section-title"><span class="ico">🔊</span> Media type</h2>

    <div class="mode-toggle">
      <button
        type="button"
        class="mode-btn"
        :class="{ active: mode === 'live' }"
        :disabled="!canManage"
        @click="emit('select-live')"
      >
        <span class="mode-ico">📡</span>
        <span>Live</span>
      </button>
      <button
        type="button"
        class="mode-btn"
        :class="{ active: mode === 'upload' }"
        :disabled="!canManage"
        @click="emit('select-upload')"
      >
        <span class="mode-ico">☁️</span>
        <span>Upload</span>
      </button>
    </div>

    <!-- Live mode -->
    <p v-if="mode === 'live'" class="media-hint">
      This show airs live — no pre-recorded file needed.
    </p>

    <!-- Upload mode -->
    <template v-else>
      <!-- Existing file (read-only; uploading/replacing happens in the upload flow) -->
      <template v-if="hasFile">
        <p class="media-file">
          <span class="media-file-label">File</span>
          <code>{{ show.prerecorded_filename || 'unknown' }}</code>
        </p>
        <AudioPlayer
          v-if="show.prerecorded_url"
          :key="show.prerecorded_url"
          :src="show.prerecorded_url"
        />
      </template>

      <!-- Status row -->
      <div class="status-row">
        <span class="status-label">Upload status</span>
        <span class="status-value" :class="{ done: confirmed }">{{ statusText }}</span>
        <BaseButton
          v-if="canManage"
          size="sm"
          variant="primary"
          :disabled="!hasFile || confirmed"
          @click="emit('mark-uploaded')"
        >
          Mark uploaded
        </BaseButton>
      </div>
    </template>
  </div>
</template>

<style scoped>
.media-card {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
}

.section-title {
  display: flex;
  align-items: center;
  gap: var(--spacing-xs);
  font-size: var(--font-size-sm);
  font-weight: 700;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  color: var(--color-text-muted);
  margin: 0;
}

.mode-toggle {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--spacing-md);
}

.mode-btn {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--spacing-xs);
  padding: var(--spacing-lg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  background: var(--color-surface);
  color: var(--color-text);
  font-family: var(--font-family);
  font-size: var(--font-size-sm);
  cursor: pointer;
  transition:
    border-color var(--transition-fast),
    background var(--transition-fast);
}

.mode-btn:hover:not(:disabled) {
  border-color: var(--color-primary);
}

.mode-btn.active {
  border-color: var(--color-primary);
  background: var(--color-surface-hover);
  box-shadow: inset 0 0 0 1px var(--color-primary);
}

.mode-btn:disabled {
  cursor: default;
  opacity: 0.7;
}

.mode-ico {
  font-size: 1.4rem;
}

.media-hint {
  margin: 0;
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}

.media-file {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  margin: 0;
}

.media-file-label {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}

.media-file code {
  font-size: var(--font-size-sm);
  word-break: break-all;
}

.status-row {
  display: flex;
  align-items: center;
  gap: var(--spacing-md);
  padding: var(--spacing-sm) var(--spacing-md);
  background: var(--color-surface);
  border-radius: var(--radius-md);
}

.status-label {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}

.status-value {
  flex: 1 1 auto;
  font-size: var(--font-size-sm);
  font-weight: 600;
  color: var(--color-text);
}

.status-value.done {
  color: var(--color-success);
}
</style>
