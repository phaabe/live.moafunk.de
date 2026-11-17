<script lang="ts">
// When the live episode went on air, kept across remounts: leaving On Air
// does not end the broadcast, and the Studio sends the host back here.
let onAirSince = null as { showId: number; at: number } | null;
</script>

<script setup lang="ts">
import { computed, onUnmounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { BaseButton } from '@shared/components';
import {
  useHostFlow,
  useAudioCapture,
  useStreamSocket,
  useUploadHealth,
  useAutoBitrate,
  DEVICE_ENDED_MESSAGE,
  type UploadQualityMode,
} from '@admin/composables';
import { streamApi, recordingApi } from '@admin/api';
import DbMeter from '@admin/components/DbMeter.vue';

/**
 * On Air for a live episode (flow screen `studio-onair`). Only End Episode
 * ends it: the end time never does. A dropped connection keeps this screen
 * while the socket reconnects by itself, and End Episode still works.
 */
const router = useRouter();
const flow = useHostFlow();
const audioCapture = useAudioCapture();
const showId = flow.showId.value!;

const since = onAirSince?.showId === showId ? onAirSince.at : Date.now();
onAirSince = { showId, at: since };

const error = ref<string | null>(null);
const ending = ref(false);

const socket = useStreamSocket({
  onError: (message) => {
    error.value = message;
  },
});
// ─── Audio input gone (flow screen `studio-onair-noinput`) ──────────────────
// The device's track ended: listeners hear silence. The notice and Audio
// input stay until a new input sends audio.
const inputGone = ref(false);
const pickedInput = ref('');
audioCapture.setOnError((message) => {
  if (message !== DEVICE_ENDED_MESSAGE) {
    error.value = message;
    return;
  }
  inputGone.value = true;
  void audioCapture.listDevices();
});
// A plugged-in device shows up in the list.
function onDeviceChange() {
  if (inputGone.value) void audioCapture.listDevices();
}
navigator.mediaDevices?.addEventListener('devicechange', onDeviceChange);

async function pickInput() {
  const id = pickedInput.value;
  // Back to the placeholder, so picking the same input again is a change too.
  pickedInput.value = '';
  if (!id) return;
  const ok =
    id === 'screen'
      ? await audioCapture.captureScreenAudio()
      : await audioCapture.captureDevice(id);
  if (!ok) {
    error.value = audioCapture.error.value;
    return;
  }
  // The capture stopped the old recorder; the new one sends to the same socket.
  if (audioCapture.startRecording()) {
    inputGone.value = false;
    error.value = null;
  }
}

// ─── On air · hh:mm:ss · REC ────────────────────────────────────────────────
const now = ref(Date.now());
const clock = setInterval(() => (now.value = Date.now()), 1000);
const elapsed = computed(() => {
  const total = Math.max(0, Math.floor((now.value - since) / 1000));
  const parts = [Math.floor(total / 3600), Math.floor((total % 3600) / 60), total % 60];
  return parts.map((n) => String(n).padStart(2, '0')).join(':');
});

// REC only while the backend records this episode.
const recording = ref(false);
async function pollRecording() {
  try {
    const status = await recordingApi.status();
    recording.value = status.is_recording && status.show_id === showId;
  } catch {
    // Keep the last state.
  }
}
const recordingPoll = flow.recordStream.value ? setInterval(pollRecording, 3000) : null;
if (recordingPoll) void pollRecording();

// ─── Listeners now ──────────────────────────────────────────────────────────
const listeners = ref<number | null>(null);
async function pollListeners() {
  try {
    const metrics = await streamApi.metrics();
    listeners.value = metrics.online ? metrics.total_listeners : null;
  } catch {
    listeners.value = null;
  }
}
const listenerPoll = setInterval(pollListeners, 10000);
void pollListeners();

// ─── Connection ─────────────────────────────────────────────────────────────
// The upload telemetry also drives the automatic quality step-down that the
// old connection card ran, so a weak line still adapts.
const health = useUploadHealth(
  computed(() => socket.state.value === 'live'),
  audioCapture.streamBitsPerSecond
);
useAutoBitrate({
  mode: ref<UploadQualityMode>('auto'),
  currentKbps: computed(() => Math.round(audioCapture.streamBitsPerSecond.value / 1000)),
  bufferSeconds: health.bufferSeconds,
  ticks: health.ticks,
  onChange: (kbps) => audioCapture.setStreamBitsPerSecond(kbps * 1000),
});
const connection = computed(() => {
  switch (socket.state.value) {
    case 'live':
      if (health.verdict.value === 'good') return 'stable';
      return health.verdict.value === 'tight' ? 'tight' : 'too slow';
    case 'connecting':
    case 'connected':
      return 'reconnecting…';
    default:
      return 'lost';
  }
});

// ─── End Episode ────────────────────────────────────────────────────────────
async function endEpisode() {
  ending.value = true;
  // "stop" ends the stream and the recording on the backend.
  socket.stopStream();
  audioCapture.stopCapture();
  onAirSince = null;
  flow.reset();
  await router.push({ path: `/shows/${showId}`, state: { episodeEnded: showId } });
}

onUnmounted(() => {
  clearInterval(clock);
  clearInterval(listenerPoll);
  if (recordingPoll) clearInterval(recordingPoll);
  navigator.mediaDevices?.removeEventListener('devicechange', onDeviceChange);
});
</script>

<template>
  <div class="on-air">
    <h1 class="page-title">Studio</h1>
    <p class="pill status-banner live">
      On air · {{ elapsed }}<template v-if="recording"> · REC</template>
    </p>

    <section class="board-card">
      <div class="board-card-body">
        <DbMeter
          :media-stream="audioCapture.processedStream.value ?? audioCapture.mediaStream.value"
          label="Live level"
        />
      </div>
    </section>

    <dl class="stat-tiles">
      <div>
        <dt>Listeners now</dt>
        <dd>{{ listeners ?? '—' }}</dd>
      </div>
      <div>
        <dt>Connection</dt>
        <dd :class="['connection', connection === 'stable' ? 'ok' : 'warn']">{{ connection }}</dd>
      </div>
    </dl>

    <p v-if="socket.audioDropped.value" class="field-error" role="alert">
      The connection was down for more than 10 minutes. The recording misses some audio.
    </p>
    <template v-if="inputGone">
      <p class="field-error" role="alert">{{ DEVICE_ENDED_MESSAGE }}</p>
      <label class="input-field">
        <span class="label">Audio input</span>
        <select v-model="pickedInput" class="input-select" @change="pickInput">
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
    </template>
    <p v-if="error" class="field-error" role="alert">{{ error }}</p>
    <div>
      <BaseButton variant="danger" :loading="ending" @click="endEpisode">End Episode</BaseButton>
    </div>
  </div>
</template>

<style scoped>
.on-air {
  max-width: 800px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: var(--spacing-lg);
}

.page-title {
  margin: 0;
}

.connection.ok {
  color: var(--color-success);
}

.connection.warn {
  color: var(--color-warning);
}

.input-field {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
}

.input-field .label {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}

.input-select {
  padding: var(--spacing-sm) var(--spacing-md);
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  color: var(--color-text);
}

.field-error {
  color: var(--color-error);
  font-size: var(--font-size-sm);
  margin: 0;
}
</style>
