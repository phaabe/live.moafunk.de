<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue';
import { useRouter } from 'vue-router';
import { useHostFlow, useAudioCapture, useAudioMeter, useStreamSocket } from '@admin/composables';
import { streamApi, recordingApi } from '@admin/api';

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
  elapsedText.value =
    h > 0
      ? `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
      : `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

// ─── Composables (live mode) ────────────────────────────────────────────────
// Singleton: same instance that was set up in FlowLive and wired in FlowWaiting
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
  streamSocket.stopStream();
  audioCapture?.stopCapture();
  // Stop recording if active
  if (isRecording.value) {
    recordingApi.stop().catch(() => {});
    isRecording.value = false;
  }
  streamEnded.value = true;
  stopElapsed();
}

// Stop show (upload mode)
async function handleStopUpload() {
  stopping.value = true;
  try {
    await streamApi.stop();
  } catch (err) {
    console.warn('[FlowStreaming] Failed to stop stream:', err);
  }
  streamEnded.value = true;
  stopElapsed();
  if (statusInterval) {
    clearInterval(statusInterval);
    statusInterval = null;
  }
}

// Stop stream and change settings (running shows)
const changingSettings = ref(false);
async function handleStopAndChangeSettings() {
  changingSettings.value = true;
  // Stop live stream resources
  if (isLive.value) {
    streamSocket.stopStream();
    audioCapture?.stopCapture();
  }
  // Stop recording if active
  if (isRecording.value) {
    recordingApi.stop().catch(() => {});
    isRecording.value = false;
  }
  stopElapsed();
  if (statusInterval) {
    clearInterval(statusInterval);
    statusInterval = null;
  }
  // Use the composable to stop stream on backend and revert to mode selection
  await flow.stopStreamAndRevert();
  changingSettings.value = false;
  router.push(flow.showId.value ? `/shows/${flow.showId.value}` : '/stream/select');
}

// ─── Upload mode: status polling ────────────────────────────────────────────
const uploadStreamActive = ref(true);
let statusInterval: ReturnType<typeof setInterval> | null = null;

// ─── Recording state ────────────────────────────────────────────────────────
const isRecording = ref(flow.recordStream.value);
const recordingElapsed = ref('');
let recordingPollInterval: ReturnType<typeof setInterval> | null = null;

async function pollRecordingStatus() {
  try {
    const status = await recordingApi.status();
    isRecording.value = status.is_recording;
    if (status.is_recording && status.elapsed_ms) {
      const sec = Math.floor(status.elapsed_ms / 1000);
      const m = Math.floor(sec / 60);
      const s = sec % 60;
      recordingElapsed.value = `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
    }
  } catch {
    // Ignore polling errors
  }
}

async function stopRecording() {
  try {
    await recordingApi.stop();
    isRecording.value = false;
  } catch (err) {
    console.warn('[FlowStreaming] Failed to stop recording:', err);
  }
}

async function checkUploadStatus() {
  // Poll the stream status endpoint (correct URL: /api/stream/status)
  try {
    const status = await streamApi.status();
    uploadStreamActive.value = status.active === true;
    if (!status.active) {
      streamEnded.value = true;
      stopElapsed();
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

// ─── Navigate back to dashboard ─────────────────────────────────────────────
function goToShowInfo() {
  flow.reset();
  router.push('/dashboard');
}

// ─── Auto-end timer (based on show end_time) ───────────────────────────────
const remainingText = ref<string | null>(null);
const endTimeWarning = ref(false);
let endTimeInterval: ReturnType<typeof setInterval> | null = null;

function getEndTargetDate(): Date | null {
  if (!show.value?.date || !show.value?.end_time) return null;
  const dateStr = show.value.date; // "YYYY-MM-DD"
  const timeStr = show.value.end_time; // "HH:MM"
  try {
    const isoStr = `${dateStr}T${timeStr}:00`;
    const localDate = new Date(isoStr);
    // Get Berlin offset
    const berlinNow = new Date(new Date().toLocaleString('en-US', { timeZone: 'Europe/Berlin' }));
    const utcNow = new Date();
    const offsetMs = berlinNow.getTime() - utcNow.getTime();
    // Target in UTC = local target - offset
    return new Date(localDate.getTime() - offsetMs);
  } catch {
    return null;
  }
}

function updateEndTimeCountdown() {
  const target = getEndTargetDate();
  if (!target) {
    remainingText.value = null;
    return;
  }

  const diff = Math.floor((target.getTime() - Date.now()) / 1000);

  if (diff <= 0) {
    remainingText.value = '00:00';
    endTimeWarning.value = false;
    // Auto-stop the stream
    if (!streamEnded.value && !stopping.value) {
      if (isLive.value) {
        handleStop();
      } else {
        handleStopUpload();
      }
    }
    stopEndTimeInterval();
    return;
  }

  // Show warning when < 5 minutes remain
  endTimeWarning.value = diff <= 300;

  const h = Math.floor(diff / 3600);
  const m = Math.floor((diff % 3600) / 60);
  const s = diff % 60;
  remainingText.value =
    h > 0
      ? `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
      : `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

function stopEndTimeInterval() {
  if (endTimeInterval) {
    clearInterval(endTimeInterval);
    endTimeInterval = null;
  }
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

  // Poll recording status if recording was enabled
  if (flow.recordStream.value) {
    pollRecordingStatus();
    recordingPollInterval = setInterval(pollRecordingStatus, 3000);
  }

  // Start end-time countdown if end_time is set
  if (show.value?.end_time) {
    updateEndTimeCountdown();
    endTimeInterval = setInterval(updateEndTimeCountdown, 1000);
  }
});

onUnmounted(() => {
  stopElapsed();
  stopEndTimeInterval();
  if (statusInterval) {
    clearInterval(statusInterval);
    statusInterval = null;
  }
  if (recordingPollInterval) {
    clearInterval(recordingPollInterval);
    recordingPollInterval = null;
  }
  // Stop recording if still active
  if (isRecording.value) {
    recordingApi.stop().catch(() => {});
  }
  // Stop stream when leaving the streaming page (final step in flow)
  if (isLive.value && !streamEnded.value) {
    streamSocket.stopStream();
    audioCapture?.stopCapture();
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
        <button class="btn-primary" @click="goToShowInfo">Return to Show Info</button>
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

      <!-- End time countdown banner -->
      <div v-if="remainingText !== null" :class="['end-time-banner', { warning: endTimeWarning }]">
        <span class="end-time-label">{{ endTimeWarning ? '⚠ Ending in' : 'Time remaining' }}</span>
        <span class="end-time-value">{{ remainingText }}</span>
      </div>

      <!-- Recording indicator -->
      <div v-if="isRecording" class="recording-banner">
        <span class="rec-dot"></span>
        <span class="rec-label">REC</span>
        <span v-if="recordingElapsed" class="rec-elapsed">{{ recordingElapsed }}</span>
        <button class="btn-stop-rec" @click="stopRecording">Stop Recording</button>
      </div>

      <!-- Show info -->
      <div class="show-card-compact">
        <span class="show-title">{{ show?.title }}</span>
        <span class="show-meta">
          {{ formattedDate }}
          <template v-if="show?.start_time"> · {{ show.start_time }}</template>
          <template v-if="show?.end_time"> – {{ show.end_time }}</template>
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
            <input
              type="range"
              min="0"
              max="2"
              step="0.01"
              :value="volume"
              class="volume-slider"
              @input="updateVolume"
            />
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
            {{
              uploadStreamActive
                ? 'Your pre-recorded set is playing'
                : 'Waiting for stream to start...'
            }}
          </p>
          <p class="upload-status-hint">
            The backend is handling playback automatically. You can close this page safely.
          </p>
        </div>

        <!-- Stop show button -->
        <div class="stop-section">
          <button class="btn-stop" :disabled="stopping" @click="handleStopUpload">
            {{ stopping ? 'Stopping...' : '⏹ Stop Show' }}
          </button>
        </div>
      </template>

      <!-- Stop stream & change settings (for running shows) -->
      <div class="change-settings-section">
        <button
          class="btn-change-settings"
          :disabled="stopping || changingSettings"
          @click="handleStopAndChangeSettings"
        >
          {{ changingSettings ? 'Stopping...' : '⚠ Stop Stream & Change Settings' }}
        </button>
        <p class="change-settings-hint">
          This will stop the current stream and let you reconfigure your show.
        </p>
      </div>

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

/* ─── Recording banner ───────────────────────────────────────────────────── */
.recording-banner {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  background: rgba(239, 68, 68, 0.08);
  border: 1px solid rgba(239, 68, 68, 0.3);
  border-radius: var(--radius-md);
  padding: var(--spacing-sm) var(--spacing-md);
  margin-bottom: var(--spacing-xl);
}

.rec-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: #ef4444;
  animation: pulse-rec 1s ease-in-out infinite;
}

@keyframes pulse-rec {
  0%,
  100% {
    opacity: 1;
  }

  50% {
    opacity: 0.3;
  }
}

.rec-label {
  font-size: var(--font-size-xs);
  font-weight: var(--font-weight-bold);
  color: #ef4444;
  text-transform: uppercase;
  letter-spacing: 0.1em;
}

.rec-elapsed {
  font-size: var(--font-size-xs);
  font-variant-numeric: tabular-nums;
  color: var(--color-text-muted);
}

.btn-stop-rec {
  margin-left: auto;
  background: none;
  border: 1px solid rgba(239, 68, 68, 0.4);
  color: #ef4444;
  padding: 2px var(--spacing-sm);
  border-radius: var(--radius-sm);
  font-family: var(--font-family);
  font-size: var(--font-size-xs);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.btn-stop-rec:hover {
  background: rgba(239, 68, 68, 0.1);
}

/* ─── End time countdown banner ──────────────────────────────────────────── */
.end-time-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  padding: var(--spacing-sm) var(--spacing-md);
  margin-bottom: var(--spacing-xl);
}

.end-time-banner.warning {
  background: rgba(245, 158, 11, 0.08);
  border-color: rgba(245, 158, 11, 0.4);
  animation: pulse-warning 2s ease-in-out infinite;
}

@keyframes pulse-warning {
  0%,
  100% {
    border-color: rgba(245, 158, 11, 0.4);
  }

  50% {
    border-color: rgba(245, 158, 11, 0.8);
  }
}

.end-time-label {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}

.end-time-banner.warning .end-time-label {
  color: #f59e0b;
  font-weight: var(--font-weight-bold);
}

.end-time-value {
  font-size: var(--font-size-lg);
  font-weight: var(--font-weight-bold);
  font-variant-numeric: tabular-nums;
  color: var(--color-text);
}

.end-time-banner.warning .end-time-value {
  color: #f59e0b;
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

/* ─── Change settings ────────────────────────────────────────────────────── */
.change-settings-section {
  margin-top: var(--spacing-2xl);
  padding-top: var(--spacing-xl);
  border-top: 1px solid var(--color-border);
  text-align: center;
}

.btn-change-settings {
  background: transparent;
  color: #ef4444;
  border: 2px solid #ef4444;
  padding: var(--spacing-sm) var(--spacing-xl);
  border-radius: var(--radius-md);
  font-family: var(--font-family);
  font-size: var(--font-size-sm);
  font-weight: var(--font-weight-bold);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.btn-change-settings:hover:not(:disabled) {
  background: #ef4444;
  color: #fff;
}

.btn-change-settings:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.change-settings-hint {
  font-size: var(--font-size-xs);
  color: var(--color-text-muted);
  margin: var(--spacing-sm) 0 0;
}

/* ─── Future panels (cont) ───────────────────────────────────────────────── */
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
