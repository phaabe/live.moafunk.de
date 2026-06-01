<script setup lang="ts">
import { ref, computed } from 'vue';
import { useRouter } from 'vue-router';
import { useHostFlow } from '@admin/composables';
import AudioPlayer from '@admin/components/AudioPlayer.vue';

const router = useRouter();
const flow = useHostFlow();

const confirming = ref(false);
const confirmError = ref<string | null>(null);

const show = computed(() => flow.show.value);

const formattedDate = computed(() => {
  if (!show.value?.date) return '';
  try {
    const d = new Date(show.value.date + 'T00:00:00');
    return d.toLocaleDateString('en-US', {
      weekday: 'long',
      year: 'numeric',
      month: 'long',
      day: 'numeric',
    });
  } catch {
    return show.value.date;
  }
});

async function handleConfirm() {
  confirming.value = true;
  confirmError.value = null;

  try {
    await flow.confirmUpload();
  } catch (err) {
    confirmError.value = err instanceof Error ? err.message : 'Confirmation failed';
  } finally {
    confirming.value = false;
  }
}

async function handleReupload() {
  try {
    await flow.deleteUpload();
    router.push('/stream/upload');
  } catch (err) {
    confirmError.value = err instanceof Error ? err.message : 'Failed to delete upload';
  }
}

function goBack() {
  // Revert: going back from confirm deletes the upload and returns to the show dashboard
  flow.revertToMode();
  router.push(flow.showId.value ? `/shows/${flow.showId.value}` : '/stream/select');
}

function goToWaiting() {
  flow.goToStep('on-air');
  router.push('/stream/on-air');
}
</script>

<template>
  <div class="flow-confirm">
    <!-- Not yet confirmed -->
    <template v-if="!flow.isConfirmed.value">
      <h1 class="flow-confirm-title">Review & Confirm</h1>
      <p class="flow-confirm-subtitle">Please review your upload before confirming.</p>

      <div class="confirm-card">
        <div class="confirm-section">
          <span class="confirm-label">Show</span>
          <span class="confirm-value">{{ show?.title }}</span>
        </div>
        <div class="confirm-section">
          <span class="confirm-label">Date</span>
          <span class="confirm-value">{{ formattedDate }}</span>
        </div>
        <div v-if="show?.start_time" class="confirm-section">
          <span class="confirm-label">Time</span>
          <span class="confirm-value">{{ show.start_time }}</span>
        </div>
        <div class="confirm-section">
          <span class="confirm-label">Uploaded file</span>
          <span class="confirm-value file-value">🎵 {{ flow.prerecordedFilename.value }}</span>
        </div>
      </div>

      <!-- Pre-listen audio player -->
      <div v-if="flow.prerecordedUrl.value" class="prelisten-section">
        <h3 class="prelisten-heading">🎧 Preview your upload</h3>
        <AudioPlayer :src="flow.prerecordedUrl.value" />
      </div>
      <div v-else class="prelisten-placeholder">
        <span class="prelisten-icon">🎧</span>
        <span class="prelisten-text">Audio preview not available</span>
      </div>

      <!-- Error -->
      <div v-if="confirmError || flow.error.value" class="confirm-error">
        {{ confirmError || flow.error.value }}
      </div>

      <div class="flow-confirm-actions">
        <button class="btn-secondary" @click="goBack">← Back</button>
        <div class="action-group">
          <button class="btn-danger-outline" @click="handleReupload">Re-upload</button>
          <button class="btn-primary" :disabled="confirming" @click="handleConfirm">
            {{ confirming ? 'Confirming...' : 'Confirm ✓' }}
          </button>
        </div>
      </div>
    </template>

    <!-- Confirmed state -->
    <template v-else>
      <div class="confirmed-state">
        <div class="confirmed-icon">✓</div>
        <h1 class="confirmed-title">You're all set!</h1>
        <p class="confirmed-message">
          Your show <strong>{{ show?.title }}</strong> is confirmed.
        </p>
        <p class="confirmed-schedule">
          It will stream on <strong>{{ formattedDate }}</strong>
          <template v-if="show?.start_time">
            at <strong>{{ show.start_time }}</strong></template
          >.
        </p>
        <p class="confirmed-file">🎵 {{ flow.prerecordedFilename.value }}</p>

        <div class="confirmed-actions">
          <button class="btn-primary" @click="goToWaiting">Continue to Waiting Room →</button>
          <button class="btn-danger-outline" @click="handleReupload">Replace upload</button>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.flow-confirm-title {
  font-size: var(--font-size-2xl);
  font-weight: var(--font-weight-bold);
  margin: 0 0 var(--spacing-sm);
}

.flow-confirm-subtitle {
  color: var(--color-text-muted);
  margin: 0 0 var(--spacing-xl);
}

/* Confirm card */
.confirm-card {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: var(--spacing-lg);
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
  margin-bottom: var(--spacing-xl);
}

.confirm-section {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.confirm-label {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}

.confirm-value {
  font-weight: var(--font-weight-medium);
  color: var(--color-text);
}

.file-value {
  word-break: break-all;
}

/* Pre-listen placeholder */
.prelisten-placeholder {
  background: var(--color-surface-alt);
  border: 1px dashed var(--color-border);
  border-radius: var(--radius-lg);
  padding: var(--spacing-lg);
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--spacing-sm);
  margin-bottom: var(--spacing-xl);
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
}

.prelisten-icon {
  font-size: 1.2rem;
}

.prelisten-section {
  margin-bottom: var(--spacing-xl);
}

.prelisten-heading {
  font-size: var(--font-size-md);
  font-weight: var(--font-weight-bold);
  color: var(--color-text);
  margin: 0 0 var(--spacing-md);
}

/* Error */
.confirm-error {
  background: var(--color-error-bg);
  color: var(--color-error);
  padding: var(--spacing-md);
  border-radius: var(--radius-md);
  font-size: var(--font-size-sm);
  margin-bottom: var(--spacing-xl);
}

/* Actions */
.flow-confirm-actions {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.action-group {
  display: flex;
  gap: var(--spacing-md);
}

/* Confirmed state */
.confirmed-state {
  text-align: center;
  padding: var(--spacing-2xl) 0;
}

.confirmed-icon {
  width: 64px;
  height: 64px;
  border-radius: var(--radius-full);
  background: var(--color-success);
  color: white;
  font-size: 2rem;
  display: flex;
  align-items: center;
  justify-content: center;
  margin: 0 auto var(--spacing-xl);
}

.confirmed-title {
  font-size: var(--font-size-3xl);
  font-weight: var(--font-weight-bold);
  color: var(--color-primary);
  margin: 0 0 var(--spacing-md);
}

.confirmed-message {
  font-size: var(--font-size-lg);
  color: var(--color-text);
  margin: 0 0 var(--spacing-sm);
}

.confirmed-schedule {
  color: var(--color-text-muted);
  margin: 0 0 var(--spacing-lg);
}

.confirmed-file {
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
  margin: 0 0 var(--spacing-2xl);
}

.confirmed-actions {
  display: flex;
  justify-content: center;
  gap: var(--spacing-md);
  flex-wrap: wrap;
}

/* Shared button styles */
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

.btn-primary:hover:not(:disabled) {
  background: var(--color-primary-hover);
}

.btn-primary:disabled {
  opacity: 0.6;
  cursor: not-allowed;
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
