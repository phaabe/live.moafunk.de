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
  type UploadQualityMode,
} from '@admin/composables';
import { recordingApi } from '@admin/api';
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
audioCapture.setOnError((message) => {
  error.value = message;
});

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
  if (recordingPoll) clearInterval(recordingPoll);
});
</script>

<template>
  <div class="on-air">
    <h1 class="page-title">Studio</h1>
    <p class="pill live">On air · {{ elapsed }}<template v-if="recording"> · REC</template></p>

    <DbMeter
      :media-stream="audioCapture.processedStream.value ?? audioCapture.mediaStream.value"
      label="Live level"
    />

    <dl class="facts">
      <div class="fact">
        <dt>Connection</dt>
        <dd :class="['connection', connection === 'stable' ? 'ok' : 'warn']">{{ connection }}</dd>
      </div>
    </dl>

    <p v-if="socket.audioDropped.value" class="field-error" role="alert">
      The connection was down for more than 10 minutes. The recording misses some audio.
    </p>
    <p v-if="error" class="field-error" role="alert">{{ error }}</p>
    <div>
      <BaseButton variant="danger" :loading="ending" @click="endEpisode">End Episode</BaseButton>
    </div>
  </div>
</template>

<style scoped>
.on-air {
  max-width: 560px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
}

.page-title {
  margin: 0;
}

.pill {
  align-self: flex-start;
  margin: 0;
  padding: var(--spacing-xs) var(--spacing-md);
  border-radius: var(--radius-md);
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  font-variant-numeric: tabular-nums;
}

.pill.live {
  border-color: var(--color-error);
  color: var(--color-error);
  font-weight: var(--font-weight-bold);
}

.facts {
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
}

.fact {
  display: flex;
  justify-content: space-between;
  gap: var(--spacing-md);
}

.fact dt {
  color: var(--color-text-muted);
}

.fact dd {
  margin: 0;
  font-variant-numeric: tabular-nums;
}

.connection.warn {
  color: var(--color-warning);
}

.field-error {
  color: var(--color-error);
  font-size: var(--font-size-sm);
  margin: 0;
}
</style>
