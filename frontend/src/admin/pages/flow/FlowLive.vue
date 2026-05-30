<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, shallowRef, watch } from 'vue';
import { useRouter } from 'vue-router';
import { useHostFlow, useAudioCapture, useStreamTest } from '@admin/composables';
import DbMeter from '@admin/components/DbMeter.vue';
import AudioPlayer from '@admin/components/AudioPlayer.vue';

const router = useRouter();
const flow = useHostFlow();

// ─── Device selection ────────────────────────────────────────────────────────
const audioCapture = useAudioCapture();
const selectedDevice = ref('');

let deviceRefreshInterval: ReturnType<typeof setInterval> | null = null;

function onDeviceChange() {
  audioCapture.listDevices();
}

onMounted(async () => {
  // First refresh prompts for permission so device labels are populated.
  await audioCapture.refreshDevices();
  // Keep the list fresh: react to hot-plug events, plus a slow timer fallback.
  navigator.mediaDevices.addEventListener('devicechange', onDeviceChange);
  deviceRefreshInterval = setInterval(() => audioCapture.listDevices(), 3000);
});

async function handleDeviceSelect() {
  if (!selectedDevice.value) return;
  await audioCapture.captureDevice(selectedDevice.value);
  // A change of input invalidates any previous successful test.
  resetTest();
}

async function handleScreenShare() {
  const ok = await audioCapture.captureScreenAudio();
  if (ok) {
    selectedDevice.value = '';
    resetTest();
  }
}

const capturedLabel = computed(() => {
  if (audioCapture.selectedDeviceId.value === 'screen') return 'Screen Audio';
  return (
    audioCapture.devices.value.find((d) => d.deviceId === audioCapture.selectedDeviceId.value)
      ?.label || 'Audio input'
  );
});

// ─── Test stream (server round-trip) ─────────────────────────────────────────
type TestPhase = 'ready' | 'recording' | 'waiting' | 'playing' | 'done' | 'error';
const testPhase = ref<TestPhase>('ready');
const testError = ref<string | null>(null);
const recordProgress = ref(0);
let recordInterval: ReturnType<typeof setInterval> | null = null;

const sentChunks = shallowRef<ArrayBuffer[]>([]);
const playbackChunks = shallowRef<ArrayBuffer[]>([]);
const playbackUrl = ref<string | null>(null);

const streamTest = useStreamTest({
  recordDuration: 10_000,
  onPlaybackData: (data: ArrayBuffer) => {
    playbackChunks.value = [...playbackChunks.value, data];
  },
  onError: (msg: string) => {
    testPhase.value = 'error';
    testError.value = msg;
    clearRecordProgress();
  },
});

watch(
  () => streamTest.state.value,
  (s) => {
    if (s === 'idle') return;
    if (s === 'recording') {
      testPhase.value = 'recording';
      return;
    }
    if (s === 'waiting') {
      testPhase.value = 'waiting';
      stopTestRecording();
      return;
    }
    if (s === 'playing') {
      testPhase.value = 'playing';
      return;
    }
    if (s === 'done') {
      testPhase.value = 'done';
      clearRecordProgress();
      buildPlaybackBlob();
      return;
    }
    if (s === 'error') {
      testPhase.value = 'error';
      testError.value = streamTest.error.value;
      clearRecordProgress();
    }
  }
);

// ─── Test recorder (dedicated MediaRecorder on the capture stream) ──────────
let testRecorder: MediaRecorder | null = null;

function startTestRecording() {
  const stream = audioCapture.processedStream.value || audioCapture.mediaStream.value;
  if (!stream) {
    testPhase.value = 'error';
    testError.value = 'No audio stream available. Select an audio device first.';
    return;
  }

  const tracks = stream.getAudioTracks();
  if (tracks.length === 0 || tracks.every((t) => t.readyState !== 'live')) {
    testPhase.value = 'error';
    testError.value = 'Audio device is no longer active. Re-select your device.';
    return;
  }

  const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
    ? 'audio/webm;codecs=opus'
    : 'audio/webm';

  testRecorder = new MediaRecorder(stream, { mimeType, audioBitsPerSecond: 192000 });

  testRecorder.ondataavailable = async (event) => {
    if (event.data.size > 0) {
      const buffer = await event.data.arrayBuffer();
      sentChunks.value = [...sentChunks.value, buffer];
      streamTest.sendChunk(buffer);
    }
  };

  testRecorder.onerror = () => {
    testError.value = 'Audio recorder error';
    testPhase.value = 'error';
  };

  testRecorder.start(250);
}

function stopTestRecording() {
  if (testRecorder && testRecorder.state !== 'inactive') {
    testRecorder.stop();
    testRecorder = null;
  }
}

// ─── Test flow ────────────────────────────────────────────────────────────────
async function runTest() {
  testError.value = null;
  testPhase.value = 'ready';
  sentChunks.value = [];
  playbackChunks.value = [];
  revokePlaybackUrl();

  try {
    await streamTest.connect();
    streamTest.startRecording();
    testPhase.value = 'recording';
    startTestRecording();

    recordProgress.value = 0;
    const start = Date.now();
    recordInterval = setInterval(() => {
      const elapsed = Date.now() - start;
      recordProgress.value = Math.min(100, (elapsed / 10_000) * 100);
    }, 100);
  } catch {
    testPhase.value = 'error';
    testError.value = 'Failed to connect to test server';
  }
}

function clearRecordProgress() {
  if (recordInterval) {
    clearInterval(recordInterval);
    recordInterval = null;
  }
}

function buildPlaybackBlob() {
  if (playbackChunks.value.length === 0) {
    testError.value = `No audio data received from server (sent: ${sentChunks.value.length} chunks)`;
    return;
  }
  const blob = new Blob(playbackChunks.value, { type: 'audio/webm;codecs=opus' });
  playbackUrl.value = URL.createObjectURL(blob);
}

function revokePlaybackUrl() {
  if (playbackUrl.value) {
    URL.revokeObjectURL(playbackUrl.value);
    playbackUrl.value = null;
  }
}

/** Reset test state (e.g. after switching inputs). */
function resetTest() {
  streamTest.stop();
  stopTestRecording();
  clearRecordProgress();
  revokePlaybackUrl();
  testPhase.value = 'ready';
  testError.value = null;
  recordProgress.value = 0;
  sentChunks.value = [];
  playbackChunks.value = [];
  flow.setLiveTestPassed(false);
}

function retryTest() {
  resetTest();
}

function markTestPassed() {
  flow.setLiveTestPassed(true);
}

function goToStream() {
  flow.goToStep('on-air');
  router.push('/stream/on-air');
}

function goBackToMode() {
  flow.revertToMode();
  router.push('/stream/mode');
}

const isDev = import.meta.env.DEV;

onUnmounted(() => {
  navigator.mediaDevices.removeEventListener('devicechange', onDeviceChange);
  if (deviceRefreshInterval) {
    clearInterval(deviceRefreshInterval);
    deviceRefreshInterval = null;
  }
  streamTest.cleanup();
  stopTestRecording();
  clearRecordProgress();
  revokePlaybackUrl();
  // NOTE: do NOT call audioCapture.stopCapture() here —
  // the singleton capture persists into the Stream step (FlowOnAir).
});
</script>

<template>
  <div class="flow-live">
    <h1 class="step-title">Set Up Audio &amp; Test</h1>
    <p class="step-subtitle">
      Pick your audio input, check the level on the meter, then run a quick test.
    </p>

    <!-- ─── Device selection ─── -->
    <div class="device-section">
      <h3>Audio Input</h3>

      <div class="device-row">
        <select v-model="selectedDevice" class="device-select" @change="handleDeviceSelect">
          <option value="">-- Select audio input --</option>
          <option v-for="device in audioCapture.devices.value" :key="device.deviceId" :value="device.deviceId">
            {{ device.label }}
          </option>
        </select>
        <button class="btn-icon" title="Refresh devices" @click="audioCapture.listDevices()">🔄</button>
      </div>

      <button class="btn-link screen-share" @click="handleScreenShare">
        🖥️ Share screen audio instead
      </button>

      <!-- Capture status + dB meter -->
      <div v-if="audioCapture.isCapturing.value" class="capture-block">
        <div class="capture-status">
          <span class="status-dot active"></span>
          Capturing — <strong>{{ capturedLabel }}</strong>
        </div>
        <DbMeter :media-stream="audioCapture.mediaStream.value" label="Input Level" />
      </div>

      <p v-if="audioCapture.error.value" class="error-text">{{ audioCapture.error.value }}</p>
    </div>

    <!-- ─── Test ─── -->
    <div class="test-panel">
      <h3>Test Your Stream</h3>
      <p class="panel-hint">
        We record 10 seconds and round-trip it through the server, then show it back as a waveform.
      </p>

      <!-- Ready -->
      <div v-if="testPhase === 'ready'" class="test-state">
        <button class="btn-primary btn-lg" :disabled="!audioCapture.isCapturing.value" @click="runTest">
          🎤 Start Test
        </button>
        <p v-if="!audioCapture.isCapturing.value" class="text-muted">Select an audio input first.</p>
      </div>

      <!-- Recording -->
      <div v-else-if="testPhase === 'recording'" class="test-state">
        <div class="test-recording-indicator">
          <span class="recording-dot"></span>
          Recording... <span class="chunk-counter">({{ sentChunks.length }} chunks)</span>
        </div>
        <div class="progress-bar">
          <div class="progress-fill" :style="{ width: `${recordProgress}%` }"></div>
        </div>
        <p class="text-muted">{{ Math.ceil((100 - recordProgress) / 10) }}s remaining</p>
      </div>

      <!-- Waiting -->
      <div v-else-if="testPhase === 'waiting'" class="test-state">
        <p>Sent {{ sentChunks.length }} chunks. Waiting for server...</p>
      </div>

      <!-- Playing (receiving) -->
      <div v-else-if="testPhase === 'playing'" class="test-state">
        <div class="test-playing-indicator">
          <span class="playing-icon">🔊</span>
          Receiving... <span class="chunk-counter">({{ playbackChunks.length }} chunks)</span>
        </div>
      </div>

      <!-- Done -->
      <div v-else-if="testPhase === 'done'" class="test-state">
        <div v-if="playbackUrl" class="playback-section">
          <AudioPlayer :src="playbackUrl" label="Your test recording" />
        </div>
        <div v-else class="playback-section">
          <p class="error-text">No audio data received from server. Sent {{ sentChunks.length }} chunks.</p>
        </div>

        <p>Did you hear your audio clearly?</p>
        <div class="test-result-actions">
          <button class="btn-success" @click="markTestPassed">✓ Yes, it sounds good!</button>
          <button class="btn-secondary" @click="retryTest">Try Again</button>
        </div>
      </div>

      <!-- Error -->
      <div v-else-if="testPhase === 'error'" class="test-state">
        <p class="error-text">{{ testError || 'An error occurred during the test.' }}</p>
        <button class="btn-secondary" @click="retryTest">Try Again</button>
      </div>
    </div>

    <!-- Continue after pass -->
    <div v-if="flow.liveTestPassed.value" class="test-passed-banner">
      <span>✓ Stream test passed</span>
      <button class="btn-primary" @click="goToStream">Continue to Stream →</button>
    </div>

    <!-- Dev-only: skip test entirely -->
    <button v-if="isDev && !flow.liveTestPassed.value" class="btn-dev-skip" @click="markTestPassed">
      🛠 Skip Test (dev)
    </button>

    <div class="step-actions">
      <button class="btn-secondary" @click="goBackToMode">← Back</button>
    </div>
  </div>
</template>

<style scoped>
.flow-live {
  max-width: 640px;
  margin: 0 auto;
}

.step-title {
  font-size: var(--font-size-2xl);
  font-weight: var(--font-weight-bold);
  color: var(--color-text);
  margin: 0 0 var(--spacing-sm);
}

.step-subtitle {
  color: var(--color-text-muted);
  margin: 0 0 var(--spacing-xl);
}

.step-actions {
  display: flex;
  justify-content: space-between;
  margin-top: var(--spacing-2xl);
  gap: var(--spacing-md);
}

/* ─── Device section ─── */
.device-section {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: var(--spacing-lg);
  margin-bottom: var(--spacing-lg);
}

.device-section h3 {
  margin: 0 0 var(--spacing-md);
  font-size: var(--font-size-md);
}

.device-row {
  display: flex;
  gap: var(--spacing-sm);
  margin-bottom: var(--spacing-sm);
}

.device-select {
  flex: 1;
  padding: var(--spacing-sm) var(--spacing-md);
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  color: var(--color-text);
  font-family: var(--font-family);
  font-size: var(--font-size-sm);
}

.btn-icon {
  background: none;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  padding: var(--spacing-sm);
  cursor: pointer;
  font-size: var(--font-size-md);
}

.screen-share {
  margin-bottom: var(--spacing-md);
}

.capture-block {
  margin-top: var(--spacing-md);
}

.capture-status {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  font-size: var(--font-size-sm);
  color: var(--color-text);
  margin-bottom: var(--spacing-md);
}

.status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--color-text-muted);
}

.status-dot.active {
  background: #22c55e;
  box-shadow: 0 0 6px rgba(34, 197, 94, 0.5);
}

/* ─── Test panel ─── */
.test-panel {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: var(--spacing-xl);
  margin-bottom: var(--spacing-lg);
}

.test-panel h3 {
  margin: 0 0 var(--spacing-xs);
  font-size: var(--font-size-md);
}

.panel-hint {
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
  margin: 0 0 var(--spacing-lg);
}

.test-state {
  text-align: center;
}

.test-state > p {
  color: var(--color-text-muted);
  margin: 0 0 var(--spacing-md);
}

.test-recording-indicator {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--spacing-sm);
  font-size: var(--font-size-lg);
  font-weight: var(--font-weight-bold);
  color: #ef4444;
  margin-bottom: var(--spacing-md);
}

.recording-dot {
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background: #ef4444;
  animation: pulse-red 1s ease-in-out infinite;
}

@keyframes pulse-red {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.4;
  }
}

.progress-bar {
  height: 8px;
  background: var(--color-surface-alt);
  border-radius: var(--radius-full);
  overflow: hidden;
  margin-bottom: var(--spacing-sm);
}

.progress-fill {
  height: 100%;
  background: var(--color-primary);
  transition: width 100ms linear;
  border-radius: var(--radius-full);
}

.test-playing-indicator {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--spacing-sm);
  font-size: var(--font-size-lg);
  font-weight: var(--font-weight-bold);
  color: var(--color-primary);
  margin-bottom: var(--spacing-md);
}

.playing-icon {
  animation: bounce 0.6s ease-in-out infinite alternate;
}

@keyframes bounce {
  from {
    transform: scale(1);
  }
  to {
    transform: scale(1.15);
  }
}

.playback-section {
  margin-bottom: var(--spacing-lg);
}

.test-result-actions {
  display: flex;
  gap: var(--spacing-md);
  justify-content: center;
  flex-wrap: wrap;
}

.test-passed-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: rgba(34, 197, 94, 0.1);
  border: 1px solid rgba(34, 197, 94, 0.3);
  border-radius: var(--radius-lg);
  padding: var(--spacing-md) var(--spacing-lg);
  color: #22c55e;
  font-weight: var(--font-weight-bold);
  gap: var(--spacing-md);
}

/* ─── Buttons ─── */
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

.btn-primary:hover:not(:disabled) {
  opacity: 0.9;
}

.btn-primary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn-lg {
  padding: var(--spacing-md) var(--spacing-2xl);
  font-size: var(--font-size-lg);
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

.btn-secondary:hover:not(:disabled) {
  color: var(--color-text);
  border-color: var(--color-border-light);
}

.btn-success {
  background: #22c55e;
  color: #fff;
  border: none;
  padding: var(--spacing-sm) var(--spacing-xl);
  border-radius: var(--radius-md);
  font-family: var(--font-family);
  font-size: var(--font-size-md);
  font-weight: var(--font-weight-bold);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.btn-success:hover {
  background: #16a34a;
}

.btn-link {
  background: none;
  border: none;
  color: var(--color-primary);
  font-family: var(--font-family);
  font-size: var(--font-size-sm);
  cursor: pointer;
  text-decoration: underline;
  padding: var(--spacing-xs) 0;
}

.btn-link:hover {
  opacity: 0.8;
}

.error-text {
  color: #ef4444;
  font-size: var(--font-size-sm);
}

.text-muted {
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
}

.chunk-counter {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
  font-weight: normal;
}

.btn-dev-skip {
  margin-top: var(--spacing-md);
  background: var(--color-warning, #f59e0b);
  color: #000;
  font-family: var(--font-family);
  font-size: var(--font-size-sm);
  font-weight: var(--font-weight-semibold);
  padding: var(--spacing-sm) var(--spacing-xl);
  border: 2px dashed #000;
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.btn-dev-skip:hover {
  background: var(--color-warning-hover, #d97706);
}
</style>
