<script setup lang="ts">
import { computed, ref } from 'vue';
import type { ShowDetail } from '../../api';
import { BaseButton } from '@shared/components';
import AudioPlayer from '../AudioPlayer.vue';

type UploadProgress = {
  phase: 'uploading' | 'finalizing';
  percent: number;
  chunkIndex?: number;
  totalChunks?: number;
} | null;

const props = defineProps<{
  show: ShowDetail;
  /** Selected media mode (owned by the page so it survives reloads). */
  mode: 'live' | 'upload';
  /** Whether the current viewer (assigned host) may change media. */
  canManage: boolean;
  uploadProgress: UploadProgress;
}>();

const emit = defineEmits<{
  'select-live': [];
  'select-upload': [];
  browse: [];
  'pick-file': [file: File];
  'mark-uploaded': [];
  remove: [];
}>();

const hasFile = computed(() => !!props.show.prerecorded_key);
const confirmed = computed(() => !!props.show.prerecorded_confirmed_at);

const statusText = computed(() => {
  if (!hasFile.value) return 'No file';
  return confirmed.value ? 'Confirmed ✓' : 'Uploaded';
});

const isDragging = ref(false);

function onDrop(e: DragEvent) {
  e.preventDefault();
  isDragging.value = false;
  if (!props.canManage) return;
  const file = e.dataTransfer?.files?.[0];
  if (file) emit('pick-file', file);
}
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
      <!-- Existing file -->
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
        <div v-if="canManage" class="media-actions">
          <BaseButton size="sm" variant="ghost" @click="emit('browse')">Replace</BaseButton>
          <BaseButton size="sm" variant="ghost" @click="emit('remove')">Remove</BaseButton>
        </div>
      </template>

      <!-- No file: drop zone (host) or notice -->
      <template v-else>
        <div
          v-if="canManage"
          class="drop-zone"
          :class="{ dragging: isDragging }"
          role="button"
          tabindex="0"
          @click="emit('browse')"
          @keydown.enter="emit('browse')"
          @dragover.prevent="isDragging = true"
          @dragleave.prevent="isDragging = false"
          @drop="onDrop"
        >
          <span class="drop-ico">📁</span>
          <span>Drag &amp; drop an audio file or click to browse</span>
        </div>
        <p v-else class="media-hint">No file uploaded yet.</p>
      </template>

      <!-- Upload progress -->
      <div v-if="uploadProgress" class="upload-progress">
        <div class="progress-info">
          <span>{{
            uploadProgress.phase === 'finalizing'
              ? 'Finalizing…'
              : `Uploading… ${uploadProgress.percent}%`
          }}</span>
          <span v-if="uploadProgress.totalChunks">
            Chunk {{ uploadProgress.chunkIndex }} / {{ uploadProgress.totalChunks }}
          </span>
        </div>
        <div class="progress-bar">
          <div class="progress-fill" :style="{ width: `${uploadProgress.percent}%` }"></div>
        </div>
      </div>

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

.media-actions {
  display: flex;
  gap: var(--spacing-sm);
}

.drop-zone {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--spacing-sm);
  border: 2px dashed var(--color-border);
  border-radius: var(--radius-lg);
  padding: var(--spacing-xl);
  text-align: center;
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
  cursor: pointer;
  transition:
    border-color var(--transition-fast),
    background var(--transition-fast);
}

.drop-zone:hover,
.drop-zone.dragging {
  border-color: var(--color-primary);
  background: var(--color-surface);
}

.drop-ico {
  font-size: 1.5rem;
}

.upload-progress {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
}

.progress-info {
  display: flex;
  justify-content: space-between;
  font-size: var(--font-size-xs);
  color: var(--color-text-muted);
}

.progress-bar {
  height: 6px;
  border-radius: var(--radius-full);
  background: var(--color-surface);
  overflow: hidden;
}

.progress-fill {
  height: 100%;
  background: var(--color-primary);
  transition: width var(--transition-fast);
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
