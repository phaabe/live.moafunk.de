<script setup lang="ts">
import { computed, ref } from 'vue';
import { BaseButton, BaseModal } from '@shared/components';
import AudioPlayer from '../AudioPlayer.vue';
import { useHostFlow } from '../../composables/useHostFlow';

/**
 * Inline pre-recorded preparation for the show dashboard: dropzone → chunked
 * upload progress → preview + confirm, absorbing the old fullscreen
 * /stream/upload and /stream/confirm steps.
 *
 * Reuses the useHostFlow singleton (upload/confirm/delete + progress); the
 * owning page selects the show in the flow before rendering this panel and
 * refreshes its own copy of the show on `changed`.
 */
const props = defineProps<{
  showId: number;
}>();

const emit = defineEmits<{
  /** Upload/confirm/delete completed — the page should reload the show. */
  changed: [];
}>();

const flow = useHostFlow();

const isDragging = ref(false);
const confirming = ref(false);
const deleting = ref(false);
const showDeleteModal = ref(false);
const localError = ref<string | null>(null);
const fileInput = ref<HTMLInputElement | null>(null);

/** Guard: the flow singleton must have this show selected. */
const flowReady = computed(() => flow.show.value?.id === props.showId);

const hasUpload = computed(() => flowReady.value && flow.hasUpload.value);
const isConfirmed = computed(() => flowReady.value && flow.isConfirmed.value);
const uploading = computed(() => flow.uploading.value);
const progress = computed(() => flow.uploadProgress.value);

const ACCEPTED = ['.mp3', '.wav', '.flac', '.ogg', '.m4a'];

function isAccepted(file: File): boolean {
  const name = file.name.toLowerCase();
  return ACCEPTED.some((ext) => name.endsWith(ext));
}

async function handleFile(file: File) {
  localError.value = null;
  if (!isAccepted(file)) {
    localError.value = `Unsupported file type. Accepted: ${ACCEPTED.join(', ')}`;
    return;
  }
  try {
    await flow.uploadFile(file);
    emit('changed');
  } catch (e) {
    localError.value = e instanceof Error ? e.message : 'Upload failed';
  }
}

function onDrop(e: DragEvent) {
  e.preventDefault();
  isDragging.value = false;
  const file = e.dataTransfer?.files?.[0];
  if (file) handleFile(file);
}

function onFileSelect(e: Event) {
  const input = e.target as HTMLInputElement;
  const file = input.files?.[0];
  if (file) handleFile(file);
  input.value = '';
}

async function confirm() {
  confirming.value = true;
  localError.value = null;
  try {
    await flow.confirmUpload();
    emit('changed');
  } catch (e) {
    localError.value = e instanceof Error ? e.message : 'Confirmation failed';
  } finally {
    confirming.value = false;
  }
}

async function deleteUpload() {
  deleting.value = true;
  localError.value = null;
  try {
    await flow.deleteUpload();
    showDeleteModal.value = false;
    emit('changed');
  } catch (e) {
    localError.value = e instanceof Error ? e.message : 'Delete failed';
  } finally {
    deleting.value = false;
  }
}
</script>

<template>
  <div class="prep-upload">
    <p v-if="!flowReady" class="pu-hint">Loading your show…</p>

    <!-- Uploading: chunked progress -->
    <div v-else-if="uploading" class="pu-progress">
      <span class="pu-file-ico">🎵</span>
      <div class="pu-progress-body">
        <p class="pu-filename">
          Uploading…
          <span v-if="progress?.totalChunks && progress.totalChunks > 1" class="pu-meta">
            chunk {{ progress.chunkIndex }} / {{ progress.totalChunks }}
          </span>
          <span v-else-if="progress?.phase === 'finalizing'" class="pu-meta">finalizing</span>
        </p>
        <div class="pu-bar">
          <div class="pu-bar-fill" :style="{ width: (progress?.percent ?? 0) + '%' }"></div>
        </div>
      </div>
      <span class="pu-percent">{{ progress?.percent ?? 0 }}%</span>
    </div>

    <!-- No upload yet: dropzone -->
    <template v-else-if="!hasUpload">
      <div
        class="pu-dropzone"
        :class="{ dragging: isDragging }"
        role="button"
        tabindex="0"
        @dragover.prevent="isDragging = true"
        @dragleave="isDragging = false"
        @drop="onDrop"
        @click="fileInput?.click()"
        @keydown.enter="fileInput?.click()"
      >
        <span class="pu-drop-ico">⬆</span>
        <p class="pu-drop-text">Drop your set here or <span class="pu-browse">browse</span></p>
        <p class="pu-drop-formats">MP3, WAV, FLAC, OGG or M4A</p>
      </div>
      <p class="pu-warn">⚠ Upload and confirm before air time — the show starts automatically</p>
    </template>

    <!-- Uploaded: preview + confirm -->
    <template v-else>
      <div class="pu-filerow">
        <span class="pu-file-ico">🎵</span>
        <div class="pu-file-info">
          <p class="pu-filename">{{ flow.prerecordedFilename.value || 'Uploaded set' }}</p>
        </div>
        <span class="pu-chip" :class="{ confirmed: isConfirmed }">
          {{ isConfirmed ? 'Confirmed' : 'Uploaded' }}
        </span>
      </div>

      <AudioPlayer
        v-if="flow.prerecordedUrl.value"
        :key="flow.prerecordedUrl.value"
        :src="flow.prerecordedUrl.value"
      />

      <div class="pu-actions">
        <p v-if="!isConfirmed" class="pu-hint">
          Listen in, then confirm to lock this file for air time
        </p>
        <p v-else class="pu-hint done">✓ Confirmed — streams automatically at air time</p>
        <BaseButton variant="ghost" size="sm" @click="fileInput?.click()">Re-upload</BaseButton>
        <BaseButton variant="danger" size="sm" @click="showDeleteModal = true">Delete</BaseButton>
        <BaseButton
          v-if="!isConfirmed"
          variant="primary"
          size="sm"
          :loading="confirming"
          @click="confirm"
        >
          ✓ Confirm
        </BaseButton>
      </div>
    </template>

    <p v-if="localError" class="pu-error">{{ localError }}</p>

    <input
      ref="fileInput"
      type="file"
      :accept="ACCEPTED.join(',')"
      class="pu-input"
      @change="onFileSelect"
    />

    <BaseModal :open="showDeleteModal" title="Delete upload" @close="showDeleteModal = false">
      <p>Delete the uploaded set?</p>
      <p class="text-muted">The show will have nothing to play at air time until you upload again.</p>
      <template #footer>
        <BaseButton variant="ghost" @click="showDeleteModal = false">Cancel</BaseButton>
        <BaseButton variant="danger" :loading="deleting" @click="deleteUpload">Delete</BaseButton>
      </template>
    </BaseModal>
  </div>
</template>

<style scoped>
.prep-upload {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
  font-family: var(--font-ui);
}

.pu-dropzone {
  border: 1px dashed var(--color-border-light);
  border-radius: var(--radius-lg);
  padding: var(--spacing-xl) var(--spacing-md);
  text-align: center;
  cursor: pointer;
  transition: border-color var(--transition-fast);
}

.pu-dropzone:hover,
.pu-dropzone.dragging {
  border-color: var(--color-primary);
}

.pu-drop-ico {
  font-size: 1.5rem;
  color: var(--color-text-muted);
}

.pu-drop-text {
  margin: var(--spacing-sm) 0 2px;
  font-size: var(--font-size-sm);
  color: var(--color-text);
}

.pu-browse {
  color: var(--color-primary);
}

.pu-drop-formats {
  margin: 0;
  font-size: var(--font-size-xs);
  color: var(--color-text-muted);
}

.pu-warn {
  margin: 0;
  font-size: var(--font-size-sm);
  color: var(--color-warning);
}

.pu-progress,
.pu-filerow {
  display: flex;
  align-items: center;
  gap: var(--spacing-md);
}

.pu-progress-body,
.pu-file-info {
  flex: 1 1 auto;
  min-width: 0;
}

.pu-file-ico {
  flex: 0 0 auto;
}

.pu-filename {
  margin: 0 0 var(--spacing-xs);
  font-size: var(--font-size-sm);
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pu-meta {
  color: var(--color-text-muted);
  font-size: var(--font-size-xs);
}

.pu-bar {
  height: 6px;
  border-radius: var(--radius-full);
  background: var(--color-surface-alt);
  overflow: hidden;
}

.pu-bar-fill {
  height: 100%;
  background: var(--color-primary);
  transition: width var(--transition-fast);
}

.pu-percent {
  flex: 0 0 auto;
  font-size: var(--font-size-sm);
  font-variant-numeric: tabular-nums;
  color: var(--color-text-muted);
}

.pu-chip {
  flex: 0 0 auto;
  font-size: var(--font-size-xs);
  font-weight: 600;
  padding: 2px 10px;
  border-radius: var(--radius-full);
  background: var(--color-surface-alt);
  color: var(--color-text-muted);
}

.pu-chip.confirmed {
  background: var(--color-success-bg);
  color: var(--color-success);
}

.pu-actions {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  flex-wrap: wrap;
}

.pu-hint {
  margin: 0;
  flex: 1 1 auto;
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}

.pu-hint.done {
  color: var(--color-success);
}

.pu-error {
  margin: 0;
  font-size: var(--font-size-sm);
  color: var(--color-error);
}

.pu-input {
  display: none;
}
</style>
