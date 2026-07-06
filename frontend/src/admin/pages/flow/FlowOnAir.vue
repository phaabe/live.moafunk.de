<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue';
import { useRouter } from 'vue-router';
import { useHostFlow, useAudioCapture, useStreamSocket } from '@admin/composables';
import { streamApi, recordingApi, hostFlowApi, type StreamMetrics } from '@admin/api';
import DbMeter from '@admin/components/DbMeter.vue';
import StreamPreviewPlayer from '@admin/components/StreamPreviewPlayer.vue';
import { config } from '@/config';

const router = useRouter();
const flow = useHostFlow();
const show = computed(() => flow.show.value);
const isLiveMode = computed(() => flow.uploadMode.value === 'live');

// Broadcaster preview (#175): only shown for a live broadcast and only when an
// Icecast /test mount is configured (empty until the Phase-2 stack is live).
const previewUrl = config.stream.icecastTestUrl;

// ═══════════════════════════════════════════════════════════════════════════════
// Phase: waiting (before stream starts) vs streaming (after go-live)
// ═══════════════════════════════════════════════════════════════════════════════
const streamActive = ref(false);
const streamEnded = ref(false);

// ─── Shared date formatting ─────────────────────────────────────────────────
function fmtDateTime(date: string, time: string): string {
  const d = new Date(date + 'T' + time + ':00');
  return (
    d.toLocaleDateString('en-US', {
      weekday: 'short',
      month: 'short',
      day: 'numeric',
      year: 'numeric',
    }) +
    ' · ' +
    time
  );
}

function computeEndDate(date: string, startTime: string, endTime: string): string {
  if (endTime <= startTime) {
    const d = new Date(date + 'T00:00:00');
    d.setDate(d.getDate() + 1);
    const yyyy = d.getFullYear();
    const mm = String(d.getMonth() + 1).padStart(2, '0');
    const dd = String(d.getDate()).padStart(2, '0');
    return `${yyyy}-${mm}-${dd}`;
  }
  return date;
}

const formattedStart = computed(() => {
  if (!show.value?.date || !show.value?.start_time) return '—';
  return fmtDateTime(show.value.date, show.value.start_time);
});

const formattedEnd = computed(() => {
  if (!show.value?.date || !show.value?.end_time) return '—';
  const endDate = show.value.start_time
    ? computeEndDate(show.value.date, show.value.start_time, show.value.end_time)
    : show.value.date;
  return fmtDateTime(endDate, show.value.end_time);
});

const formattedDate = computed(() => {
  if (!show.value?.date) return '';
  try {
    const d = new Date(show.value.date + 'T00:00:00');
    return d.toLocaleDateString('en-US', {
      weekday: 'long',
      year: 'numeric',
      month: 'long',
      day: 'numeric',
    });
  } catch {
    return show.value.date;
  }
});

// ═══════════════════════════════════════════════════════════════════════════════
// WAITING PHASE
// ═══════════════════════════════════════════════════════════════════════════════

// ─── Countdown ──────────────────────────────────────────────────────────────
const remaining = ref<number>(0);
const countdownText = ref('--:--:--');
type AlertState = 'normal' | 'warning' | 'critical';
const alertState = ref<AlertState>('normal');
let countdownInterval: ReturnType<typeof setInterval> | null = null;

function getTargetDate(): Date | null {
  if (!show.value?.date || !show.value?.start_time) return null;
  try {
    return flow.berlinToUtcDate(show.value.date, show.value.start_time);
  } catch {
    return null;
  }
}

function updateCountdown() {
  const target = getTargetDate();
  if (!target) {
    countdownText.value = '--:--:--';
    remaining.value = 0;
    return;
  }

  const diff = Math.floor((target.getTime() - Date.now()) / 1000);
  remaining.value = diff;

  if (diff <= 0) {
    countdownText.value = '00:00:00';
    alertState.value = 'critical';
    // Auto-start the show
    if (!autoStarted.value && !goLiveLoading.value) {
      autoStarted.value = true;
      handleGoLive();
    }
    return;
  }

  if (diff <= 10) {
    alertState.value = 'critical';
    playBeep();
  } else if (diff <= 60) {
    alertState.value = 'warning';
  } else {
    alertState.value = 'normal';
  }

  const hours = Math.floor(diff / 3600);
  const minutes = Math.floor((diff % 3600) / 60);
  const seconds = diff % 60;
  countdownText.value = `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
}

// ─── Beep ───────────────────────────────────────────────────────────────────
let beepCtx: AudioContext | null = null;
let lastBeepSecond = -1;

function playBeep() {
  const sec = remaining.value;
  if (sec === lastBeepSecond) return;
  lastBeepSecond = sec;
  try {
    if (!beepCtx) beepCtx = new AudioContext();
    const osc = beepCtx.createOscillator();
    const gain = beepCtx.createGain();
    osc.type = 'sine';
    osc.frequency.value = sec <= 3 ? 880 : 660;
    gain.gain.value = 0.15;
    osc.connect(gain);
    gain.connect(beepCtx.destination);
    osc.start();
    osc.stop(beepCtx.currentTime + 0.12);
  } catch {
    // Audio context may be blocked
  }
}

// ─── Audio device status (live mode, waiting + streaming phases) ────────────
const audioCapture = isLiveMode.value ? useAudioCapture() : null;
const audioDeviceOk = computed(() => audioCapture?.isCapturing.value ?? false);

// ─── Stream socket ──────────────────────────────────────────────────────────
const streamSocket = useStreamSocket({
  onLive: () => {
    // Socket connected — transition from waiting → streaming
    transitionToStreaming();
  },
  onDisconnected: () => {
    if (streamActive.value && !streamEnded.value) {
      streamEnded.value = true;
      stopElapsed();
      stopMetrics();
    }
  },
  onError: (msg) => {
    goLiveError.value = msg;
  },
});

const goLiveLoading = ref(false);
const goLiveError = ref<string | null>(null);
const autoStarted = ref(false);

// ─── Recording option ───────────────────────────────────────────────────────
function toggleRecordStream() {
  flow.setRecordStream(!flow.recordStream.value);
}

// ─── Go Live (transition from waiting → streaming) ─────────────────────────
async function handleGoLive() {
  goLiveLoading.value = true;
  goLiveError.value = null;
  flow.setShowStarted();

  try {
    if (isLiveMode.value) {
      // Recording now starts automatically on the backend when the stream goes
      // live (keyed on show_id), so it survives a dropped tab / WS reconnect.
      await streamSocket.connect(false, show.value?.id);
      if (audioCapture) {
        audioCapture.setOnData((data) => streamSocket.send(data));
        audioCapture.startRecording();
      }
      // Navigation happens via onLive callback
    } else {
      if (!show.value?.id) throw new Error('No show selected');
      await hostFlowApi.goLive(show.value.id);
      transitionToStreaming();
    }
  } catch (err) {
    goLiveError.value = err instanceof Error ? err.message : 'Failed to go live';
    goLiveLoading.value = false;
  }
}

function transitionToStreaming() {
  streamActive.value = true;
  startedAt.value = Date.now();
  elapsedInterval = setInterval(updateElapsed, 1000);

  if (!isLiveMode.value) {
    statusInterval = setInterval(checkUploadStatus, 5000);
  }
  if (flow.recordStream.value) {
    pollRecordingStatus();
    recordingPollInterval = setInterval(pollRecordingStatus, 3000);
  }
  // Live listener/quality telemetry (#177) — only useful for a live broadcast.
  if (isLiveMode.value) {
    pollMetrics();
    metricsInterval = setInterval(pollMetrics, 10000);
  }
  if (show.value?.end_time) {
    updateEndTimeCountdown();
    endTimeInterval = setInterval(updateEndTimeCountdown, 1000);
  }

  // Stop waiting countdown
  if (countdownInterval) {
    clearInterval(countdownInterval);
    countdownInterval = null;
  }
}

const isDev = import.meta.env.DEV;

// ═══════════════════════════════════════════════════════════════════════════════
// STREAMING PHASE
// ═══════════════════════════════════════════════════════════════════════════════

// ─── Elapsed time ───────────────────────────────────────────────────────────
const startedAt = ref(Date.now());
const elapsedText = ref('00:00');
let elapsedInterval: ReturnType<typeof setInterval> | null = null;

function updateElapsed() {
  const diff = Math.floor((Date.now() - startedAt.value) / 1000);
  const h = Math.floor(diff / 3600);
  const m = Math.floor((diff % 3600) / 60);
  const s = diff % 60;
  elapsedText.value =
    h > 0
      ? `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
      : `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

function stopElapsed() {
  if (elapsedInterval) {
    clearInterval(elapsedInterval);
    elapsedInterval = null;
  }
}

// ─── Live Icecast telemetry (#177) ───────────────────────────────────────────
// Polled while streaming. Degrades gracefully: when the Icecast stack isn't live
// (or offline), the panels just show a dash instead of real numbers.
const metrics = ref<StreamMetrics | null>(null);
let metricsInterval: ReturnType<typeof setInterval> | null = null;

async function pollMetrics() {
  try {
    metrics.value = await streamApi.metrics();
  } catch {
    // Ignore — backend may not expose metrics yet / Icecast not configured.
  }
}

function stopMetrics() {
  if (metricsInterval) {
    clearInterval(metricsInterval);
    metricsInterval = null;
  }
}

const metricsOnline = computed(() => metrics.value?.online === true);
const listenerCount = computed(() => (metricsOnline.value ? metrics.value!.total_listeners : null));
const primaryMount = computed(() => metrics.value?.mounts?.[0] ?? null);
const audioQuality = computed(() => {
  if (!metricsOnline.value) return null;
  const m = primaryMount.value;
  if (!m) return null;
  const parts: string[] = [];
  if (m.bitrate) parts.push(`${m.bitrate} kbps`);
  if (m.samplerate) parts.push(`${(m.samplerate / 1000).toFixed(1)} kHz`);
  if (m.channels === 2) parts.push('stereo');
  else if (m.channels === 1) parts.push('mono');
  return parts.length ? parts.join(' · ') : null;
});

// ─── Stop streaming ─────────────────────────────────────────────────────────
const stopping = ref(false);

function handleStop() {
  stopping.value = true;
  streamSocket.stopStream();
  audioCapture?.stopCapture();
  if (isRecording.value) {
    recordingApi.stop().catch(() => {});
    isRecording.value = false;
  }
  streamEnded.value = true;
  stopElapsed();
  stopMetrics();
}

async function handleStopUpload() {
  stopping.value = true;
  try {
    await streamApi.stop();
  } catch (err) {
    console.warn('[FlowOnAir] Failed to stop stream:', err);
  }
  streamEnded.value = true;
  stopElapsed();
  stopMetrics();
  if (statusInterval) {
    clearInterval(statusInterval);
    statusInterval = null;
  }
}

// ─── Stop stream & change settings ──────────────────────────────────────────
const changingSettings = ref(false);
async function handleStopAndChangeSettings() {
  changingSettings.value = true;
  if (isLiveMode.value) {
    streamSocket.stopStream();
    audioCapture?.stopCapture();
  }
  if (isRecording.value) {
    recordingApi.stop().catch(() => {});
    isRecording.value = false;
  }
  stopElapsed();
  stopMetrics();
  if (statusInterval) {
    clearInterval(statusInterval);
    statusInterval = null;
  }
  await flow.stopStreamAndRevert();
  changingSettings.value = false;
  router.push(flow.showId.value ? `/shows/${flow.showId.value}` : '/stream/select');
}

// ─── Upload mode: status polling ────────────────────────────────────────────
const uploadStreamActive = ref(true);
let statusInterval: ReturnType<typeof setInterval> | null = null;

async function checkUploadStatus() {
  try {
    const status = await streamApi.status();
    uploadStreamActive.value = status.active === true;
    if (!status.active) {
      streamEnded.value = true;
      stopElapsed();
    }
  } catch {
    // Ignore polling errors
  }
}

// ─── Recording state ────────────────────────────────────────────────────────
const isRecording = ref(flow.recordStream.value);
const recordingElapsed = ref('');
let recordingPollInterval: ReturnType<typeof setInterval> | null = null;

async function pollRecordingStatus() {
  try {
    const status = await recordingApi.status();
    isRecording.value = status.is_recording;
    if (status.is_recording && status.elapsed_ms) {
      const sec = Math.floor(status.elapsed_ms / 1000);
      const m = Math.floor(sec / 60);
      const s = sec % 60;
      recordingElapsed.value = `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
    }
  } catch {
    // Ignore polling errors
  }
}

async function stopRecording() {
  try {
    await recordingApi.stop();
    isRecording.value = false;
  } catch (err) {
    console.warn('[FlowOnAir] Failed to stop recording:', err);
  }
}

// ─── Navigate back after stream ends ────────────────────────────────────────
function goToDashboard() {
  flow.reset();
  router.push('/dashboard');
}

// ─── Auto-end timer (based on show end_time) ───────────────────────────────
const remainingText = ref<string | null>(null);
const endTimeWarning = ref(false);
let endTimeInterval: ReturnType<typeof setInterval> | null = null;

function getEndTargetDate(): Date | null {
  if (!show.value?.date || !show.value?.end_time) return null;
  try {
    // Handle overnight shows (end time wraps past midnight)
    const endDateStr =
      show.value.start_time && show.value.end_time <= show.value.start_time
        ? (() => {
            const d = new Date(`${show.value!.date}T00:00:00`);
            d.setDate(d.getDate() + 1);
            return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
          })()
        : show.value.date;
    return flow.berlinToUtcDate(endDateStr, show.value.end_time);
  } catch {
    return null;
  }
}

function updateEndTimeCountdown() {
  const target = getEndTargetDate();
  if (!target) {
    remainingText.value = null;
    return;
  }

  const diff = Math.floor((target.getTime() - Date.now()) / 1000);

  if (diff <= 0) {
    remainingText.value = '00:00';
    endTimeWarning.value = false;
    if (!streamEnded.value && !stopping.value) {
      if (isLiveMode.value) {
        handleStop();
      } else {
        handleStopUpload();
      }
    }
    stopEndTimeInterval();
    return;
  }

  endTimeWarning.value = diff <= 300;

  const h = Math.floor(diff / 3600);
  const m = Math.floor((diff % 3600) / 60);
  const s = diff % 60;
  remainingText.value =
    h > 0
      ? `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
      : `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

function stopEndTimeInterval() {
  if (endTimeInterval) {
    clearInterval(endTimeInterval);
    endTimeInterval = null;
  }
}

// ═══════════════════════════════════════════════════════════════════════════════
// LIFECYCLE
// ═══════════════════════════════════════════════════════════════════════════════

onMounted(() => {
  // Start countdown (waiting phase)
  updateCountdown();
  countdownInterval = setInterval(updateCountdown, 1000);
});

onUnmounted(() => {
  if (countdownInterval) {
    clearInterval(countdownInterval);
    countdownInterval = null;
  }
  if (beepCtx) {
    beepCtx.close();
    beepCtx = null;
  }
  stopElapsed();
  stopMetrics();
  stopEndTimeInterval();
  if (statusInterval) {
    clearInterval(statusInterval);
    statusInterval = null;
  }
  if (recordingPollInterval) {
    clearInterval(recordingPollInterval);
    recordingPollInterval = null;
  }
  if (isRecording.value) {
    recordingApi.stop().catch(() => {});
  }
  if (isLiveMode.value && streamActive.value && !streamEnded.value) {
    streamSocket.stopStream();
    audioCapture?.stopCapture();
  }
});
</script>

<template>
  <div class="flow-on-air">
    <!-- ═══════════════════════════════════════════════════════════════════ -->
    <!-- STREAM ENDED                                                       -->
    <!-- ═══════════════════════════════════════════════════════════════════ -->
    <template v-if="streamEnded">
      <div class="stream-ended">
        <div class="ended-icon">✓</div>
        <h1 class="ended-title">Stream Ended</h1>
        <p class="ended-message">
          Your show <strong>{{ show?.title }}</strong> has finished.
        </p>
        <p class="ended-duration">Duration: {{ elapsedText }}</p>
        <button class="btn-primary" @click="goToDashboard">Done</button>
      </div>
    </template>

    <!-- ═══════════════════════════════════════════════════════════════════ -->
    <!-- STREAMING PHASE (stream is active)                                 -->
    <!-- ═══════════════════════════════════════════════════════════════════ -->
    <template v-else-if="streamActive">
      <!-- One on-air card: status header → end-time banner → body → actions -->
      <div class="panel-card">
        <div class="onair-header">
          <span class="status-dot live"></span>
          <span class="status-label">LIVE</span>
          <span v-if="isRecording" class="rec-chip">
            <span class="rec-dot"></span>
            REC
            <template v-if="recordingElapsed">{{ recordingElapsed }}</template>
          </span>
          <button v-if="isRecording" class="btn-stop-rec" @click="stopRecording">
            Stop recording
          </button>
          <span class="onair-spacer"></span>
          <span class="elapsed-timer">{{ elapsedText }}</span>
        </div>

        <div
          v-if="remainingText !== null"
          :class="['end-time-banner', { warning: endTimeWarning }]"
        >
          <span class="end-time-label">{{ endTimeWarning ? '⚠ Ending in' : 'Time remaining' }}</span>
          <span class="end-time-value">{{ remainingText }}</span>
        </div>

        <div class="onair-body">
          <p class="onair-show">
            {{ show?.title }}
            <span class="onair-meta">
              {{ formattedDate }}
              <template v-if="show?.start_time"> · {{ show.start_time }}</template>
              <template v-if="show?.end_time"> – {{ show.end_time }}</template>
            </span>
          </p>

          <!-- Live mode: level meter + self-monitor -->
          <template v-if="isLiveMode">
            <div v-if="audioCapture" class="audio-level-section">
              <DbMeter :media-stream="audioCapture.mediaStream.value" label="Audio Level" />
            </div>

            <!-- Broadcaster preview: hear the relay feed (#175). Hidden unless a
                 /test mount is configured. -->
            <StreamPreviewPlayer v-if="previewUrl" :src="previewUrl" />
          </template>

          <!-- Upload mode: passive monitoring -->
          <div v-else class="upload-streaming-status">
            <span :class="['status-dot', uploadStreamActive ? 'live' : 'offline']"></span>
            <div class="upload-status-body">
              <p class="upload-status-text">
                {{
                  uploadStreamActive
                    ? 'Your pre-recorded set is playing'
                    : 'Waiting for stream to start...'
                }}
              </p>
              <p class="upload-status-hint">
                The backend is handling playback automatically. You can close this page safely.
              </p>
            </div>
          </div>

          <div class="onair-actions">
            <button
              class="btn-change-settings"
              :disabled="stopping || changingSettings"
              :title="'Stop the current stream and reconfigure your show'"
              @click="handleStopAndChangeSettings"
            >
              {{ changingSettings ? 'Stopping...' : '⚠ Stop & change settings' }}
            </button>
            <button
              v-if="isLiveMode"
              class="btn-stop"
              :disabled="stopping"
              @click="handleStop"
            >
              {{ stopping ? 'Stopping...' : '⏹ Stop streaming' }}
            </button>
            <button v-else class="btn-stop" :disabled="stopping" @click="handleStopUpload">
              {{ stopping ? 'Stopping...' : '⏹ Stop show' }}
            </button>
          </div>
        </div>
      </div>

      <!-- Live telemetry (#177) + remaining placeholder -->
      <div class="future-panels">
        <div class="future-panel" :class="{ active: listenerCount !== null }">
          <span class="future-icon">👥</span>
          <span class="future-label">Listeners</span>
          <span v-if="listenerCount !== null" class="metric-value">{{ listenerCount }}</span>
          <span v-else class="future-badge">—</span>
        </div>
        <div class="future-panel" :class="{ active: audioQuality !== null }">
          <span class="future-icon">📊</span>
          <span class="future-label">Audio Quality</span>
          <span v-if="audioQuality" class="metric-value metric-value-sm">{{ audioQuality }}</span>
          <span v-else class="future-badge">—</span>
        </div>
        <div class="future-panel">
          <span class="future-icon">💬</span>
          <span class="future-label">Live Chat</span>
          <span class="future-badge">Coming soon</span>
        </div>
      </div>
    </template>

    <!-- ═══════════════════════════════════════════════════════════════════ -->
    <!-- WAITING PHASE (before stream starts)                               -->
    <!-- ═══════════════════════════════════════════════════════════════════ -->
    <template v-else>
      <!-- Waiting room: one centered card — meta line → countdown → status row -->
      <div class="panel-card waiting-card">
        <p class="waiting-meta">
          {{ show?.title }} · {{ formattedDate
          }}<template v-if="show?.start_time"> · {{ show.start_time }}</template
          ><template v-if="show?.end_time">–{{ show.end_time }}</template>
          · {{ isLiveMode ? '🎙 Live' : '📁 Pre-recorded' }} · Berlin time
        </p>

        <p class="countdown-label">
          {{ remaining > 0 ? 'Show starts in' : 'Show time!' }}
        </p>
        <div :class="['countdown-box', alertState]">
          <span class="countdown-display">{{ countdownText }}</span>
        </div>
        <p v-if="alertState === 'warning'" class="countdown-alert warning-text">
          Less than 1 minute — goes live automatically
        </p>
        <p v-if="alertState === 'critical' && remaining > 0" class="countdown-alert critical-text">
          Starting soon!
        </p>

        <!-- Recording toggle + device / upload readiness -->
        <div class="waiting-status-row">
          <template v-if="isLiveMode">
            <label class="record-checkbox-label" @click="toggleRecordStream">
              <span :class="['checkbox-icon', { checked: flow.recordStream.value }]">
                {{ flow.recordStream.value ? '☑' : '☐' }}
              </span>
              <span>Record this show</span>
            </label>
            <span class="audio-status">
              <span :class="['status-dot', audioDeviceOk ? 'ok' : 'lost']"></span>
              <span v-if="audioDeviceOk">Audio device active</span>
              <span v-else class="status-lost-text">
                Audio device disconnected — return to setup
              </span>
            </span>
          </template>
          <span v-else class="upload-status">
            <span class="upload-ready-icon">✓</span>
            <span>Your pre-recorded set is ready to go</span>
          </span>
        </div>
        <p v-if="isLiveMode" class="record-hint">
          Audio will be saved for later download &amp; editing
        </p>

        <!-- dB meter stays live throughout the countdown -->
        <div v-if="isLiveMode && audioCapture && audioDeviceOk" class="waiting-meter">
          <DbMeter :media-stream="audioCapture.mediaStream.value" label="Input Level" />
        </div>

        <!-- Auto-start status -->
        <div class="go-live-section">
          <p v-if="goLiveLoading" class="go-live-status">Connecting...</p>
          <p v-if="goLiveError" class="go-live-error">
            {{ goLiveError }}
            <button class="btn-retry" @click="autoStarted = false">Retry</button>
          </p>

          <!-- Dev-only: start stream without waiting for countdown -->
          <button
            v-if="isDev && !goLiveLoading && remaining > 0"
            class="btn-dev-start"
            @click="handleGoLive"
          >
            🛠 Start stream now (dev)
          </button>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.flow-on-air {
  max-width: 600px;
  margin: 0 auto;
}

/* ═══════════════════════════════════════════════════════════════════════════ */
/* STREAM ENDED                                                              */
/* ═══════════════════════════════════════════════════════════════════════════ */
.stream-ended {
  text-align: center;
  padding: var(--spacing-3xl) 0;
}

.ended-icon {
  width: 64px;
  height: 64px;
  border-radius: var(--radius-full);
  background: var(--color-success, #22c55e);
  color: white;
  font-size: 2rem;
  display: flex;
  align-items: center;
  justify-content: center;
  margin: 0 auto var(--spacing-xl);
}

.ended-title {
  font-size: var(--font-size-3xl);
  font-weight: var(--font-weight-bold);
  margin: 0 0 var(--spacing-md);
}

.ended-message {
  color: var(--color-text-muted);
  margin: 0 0 var(--spacing-sm);
}

.ended-duration {
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
  margin: 0 0 var(--spacing-2xl);
}

/* ═══════════════════════════════════════════════════════════════════════════ */
/* STREAMING PHASE                                                           */
/* ═══════════════════════════════════════════════════════════════════════════ */
.streaming-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--spacing-xl);
}

.stream-status {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
}

.status-dot {
  width: 12px;
  height: 12px;
  border-radius: 50%;
}

.status-dot.live {
  background: #ef4444;
  box-shadow: 0 0 8px rgba(239, 68, 68, 0.6);
  animation: pulse-live 1.5s ease-in-out infinite;
}

.status-dot.offline {
  background: var(--color-text-muted);
}

.status-dot.ok {
  background: #22c55e;
  box-shadow: 0 0 6px rgba(34, 197, 94, 0.5);
  width: 10px;
  height: 10px;
}

.status-dot.lost {
  background: #ef4444;
  box-shadow: 0 0 6px rgba(239, 68, 68, 0.5);
  width: 10px;
  height: 10px;
}

@keyframes pulse-live {
  0%,
  100% {
    opacity: 1;
  }

  50% {
    opacity: 0.5;
  }
}

.status-label {
  font-size: var(--font-size-sm);
  font-weight: var(--font-weight-bold);
  color: #ef4444;
  text-transform: uppercase;
  letter-spacing: 0.08em;
}

.elapsed-timer {
  font-size: var(--font-size-xl);
  font-weight: var(--font-weight-bold);
  font-variant-numeric: tabular-nums;
  color: var(--color-text);
}

/* Recording banner */
.recording-banner {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  background: rgba(239, 68, 68, 0.08);
  border: 1px solid rgba(239, 68, 68, 0.3);
  border-radius: var(--radius-md);
  padding: var(--spacing-sm) var(--spacing-md);
  margin-bottom: var(--spacing-xl);
}

.rec-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: #ef4444;
  animation: pulse-rec 1s ease-in-out infinite;
}

@keyframes pulse-rec {
  0%,
  100% {
    opacity: 1;
  }

  50% {
    opacity: 0.3;
  }
}

.rec-label {
  font-size: var(--font-size-xs);
  font-weight: var(--font-weight-bold);
  color: #ef4444;
  text-transform: uppercase;
  letter-spacing: 0.1em;
}

.rec-elapsed {
  font-size: var(--font-size-xs);
  font-variant-numeric: tabular-nums;
  color: var(--color-text-muted);
}

.btn-stop-rec {
  margin-left: auto;
  background: none;
  border: 1px solid rgba(239, 68, 68, 0.4);
  color: #ef4444;
  padding: 2px var(--spacing-sm);
  border-radius: var(--radius-sm);
  font-family: var(--font-family);
  font-size: var(--font-size-xs);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.btn-stop-rec:hover {
  background: rgba(239, 68, 68, 0.1);
}

/* End time countdown banner */
.end-time-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  padding: var(--spacing-sm) var(--spacing-md);
  margin-bottom: var(--spacing-xl);
}

.end-time-banner.warning {
  background: rgba(245, 158, 11, 0.08);
  border-color: rgba(245, 158, 11, 0.4);
  animation: pulse-warning 2s ease-in-out infinite;
}

@keyframes pulse-warning {
  0%,
  100% {
    border-color: rgba(245, 158, 11, 0.4);
  }

  50% {
    border-color: rgba(245, 158, 11, 0.8);
  }
}

.end-time-label {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}

.end-time-banner.warning .end-time-label {
  color: #f59e0b;
  font-weight: var(--font-weight-bold);
}

.end-time-value {
  font-size: var(--font-size-lg);
  font-weight: var(--font-weight-bold);
  font-variant-numeric: tabular-nums;
  color: var(--color-text);
}

.end-time-banner.warning .end-time-value {
  color: #f59e0b;
}

/* Show card (streaming) */
.show-card-compact {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: var(--spacing-md) var(--spacing-lg);
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: var(--spacing-xl);
}

.show-title {
  font-weight: var(--font-weight-bold);
  color: var(--color-text);
}

.show-meta {
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
}

/* Audio level (dB meter) */
.audio-level-section {
  margin-bottom: var(--spacing-xl);
}

/* dB meter shown during the waiting countdown */
.waiting-meter {
  margin-top: var(--spacing-lg);
  max-width: 360px;
  margin-left: auto;
  margin-right: auto;
}

/* Stop button */
.stop-section {
  margin-bottom: var(--spacing-xl);
  text-align: center;
}

.btn-stop {
  background: none;
  border: 2px solid #ef4444;
  color: #ef4444;
  padding: var(--spacing-sm) var(--spacing-2xl);
  border-radius: var(--radius-md);
  font-family: var(--font-family);
  font-size: var(--font-size-md);
  font-weight: var(--font-weight-bold);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.btn-stop:hover:not(:disabled) {
  background: rgba(239, 68, 68, 0.1);
}

.btn-stop:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* Upload streaming status */
.upload-streaming-status {
  text-align: center;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: var(--spacing-xl);
  margin-bottom: var(--spacing-xl);
}

.upload-status-dot {
  margin-bottom: var(--spacing-md);
}

.upload-status-text {
  font-weight: var(--font-weight-bold);
  color: var(--color-text);
  margin: 0 0 var(--spacing-sm);
}

.upload-status-hint {
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
  margin: 0;
}

/* Change settings */
.change-settings-section {
  margin-top: var(--spacing-2xl);
  padding-top: var(--spacing-xl);
  border-top: 1px solid var(--color-border);
  text-align: center;
}

.btn-change-settings {
  background: transparent;
  color: #ef4444;
  border: 2px solid #ef4444;
  padding: var(--spacing-sm) var(--spacing-xl);
  border-radius: var(--radius-md);
  font-family: var(--font-family);
  font-size: var(--font-size-sm);
  font-weight: var(--font-weight-bold);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.btn-change-settings:hover:not(:disabled) {
  background: #ef4444;
  color: #fff;
}

.btn-change-settings:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.change-settings-hint {
  font-size: var(--font-size-xs);
  color: var(--color-text-muted);
  margin: var(--spacing-sm) 0 0;
}

/* Future panels */
.future-panels {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: var(--spacing-md);
  margin-top: var(--spacing-xl);
}

@media (max-width: 480px) {
  .future-panels {
    grid-template-columns: 1fr;
  }
}

/* Metric-card styling, matching the show dashboard's status strip. */
.future-panel {
  background: var(--color-surface);
  border: 1px dashed var(--color-border);
  border-radius: var(--radius-lg);
  padding: var(--spacing-md) var(--spacing-lg);
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--spacing-xs);
  opacity: 0.6;
}

/* A panel backed by real live data — solid border, full opacity. */
.future-panel.active {
  opacity: 1;
  border-style: solid;
  border-color: var(--color-border);
}

.metric-value {
  font-size: var(--font-size-2xl);
  font-weight: var(--font-weight-bold);
  font-variant-numeric: tabular-nums;
  color: var(--color-text);
}

.metric-value-sm {
  font-size: var(--font-size-sm);
  font-weight: var(--font-weight-medium);
}

.future-icon {
  font-size: 1.1rem;
}

.future-label {
  font-family: var(--font-ui);
  font-size: var(--font-size-xs);
  font-weight: var(--font-weight-medium);
  letter-spacing: 0.05em;
  text-transform: uppercase;
  color: var(--color-text-muted);
}

.future-badge {
  font-size: var(--font-size-xs);
  color: var(--color-text-muted);
  background: var(--color-surface);
  padding: 2px 8px;
  border-radius: var(--radius-full);
  border: 1px solid var(--color-border);
}

/* ═══════════════════════════════════════════════════════════════════════════ */
/* WAITING PHASE                                                             */
/* ═══════════════════════════════════════════════════════════════════════════ */

.waiting-title {
  font-size: var(--font-size-2xl);
  font-weight: var(--font-weight-bold);
  margin: 0 0 var(--spacing-xl);
  text-align: center;
}

/* Show card (waiting) */
.show-card {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: var(--spacing-lg);
  display: flex;
  flex-direction: column;
  gap: var(--spacing-sm);
  margin-bottom: var(--spacing-2xl);
  text-align: left;
}

.show-card-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.show-card-label {
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
}

.show-card-value {
  font-weight: var(--font-weight-medium);
  color: var(--color-text);
}

/* Countdown */
.countdown-section {
  background: var(--color-surface);
  border: 2px solid var(--color-border);
  border-radius: var(--radius-xl);
  padding: var(--spacing-2xl) var(--spacing-xl);
  margin-bottom: var(--spacing-2xl);
  transition: all 0.3s ease;
  text-align: center;
}

.countdown-section.warning {
  border-color: #eab308;
  animation: pulse-yellow 1.5s ease-in-out infinite;
}

.countdown-section.critical {
  border-color: #ef4444;
  animation: pulse-red 1s ease-in-out infinite;
}

@keyframes pulse-yellow {
  0%,
  100% {
    box-shadow: 0 0 0 0 rgba(234, 179, 8, 0);
  }

  50% {
    box-shadow: 0 0 20px 4px rgba(234, 179, 8, 0.3);
  }
}

@keyframes pulse-red {
  0%,
  100% {
    box-shadow: 0 0 0 0 rgba(239, 68, 68, 0);
  }

  50% {
    box-shadow: 0 0 24px 6px rgba(239, 68, 68, 0.4);
  }
}

.countdown-label {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
  margin: 0 0 var(--spacing-md);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.countdown-display {
  font-size: 3.5rem;
  font-weight: var(--font-weight-bold);
  font-variant-numeric: tabular-nums;
  color: var(--color-text);
  letter-spacing: 0.04em;
  line-height: 1;
  margin-bottom: var(--spacing-sm);
}

.countdown-alert {
  font-size: var(--font-size-sm);
  font-weight: var(--font-weight-bold);
  margin: var(--spacing-sm) 0 0;
}

.warning-text {
  color: #eab308;
}

.critical-text {
  color: #ef4444;
}

/* Recording option */
.record-option {
  margin-bottom: var(--spacing-xl);
  text-align: center;
}

.record-checkbox-label {
  display: inline-flex;
  align-items: center;
  gap: var(--spacing-sm);
  font-size: var(--font-size-md);
  font-weight: var(--font-weight-medium);
  cursor: pointer;
  user-select: none;
}

.checkbox-icon {
  font-size: 1.3rem;
  color: var(--color-text-muted);
  transition: color var(--transition-fast);
}

.checkbox-icon.checked {
  color: #ef4444;
}

.record-hint {
  font-size: var(--font-size-xs);
  color: var(--color-text-muted);
  margin: var(--spacing-xs) 0 0;
}

/* Mode status (waiting) */
.mode-status {
  margin-bottom: var(--spacing-2xl);
}

.audio-status,
.upload-status {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--spacing-sm);
  font-size: var(--font-size-sm);
  color: var(--color-text);
}

.status-lost-text {
  color: #ef4444;
}

.upload-ready-icon {
  color: #22c55e;
  font-weight: var(--font-weight-bold);
}

/* Go-live / auto-start */
.go-live-section {
  margin-top: var(--spacing-xl);
  text-align: center;
}

.go-live-status {
  color: var(--color-text-muted);
  font-size: var(--font-size-md);
  margin: 0;
}

.go-live-error {
  color: #ef4444;
  font-size: var(--font-size-sm);
  margin: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--spacing-sm);
}

.btn-retry {
  background: none;
  border: 1px solid var(--color-border);
  color: var(--color-text);
  padding: var(--spacing-xs) var(--spacing-md);
  border-radius: var(--radius-md);
  font-family: var(--font-family);
  font-size: var(--font-size-sm);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.btn-retry:hover {
  border-color: var(--color-text-muted);
}

.btn-dev-start {
  margin-top: var(--spacing-lg);
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

.btn-dev-start:hover {
  background: var(--color-warning-hover, #d97706);
}

/* Shared */
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

.btn-primary:hover {
  opacity: 0.9;
}

/* ── Redesigned live panel (matches the show dashboard's design language) ── */
.panel-card {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-xl);
  overflow: hidden;
  margin-bottom: var(--spacing-lg);
}

.onair-header {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  padding: var(--spacing-md) var(--spacing-lg);
  border-bottom: 1px solid var(--color-border);
}

.onair-spacer {
  flex: 1 1 auto;
}

.rec-chip {
  display: inline-flex;
  align-items: center;
  gap: var(--spacing-xs);
  padding: 2px 10px;
  border-radius: var(--radius-full);
  background: var(--color-error-bg);
  color: var(--color-error);
  font-family: var(--font-ui);
  font-size: var(--font-size-xs);
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}

/* The banner sits flush inside the card, under the header. */
.panel-card .end-time-banner {
  border-radius: 0;
  margin: 0;
  border-left: none;
  border-right: none;
}

.onair-body {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
  padding: var(--spacing-md) var(--spacing-lg) var(--spacing-lg);
}

.onair-show {
  margin: 0;
  font-size: var(--font-size-md);
  font-weight: var(--font-weight-bold);
  color: var(--color-text);
}

.onair-meta {
  font-family: var(--font-ui);
  font-size: var(--font-size-sm);
  font-weight: 400;
  color: var(--color-text-muted);
}

.onair-actions {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: var(--spacing-sm);
  flex-wrap: wrap;
}

/* New horizontal layout for the pre-recorded monitoring row. */
.panel-card .upload-streaming-status {
  display: flex;
  align-items: center;
  gap: var(--spacing-md);
  text-align: left;
  padding: 0;
  margin: 0;
  background: none;
  border: none;
}

.upload-status-body {
  flex: 1 1 auto;
  min-width: 0;
}

.panel-card .upload-status-text {
  margin: 0;
}

.panel-card .upload-status-hint {
  margin: 0;
}

/* ── Waiting room card ── */
.waiting-card {
  padding: var(--spacing-xl);
  text-align: center;
}

.waiting-meta {
  margin: 0 0 var(--spacing-lg);
  font-family: var(--font-ui);
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}

.countdown-box {
  display: inline-block;
  border: 2px solid var(--color-border);
  border-radius: var(--radius-xl);
  padding: var(--spacing-md) var(--spacing-2xl);
  margin-bottom: var(--spacing-sm);
  transition: border-color 0.3s ease;
}

.countdown-box.warning {
  border-color: #eab308;
  animation: pulse-yellow 1.5s ease-in-out infinite;
}

.countdown-box.critical {
  border-color: #ef4444;
  animation: pulse-red 1s ease-in-out infinite;
}

.waiting-status-row {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--spacing-xl);
  flex-wrap: wrap;
  margin-top: var(--spacing-lg);
}

.waiting-card .record-checkbox-label {
  margin: 0;
}

.waiting-card .record-hint {
  margin: var(--spacing-xs) 0 0;
}

.waiting-card .waiting-meter {
  margin-top: var(--spacing-lg);
  text-align: left;
}
</style>
