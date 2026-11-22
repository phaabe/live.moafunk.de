<script setup lang="ts">
import { computed, onUnmounted, ref, watchEffect } from 'vue';
import { useRouter } from 'vue-router';
import { BaseButton } from '@shared/components';
import { useHostFlow, useAudioCapture, DEVICE_ENDED_MESSAGE } from '@admin/composables';
import { useListeners } from '@admin/composables/useListeners';
import { recordingApi } from '@admin/api';
import { config } from '@/config';
import { AUDIO_DROPPED_MESSAGE, startLiveSession, useLiveSession } from '@admin/liveSession';
import LiveChatCard from '@admin/components/LiveChatCard.vue';
import LiveAudioCard from '@admin/components/live-panel/LiveAudioCard.vue';
import LiveConnectionCard from '@admin/components/live-panel/LiveConnectionCard.vue';
import LiveListenersCard from '@admin/components/live-panel/LiveListenersCard.vue';
import LiveMomentsCard from '@admin/components/live-panel/LiveMomentsCard.vue';
import PanelIcon from '@admin/components/live-panel/PanelIcon.vue';
import {
  ENDING_SOON_S,
  formatLeft,
  slotWindow,
  useMoments,
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
 * (#1052). It has no title. After End Episode the page shows the Wrap-up (it
 * watches the session's end; the page is already on the episode's path).
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
const monitoring = ref(false);
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
  <div :class="['on-air', 'host-page', { embedded }]">
    <h1 v-if="!embedded" class="page-title studio-title">Studio</h1>

    <section :class="['bar', { warn: barAlert }]" aria-label="Broadcast status">
      <!-- The flow's pill "On air · 00:42:10 · REC"; its parts sit in the bar's grid. -->
      <p class="pill on-air-pill">
        <span class="onair-badge"><span class="pulse" aria-hidden="true"></span>On air</span
        ><span class="sep"> · </span><span class="on-air-clock">{{ elapsed }}</span
        ><template v-if="recording"
          ><span class="sep"> · </span
          ><span class="rec"><i aria-hidden="true"></i>REC</span></template
        >
      </p>
      <span v-if="screenOn" class="wake-chip"
        ><PanelIcon name="monitor" :size="14" />Screen stays on</span
      >
      <span v-if="listeners.current.value !== null" class="bar-listeners"
        >{{ listeners.current.value }} listening</span
      >
      <div class="bar-title">
        <b>{{ show?.title }}</b
        ><span v-if="slotText">{{ slotText }}</span>
      </div>
      <span v-if="leftText" :class="['bar-left', { soon: endingSoon }]">{{ leftText }}</span>
      <p v-if="barAlert" class="bar-alert" role="status">
        <span class="warn-chip">{{ barAlert.chip }}</span
        ><span class="bar-alert-text">{{ barAlert.text }}</span>
      </p>
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
      <div
        v-if="progress !== null"
        class="progress"
        role="img"
        :aria-label="`Show progress ${progress} percent`"
      >
        <i :style="{ width: `${progress}%` }"></i>
      </div>
    </section>

    <p v-if="audioDropped" class="notice warn" role="alert">{{ AUDIO_DROPPED_MESSAGE }}</p>
    <p v-if="error" class="notice bad" role="alert">{{ error }}</p>

    <div class="cols">
      <div class="main">
        <LiveAudioCard
          :stream="audioCapture.processedStream.value ?? audioCapture.mediaStream.value"
          :analyser="audioCapture.analyserNode.value"
          :input-label="inputLabel"
          :input-gone="inputGone"
          :picker="inputGone || picking"
          :gain="gain"
          :monitor-src="config.stream.icecast"
          @change-input="changeInput"
          @update:gain="setGain"
          @monitoring="monitoring = $event"
        >
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
        </LiveAudioCard>
        <LiveConnectionCard
          :connection="connection"
          :upload-kbps="health.uploadKbps.value"
          :buffer-seconds="health.bufferSeconds.value"
          :rtt-ms="health.rttMs.value"
          :history="health.history.value"
          :target-kbps="targetKbps"
          :quality="quality"
          :last-step="lastStep"
          @update:quality="setQuality"
        />
      </div>
      <aside class="side">
        <LiveListenersCard
          :current="listeners.current.value"
          :peak="listeners.peak.value"
          :samples="listeners.samples.value"
          :now="now"
          :monitoring="monitoring"
        />
        <LiveChatCard :active="true" />
        <LiveMomentsCard :moments="moments.list.value" @mark="markMoment" />
      </aside>
    </div>
    <div class="dock" aria-hidden="true"></div>
  </div>
</template>

<style scoped>
.on-air {
  max-width: 1240px;
  margin: 0 auto;
}

.studio-title {
  font-size: 1.25rem;
  letter-spacing: 0.06em;
  color: #a8a8bc;
}

/* ---------- status bar ---------- */
.bar {
  position: relative;
  display: grid;
  grid-template-columns: auto auto minmax(0, 1fr) auto minmax(0, 1fr);
  grid-template-areas:
    'onair rec chip clock end'
    'title title title left end'
    'alert alert alert alert alert';
  align-items: center;
  gap: 10px 12px;
  padding: 20px 24px 26px;
  border: 1px solid #5a2424;
  border-radius: var(--radius-xl);
  background: #140a0d;
  overflow: hidden;
}

.bar.warn {
  border-color: #7a5a12;
  background: #171106;
}

.on-air-pill {
  display: contents;
}

.onair-badge {
  grid-area: onair;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  justify-self: start;
  padding: 6px 12px;
  border-radius: var(--radius-lg);
  background: #dc2626;
  color: #fff;
  font: 700 13px/1.2 var(--font-ui);
  letter-spacing: 0.1em;
  text-transform: uppercase;
}

.pulse {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: #fff;
  animation: on-air-pulse 1.6s infinite;
}

@keyframes on-air-pulse {
  0% {
    box-shadow: 0 0 0 0 rgba(255, 255, 255, 0.6);
  }
  70% {
    box-shadow: 0 0 0 8px rgba(255, 255, 255, 0);
  }
  100% {
    box-shadow: 0 0 0 0 rgba(255, 255, 255, 0);
  }
}

@media (prefers-reduced-motion: reduce) {
  .pulse {
    animation: none;
    box-shadow: 0 0 0 4px rgba(255, 255, 255, 0.25);
  }
}

.rec {
  grid-area: rec;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  justify-self: start;
  padding: 5px 10px;
  border: 1px solid var(--color-error);
  border-radius: var(--radius-full);
  color: #ffb3ab;
  font: 700 12px/1.2 var(--font-family);
  letter-spacing: 0.08em;
}

.rec i {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--color-error);
}

.wake-chip {
  grid-area: chip;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  justify-self: start;
  padding: 5px 10px;
  border: 1px solid #4a4a60;
  border-radius: var(--radius-full);
  color: #c4c4cc;
  font: 600 12px/1.2 var(--font-ui);
  white-space: nowrap;
}

.bar-listeners {
  display: none;
  grid-area: listen;
  justify-self: end;
  font: 700 13px var(--font-family);
  color: #ffd0cc;
  white-space: nowrap;
}

.bar-title {
  grid-area: title;
  min-width: 0;
}

.bar-title b {
  display: block;
  font: 700 22px/1.15 var(--font-family);
  color: #fff;
  overflow-wrap: anywhere;
}

.bar-title span {
  font-size: var(--font-size-sm);
  color: #d0c4c4;
}

.on-air-clock {
  grid-area: clock;
  justify-self: center;
  font: 700 clamp(2.75rem, 6vw, 3.75rem) / 1 var(--font-family);
  font-variant-numeric: tabular-nums;
  letter-spacing: 0.02em;
  color: #fff;
}

.bar.warn .on-air-clock {
  color: #fbbf24;
}

.bar-left {
  grid-area: left;
  justify-self: center;
  align-self: start;
  font: 700 14px var(--font-family);
  color: #ffb3ab;
  white-space: nowrap;
}

.bar-left.soon {
  color: #fbbf24;
}

.bar-alert {
  grid-area: alert;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
  margin: 0;
  color: #f4e6c8;
  font-size: var(--font-size-sm);
}

.warn-chip {
  display: inline-flex;
  align-items: center;
  padding: 5px 10px;
  border: 1px solid var(--color-warning);
  border-radius: var(--radius-lg);
  background: rgba(245, 158, 11, 0.16);
  color: #fbbf24;
  font: 700 12px/1.2 var(--font-ui);
  letter-spacing: 0.06em;
  text-transform: uppercase;
  white-space: nowrap;
}

.progress {
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  height: 6px;
  background: #3a1c1c;
}

.progress i {
  display: block;
  height: 100%;
  background: var(--color-error);
}

.bar.warn .progress {
  background: #3a2a10;
}

.bar.warn .progress i {
  background: var(--color-warning);
}

/* ---------- End Episode ---------- */
.end {
  grid-area: end;
  justify-self: end;
}

.end-button {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  min-width: 220px;
}

.end.confirming {
  grid-area: auto / 1 / auto / -1;
  justify-self: stretch;
  display: flex;
  flex-direction: column;
  gap: var(--spacing-sm);
  padding: var(--spacing-md);
  border: 2px solid #5c2020;
  border-radius: var(--radius-xl);
  background: #120808;
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

/* ---------- columns ---------- */
.cols > .main,
.cols > .side {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-lg);
}

.dock {
  display: none;
}

/* Narrow screens: the clock gets its own row. */
@media (max-width: 1000px) {
  .bar {
    grid-template-columns: auto auto minmax(0, 1fr);
    grid-template-areas:
      'onair rec chip'
      'clock clock clock'
      'title title left'
      'alert alert alert'
      'end end end';
  }

  .bar-left {
    justify-self: end;
    align-self: center;
  }

  .end {
    justify-self: stretch;
  }

  .end-button {
    width: 100%;
  }
}

/* Phones: listeners in the bar; Mark moment and End Episode in a dock at the
   bottom, in thumb reach (Mark moment is placed by its card). */
@media (max-width: 640px) {
  .on-air {
    padding-bottom: calc(96px + env(safe-area-inset-bottom, 0px));
  }

  .bar {
    grid-template-columns: auto auto minmax(0, 1fr);
    grid-template-areas:
      'onair rec listen'
      'clock clock clock'
      'title title left'
      'alert alert alert';
    gap: 8px;
    padding: 14px 16px 18px;
  }

  .bar-listeners {
    display: block;
  }

  .wake-chip {
    grid-area: auto / 1 / auto / -1;
  }

  .on-air-clock {
    font-size: 46px;
  }

  .bar-title b {
    font-size: 16px;
  }

  .bar-left {
    font-size: 13px;
  }

  .dock {
    display: block;
    position: fixed;
    left: 0;
    right: 0;
    bottom: 0;
    z-index: 20;
    height: calc(88px + env(safe-area-inset-bottom, 0px));
    border-top: 1px solid #2a2a42;
    background: #05050f;
  }

  .end {
    position: fixed;
    left: calc(50% + 5px);
    right: 16px;
    bottom: calc(16px + env(safe-area-inset-bottom, 0px));
    z-index: 21;
  }

  .end-button {
    min-width: 0;
    padding: 0 10px;
    gap: 8px;
    font-size: 14px;
    white-space: nowrap;
  }

  .end.confirming {
    left: 16px;
    z-index: 22;
    box-shadow: 0 -12px 32px rgba(0, 0, 0, 0.6);
  }
}
</style>
