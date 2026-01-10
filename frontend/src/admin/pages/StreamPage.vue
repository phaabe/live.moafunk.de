<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue';
import { streamApi, type StreamStatus } from '../api';
import { useAuthStore } from '../stores/auth';
import { BaseButton } from '@shared/components';

const authStore = useAuthStore();

const status = ref<StreamStatus | null>(null);
const loading = ref(true);
const error = ref<string | null>(null);

// Polling interval
let pollInterval: number | null = null;

async function loadStatus() {
  try {
    status.value = await streamApi.status();
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to load stream status';
  } finally {
    loading.value = false;
  }
}

async function stopStream() {
  try {
    await streamApi.stop();
    await loadStatus();
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to stop stream';
  }
}

onMounted(() => {
  loadStatus();
  // Poll every 5 seconds
  pollInterval = window.setInterval(loadStatus, 5000);
});

onUnmounted(() => {
  if (pollInterval) {
    clearInterval(pollInterval);
  }
});
</script>

<template>
  <div class="stream-page">
    <div class="page-header">
      <h1 class="page-title">Stream Control</h1>
    </div>

    <div v-if="error" class="flash-message error">{{ error }}</div>

    <div v-if="loading" class="loading-spinner"></div>

    <template v-else>
      <!-- Stream Status -->
      <div class="card status-card">
        <div class="status-indicator" :class="{ active: status?.active }">
          <span class="status-dot"></span>
          <span class="status-text">{{ status?.active ? 'LIVE' : 'OFFLINE' }}</span>
        </div>
        <p v-if="status?.active && status.user" class="status-user">
          Streaming by: <strong>{{ status.user }}</strong>
        </p>
      </div>

      <!-- Stream Controls -->
      <div class="card controls-card">
        <h2 class="section-title">Controls</h2>

        <div class="controls-info">
          <p class="text-muted">
            Stream control functionality is coming soon. This page will allow you to:
          </p>
          <ul class="feature-list">
            <li>Select audio input device</li>
            <li>Monitor audio levels</li>
            <li>Start and stop streaming</li>
            <li>View connection status</li>
          </ul>
        </div>

        <div v-if="status?.active" class="stop-section">
          <p class="text-muted">
            Current stream is active. You can stop it using the button below.
          </p>
          <BaseButton
            v-if="status.user === authStore.user?.username || authStore.user?.role === 'superadmin'"
            variant="danger"
            @click="stopStream"
          >
            Stop Stream
          </BaseButton>
          <p v-else class="text-muted">
            Only {{ status.user }} or a superadmin can stop this stream.
          </p>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.status-card {
  text-align: center;
  padding: var(--spacing-2xl);
}

.status-indicator {
  display: inline-flex;
  align-items: center;
  gap: var(--spacing-sm);
  padding: var(--spacing-sm) var(--spacing-lg);
  border-radius: var(--radius-full);
  background-color: var(--color-surface-alt);
}

.status-indicator.active {
  background-color: var(--color-success-bg);
}

.status-dot {
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background-color: var(--color-text-muted);
}

.status-indicator.active .status-dot {
  background-color: var(--color-success);
  animation: pulse 1.5s infinite;
}

@keyframes pulse {
  0%, 100% {
    opacity: 1;
  }
  50% {
    opacity: 0.5;
  }
}

.status-text {
  font-size: var(--font-size-xl);
  font-weight: var(--font-weight-bold);
}

.status-indicator.active .status-text {
  color: var(--color-success);
}

.status-user {
  margin-top: var(--spacing-md);
  color: var(--color-text-muted);
}

.controls-card {
  margin-top: var(--spacing-lg);
}

.section-title {
  font-size: var(--font-size-lg);
  margin-bottom: var(--spacing-md);
  padding-bottom: var(--spacing-sm);
  border-bottom: 1px solid var(--color-border);
}

.controls-info {
  margin-bottom: var(--spacing-lg);
}

.feature-list {
  margin-top: var(--spacing-sm);
  padding-left: var(--spacing-lg);
  color: var(--color-text-muted);
}

.feature-list li {
  margin-bottom: var(--spacing-xs);
}

.stop-section {
  padding-top: var(--spacing-lg);
  border-top: 1px solid var(--color-border);
}

.stop-section p {
  margin-bottom: var(--spacing-md);
}
</style>
