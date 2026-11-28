<script setup lang="ts">
import { computed, onUnmounted, ref, watchEffect } from 'vue';
import { useRouter } from 'vue-router';
import { BaseButton } from '@shared/components';
import { useHostFlow, useAudioCapture, DEVICE_ENDED_MESSAGE } from '@admin/composables';
import { useListeners } from '@admin/composables/useListeners';
import { recordingApi } from '@admin/api';
import { config } from '@/config';
import { AUDIO_DROPPED_MESSAGE, startLiveSession, useLiveSession } from '@admin/liveSession';
import LivePanel from '@admin/components/live-panel/LivePanel.vue';
import PanelIcon from '@admin/components/live-panel/PanelIcon.vue';
import {
  ENDING_SOON_S,
  formatLeft,
  slotWindow,
  useMoments,
  type PanelAudio,
  type PanelBar,
  type PanelLink,
} from '@admin/components/live-panel/onAir';

/**
 * On Air for a live episode (flow screen `studio-onair`), drawn as the live
 * panel (#1053): status bar, Audio, Connection, Listeners, Live chat and
 * Moments. Only End Episode ends it: the end time never does. A dropped
 * connection keeps this screen while the socket reconnects by itself, and
 * End Episode still works.
 *
 * The broadcast's state lives in the live session (liveSession.ts, #1063),
 * not here: it goes on while the host opens other admin pages, and this
 * screen shows it again when it opens.
 *
 * `embedded`: the live panel as the Show time tab of the host's episode page
 * (#1052). It has no title. After End Episode the page shows the Recording tab
 * (it watches the session's end; the page is already on the episode's path).
 * `shown`: false while the page shows another tab; the on-air bar then shows.
 */
const props = withDefaults(defineProps<{ embedded?: boolean; shown?: boolean }>(), {
  shown: true,
});
const router = useRouter();
const flow = useHostFlow();
const audioCapture = useAudioCapture();
const showId = flow.showId.value!;

// Go Live started it; this returns it (or starts it, the clock from now).
const live = startLiveSession(
  showId,
  props.embedded ? `/shows/${showId}` : `/stream/on-air?show=${showId}`
);
const { now, elapsed, error, inputGone, screenOn, audioDropped } = live;
const { health, quality, targetKbps, lastStep, setQuality, connection, downFor } = live;

// The on-air bar hides while this panel is on screen.
const { panelShown } = useLiveSession();
watchEffect(() => {
  panelShown.value = props.shown;
});

const ending = ref(false);

// ─── Audio input gone (flow screen `studio-onair-noinput`) ──────────────────
// The device's track ended: listeners hear silence. The notice and Audio
// input stay until a new input sends audio. The host can also change the
// input while it works (Change in the Audio card): the same picker.
const picking = ref(false);
// A plugged-in device shows up in the list, also one plugged in while the
// host was on another page.
if (inputGone.value) void audioCapture.listDevices();
function onDeviceChange() {
  if (inputGone.value || picking.value) void audioCapture.listDevices();
}
navigator.mediaDevices?.addEventListener('devicechange', onDeviceChange);

function changeInput() {
  picking.value = true;
  void audioCapture.listDevices();
}

async function pickInput(event: Event) {
  const select = event.target as HTMLSelectElement;
  const id = select.value;
  // Back to the placeholder, so picking the same input again is a change too.
  // Set on the element: the picker is in the Audio card's slot, which a
  // change here does not re-render.
  select.value = '';
  if (!id) return;
  if (await live.changeInput(id)) picking.value = false;
}

const inputLabel = computed(() => {
  const id = audioCapture.selectedDeviceId.value;
  if (id === 'screen') return 'Screen audio';
  return audioCapture.devices.value.find((d) => d.deviceId === id)?.label || 'Audio input';
});

// ─── On air · hh:mm:ss · REC ────────────────────────────────────────────────
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

// ─── The slot: ends in, show progress, the last 5 minutes ───────────────────
// The slot end only informs: the stream goes on until End Episode.
const show = computed(() => flow.show.value);
const slot = computed(() => slotWindow(show.value, flow.berlinToUtcDate));
const slotText = computed(() =>
  show.value?.start_time && show.value.end_time
    ? `${show.value.start_time}–${show.value.end_time}`
    : ''
);
const secondsLeft = computed(() => {
  const end = slot.value.end;
  return end ? Math.floor((end.getTime() - now.value) / 1000) : null;
});
const leftText = computed(() => {
  const left = secondsLeft.value;
  if (left === null) return '';
  return left > 0 ? `ends in ${formatLeft(left)}` : `over by ${formatLeft(-left)}`;
});
/** The last 5 minutes of the slot, or past its end. */
const endingSoon = computed(() => secondsLeft.value !== null && secondsLeft.value <= ENDING_SOON_S);
const progress = computed(() => {
  const { start, end } = slot.value;
  if (!start || !end || end <= start) return null;
  const part = (now.value - start.getTime()) / (end.getTime() - start.getTime());
  return Math.round(Math.max(0, Math.min(1, part)) * 100);
});

// ─── Listeners, moments ─────────────────────────────────────────────────────
// The screen wake lock is the session's (screenOn).
const listeners = useListeners(showId);
const moments = useMoments(showId);
function markMoment() {
  moments.mark(live.elapsedSeconds.value);
}

// ─── Audio ──────────────────────────────────────────────────────────────────
const gain = computed(() => audioCapture.inputVolume.value);
function setGain(value: number) {
  audioCapture.setInputVolume(value);
}

/** The status bar's problem line: the connection first, then the slot end. */
const barAlert = computed<{ chip: string; text: string } | null>(() => {
  if (connection.value === 'reconnecting…') {
    return {
      chip: `Reconnecting · ${downFor.value}`,
      text: 'The connection dropped. Your audio waits in this browser and goes out when it is back.',
    };
  }
  if (connection.value === 'lost') {
    return { chip: 'Connection lost', text: 'Listeners may hear silence. See Connection.' };
  }
  const left = secondsLeft.value;
  const endTime = show.value?.end_time;
  if (left === null || !endTime) return null;
  if (left > 0 && left <= ENDING_SOON_S) {
    return {
      chip: `Ends in ${formatLeft(left)}`,
      text: `Your slot ends at ${endTime}. Say goodbye to your listeners.`,
    };
  }
  if (left <= 0) {
    return {
      chip: `Over by ${formatLeft(-left)}`,
      text: `Your slot ended at ${endTime}. The stream goes on until you end the episode.`,
    };
  }
  return null;
});

// ─── The panel's values ─────────────────────────────────────────────────────
const bar = computed<PanelBar>(() => ({
  badge: 'live',
  clock: elapsed.value,
  rec: recording.value,
  screenOn: screenOn.value,
  listeners: listeners.current.value,
  title: show.value?.title ?? '',
  slot: slotText.value,
  left: leftText.value,
  soon: endingSoon.value,
  progress: progress.value,
  alert: barAlert.value,
}));
const audio = computed<PanelAudio>(() => ({
  stream: audioCapture.processedStream.value ?? audioCapture.mediaStream.value,
  analyser: audioCapture.analyserNode.value,
  inputLabel: inputLabel.value,
  inputGone: inputGone.value,
  picker: inputGone.value || picking.value,
  gain: gain.value,
  monitorSrc: config.stream.icecast,
}));
const link = computed<PanelLink>(() => ({
  connection: connection.value,
  uploadKbps: health.uploadKbps.value,
  bufferSeconds: health.bufferSeconds.value,
  rttMs: health.rttMs.value,
  history: health.history.value,
  targetKbps: targetKbps.value,
  quality: quality.value,
  lastStep: lastStep.value,
}));

// ─── End Episode ────────────────────────────────────────────────────────────
// The first End Episode only asks; the stream and the recording go on until
// the second one.
const confirming = ref(false);
async function endEpisode() {
  ending.value = true;
  const id = await live.end();
  if (!props.embedded) await router.push({ path: `/shows/${id}`, state: { episodeEnded: id } });
}

onUnmounted(() => {
  panelShown.value = false;
  if (recordingPoll) clearInterval(recordingPoll);
  navigator.mediaDevices?.removeEventListener('devicechange', onDeviceChange);
});
</script>

<template>
  <LivePanel
    :embedded="embedded"
    :bar="bar"
    :audio="audio"
    :link="link"
    :listeners="{
      current: listeners.current.value,
      peak: listeners.peak.value,
      samples: listeners.samples.value,
    }"
    :now="now"
    :moments="moments.list.value"
    :chat-active="true"
    @change-input="changeInput"
    @update:gain="setGain"
    @update:quality="setQuality"
    @mark="markMoment"
  >
    <template #end>
      <!-- End Episode asks once (flow screen studio-onair-end): one slip must not end the broadcast. -->
      <div :class="['end', { confirming }]">
        <template v-if="confirming">
          <p class="end-question">End the episode?</p>
          <div class="end-actions">
            <BaseButton
              variant="secondary"
              class="xl"
              :disabled="ending"
              @click="confirming = false"
              >Keep on air</BaseButton
            >
            <BaseButton variant="danger" class="xl" :loading="ending" @click="endEpisode"
              >End Episode</BaseButton
            >
          </div>
        </template>
        <BaseButton v-else variant="danger" class="xl end-button" @click="confirming = true"
          ><PanelIcon name="stop" :size="16" />End Episode</BaseButton
        >
      </div>
    </template>
    <template #notices>
      <p v-if="audioDropped" class="notice warn" role="alert">{{ AUDIO_DROPPED_MESSAGE }}</p>
      <p v-if="error" class="notice bad" role="alert">{{ error }}</p>
    </template>
    <template #input>
      <section v-if="inputGone || picking" :class="['fix', { gone: inputGone }]">
        <p v-if="inputGone" class="field-error" role="alert">{{ DEVICE_ENDED_MESSAGE }}</p>
        <label class="input-field">
          <span class="label">Audio input</span>
          <select class="input-select" @change="pickInput">
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
        <BaseButton v-if="!inputGone" variant="secondary" @click="picking = false"
          >Keep this input</BaseButton
        >
      </section>
    </template>
  </LivePanel>
</template>

<style scoped>
.end-button {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  min-width: 220px;
}

.end-question {
  margin: 0;
  font: 700 1.25rem var(--font-family);
  color: #fff;
}

.end-actions {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--spacing-sm);
}

/* ---------- notices ---------- */
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
.fix.gone {
  border-color: #6b2424;
  border-left-color: var(--color-error);
  background: rgba(239, 68, 68, 0.12);
  color: #ffd2cc;
}

/* ---------- input picker (in the Audio card) ---------- */
.fix {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: var(--spacing-md);
  border: 1px solid #34344c;
  border-left: 4px solid var(--color-primary);
  border-radius: var(--radius-xl);
  background: #0a0a1c;
}

.fix.gone {
  border: 1px solid #6b2424;
  border-left: 4px solid var(--color-error);
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
  color: #a8a8bc;
}

.fix.gone .input-field .label {
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

@media (max-width: 1000px) {
  .end-button {
    width: 100%;
  }
}

@media (max-width: 640px) {
  .end-button {
    min-width: 0;
    padding: 0 10px;
    gap: 8px;
    font-size: 14px;
    white-space: nowrap;
  }
}
</style>
