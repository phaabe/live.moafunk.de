<script setup lang="ts">
import { ref, computed } from 'vue';
import { useRouter } from 'vue-router';
import { useHostFlow } from '@admin/composables';

const router = useRouter();
const flow = useHostFlow();

const isDragging = ref(false);
const selectedFile = ref<File | null>(null);
const uploadError = ref<string | null>(null);

const hasExistingUpload = computed(() => flow.hasUpload.value);
const existingFilename = computed(() => flow.prerecordedFilename.value);

function handleDragOver(e: DragEvent) {
  e.preventDefault();
  isDragging.value = true;
}

function handleDragLeave() {
  isDragging.value = false;
}

function handleDrop(e: DragEvent) {
  e.preventDefault();
  isDragging.value = false;
  const files = e.dataTransfer?.files;
  if (files?.length) {
    selectFile(files[0]);
  }
}

function handleFileInput(e: Event) {
  const input = e.target as HTMLInputElement;
  if (input.files?.length) {
    selectFile(input.files[0]);
  }
}

function selectFile(file: File) {
  if (!file.type.startsWith('audio/')) {
    uploadError.value = 'Please select an audio file.';
    return;
  }
  uploadError.value = null;
  selectedFile.value = file;
}

function formatSize(bytes: number): string {
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

async function startUpload() {
  if (!selectedFile.value) return;
  uploadError.value = null;

  try {
    await flow.uploadFile(selectedFile.value);
    // On success, composable navigates to confirm step
    router.push('/stream/confirm');
  } catch (err) {
    uploadError.value = err instanceof Error ? err.message : 'Upload failed';
  }
}

async function deleteAndReupload() {
  try {
    await flow.deleteUpload();
    selectedFile.value = null;
  } catch (err) {
    uploadError.value = err instanceof Error ? err.message : 'Failed to delete upload';
  }
}

function goBack() {
  flow.goToStep('mode');
  router.push('/stream/mode');
}
</script>

<template>
  <div class="flow-upload">
    <h1 class="flow-upload-title">Upload Your Set</h1>
    <p class="flow-upload-subtitle">
      Upload your pre-recorded audio file. We accept MP3, WAV, FLAC, OGG, and M4A.
    </p>

    <!-- Existing upload -->
    <div v-if="hasExistingUpload && !selectedFile" class="existing-upload">
      <div class="existing-file">
        <span class="file-icon">🎵</span>
        <div class="file-info">
          <span class="file-name">{{ existingFilename }}</span>
          <span class="file-status">Uploaded</span>
        </div>
      </div>
      <div class="existing-actions">
        <button class="btn-primary" @click="router.push('/stream/confirm')">
          Continue →
        </button>
        <button class="btn-danger-outline" @click="deleteAndReupload">
          Re-upload
        </button>
      </div>
    </div>

    <!-- Upload area -->
    <template v-else>
      <!-- Drop zone -->
      <div v-if="!flow.uploading.value" :class="['drop-zone', { dragging: isDragging }]" @dragover="handleDragOver"
        @dragleave="handleDragLeave" @drop="handleDrop" @click="($refs.fileInput as HTMLInputElement)?.click()">
        <div v-if="!selectedFile" class="drop-zone-content">
          <span class="drop-icon">📁</span>
          <p class="drop-text">Drag & drop your audio file here</p>
          <p class="drop-hint">or click to browse</p>
        </div>
        <div v-else class="drop-zone-content selected">
          <span class="file-icon-large">🎵</span>
          <p class="file-name-large">{{ selectedFile.name }}</p>
          <p class="file-size">{{ formatSize(selectedFile.size) }}</p>
        </div>
        <input ref="fileInput" type="file" accept="audio/*" class="file-input-hidden" @change="handleFileInput" />
      </div>

      <!-- Upload progress -->
      <div v-if="flow.uploading.value && flow.uploadProgress.value" class="upload-progress">
        <div class="progress-info">
          <span>{{ selectedFile?.name }}</span>
          <span>{{ flow.uploadProgress.value.phase === 'finalizing' ? 'Finalizing...' :
            `${flow.uploadProgress.value.percent}%` }}</span>
        </div>
        <div class="progress-bar">
          <div class="progress-fill" :style="{ width: `${flow.uploadProgress.value.percent}%` }" />
        </div>
        <p v-if="flow.uploadProgress.value.totalChunks" class="progress-detail">
          Chunk {{ flow.uploadProgress.value.chunkIndex }} / {{ flow.uploadProgress.value.totalChunks }}
        </p>
      </div>

      <!-- Error -->
      <div v-if="uploadError || flow.error.value" class="upload-error">
        {{ uploadError || flow.error.value }}
      </div>

      <!-- Actions -->
      <div class="flow-upload-actions">
        <button class="btn-secondary" @click="goBack">
          ← Back
        </button>
        <button v-if="selectedFile && !flow.uploading.value" class="btn-primary" @click="startUpload">
          Upload
        </button>
      </div>
    </template>
  </div>
</template>

<style scoped>
.flow-upload-title {
  font-size: var(--font-size-2xl);
  font-weight: var(--font-weight-bold);
  margin: 0 0 var(--spacing-sm);
}

.flow-upload-subtitle {
  color: var(--color-text-muted);
  margin: 0 0 var(--spacing-xl);
}

/* Drop zone */
.drop-zone {
  border: 2px dashed var(--color-border);
  border-radius: var(--radius-xl);
  padding: var(--spacing-2xl);
  text-align: center;
  cursor: pointer;
  transition: all var(--transition-fast);
  margin-bottom: var(--spacing-xl);
}

.drop-zone:hover,
.drop-zone.dragging {
  border-color: var(--color-primary);
  background: var(--color-surface);
}

.drop-zone-content {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--spacing-sm);
}

.drop-icon {
  font-size: 2.5rem;
}

.drop-text {
  font-size: var(--font-size-lg);
  color: var(--color-text);
  margin: 0;
}

.drop-hint {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
  margin: 0;
}

.file-icon-large {
  font-size: 2rem;
}

.file-name-large {
  font-size: var(--font-size-md);
  color: var(--color-text);
  margin: 0;
  word-break: break-all;
}

.file-size {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
  margin: 0;
}

.file-input-hidden {
  display: none;
}

/* Existing upload */
.existing-upload {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: var(--spacing-lg);
  margin-bottom: var(--spacing-xl);
}

.existing-file {
  display: flex;
  align-items: center;
  gap: var(--spacing-md);
  margin-bottom: var(--spacing-lg);
}

.file-icon {
  font-size: 1.5rem;
}

.file-info {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
}

.file-name {
  color: var(--color-text);
  font-weight: var(--font-weight-medium);
}

.file-status {
  font-size: var(--font-size-sm);
  color: var(--color-success);
}

.existing-actions {
  display: flex;
  gap: var(--spacing-md);
}

/* Progress */
.upload-progress {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: var(--spacing-lg);
  margin-bottom: var(--spacing-xl);
}

.progress-info {
  display: flex;
  justify-content: space-between;
  margin-bottom: var(--spacing-sm);
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}

.progress-bar {
  height: 6px;
  background: var(--color-border);
  border-radius: var(--radius-full);
  overflow: hidden;
}

.progress-fill {
  height: 100%;
  background: var(--color-primary);
  transition: width var(--transition-fast);
  border-radius: var(--radius-full);
}

.progress-detail {
  font-size: var(--font-size-xs);
  color: var(--color-text-muted);
  margin: var(--spacing-sm) 0 0;
}

/* Error */
.upload-error {
  background: var(--color-error-bg);
  color: var(--color-error);
  padding: var(--spacing-md);
  border-radius: var(--radius-md);
  font-size: var(--font-size-sm);
  margin-bottom: var(--spacing-xl);
}

/* Actions */
.flow-upload-actions {
  display: flex;
  justify-content: space-between;
}

.btn-primary {
  background: var(--color-primary);
  color: var(--color-primary-text);
  border: none;
  padding: var(--spacing-sm) var(--spacing-xl);
  border-radius: var(--radius-md);
  font-family: var(--font-family);
  font-size: var(--font-size-md);
  font-weight: var(--font-weight-bold);
  cursor: pointer;
  transition: background var(--transition-fast);
}

.btn-primary:hover {
  background: var(--color-primary-hover);
}

.btn-secondary {
  background: none;
  border: 1px solid var(--color-border);
  color: var(--color-text-muted);
  padding: var(--spacing-sm) var(--spacing-xl);
  border-radius: var(--radius-md);
  font-family: var(--font-family);
  font-size: var(--font-size-md);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.btn-secondary:hover {
  color: var(--color-text);
  border-color: var(--color-border-light);
}

.btn-danger-outline {
  background: none;
  border: 1px solid var(--color-error);
  color: var(--color-error);
  padding: var(--spacing-sm) var(--spacing-xl);
  border-radius: var(--radius-md);
  font-family: var(--font-family);
  font-size: var(--font-size-md);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.btn-danger-outline:hover {
  background: var(--color-error-bg);
}
</style>
