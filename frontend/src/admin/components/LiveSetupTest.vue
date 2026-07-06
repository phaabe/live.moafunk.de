<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue';
import {
  useHostFlow,
  useAudioCapture,
  useStreamSocket,
  useUploadHealth,
  STREAM_AUDIO_BITS_PER_SECOND,
} from '@admin/composables';
import DbMeter from '@admin/components/DbMeter.vue';
import StreamPreviewPlayer from '@admin/components/StreamPreviewPlayer.vue';
import { config } from '@/config';

/**
 * Live preparation: audio input selection + level meter + rehearsal broadcast
 * against the private Icecast `/test` mount.
 *
 * Extracted from FlowLive so it can render both inline on the show dashboard
 * and inside the fullscreen /stream/live step. All logic is identical; the
 * host confirming "sounds good" emits `passed` and the parent decides where
 * to navigate. Uses the useAudioCapture / useHostFlow singletons, so capture
 * survives into the on-air step regardless of which surface ran the test.
 */
const emit = defineEmits<{
  /** The host confirmed the test stream sounds right (test marked passed). */
  passed: [];
}>();

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
  // The capture singleton persists across navigations; mirror a still-active
  // device in the dropdown instead of showing "no input selected".
  const activeId = audioCapture.selectedDeviceId.value;
  if (audioCapture.isCapturing.value && activeId && activeId !== 'screen') {
    selectedDevice.value = activeId;
  }
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

// ─── Test broadcast (real producer → /test harbour → Icecast /test.mp3) ──────
type TestPhase = 'ready' | 'connecting' | 'live' | 'error';
const testPhase = ref<TestPhase>('ready');
const testError = ref<string | null>(null);
const sentChunks = ref(0);
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
    if (testPhase.value === 'live' || testPhase.value === 'connecting') {
      testPhase.value = 'ready';
      stopTestRecording();
    }
  },
});

// Dedicated MediaRecorder on the capture stream — independent of the
// singleton capture's main onData wiring, so it never disturbs the real
// go-live pipeline.
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

  testRecorder = new MediaRecorder(stream, {
    mimeType,
    audioBitsPerSecond: STREAM_AUDIO_BITS_PER_SECOND,
  });

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
  emit('passed');
}

// ─── Rehearsal connection verdict (#275) ─────────────────────────────────────
// The same browser-side upload telemetry as the on-air connection card runs
// against the test broadcast, so the host knows "connection good enough"
// BEFORE air time.
const testHealth = useUploadHealth(
  computed(() => testPhase.value === 'live'),
  ref(STREAM_AUDIO_BITS_PER_SECOND)
);

const isDev = import.meta.env.DEV;

onUnmounted(() => {
  navigator.mediaDevices.removeEventListener('devicechange', onDeviceChange);
  if (deviceRefreshInterval) {
    clearInterval(deviceRefreshInterval);
    deviceRefreshInterval = null;
  }
  // Never leave a producer pushing to `/test` after navigating away.
  if (testActive.value) {
    stopTestBroadcast();
  }
  // NOTE: do NOT call audioCapture.stopCapture() here —
  // the singleton capture persists into the on-air step.
});
</script>

<template>
  <div class="live-setup">
    <!-- ─── Device selection ─── -->
    <div class="ls-device-row">
      <select v-model="selectedDevice" class="ls-select" @change="handleDeviceSelect">
        <option value="">-- Select audio input --</option>
        <option
          v-for="device in audioCapture.devices.value"
          :key="device.deviceId"
          :value="device.deviceId"
        >
          {{ device.label }}
        </option>
      </select>
      <button
        class="ls-btn-icon"
        type="button"
        title="Refresh devices"
        @click="audioCapture.listDevices()"
      >
        🔄
      </button>
    </div>

    <button class="ls-link" type="button" @click="handleScreenShare">
      🖥️ Share screen audio instead
    </button>

    <div v-if="audioCapture.isCapturing.value" class="ls-capture">
      <p class="ls-capture-status">
        <span class="ls-dot active"></span>
        Capturing — <strong>{{ capturedLabel }}</strong>
      </p>
      <DbMeter :media-stream="audioCapture.mediaStream.value" label="Input Level" />
    </div>

    <p v-if="audioCapture.error.value" class="ls-error">{{ audioCapture.error.value }}</p>

    <!-- ─── Test broadcast ─── -->
    <div class="ls-test">
      <div class="ls-test-head">
        <p class="ls-test-title">Test broadcast</p>
        <p class="ls-test-hint">Private /test mount — listeners can't hear this</p>
      </div>

      <div v-if="testPhase === 'ready'" class="ls-test-state">
        <button
          class="ls-btn-primary"
          type="button"
          :disabled="!audioCapture.isCapturing.value || !previewUrl"
          @click="runTest"
        >
          🎤 Start test
        </button>
        <p v-if="!audioCapture.isCapturing.value" class="ls-muted">Select an audio input first.</p>
        <p v-else-if="!previewUrl" class="ls-muted">Test stream isn't configured on this server.</p>
      </div>

      <div v-else-if="testPhase === 'connecting'" class="ls-test-state">
        <p class="ls-muted">Connecting to the test stream…</p>
      </div>

      <div v-else-if="testPhase === 'live'" class="ls-test-state">
        <p class="ls-onair">
          <span class="ls-dot rec"></span>
          Test broadcast live
          <span class="ls-muted">· {{ sentChunks }} chunks sent</span>
        </p>

        <!-- Same upload telemetry as the on-air connection card (#275). -->
        <p class="ls-conn">
          <span :class="['ls-conn-pill', `ls-conn-${testHealth.verdict.value}`]">
            {{ testHealth.verdictText.value }}
          </span>
          <span class="ls-muted">· {{ testHealth.uploadKbps.value }} kbps up</span>
        </p>

        <!-- Runs a few seconds behind — use headphones. -->
        <StreamPreviewPlayer v-if="previewUrl" :src="previewUrl" />

        <div class="ls-test-actions">
          <p class="ls-question">Does it sound clear on the test stream?</p>
          <button class="ls-btn-secondary" type="button" @click="retryTest">⏹ Stop test</button>
          <button class="ls-btn-success" type="button" @click="markTestPassed">
            ✓ Yes, ready to go live
          </button>
        </div>
      </div>

      <div v-else-if="testPhase === 'error'" class="ls-test-state">
        <p class="ls-error">{{ testError || 'An error occurred during the test.' }}</p>
        <button class="ls-btn-secondary" type="button" @click="retryTest">Try again</button>
      </div>
    </div>

    <button
      v-if="isDev && !flow.liveTestPassed.value"
      class="ls-dev-skip"
      type="button"
      @click="markTestPassed"
    >
      🛠 Skip test (dev)
    </button>
  </div>
</template>

<style scoped>
.live-setup {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-sm);
  font-family: var(--font-ui);
}

.ls-device-row {
  display: flex;
  gap: var(--spacing-sm);
}

.ls-select {
  flex: 1;
  min-width: 0;
  padding: var(--spacing-sm) var(--spacing-md);
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  color: var(--color-text);
  font-family: var(--font-ui);
  font-size: var(--font-size-sm);
}

.ls-btn-icon {
  background: none;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  padding: var(--spacing-sm);
  cursor: pointer;
  font-size: var(--font-size-md);
}

.ls-link {
  align-self: flex-start;
  background: none;
  border: none;
  padding: 0;
  color: var(--color-primary);
  font-family: var(--font-ui);
  font-size: var(--font-size-sm);
  cursor: pointer;
  text-decoration: underline;
}

.ls-capture {
  background: var(--color-surface-alt);
  border-radius: var(--radius-md);
  padding: var(--spacing-md);
}

.ls-capture-status {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  margin: 0 0 var(--spacing-sm);
  font-size: var(--font-size-sm);
  color: var(--color-success);
}

.ls-dot {
  width: 8px;
  height: 8px;
  border-radius: var(--radius-full);
  background: var(--color-text-muted);
  flex: 0 0 auto;
}

.ls-dot.active {
  background: var(--color-success);
  box-shadow: 0 0 6px rgba(34, 197, 94, 0.5);
}

.ls-dot.rec {
  width: 10px;
  height: 10px;
  background: var(--color-error);
  animation: ls-pulse 1s ease-in-out infinite;
}

@keyframes ls-pulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.4;
  }
}

.ls-test {
  border-top: 1px solid var(--color-border);
  padding-top: var(--spacing-md);
  margin-top: var(--spacing-xs);
}

.ls-test-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--spacing-sm);
  flex-wrap: wrap;
  margin-bottom: var(--spacing-sm);
}

.ls-test-title {
  margin: 0;
  font-size: var(--font-size-sm);
  font-weight: 600;
  color: var(--color-text);
}

.ls-test-hint {
  margin: 0;
  font-size: var(--font-size-xs);
  color: var(--color-text-muted);
}

.ls-test-state {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-sm);
}

/* The preview player is chrome-less since the analyzer rework (#274) — give
   it back an inset card inside the test panel. */
.ls-test-state :deep(.preview-player) {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: var(--spacing-md) var(--spacing-lg);
}

.ls-onair {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  margin: 0;
  font-size: var(--font-size-sm);
  font-weight: 600;
  color: var(--color-error);
}

.ls-conn {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  margin: 0;
  font-size: var(--font-size-sm);
}

.ls-conn-pill {
  font-family: var(--font-ui);
  font-size: var(--font-size-xs);
  padding: 2px 10px;
  border-radius: var(--radius-full);
  white-space: nowrap;
}

.ls-conn-good {
  background: var(--color-success-bg);
  color: var(--color-success);
}

.ls-conn-tight {
  background: var(--color-warning-bg);
  color: var(--color-warning);
}

.ls-conn-slow {
  background: var(--color-error-bg);
  color: var(--color-error);
}

.ls-test-actions {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  flex-wrap: wrap;
}

.ls-question {
  margin: 0;
  flex: 1 1 auto;
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}

.ls-btn-primary {
  align-self: flex-start;
  background: var(--color-primary);
  color: var(--color-primary-text);
  border: none;
  padding: var(--spacing-sm) var(--spacing-lg);
  border-radius: var(--radius-md);
  font-family: var(--font-ui);
  font-size: var(--font-size-sm);
  font-weight: 700;
  cursor: pointer;
}

.ls-btn-primary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.ls-btn-secondary {
  background: none;
  border: 1px solid var(--color-border);
  color: var(--color-text-muted);
  padding: var(--spacing-sm) var(--spacing-md);
  border-radius: var(--radius-md);
  font-family: var(--font-ui);
  font-size: var(--font-size-sm);
  cursor: pointer;
}

.ls-btn-secondary:hover {
  color: var(--color-text);
  border-color: var(--color-border-light);
}

.ls-btn-success {
  background: var(--color-success);
  color: #fff;
  border: none;
  padding: var(--spacing-sm) var(--spacing-md);
  border-radius: var(--radius-md);
  font-family: var(--font-ui);
  font-size: var(--font-size-sm);
  font-weight: 700;
  cursor: pointer;
}

.ls-btn-success:hover {
  background: #16a34a;
}

.ls-muted {
  margin: 0;
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
}

.ls-error {
  margin: 0;
  color: var(--color-error);
  font-size: var(--font-size-sm);
}

.ls-dev-skip {
  align-self: flex-start;
  margin-top: var(--spacing-xs);
  background: var(--color-warning);
  color: #000;
  font-family: var(--font-ui);
  font-size: var(--font-size-xs);
  font-weight: 700;
  padding: var(--spacing-xs) var(--spacing-md);
  border: 2px dashed #000;
  border-radius: var(--radius-md);
  cursor: pointer;
}
</style>
