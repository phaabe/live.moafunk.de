<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue';
import { useRouter } from 'vue-router';
import { useHostFlow, useAudioCapture, useStreamSocket } from '@admin/composables';
import DbMeter from '@admin/components/DbMeter.vue';
import StreamPreviewPlayer from '@admin/components/StreamPreviewPlayer.vue';
import { config } from '@/config';

const router = useRouter();
const flow = useHostFlow();

// The private Icecast `/test` mount the rehearsal plays back from. Same MP3 the
// public `/live.mp3` would serve, but non-public. Empty (e.g. local dev) → the
// real test can't run; the dev-skip below covers that case.
const previewUrl = config.stream.icecastTestUrl;

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
  // The capture singleton persists across navigations (so audio survives into
  // the Stream step). If we re-enter setup while a device is still being
  // captured, mirror it in the dropdown — otherwise the UI shows "no input
  // selected" while actually recording that stale device on Start Test.
  const activeId = audioCapture.selectedDeviceId.value;
  if (audioCapture.isCapturing.value && activeId && activeId !== 'screen') {
    selectedDevice.value = activeId;
  }
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

// ─── Test broadcast (real producer → /test harbour → Icecast /test.mp3) ───────
// This is the *same* pipeline as going live (browser → WS → backend ffmpeg →
// Liquidsoap harbor → Icecast), just pushed to the private `/test` mount. The
// host plays it back below to confirm it sounds exactly like `/live.mp3` would —
// without anything reaching the public. No recording, no Telegram (backend skips
// both for `?test=true`).
type TestPhase = 'ready' | 'connecting' | 'live' | 'error';
const testPhase = ref<TestPhase>('ready');
const testError = ref<string | null>(null);
const sentChunks = ref(0);
// Guards onUnmounted so passing the test (which already stops the rehearsal)
// doesn't race with cleanup.
const testActive = computed(() => testPhase.value === 'connecting' || testPhase.value === 'live');

const streamSocket = useStreamSocket({
  onLive: () => {
    testPhase.value = 'live';
  },
  onError: (msg) => {
    testPhase.value = 'error';
    testError.value = msg;
    stopTestRecording();
  },
  onDisconnected: () => {
    // The rehearsal ended (server-side or dropped) before the host accepted it.
    if (testPhase.value === 'live' || testPhase.value === 'connecting') {
      testPhase.value = 'ready';
      stopTestRecording();
    }
  },
});

// ─── Test recorder (dedicated MediaRecorder on the capture stream) ──────────
// Independent of the singleton capture's main onData wiring, so it never
// disturbs the real go-live pipeline in the next step.
let testRecorder: MediaRecorder | null = null;

function startTestRecording(): boolean {
  const stream = audioCapture.processedStream.value || audioCapture.mediaStream.value;
  if (!stream) {
    testPhase.value = 'error';
    testError.value = 'No audio stream available. Select an audio device first.';
    return false;
  }

  const tracks = stream.getAudioTracks();
  if (tracks.length === 0 || tracks.every((t) => t.readyState !== 'live')) {
    testPhase.value = 'error';
    testError.value = 'Audio device is no longer active. Re-select your device.';
    return false;
  }

  const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
    ? 'audio/webm;codecs=opus'
    : 'audio/webm';

  testRecorder = new MediaRecorder(stream, { mimeType, audioBitsPerSecond: 192000 });

  testRecorder.ondataavailable = async (event) => {
    if (event.data.size > 0) {
      const buffer = await event.data.arrayBuffer();
      streamSocket.send(buffer);
      sentChunks.value++;
    }
  };

  testRecorder.onerror = () => {
    testError.value = 'Audio recorder error';
    testPhase.value = 'error';
  };

  testRecorder.start(250);
  return true;
}

function stopTestRecording() {
  if (testRecorder && testRecorder.state !== 'inactive') {
    testRecorder.stop();
  }
  testRecorder = null;
}

// ─── Test flow ────────────────────────────────────────────────────────────────
async function runTest() {
  if (!previewUrl) {
    testPhase.value = 'error';
    testError.value = 'Test stream is not configured on this server.';
    return;
  }

  testError.value = null;
  testPhase.value = 'connecting';
  sentChunks.value = 0;

  try {
    // Connect in TEST mode → backend pushes to `/test`, not `/live`.
    await streamSocket.connect(false, undefined, true);
    // Stream live audio through the real pipeline. `onLive` flips to 'live'.
    if (!startTestRecording()) {
      streamSocket.stopStream();
    }
  } catch {
    testPhase.value = 'error';
    testError.value = 'Failed to connect to the test stream.';
    stopTestRecording();
  }
}

/** Stop the rehearsal broadcast (keeps the audio capture for the next step). */
function stopTestBroadcast() {
  stopTestRecording();
  streamSocket.stopStream();
}

/** Reset test state (e.g. after switching inputs). */
function resetTest() {
  stopTestBroadcast();
  testPhase.value = 'ready';
  testError.value = null;
  sentChunks.value = 0;
  flow.setLiveTestPassed(false);
}

function retryTest() {
  resetTest();
}

function markTestPassed() {
  stopTestBroadcast();
  flow.setLiveTestPassed(true);
  goToStream();
}

function goToStream() {
  flow.goToStep('on-air');
  router.push('/stream/on-air');
}

function goBackToMode() {
  flow.revertToMode();
  router.push(flow.showId.value ? `/shows/${flow.showId.value}` : '/stream/select');
}

const isDev = import.meta.env.DEV;

onUnmounted(() => {
  navigator.mediaDevices.removeEventListener('devicechange', onDeviceChange);
  if (deviceRefreshInterval) {
    clearInterval(deviceRefreshInterval);
    deviceRefreshInterval = null;
  }
  // Stop a still-running rehearsal so we never leave a producer pushing to
  // `/test` after navigating away. Passing the test already stopped it (this is
  // then a no-op); a fresh go-live in FlowOnAir reconnects to `/live`.
  if (testActive.value) {
    stopTestBroadcast();
  }
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
          <option
            v-for="device in audioCapture.devices.value"
            :key="device.deviceId"
            :value="device.deviceId"
          >
            {{ device.label }}
          </option>
        </select>
        <button class="btn-icon" title="Refresh devices" @click="audioCapture.listDevices()">
          🔄
        </button>
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
        This sends your audio through the real broadcast path to a <strong>private</strong> test
        mount — exactly what listeners would hear on the live stream, but not public. Press play
        below to check it before going live.
      </p>

      <!-- Ready -->
      <div v-if="testPhase === 'ready'" class="test-state">
        <button
          class="btn-primary btn-lg"
          :disabled="!audioCapture.isCapturing.value || !previewUrl"
          @click="runTest"
        >
          🎤 Start Test
        </button>
        <p v-if="!audioCapture.isCapturing.value" class="text-muted">
          Select an audio input first.
        </p>
        <p v-else-if="!previewUrl" class="text-muted">
          Test stream isn’t configured on this server.
        </p>
      </div>

      <!-- Connecting -->
      <div v-else-if="testPhase === 'connecting'" class="test-state">
        <p class="text-muted">Connecting to the test stream…</p>
      </div>

      <!-- Live (test broadcast running) -->
      <div v-else-if="testPhase === 'live'" class="test-state">
        <div class="test-recording-indicator">
          <span class="recording-dot"></span>
          Test broadcast live
          <span class="chunk-counter">({{ sentChunks }} chunks sent)</span>
        </div>

        <!-- Play back the private /test mount. Same component as the on-air
             preview; runs a few seconds behind, so use headphones. -->
        <StreamPreviewPlayer v-if="previewUrl" :src="previewUrl" />

        <p>Does it sound clear on the test stream?</p>
        <div class="test-result-actions">
          <button class="btn-success" @click="markTestPassed">✓ Yes, go live</button>
          <button class="btn-secondary" @click="retryTest">⏹ Stop test</button>
        </div>
      </div>

      <!-- Error -->
      <div v-else-if="testPhase === 'error'" class="test-state">
        <p class="error-text">{{ testError || 'An error occurred during the test.' }}</p>
        <button class="btn-secondary" @click="retryTest">Try Again</button>
      </div>
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
