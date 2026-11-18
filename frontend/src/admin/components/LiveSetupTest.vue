<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue';
import {
  useHostFlow,
  useAudioCapture,
  useStreamSocket,
  useUploadHealth,
  useDbMeter,
  useSoundcheck,
  createReturnFeed,
  getStreamSocketStats,
  STREAM_AUDIO_BITS_PER_SECOND,
  BROWSER_UNSUPPORTED_MESSAGE,
  streamRecordingMimeType,
  type UseStreamSocketOptions,
  type ReturnFeed,
  type SoundcheckSource,
  type SoundcheckStep,
  type SoundcheckStepState,
} from '@admin/composables';
import DbMeter from '@admin/components/DbMeter.vue';
import BoardIcon from '@admin/components/BoardIcon.vue';
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
 *
 * `studio` renders the flow's Studio card: the input level meter, "Audio
 * input" (screen audio is one of its options) and the soundcheck: the host
 * tests the music and the mic, then useSoundcheck checks the server and the
 * return feed and proposes the result.
 */
const props = withDefaults(defineProps<{ studio?: boolean }>(), { studio: false });

const emit = defineEmits<{
  /** The host confirmed the test stream sounds right (test marked passed). */
  passed: [];
}>();

const flow = useHostFlow();

// The private Icecast `/test` mount the rehearsal plays back from. Same MP3 the
// public `/live.mp3` would serve, but non-public. Empty (e.g. local dev) → the
// real test can't run; the dev-skip below covers that case.
const previewUrl = config.stream.icecastTestUrl;

// Checked once: the browser's recorder support does not change at runtime.
const browserSupported = streamRecordingMimeType() !== null;

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
  if (audioCapture.isCapturing.value && activeId && (props.studio || activeId !== 'screen')) {
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

/** Studio "Audio input": a device, or screen audio. */
async function handleInputSelect() {
  if (selectedDevice.value !== 'screen') {
    await handleDeviceSelect();
    return;
  }
  if (await audioCapture.captureScreenAudio()) {
    resetTest();
  } else {
    selectedDevice.value = '';
  }
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

// The socket's callbacks are shared: the Studio's Go Live takes them over,
// so each test broadcast takes them back before it connects.
const socketCallbacks: UseStreamSocketOptions = {
  onLive: () => {
    testPhase.value = 'live';
  },
  onError: (msg: string) => {
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
};
const streamSocket = useStreamSocket(socketCallbacks);

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

  const mimeType = streamRecordingMimeType();
  if (!mimeType) {
    testPhase.value = 'error';
    testError.value = BROWSER_UNSUPPORTED_MESSAGE;
    return false;
  }

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

/** Start the test broadcast and its recorder. Rejects with the reason. */
async function startBroadcast() {
  if (!previewUrl) throw new Error('Test stream is not configured on this server.');
  if (!browserSupported) throw new Error(BROWSER_UNSUPPORTED_MESSAGE);

  testError.value = null;
  testPhase.value = 'connecting';
  sentChunks.value = 0;
  // A user gesture: start the capture's audio context if it is suspended.
  audioCapture.resumeAudioContext();

  useStreamSocket(socketCallbacks);
  try {
    // Connect in TEST mode → backend pushes to `/test`, not `/live`.
    await streamSocket.connect(false, undefined, true);
  } catch {
    throw new Error('Failed to connect to the test stream.');
  }
  if (!startTestRecording()) {
    streamSocket.stopStream();
    throw new Error(testError.value ?? 'Failed to start audio');
  }
}

async function runTest() {
  // A new test has to be listened to again.
  flow.setLiveTestPassed(false);
  try {
    await startBroadcast();
  } catch (e) {
    testPhase.value = 'error';
    testError.value = e instanceof Error ? e.message : 'Failed to connect to the test stream.';
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
  soundcheck.reset();
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

// ─── Studio soundcheck ───────────────────────────────────────────────────────
// What is sent is what is measured: the processed stream when there is one.
const sentStream = computed(() =>
  props.studio ? (audioCapture.processedStream.value ?? audioCapture.mediaStream.value) : null
);
const sentMeter = useDbMeter(sentStream);

// One context for the return feed. Made in a click, so it may play.
let returnContext: AudioContext | null = null;
const noReturn: ReturnFeed = {
  peakDb: () => null,
  playing: () => false,
  setAudible: () => {},
  close: () => {},
};

const soundcheck = useSoundcheck({
  inputLevel: () =>
    audioCapture.isCapturing.value && sentStream.value
      ? { rmsDb: sentMeter.db.value, peakDb: sentMeter.peakDb.value }
      : null,
  inputLabel: () => capturedLabel.value,
  startBroadcast,
  stopBroadcast: stopTestBroadcast,
  broadcastError: () => (testPhase.value === 'error' ? testError.value : null),
  sentChunks: () => sentChunks.value,
  // Counted by the server and sent back with each pong: proof that audio arrived.
  serverChunks: () => getStreamSocketStats().serverChunks,
  openReturn: () => {
    if (!previewUrl) return noReturn;
    returnContext ??= new AudioContext();
    return createReturnFeed(previewUrl, returnContext);
  },
  setPassed: (passed) => flow.setLiveTestPassed(passed),
});

const STATE_LABELS: Record<SoundcheckStepState, string> = {
  pending: 'Waiting',
  running: 'Checking',
  ok: 'OK',
  warn: 'Warning',
  fail: 'Failed',
  skipped: 'Skipped',
};

// Music and Mic have their buttons in the row; the host starts each test.
const isSource = (id: SoundcheckStep['id']) => id === 'music' || id === 'mic';
const testSource = (id: SoundcheckStep['id']) => void soundcheck.test(id as SoundcheckSource);
const skipSource = (id: SoundcheckStep['id']) => void soundcheck.skip(id as SoundcheckSource);

// Go Live uses the same socket: the page stops the test before it connects.
defineExpose({ stopTest: () => soundcheck.dispose() });

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
  soundcheck.dispose();
  void returnContext?.close();
  if (testActive.value) {
    stopTestBroadcast();
  }
  // NOTE: do NOT call audioCapture.stopCapture() here —
  // the singleton capture persists into the on-air step.
});
</script>

<template>
  <div v-if="studio" class="live-setup studio-chain">
    <p v-if="!browserSupported" class="ls-error">{{ BROWSER_UNSUPPORTED_MESSAGE }}</p>
    <!-- One card: input, then the soundcheck. Go Live is on the page. -->
    <section class="studio-step">
      <span class="step-icon" aria-hidden="true"><BoardIcon name="mic" :size="18" /></span>
      <div class="step-body">
        <DbMeter :media-stream="audioCapture.mediaStream.value" label="Input level" />
        <label class="ls-field">
          <span class="ls-label">Audio input</span>
          <select v-model="selectedDevice" class="ls-select" @change="handleInputSelect">
            <option value="">Select audio input</option>
            <option
              v-for="device in audioCapture.devices.value"
              :key="device.deviceId"
              :value="device.deviceId"
            >
              {{ device.label }}
            </option>
            <option value="screen">Screen audio</option>
          </select>
        </label>
        <p v-if="audioCapture.error.value" class="ls-error">{{ audioCapture.error.value }}</p>
        <ol class="sc-steps" aria-live="polite">
          <li
            v-for="step in soundcheck.steps.value"
            :key="step.id"
            :class="['sc-step', step.state]"
          >
            <span class="sc-state" role="img" :aria-label="STATE_LABELS[step.state]"></span>
            <span :id="`sc-name-${step.id}`" class="sc-name">{{ step.name }}</span>
            <span class="sc-detail">{{ step.detail }}</span>
            <span v-if="isSource(step.id)" class="sc-buttons">
              <button
                v-if="step.state === 'running'"
                class="ls-btn-secondary"
                type="button"
                :aria-describedby="`sc-name-${step.id}`"
                @click="soundcheck.stop()"
              >
                Stop
              </button>
              <template v-else-if="step.state === 'pending' || step.state === 'skipped'">
                <button
                  class="ls-btn-primary"
                  type="button"
                  :disabled="soundcheck.running.value"
                  :aria-describedby="`sc-name-${step.id}`"
                  @click="testSource(step.id)"
                >
                  Start test
                </button>
                <button
                  v-if="step.state === 'pending'"
                  class="ls-btn-secondary"
                  type="button"
                  :disabled="soundcheck.running.value"
                  :aria-describedby="`sc-name-${step.id}`"
                  @click="skipSource(step.id)"
                >
                  Skip
                </button>
              </template>
              <template v-else>
                <button
                  class="ls-btn-secondary"
                  type="button"
                  :disabled="soundcheck.running.value"
                  :aria-describedby="`sc-name-${step.id}`"
                  @click="testSource(step.id)"
                >
                  Re-run
                </button>
                <button
                  class="ls-btn-secondary"
                  type="button"
                  :aria-pressed="soundcheck.listening.value"
                  :aria-describedby="`sc-name-${step.id}`"
                  @click="soundcheck.setListening(!soundcheck.listening.value)"
                >
                  {{ soundcheck.listening.value ? 'Stop listening' : 'Listen to return feed' }}
                </button>
              </template>
            </span>
          </li>
        </ol>
        <div class="sc-result" aria-live="polite">
          <p v-if="soundcheck.prompt.value" class="sc-question">
            {{ soundcheck.prompt.value }}
          </p>
          <p v-else-if="flow.liveTestPassed.value" class="pill status-banner ok">
            <span class="dot" aria-hidden="true"></span
            >{{
              soundcheck.outcome.value === 'ok'
                ? 'Sounds good — ready to go live'
                : 'Soundcheck confirmed · ready to go live'
            }}
          </p>
          <p v-else-if="soundcheck.outcome.value === 'manual'" class="sc-question">
            Do you hear your audio? This browser cannot measure the return feed.
          </p>
          <p v-else-if="soundcheck.problem.value" class="notice" role="alert">
            {{ soundcheck.problem.value }}
          </p>
        </div>
        <div class="sc-actions">
          <template v-if="!soundcheck.running.value">
            <button
              v-if="soundcheck.outcome.value === 'manual' && !flow.liveTestPassed.value"
              class="ls-btn-success"
              type="button"
              @click="soundcheck.confirm()"
            >
              Sounds right
            </button>
            <button
              v-if="flow.liveTestPassed.value || soundcheck.outcome.value === 'manual'"
              class="ls-btn-secondary"
              type="button"
              @click="soundcheck.reject()"
            >
              Not OK
            </button>
            <button
              v-else-if="soundcheck.outcome.value === 'problem' && soundcheck.canOverride.value"
              class="ls-btn-secondary"
              type="button"
              @click="soundcheck.confirm()"
            >
              Confirm anyway
            </button>
          </template>
        </div>
        <p v-if="soundcheck.listening.value" class="ls-muted">
          Use <strong>headphones</strong>: the return feed runs a few seconds behind live, so
          speakers will echo.
        </p>
        <p v-if="soundcheck.listenError.value" class="ls-error" role="alert">
          {{ soundcheck.listenError.value }}
        </p>
        <button
          v-if="isDev && !flow.liveTestPassed.value"
          class="ls-dev-skip"
          type="button"
          @click="markTestPassed"
        >
          🛠 Skip test (dev)
        </button>
      </div>
    </section>
  </div>
  <div v-else class="live-setup">
    <p v-if="!browserSupported" class="ls-error">{{ BROWSER_UNSUPPORTED_MESSAGE }}</p>

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
          :disabled="!browserSupported || !audioCapture.isCapturing.value || !previewUrl"
          @click="runTest"
        >
          🎤 Start test
        </button>
        <p v-if="!browserSupported" class="ls-muted">Switch browser to run the test.</p>
        <p v-else-if="!audioCapture.isCapturing.value" class="ls-muted">
          Select an audio input first.
        </p>
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
          <!-- No chunk sent means no audio leaves the browser: never pass that. -->
          <p v-if="sentChunks === 0" class="ls-muted">
            Waiting for audio… If this stays, check that your input is active.
          </p>
          <button class="ls-btn-secondary" type="button" @click="retryTest">⏹ Stop test</button>
          <button
            class="ls-btn-success"
            type="button"
            :disabled="sentChunks === 0"
            @click="markTestPassed"
          >
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

.ls-field {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
}

.ls-label {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
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

.ls-btn-success:hover:not(:disabled) {
  background: #16a34a;
}

.ls-btn-success:disabled {
  opacity: 0.5;
  cursor: not-allowed;
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

/* Studio: one card, all of it visible. */

.studio-step {
  display: flex;
  gap: 12px;
  padding: var(--spacing-md);
  background: var(--color-surface);
  border: 1px solid #2a2a42;
  border-radius: var(--radius-xl);
}

.step-icon {
  flex: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  border: 1.5px solid var(--color-primary);
  border-radius: 16px;
  background: var(--color-surface-alt);
  color: var(--color-primary);
}

.step-body {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.studio-chain .ls-label {
  font-weight: 700;
  font-size: 11px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.studio-chain .ls-select {
  min-height: 44px;
  border-radius: var(--radius-lg);
  font-size: var(--font-size-md);
}

.studio-chain button {
  min-height: 44px;
}

.studio-chain button:focus-visible {
  outline: 2px solid var(--color-primary);
  outline-offset: 2px;
}

.sc-steps {
  margin: 0;
  padding: 0;
  list-style: none;
  border: 1px solid #2a2a42;
  border-radius: var(--radius-lg);
}

.sc-step {
  display: grid;
  grid-template-columns: 24px 7.5em 1fr auto;
  align-items: center;
  gap: var(--spacing-sm);
  min-height: 60px;
  padding: var(--spacing-sm) var(--spacing-md);
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}

.sc-buttons {
  display: flex;
  flex-wrap: wrap;
  gap: var(--spacing-sm);
}

.sc-buttons .ls-btn-primary {
  align-self: auto;
}

.sc-buttons button:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* Phones: the buttons go under the row's text. */
@media (max-width: 640px) {
  .sc-step {
    grid-template-columns: 24px 1fr;
  }

  .sc-detail,
  .sc-buttons {
    grid-column: 2;
  }
}

.sc-step + .sc-step {
  border-top: 1px solid #2a2a42;
}

.sc-name {
  font-weight: 700;
  color: var(--color-text);
}

/* The state is a shape and a colour; its name is the aria-label. */
.sc-state {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  border: 2px solid #4a4a66;
  border-radius: 50%;
  font: 700 13px/1 var(--font-ui);
}

.sc-step.running .sc-state {
  border-color: var(--color-primary);
  border-top-color: transparent;
  animation: sc-spin 0.9s linear infinite;
}

.sc-step.ok .sc-state {
  border-color: var(--color-success);
  background: var(--color-success);
  color: #000;
}

.sc-step.ok .sc-state::before {
  content: '✓';
}

.sc-step.warn .sc-state {
  border-color: var(--color-warning);
  border-radius: 4px;
  background: var(--color-warning);
  color: #000;
}

.sc-step.warn .sc-state::before {
  content: '!';
}

.sc-step.fail .sc-state {
  border-color: var(--color-error);
  border-radius: 4px;
  background: var(--color-error);
  color: #000;
}

.sc-step.fail .sc-state::before {
  content: '✕';
}

.sc-step.skipped .sc-state::before {
  content: '–';
}

.sc-step.ok .sc-detail,
.sc-step.running .sc-detail {
  color: var(--color-text);
}

.sc-step.warn .sc-detail {
  color: #fde7b0;
}

.sc-step.fail .sc-detail {
  color: #ffd2cc;
}

@keyframes sc-spin {
  to {
    transform: rotate(360deg);
  }
}

@media (prefers-reduced-motion: reduce) {
  .sc-step.running .sc-state {
    animation: none;
  }
}

.sc-result:empty {
  display: none;
}

.sc-result p {
  margin: 0;
}

.sc-question {
  font-weight: 700;
  color: var(--color-text);
}

.sc-result .notice {
  padding: var(--spacing-md);
  border: 1px solid #5a4416;
  border-left: 4px solid var(--color-warning);
  border-radius: var(--radius-xl);
  background: rgba(245, 158, 11, 0.1);
  color: #fde7b0;
  font-weight: 600;
}

.sc-actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--spacing-sm);
}
</style>
