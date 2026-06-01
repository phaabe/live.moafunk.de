<script setup lang="ts">
import { computed } from 'vue';
import type { ShowDetail } from '../../api';
import AudioPlayer from '../AudioPlayer.vue';

const props = defineProps<{
  show: ShowDetail;
}>();

/** Media mode is implicit: a prerecorded file present => "upload" mode, otherwise "live". */
const isUpload = computed(() => !!props.show.prerecorded_key);

function formatDateTime(dateStr: string): string {
  return new Date(dateStr).toLocaleString();
}
</script>

<template>
  <div class="card media-card">
    <h2 class="section-title">Media</h2>

    <div class="media-status">
      <span :class="['media-badge', isUpload ? 'upload' : 'live']">
        {{ isUpload ? '⬆ Upload' : '🔴 Live' }}
      </span>
      <span class="media-status-text">
        <template v-if="isUpload">A pre-recorded file will be streamed for this show.</template>
        <template v-else>This show streams live; no pre-recorded file.</template>
      </span>
    </div>

    <template v-if="isUpload">
      <div class="media-file">
        <span class="media-file-label">File</span>
        <code class="media-file-name">{{ show.prerecorded_filename || 'unknown' }}</code>
      </div>

      <div v-if="show.prerecorded_confirmed_at" class="media-confirmed">
        Confirmed by host on {{ formatDateTime(show.prerecorded_confirmed_at) }}
      </div>
      <div v-else class="media-confirmed pending">Not yet confirmed by the host.</div>

      <AudioPlayer v-if="show.prerecorded_url" :src="show.prerecorded_url" />
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
  font-size: var(--font-size-lg);
  font-weight: 600;
  margin: 0;
}

.media-status {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  flex-wrap: wrap;
}

.media-badge {
  font-size: var(--font-size-sm);
  font-weight: 600;
  padding: var(--spacing-xs) var(--spacing-sm);
  border-radius: var(--radius-md);
}

.media-badge.upload {
  background-color: var(--color-primary);
  color: #fff;
}

.media-badge.live {
  background-color: var(--color-error);
  color: #fff;
}

.media-status-text {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}

.media-file {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
}

.media-file-label {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}

.media-file-name {
  font-family: var(--font-family-mono, monospace);
  font-size: var(--font-size-sm);
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  padding: 2px var(--spacing-xs);
  word-break: break-all;
}

.media-confirmed {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}

.media-confirmed.pending {
  color: var(--color-warning, var(--color-text-muted));
}
</style>
