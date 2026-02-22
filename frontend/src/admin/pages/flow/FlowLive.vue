<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, shallowRef } from 'vue';
import { useRouter } from 'vue-router';
import {
  useHostFlow,
  useAudioCapture,
  useAudioMeter,
  useStreamTest,
  type LiveSubStep,
  type SelectedOs,
} from '@admin/composables';

const router = useRouter();
const flow = useHostFlow();

// ─── Sub-step state ──────────────────────────────────────────────────────────
const liveStep = ref<LiveSubStep>(flow.liveSubStep.value);

// ─── B.1 — OS Selection ─────────────────────────────────────────────────────
const selectedOs = ref<SelectedOs | null>(flow.selectedOs.value);

function selectOs(os: SelectedOs) {
  selectedOs.value = os;
  flow.setSelectedOs(os);
  // Brief delay so the selection animation is visible before navigating
  setTimeout(() => {
    liveStep.value = 'tutorial';
    flow.setLiveSubStep('tutorial');
  }, 350);
}

function goBackToMode() {
  router.push('/stream/mode');
}

// ─── B.2 — Tutorial + Device Selection ──────────────────────────────────────
const audioCapture = useAudioCapture();
const audioMeter = useAudioMeter(audioCapture.mediaStream);
const deviceSelected = ref(false);
const selectedDevice = ref('');

onMounted(async () => {
  await audioCapture.refreshDevices();
});

async function handleDeviceSelect() {
  if (!selectedDevice.value) return;
  const ok = await audioCapture.captureDevice(selectedDevice.value);
  if (ok) {
    deviceSelected.value = true;
  }
}

async function handleScreenShare() {
  const ok = await audioCapture.captureScreenAudio();
  if (ok) {
    deviceSelected.value = true;
  }
}

function goBackToOs() {
  liveStep.value = 'os-select';
  flow.setLiveSubStep('os-select');
}

function goToTest() {
  if (!deviceSelected.value && !audioCapture.isCapturing.value) return;
  liveStep.value = 'test';
  flow.setLiveSubStep('test');
}

// ─── B.3 — Test Stream ─────────────────────────────────────────────────────
type TestPhase = 'ready' | 'recording' | 'waiting' | 'playing' | 'done' | 'error';
const testPhase = ref<TestPhase>('ready');
const testError = ref<string | null>(null);
const recordProgress = ref(0);
let recordInterval: ReturnType<typeof setInterval> | null = null;

// Collect ALL chunks (sent + received) for debugging
const sentChunks = shallowRef<ArrayBuffer[]>([]);
const playbackChunks = shallowRef<ArrayBuffer[]>([]);
const playbackUrl = ref<string | null>(null);

const streamTest = useStreamTest({
  recordDuration: 10_000,
  onPlaybackData: (data: ArrayBuffer) => {
    console.log(`[FlowLive] Received playback chunk: ${data.byteLength} bytes (total: ${playbackChunks.value.length + 1})`);
    playbackChunks.value = [...playbackChunks.value, data];
  },
  onError: (msg: string) => {
    console.error('[FlowLive] StreamTest error:', msg);
    testPhase.value = 'error';
    testError.value = msg;
    clearRecordProgress();
  },
});

// Watch streamTest state and sync to local testPhase
import { watch } from 'vue';
watch(() => streamTest.state.value, (s) => {
  console.log('[FlowLive] streamTest state →', s);

  if (s === 'idle') return;
  if (s === 'recording') { testPhase.value = 'recording'; return; }
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
});

// ─── Test recorder (dedicated MediaRecorder on the capture stream) ──────────
let testRecorder: MediaRecorder | null = null;

function startTestRecording() {
  const stream = audioCapture.processedStream.value || audioCapture.mediaStream.value;
  if (!stream) {
    console.error('[FlowLive] No audio stream available for test recording');
    testPhase.value = 'error';
    testError.value = 'No audio stream available. Go back and select an audio device.';
    return;
  }

  // Verify stream has active tracks
  const tracks = stream.getAudioTracks();
  console.log(`[FlowLive] Stream tracks: ${tracks.length}, active: ${tracks.filter(t => t.enabled && t.readyState === 'live').length}`);
  if (tracks.length === 0 || tracks.every(t => t.readyState !== 'live')) {
    testPhase.value = 'error';
    testError.value = 'Audio device is no longer active. Go back and re-select your device.';
    return;
  }

  const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
    ? 'audio/webm;codecs=opus'
    : 'audio/webm';

  console.log('[FlowLive] Starting test recorder, mimeType:', mimeType);

  testRecorder = new MediaRecorder(stream, {
    mimeType,
    audioBitsPerSecond: 192000,
  });

  testRecorder.ondataavailable = async (event) => {
    if (event.data.size > 0) {
      const buffer = await event.data.arrayBuffer();
      // Keep a copy for debugging/download
      sentChunks.value = [...sentChunks.value, buffer];
      const ok = streamTest.sendChunk(buffer);
      console.log(`[FlowLive] Sent chunk: ${buffer.byteLength} bytes, accepted: ${ok}, total sent: ${sentChunks.value.length}`);
    }
  };

  testRecorder.onerror = (e) => {
    console.error('[FlowLive] MediaRecorder error:', e);
    testError.value = 'Audio recorder error';
    testPhase.value = 'error';
  };

  testRecorder.start(250);
  console.log('[FlowLive] Test recorder started');
}

function stopTestRecording() {
  if (testRecorder && testRecorder.state !== 'inactive') {
    console.log('[FlowLive] Stopping test recorder');
    testRecorder.stop();
    testRecorder = null;
  }
}

// ─── Test flow ──────────────────────────────────────────────────────────────
async function runTest() {
  testError.value = null;
  testPhase.value = 'ready';
  sentChunks.value = [];
  playbackChunks.value = [];
  revokePlaybackUrl();

  try {
    console.log('[FlowLive] Connecting to stream test WebSocket...');
    await streamTest.connect();
    console.log('[FlowLive] Connected. Starting recording...');

    streamTest.startRecording();
    testPhase.value = 'recording';

    // Start dedicated test recorder on the audio stream
    startTestRecording();

    // Progress bar: 0→100% over 10s
    recordProgress.value = 0;
    const start = Date.now();
    recordInterval = setInterval(() => {
      const elapsed = Date.now() - start;
      recordProgress.value = Math.min(100, (elapsed / 10_000) * 100);
    }, 100);
  } catch (err) {
    console.error('[FlowLive] runTest failed:', err);
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

// ─── Playback ───────────────────────────────────────────────────────────────
function buildPlaybackBlob() {
  console.log(`[FlowLive] Building playback blob from ${playbackChunks.value.length} chunks`);
  if (playbackChunks.value.length === 0) {
    console.warn('[FlowLive] No playback chunks received!');
    testError.value = `No audio data received back from server (sent: ${sentChunks.value.length} chunks)`;
    return;
  }

  const blob = new Blob(playbackChunks.value, { type: 'audio/webm;codecs=opus' });
  console.log(`[FlowLive] Playback blob size: ${blob.size} bytes`);
  const url = URL.createObjectURL(blob);
  playbackUrl.value = url;
}

function downloadBlob(chunks: ArrayBuffer[], filename: string) {
  if (chunks.length === 0) return;
  const blob = new Blob(chunks, { type: 'audio/webm;codecs=opus' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

function downloadSent() {
  downloadBlob(sentChunks.value, 'stream-test-sent.webm');
}

function downloadReceived() {
  downloadBlob(playbackChunks.value, 'stream-test-received.webm');
}

function revokePlaybackUrl() {
  if (playbackUrl.value) {
    URL.revokeObjectURL(playbackUrl.value);
    playbackUrl.value = null;
  }
}

function markTestPassed() {
  flow.setLiveTestPassed(true);
}

function goToWaiting() {
  flow.goToStep('waiting');
  router.push('/stream/waiting');
}

function retryTest() {
  streamTest.stop();
  testPhase.value = 'ready';
  testError.value = null;
  recordProgress.value = 0;
  playbackChunks.value = [];
  revokePlaybackUrl();
}

function goBackToTutorial() {
  streamTest.stop();
  stopTestRecording();
  clearRecordProgress();
  revokePlaybackUrl();
  testPhase.value = 'ready';
  liveStep.value = 'tutorial';
  flow.setLiveSubStep('tutorial');
}

const canGoBackFromTest = computed(() =>
  testPhase.value === 'ready' || testPhase.value === 'done' || testPhase.value === 'error'
);

const isDev = import.meta.env.DEV;

onUnmounted(() => {
  streamTest.cleanup();
  stopTestRecording();
  clearRecordProgress();
  revokePlaybackUrl();
  // NOTE: do NOT call audioCapture.stopCapture() here —
  // the singleton persists to FlowWaiting and FlowStreaming.
});
</script>

<template>
  <div class="flow-live">
    <!-- ═══ B.1 — OS Selection ═══ -->
    <template v-if="liveStep === 'os-select'">
      <h1 class="step-title">What's your operating system?</h1>
      <p class="step-subtitle">We'll show you how to set up audio capture for your system.</p>

      <div class="os-cards">
        <button v-for="os in (['windows', 'macos', 'linux'] as const)" :key="os"
          :class="['os-card', { selected: selectedOs === os }]" @click="selectOs(os)">
          <div class="os-icon">
            {{ os === 'windows' ? '🪟' : os === 'macos' ? '🍎' : '🐧' }}
          </div>
          <span class="os-label">
            {{ os === 'windows' ? 'Windows' : os === 'macos' ? 'macOS' : 'Linux' }}
          </span>
        </button>
      </div>

      <div class="step-actions">
        <button class="btn-secondary" @click="goBackToMode">← Back</button>
      </div>
    </template>

    <!-- ═══ B.2 — Tutorial + Device Selection ═══ -->
    <template v-else-if="liveStep === 'tutorial'">
      <h1 class="step-title">Set Up Audio Capture</h1>

      <!-- macOS instructions -->
      <div v-if="selectedOs === 'macos'" class="tutorial-content">
        <div class="tutorial-section">
          <h3>Option A: BlackHole (recommended for system audio)</h3>
          <ol>
            <li>Install <a href="https://existential.audio/blackhole/" target="_blank" rel="noopener">BlackHole</a>
              (free virtual audio driver)</li>
            <li>Open <strong>Audio MIDI Setup</strong> → create a <strong>Multi-Output Device</strong> combining your
              speakers + BlackHole</li>
            <li>Set the Multi-Output as your system output</li>
            <li>Select <strong>BlackHole</strong> as your audio source below</li>
          </ol>
        </div>
        <div class="tutorial-section">
          <h3>Option B: External audio (mixer, turntable)</h3>
          <p>If you're using an external audio interface, simply select it from the dropdown below.</p>
        </div>
      </div>

      <!-- Windows instructions -->
      <div v-if="selectedOs === 'windows'" class="tutorial-content">
        <div class="tutorial-section">
          <h3>Capture System Audio</h3>
          <p>Click <strong>"Share Screen Audio"</strong> below. In the browser dialog:</p>
          <ol>
            <li>Select any tab or screen to share</li>
            <li><strong>Check "Share audio"</strong> at the bottom of the dialog</li>
            <li>The video is discarded — only the audio is captured</li>
          </ol>
        </div>
        <div class="tutorial-section">
          <h3>Or use an audio device</h3>
          <p>If you have an external audio interface or mixer, select it from the dropdown below.</p>
        </div>
      </div>

      <!-- Linux instructions -->
      <div v-if="selectedOs === 'linux'" class="tutorial-content">
        <div class="tutorial-section">
          <h3>PulseAudio / PipeWire Monitor</h3>
          <p>Select a <strong>Monitor</strong> source from the dropdown below to capture system audio output.</p>
          <p class="text-muted">
            If you don't see a monitor source, ensure PulseAudio or PipeWire is installed and the
            <code>module-loopback</code> is loaded.
          </p>
        </div>
      </div>

      <!-- Device selector -->
      <div class="device-section">
        <h3>Select Audio Source</h3>

        <div class="device-row">
          <select v-model="selectedDevice" class="device-select">
            <option value="">-- Select audio device --</option>
            <option v-for="device in audioCapture.devices.value" :key="device.deviceId" :value="device.deviceId">
              {{ device.label }}
            </option>
          </select>
          <button class="btn-icon" @click="audioCapture.refreshDevices()" title="Refresh devices">
            🔄
          </button>
        </div>

        <div class="device-actions">
          <button class="btn-primary" :disabled="!selectedDevice" @click="handleDeviceSelect">
            🎧 Use Selected Device
          </button>
          <button v-if="selectedOs === 'windows'" class="btn-secondary" @click="handleScreenShare">
            🖥️ Share Screen Audio
          </button>
        </div>

        <!-- Audio level preview -->
        <div v-if="audioCapture.isCapturing.value" class="audio-preview">
          <div class="capture-status">
            <span class="status-dot active"></span>
            Audio captured — <strong>{{audioCapture.devices.value.find(d => d.deviceId ===
              audioCapture.selectedDeviceId.value)?.label || 'Screen Audio'}}</strong>
          </div>
          <div class="audio-level">
            <div class="audio-level-bar" :style="{ width: `${audioMeter.level.value}%` }"></div>
          </div>
        </div>

        <p v-if="audioCapture.error.value" class="error-text">{{ audioCapture.error.value }}</p>
      </div>

      <div class="step-actions">
        <button class="btn-secondary" @click="goBackToOs">← Back</button>
        <button class="btn-primary" :disabled="!audioCapture.isCapturing.value" @click="goToTest">
          Continue to Test →
        </button>
      </div>
    </template>

    <!-- ═══ B.3 — Test Stream ═══ -->
    <template v-else-if="liveStep === 'test'">
      <h1 class="step-title">Test Your Stream</h1>
      <p class="step-subtitle">
        We'll record 10 seconds of your audio and play it back so you can verify it sounds right.
      </p>

      <div class="test-panel">
        <!-- Audio level meter (always visible when capturing) -->
        <div v-if="audioCapture.isCapturing.value" class="audio-level test-level">
          <div class="audio-level-bar" :style="{ width: `${audioMeter.level.value}%` }"></div>
        </div>

        <!-- Ready state -->
        <div v-if="testPhase === 'ready'" class="test-state">
          <p>Play some audio from your music source, then click start.</p>
          <button class="btn-primary btn-lg" @click="runTest">
            🎤 Start Test
          </button>
        </div>

        <!-- Recording state -->
        <div v-if="testPhase === 'recording'" class="test-state">
          <div class="test-recording-indicator">
            <span class="recording-dot"></span>
            Recording... <span class="chunk-counter">({{ sentChunks.length }} chunks sent)</span>
          </div>
          <div class="progress-bar">
            <div class="progress-fill" :style="{ width: `${recordProgress}%` }"></div>
          </div>
          <p class="text-muted">{{ Math.ceil((100 - recordProgress) / 10) }}s remaining</p>
        </div>

        <!-- Waiting state -->
        <div v-if="testPhase === 'waiting'" class="test-state">
          <p>Sent {{ sentChunks.length }} chunks. Waiting for server...</p>
        </div>

        <!-- Playing state -->
        <div v-if="testPhase === 'playing'" class="test-state">
          <div class="test-playing-indicator">
            <span class="playing-icon">🔊</span>
            Receiving... <span class="chunk-counter">({{ playbackChunks.length }} chunks)</span>
          </div>
          <p class="text-muted">Server is sending your audio back. Playback starts when complete.</p>
        </div>

        <!-- Done state -->
        <div v-if="testPhase === 'done'" class="test-state">
          <!-- Playback audio element -->
          <div v-if="playbackUrl" class="playback-section">
            <p class="playback-label">Your test recording ({{ playbackChunks.length }} chunks received):</p>
            <audio :src="playbackUrl" controls autoplay class="playback-audio"></audio>
          </div>
          <div v-else-if="playbackChunks.length === 0" class="playback-section">
            <p class="error-text">No audio data received from server. Sent {{ sentChunks.length }} chunks.</p>
          </div>

          <p>Did you hear your audio clearly?</p>
          <div class="test-result-actions">
            <button class="btn-success" @click="markTestPassed">
              ✓ Yes, it sounds good!
            </button>
            <button class="btn-secondary" @click="retryTest">
              Try Again
            </button>
          </div>

          <!-- Debug download buttons -->
          <div class="debug-downloads">
            <button v-if="sentChunks.length > 0" class="btn-link" @click="downloadSent">
              ⬇ Download sent audio ({{ sentChunks.length }} chunks)
            </button>
            <button v-if="playbackChunks.length > 0" class="btn-link" @click="downloadReceived">
              ⬇ Download received audio ({{ playbackChunks.length }} chunks)
            </button>
          </div>
        </div>

        <!-- Error state -->
        <div v-if="testPhase === 'error'" class="test-state">
          <p class="error-text">{{ testError || 'An error occurred during the test.' }}</p>
          <!-- Show download even on error if we have data -->
          <div v-if="sentChunks.length > 0" class="debug-downloads">
            <button class="btn-link" @click="downloadSent">
              ⬇ Download sent audio ({{ sentChunks.length }} chunks)
            </button>
          </div>
          <button class="btn-secondary" @click="retryTest">
            Try Again
          </button>
        </div>
      </div>

      <!-- Continue after pass -->
      <div v-if="flow.liveTestPassed.value" class="test-passed-banner">
        <span>✓ Stream test passed</span>
        <button class="btn-primary" @click="goToWaiting">
          Continue to Waiting Room →
        </button>
      </div>

      <!-- Dev-only: skip test entirely -->
      <button v-if="isDev && !flow.liveTestPassed.value" class="btn-dev-skip" @click="markTestPassed">
        🛠 Skip Test (dev)
      </button>

      <div class="step-actions">
        <button class="btn-secondary" :disabled="!canGoBackFromTest" @click="goBackToTutorial">
          ← Back
        </button>
      </div>
    </template>
  </div>
</template>

<style scoped>
.flow-live {
  max-width: 640px;
  margin: 0 auto;
}

/* ─── Common step styling ────────────────────────────────────────────────── */
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

/* ─── B.1 — OS Cards ─────────────────────────────────────────────────────── */
.os-cards {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: var(--spacing-md);
  margin-bottom: var(--spacing-lg);
}

@media (max-width: 480px) {
  .os-cards {
    grid-template-columns: 1fr;
  }
}

.os-card {
  background: var(--color-surface);
  border: 2px solid var(--color-border);
  border-radius: var(--radius-xl);
  padding: var(--spacing-xl) var(--spacing-md);
  text-align: center;
  cursor: pointer;
  transition: all var(--transition-fast);
  font-family: var(--font-family);
  color: var(--color-text);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--spacing-sm);
}

.os-card:hover {
  border-color: var(--color-primary);
  background: var(--color-surface-hover);
}

.os-card.selected {
  border-color: var(--color-primary);
  background: var(--color-primary-bg, rgba(255, 152, 0, 0.08));
}

.os-icon {
  font-size: 2.5rem;
}

.os-label {
  font-weight: var(--font-weight-bold);
}

/* ─── B.2 — Tutorial ─────────────────────────────────────────────────────── */
.tutorial-content {
  margin-bottom: var(--spacing-xl);
}

.tutorial-section {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: var(--spacing-lg);
  margin-bottom: var(--spacing-md);
}

.tutorial-section h3 {
  margin: 0 0 var(--spacing-sm);
  font-size: var(--font-size-md);
  color: var(--color-text);
}

.tutorial-section ol,
.tutorial-section p {
  margin: 0;
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
  line-height: var(--line-height-relaxed);
}

.tutorial-section ol {
  padding-left: var(--spacing-xl);
}

.tutorial-section li {
  margin-bottom: var(--spacing-xs);
}

.device-section {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: var(--spacing-lg);
}

.device-section h3 {
  margin: 0 0 var(--spacing-md);
  font-size: var(--font-size-md);
}

.device-row {
  display: flex;
  gap: var(--spacing-sm);
  margin-bottom: var(--spacing-md);
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

.device-actions {
  display: flex;
  gap: var(--spacing-sm);
  flex-wrap: wrap;
}

.audio-preview {
  margin-top: var(--spacing-md);
}

.capture-status {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  font-size: var(--font-size-sm);
  color: var(--color-text);
  margin-bottom: var(--spacing-sm);
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

/* ─── Audio level meter ──────────────────────────────────────────────────── */
.audio-level {
  height: 6px;
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

.test-level {
  margin-bottom: var(--spacing-lg);
  height: 10px;
}

/* ─── B.3 — Test Stream ──────────────────────────────────────────────────── */
.test-panel {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: var(--spacing-xl);
  margin-bottom: var(--spacing-lg);
}

.test-state {
  text-align: center;
}

.test-state p {
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

.playback-label {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
  margin: 0 0 var(--spacing-sm);
}

.playback-audio {
  width: 100%;
  max-width: 400px;
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

/* ─── Buttons ────────────────────────────────────────────────────────────── */
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

.btn-secondary:disabled {
  opacity: 0.4;
  cursor: not-allowed;
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

.error-text {
  color: #ef4444;
  font-size: var(--font-size-sm);
}

.text-muted {
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
}

/* ─── Debug / download helpers ───────────────────────────────────────────── */
.chunk-counter {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
  font-weight: normal;
}

.debug-downloads {
  display: flex;
  gap: var(--spacing-md);
  justify-content: center;
  margin-top: var(--spacing-md);
  flex-wrap: wrap;
}

.btn-link {
  background: none;
  border: none;
  color: var(--color-primary);
  font-family: var(--font-family);
  font-size: var(--font-size-sm);
  cursor: pointer;
  text-decoration: underline;
  padding: var(--spacing-xs) var(--spacing-sm);
}

.btn-link:hover {
  opacity: 0.8;
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
