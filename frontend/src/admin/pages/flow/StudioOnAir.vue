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
import BoardIcon from '@admin/components/BoardIcon.vue';

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
/** Colour of the Connection tile: stable green, reconnecting or slow amber, lost red. */
const connectionTone = computed(() => {
  if (connection.value === 'stable') return 'ok';
  return connection.value === 'lost' ? 'bad' : 'warn';
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
  <div class="on-air host-page">
    <h1 class="page-title studio-title">Studio</h1>
    <!-- Text stays "On air · 00:42:10 · REC"; the clock is drawn big to read from afar. -->
    <p class="pill status-banner live on-air-pill">
      <span class="dot" aria-hidden="true"></span><span class="on-air-label">On air</span
      ><span class="sep"> · </span><span class="on-air-clock">{{ elapsed }}</span
      ><template v-if="recording"
        ><span class="sep"> · </span
        ><span class="rec"><i aria-hidden="true"></i>REC</span></template
      >
    </p>
    <p v-if="socket.audioDropped.value" class="notice warn" role="alert">
      The connection was down for more than 10 minutes. The recording misses some audio.
    </p>
    <section v-if="inputGone" class="fix">
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
    </section>
    <p v-if="error" class="notice bad" role="alert">{{ error }}</p>
    <section class="board-card meter">
      <div class="board-card-body">
        <DbMeter
          :media-stream="audioCapture.processedStream.value ?? audioCapture.mediaStream.value"
          label="Live level"
        />
      </div>
    </section>
    <dl class="stat-tiles">
      <div>
        <dt><BoardIcon name="users" :size="14" />Listeners now</dt>
        <dd>{{ listeners ?? '—' }}</dd>
      </div>
      <div :class="['connection-tile', connectionTone]">
        <dt><BoardIcon name="wifi" :size="14" />Connection</dt>
        <dd class="connection">{{ connection }}</dd>
      </div>
    </dl>
    <div class="end">
      <BaseButton variant="danger" class="xl end-button" :loading="ending" @click="endEpisode"
        >End Episode</BaseButton
      >
    </div>
  </div>
</template>

<style scoped>
.on-air {
  max-width: 1120px;
  margin: 0 auto;
}

.studio-title {
  font-size: 1.25rem;
  letter-spacing: 0.06em;
  color: #a8a8bc;
}

.on-air-pill {
  display: grid;
  grid-template-columns: auto 1fr auto;
  grid-template-areas:
    'dot label rec'
    'clock clock clock';
  align-items: center;
  gap: 6px 10px;
  padding: var(--spacing-md) var(--spacing-lg) var(--spacing-lg);
}

.on-air-pill .dot {
  grid-area: dot;
}

.on-air-label {
  grid-area: label;
  font: 700 13px var(--font-ui);
  letter-spacing: 0.12em;
  text-transform: uppercase;
}

.on-air-clock {
  grid-area: clock;
  font: 700 clamp(3.5rem, 12vw, 7.5rem) / 1 var(--font-family);
  font-variant-numeric: tabular-nums;
  color: #fff;
}

.rec {
  grid-area: rec;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 9px;
  border-radius: 6px;
  background: var(--color-error);
  color: #000;
  font: 700 12px var(--font-family);
  letter-spacing: 0.1em;
}

.rec i {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #000;
}

.notice {
  margin: 0;
  padding: var(--spacing-md);
  border: 1px solid #5a4416;
  border-left: 4px solid var(--color-warning);
  border-radius: var(--radius-xl);
  background: rgba(245, 158, 11, 0.1);
  color: #fde7b0;
  font-weight: 600;
}

.notice.bad,
.fix {
  border-color: #6b2424;
  border-left-color: var(--color-error);
  background: rgba(239, 68, 68, 0.12);
  color: #ffd2cc;
}

.fix {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: var(--spacing-md);
  border: 1px solid #6b2424;
  border-left: 4px solid var(--color-error);
  border-radius: var(--radius-xl);
}

.fix .field-error {
  color: #ffd2cc;
  font-weight: 700;
}

.input-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.input-field .label {
  font: 700 11px var(--font-ui);
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: #ffc2bb;
}

.input-select {
  min-height: 48px;
  padding: 0 var(--spacing-md);
  background: #06060f;
  border: 2px solid var(--color-primary);
  border-radius: var(--radius-lg);
  color: #fff;
  font: 600 var(--font-size-md) var(--font-ui);
}

.stat-tiles {
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.stat-tiles dt {
  display: flex;
  align-items: center;
  gap: 6px;
}

.stat-tiles dd {
  font: 700 2.5rem/1.1 var(--font-family);
}

.stat-tiles dd.connection {
  font-size: 1.5rem;
  line-height: 1.7;
}

.connection-tile.ok dd {
  color: #4ade80;
}

.connection-tile.warn {
  border-color: #5a4416;
  background: rgba(245, 158, 11, 0.08);
}

.connection-tile.warn dd {
  color: #fbbf24;
}

.connection-tile.bad {
  border-color: #6b2424;
  background: rgba(239, 68, 68, 0.1);
}

.connection-tile.bad dd {
  color: #ff8a80;
}

.end {
  display: flex;
  justify-content: flex-end;
  padding-top: var(--spacing-md);
  border-top: 1px solid #1e1e34;
}

.end-button {
  min-width: 240px;
}

/* Phones: End Episode alone in a bar at the bottom, in thumb reach. */
@media (max-width: 640px) {
  .end {
    position: sticky;
    bottom: 0;
    padding: var(--spacing-sm) 0 var(--spacing-md);
    background: #000;
  }

  .end-button {
    width: 100%;
  }
}
</style>
