<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue';
import { useRouter } from 'vue-router';
import {
  useHostFlow,
  useAudioCapture,
  useAudioMeter,
  useStreamSocket,
} from '@admin/composables';

const router = useRouter();
const flow = useHostFlow();
const show = computed(() => flow.show.value);
const isLive = computed(() => flow.uploadMode.value === 'live');

// ─── Stream state ───────────────────────────────────────────────────────────
const streamEnded = ref(false);
const startedAt = ref(Date.now());
const elapsedText = ref('00:00');
let elapsedInterval: ReturnType<typeof setInterval> | null = null;

function updateElapsed() {
  const diff = Math.floor((Date.now() - startedAt.value) / 1000);
  const h = Math.floor(diff / 3600);
  const m = Math.floor((diff % 3600) / 60);
  const s = diff % 60;
  elapsedText.value = h > 0
    ? `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
    : `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

// ─── Composables (live mode) ────────────────────────────────────────────────
const audioCapture = isLive.value ? useAudioCapture() : null;
const audioMeter = audioCapture ? useAudioMeter(audioCapture.mediaStream) : null;
const streamSocket = useStreamSocket({
  onDisconnected: () => {
    if (!streamEnded.value) {
      streamEnded.value = true;
      stopElapsed();
    }
  },
});

// Volume control (live mode)
const volume = ref(1);
function updateVolume(event: Event) {
  const val = parseFloat((event.target as HTMLInputElement).value);
  volume.value = val;
  audioCapture?.setInputVolume(val);
}

// Stop streaming (live mode)
const stopping = ref(false);
function handleStop() {
  stopping.value = true;
  streamSocket.disconnect();
  audioCapture?.stopCapture();
  streamEnded.value = true;
  stopElapsed();
}

// ─── Upload mode: status polling ────────────────────────────────────────────
const uploadStreamActive = ref(true);
let statusInterval: ReturnType<typeof setInterval> | null = null;

async function checkUploadStatus() {
  // Simple check: poll the stream status endpoint
  try {
    const resp = await fetch('/api/stream-status');
    if (resp.ok) {
      const data = await resp.json();
      uploadStreamActive.value = data.is_live === true;
      if (!data.is_live) {
        streamEnded.value = true;
        stopElapsed();
      }
    }
  } catch {
    // Ignore polling errors
  }
}

// ─── Show info formatting ───────────────────────────────────────────────────
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

// ─── Navigate back to show ──────────────────────────────────────────────────
function goToShowInfo() {
  flow.reset();
  router.push('/stream/show');
}

// ─── Lifecycle ──────────────────────────────────────────────────────────────
function stopElapsed() {
  if (elapsedInterval) {
    clearInterval(elapsedInterval);
    elapsedInterval = null;
  }
}

onMounted(() => {
  startedAt.value = Date.now();
  elapsedInterval = setInterval(updateElapsed, 1000);

  if (!isLive.value) {
    statusInterval = setInterval(checkUploadStatus, 5000);
  }
});

onUnmounted(() => {
  stopElapsed();
  if (statusInterval) {
    clearInterval(statusInterval);
    statusInterval = null;
  }
});
</script>

<template>
  <div class="flow-streaming">
    <!-- Stream ended overlay -->
    <template v-if="streamEnded">
      <div class="stream-ended">
        <div class="ended-icon">✓</div>
        <h1 class="ended-title">Stream Ended</h1>
        <p class="ended-message">
          Your show <strong>{{ show?.title }}</strong> has finished.
        </p>
        <p class="ended-duration">Duration: {{ elapsedText }}</p>
        <button class="btn-primary" @click="goToShowInfo">
          Return to Show Info
        </button>
      </div>
    </template>

    <!-- Active streaming UI -->
    <template v-else>
      <!-- Header with status -->
      <div class="streaming-header">
        <div class="stream-status">
          <span class="status-dot live"></span>
          <span class="status-label">LIVE</span>
        </div>
        <div class="elapsed-timer">{{ elapsedText }}</div>
      </div>

      <!-- Show info -->
      <div class="show-card-compact">
        <span class="show-title">{{ show?.title }}</span>
        <span class="show-meta">
          {{ formattedDate }}
          <template v-if="show?.start_time"> · {{ show.start_time }}</template>
        </span>
      </div>

      <!-- Live mode controls -->
      <template v-if="isLive">
        <!-- Audio level meter -->
        <div v-if="audioMeter" class="audio-level-section">
          <label class="section-label">Audio Level</label>
          <div class="audio-level">
            <div class="audio-level-bar" :style="{ width: `${audioMeter.level.value}%` }"></div>
          </div>
        </div>

        <!-- Volume slider -->
        <div v-if="audioCapture" class="volume-section">
          <label class="section-label">Input Volume</label>
          <div class="volume-row">
            <span class="volume-icon">🔇</span>
            <input type="range" min="0" max="2" step="0.01" :value="volume" class="volume-slider"
              @input="updateVolume" />
            <span class="volume-icon">🔊</span>
            <span class="volume-value">{{ Math.round(volume * 100) }}%</span>
          </div>
        </div>

        <!-- Stop button -->
        <div class="stop-section">
          <button class="btn-stop" :disabled="stopping" @click="handleStop">
            {{ stopping ? 'Stopping...' : '⏹ Stop Streaming' }}
          </button>
        </div>
      </template>

      <!-- Upload mode: passive monitoring -->
      <template v-else>
        <div class="upload-streaming-status">
          <div class="upload-status-dot">
            <span :class="['status-dot', uploadStreamActive ? 'live' : 'offline']"></span>
          </div>
          <p class="upload-status-text">
            {{ uploadStreamActive ? 'Your pre-recorded set is playing' : 'Waiting for stream to start...' }}
          </p>
          <p class="upload-status-hint">
            The backend is handling playback automatically. You can close this page safely.
          </p>
        </div>
      </template>

      <!-- Future feature placeholders -->
      <div class="future-panels">
        <div class="future-panel">
          <span class="future-icon">👥</span>
          <span class="future-label">Listener Count</span>
          <span class="future-badge">Coming soon</span>
        </div>
        <div class="future-panel">
          <span class="future-icon">📊</span>
          <span class="future-label">Audio Quality</span>
          <span class="future-badge">Coming soon</span>
        </div>
        <div class="future-panel">
          <span class="future-icon">💬</span>
          <span class="future-label">Live Chat</span>
          <span class="future-badge">Coming soon</span>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.flow-streaming {
  max-width: 600px;
  margin: 0 auto;
}

/* ─── Stream ended ───────────────────────────────────────────────────────── */
.stream-ended {
  text-align: center;
  padding: var(--spacing-3xl) 0;
}

.ended-icon {
  width: 64px;
  height: 64px;
  border-radius: var(--radius-full);
  background: var(--color-success, #22c55e);
  color: white;
  font-size: 2rem;
  display: flex;
  align-items: center;
  justify-content: center;
  margin: 0 auto var(--spacing-xl);
}

.ended-title {
  font-size: var(--font-size-3xl);
  font-weight: var(--font-weight-bold);
  margin: 0 0 var(--spacing-md);
}

.ended-message {
  color: var(--color-text-muted);
  margin: 0 0 var(--spacing-sm);
}

.ended-duration {
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
  margin: 0 0 var(--spacing-2xl);
}

/* ─── Header ─────────────────────────────────────────────────────────────── */
.streaming-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--spacing-xl);
}

.stream-status {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
}

.status-dot {
  width: 12px;
  height: 12px;
  border-radius: 50%;
}

.status-dot.live {
  background: #ef4444;
  box-shadow: 0 0 8px rgba(239, 68, 68, 0.6);
  animation: pulse-live 1.5s ease-in-out infinite;
}

.status-dot.offline {
  background: var(--color-text-muted);
}

@keyframes pulse-live {

  0%,
  100% {
    opacity: 1;
  }

  50% {
    opacity: 0.5;
  }
}

.status-label {
  font-size: var(--font-size-sm);
  font-weight: var(--font-weight-bold);
  color: #ef4444;
  text-transform: uppercase;
  letter-spacing: 0.08em;
}

.elapsed-timer {
  font-size: var(--font-size-xl);
  font-weight: var(--font-weight-bold);
  font-variant-numeric: tabular-nums;
  color: var(--color-text);
}

/* ─── Show card ──────────────────────────────────────────────────────────── */
.show-card-compact {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: var(--spacing-md) var(--spacing-lg);
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: var(--spacing-xl);
}

.show-title {
  font-weight: var(--font-weight-bold);
  color: var(--color-text);
}

.show-meta {
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
}

/* ─── Audio level ────────────────────────────────────────────────────────── */
.audio-level-section {
  margin-bottom: var(--spacing-lg);
}

.section-label {
  display: block;
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
  margin-bottom: var(--spacing-sm);
}

.audio-level {
  height: 10px;
  background: var(--color-surface-alt);
  border-radius: var(--radius-full);
  overflow: hidden;
}

.audio-level-bar {
  height: 100%;
  background: linear-gradient(90deg, #22c55e, #eab308, #ef4444);
  transition: width 50ms;
  border-radius: var(--radius-full);
}

/* ─── Volume ─────────────────────────────────────────────────────────────── */
.volume-section {
  margin-bottom: var(--spacing-xl);
}

.volume-row {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
}

.volume-icon {
  font-size: var(--font-size-sm);
}

.volume-slider {
  flex: 1;
  accent-color: var(--color-primary);
  cursor: pointer;
}

.volume-value {
  font-size: var(--font-size-sm);
  font-variant-numeric: tabular-nums;
  color: var(--color-text-muted);
  min-width: 3em;
  text-align: right;
}

/* ─── Stop button ────────────────────────────────────────────────────────── */
.stop-section {
  margin-bottom: var(--spacing-xl);
  text-align: center;
}

.btn-stop {
  background: none;
  border: 2px solid #ef4444;
  color: #ef4444;
  padding: var(--spacing-sm) var(--spacing-2xl);
  border-radius: var(--radius-md);
  font-family: var(--font-family);
  font-size: var(--font-size-md);
  font-weight: var(--font-weight-bold);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.btn-stop:hover:not(:disabled) {
  background: rgba(239, 68, 68, 0.1);
}

.btn-stop:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* ─── Upload streaming status ────────────────────────────────────────────── */
.upload-streaming-status {
  text-align: center;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: var(--spacing-xl);
  margin-bottom: var(--spacing-xl);
}

.upload-status-dot {
  margin-bottom: var(--spacing-md);
}

.upload-status-text {
  font-weight: var(--font-weight-bold);
  color: var(--color-text);
  margin: 0 0 var(--spacing-sm);
}

.upload-status-hint {
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
  margin: 0;
}

/* ─── Future panels ──────────────────────────────────────────────────────── */
.future-panels {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: var(--spacing-md);
}

@media (max-width: 480px) {
  .future-panels {
    grid-template-columns: 1fr;
  }
}

.future-panel {
  background: var(--color-surface-alt);
  border: 1px dashed var(--color-border);
  border-radius: var(--radius-lg);
  padding: var(--spacing-lg) var(--spacing-md);
  text-align: center;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--spacing-xs);
  opacity: 0.6;
}

.future-icon {
  font-size: 1.5rem;
}

.future-label {
  font-size: var(--font-size-sm);
  font-weight: var(--font-weight-medium);
  color: var(--color-text);
}

.future-badge {
  font-size: var(--font-size-xs);
  color: var(--color-text-muted);
  background: var(--color-surface);
  padding: 2px 8px;
  border-radius: var(--radius-full);
  border: 1px solid var(--color-border);
}

/* ─── Shared button ──────────────────────────────────────────────────────── */
.btn-primary {
  background: var(--color-primary);
  color: var(--color-primary-text, #fff);
  border: none;
  padding: var(--spacing-sm) var(--spacing-xl);
  border-radius: var(--radius-md);
  font-family: var(--font-family);
  font-size: var(--font-size-md);
  font-weight: var(--font-weight-bold);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.btn-primary:hover {
  opacity: 0.9;
}
</style>
