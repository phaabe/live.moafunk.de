<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue';
import { useRouter } from 'vue-router';
import { useHostFlow } from '@admin/composables';
import { streamApi, hostFlowApi } from '@admin/api';
import LiveChatCard from '@admin/components/LiveChatCard.vue';

// On Air for a pre-recorded set: waiting room, then playback status. Live
// episodes use StudioOnAir (see FlowOnAirPage).
const router = useRouter();
const flow = useHostFlow();
const show = computed(() => flow.show.value);

// ═══════════════════════════════════════════════════════════════════════════════
// Phase: waiting (before stream starts) vs streaming (after go-live)
// ═══════════════════════════════════════════════════════════════════════════════
const streamActive = ref(false);
const streamEnded = ref(false);

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

const goLiveLoading = ref(false);
const goLiveError = ref<string | null>(null);
const autoStarted = ref(false);

// ─── Go Live (transition from waiting → streaming) ─────────────────────────
async function handleGoLive() {
  goLiveLoading.value = true;
  goLiveError.value = null;
  flow.setShowStarted();

  try {
    if (!show.value?.id) throw new Error('No show selected');
    await hostFlowApi.goLive(show.value.id);
    transitionToStreaming();
  } catch (err) {
    goLiveError.value = err instanceof Error ? err.message : 'Failed to go live';
    goLiveLoading.value = false;
  }
}

function transitionToStreaming() {
  streamActive.value = true;
  startedAt.value = Date.now();
  elapsedInterval = setInterval(updateElapsed, 1000);

  statusInterval = setInterval(checkUploadStatus, 5000);
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
  updateProgress();
}

// Show-progress bar under the status bar: on-air elapsed vs scheduled duration.
const progressPct = ref<number | null>(null);

function updateProgress() {
  const start = getTargetDate();
  const end = getEndTargetDate();
  if (!start || !end || end.getTime() <= start.getTime()) {
    progressPct.value = null;
    return;
  }
  const totalSec = (end.getTime() - start.getTime()) / 1000;
  const elapsedSec = (Date.now() - startedAt.value) / 1000;
  progressPct.value = Math.max(0, Math.min(100, (elapsedSec / totalSec) * 100));
}

function stopElapsed() {
  if (elapsedInterval) {
    clearInterval(elapsedInterval);
    elapsedInterval = null;
  }
}

// ─── Stop streaming ─────────────────────────────────────────────────────────
const stopping = ref(false);

async function handleStopUpload() {
  stopping.value = true;
  try {
    await streamApi.stop();
  } catch (err) {
    console.warn('[FlowOnAir] Failed to stop stream:', err);
  }
  streamEnded.value = true;
  stopElapsed();
  if (statusInterval) {
    clearInterval(statusInterval);
    statusInterval = null;
  }
}

// ─── Stop stream & change settings ──────────────────────────────────────────
const changingSettings = ref(false);
async function handleStopAndChangeSettings() {
  changingSettings.value = true;
  stopElapsed();
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

// ─── Navigate back after stream ends ────────────────────────────────────────
function goToDashboard() {
  flow.reset();
  router.push('/dashboard');
}

// ─── Time left until the scheduled end_time ────────────────────────────────
// The end time never cuts a show: the stream goes on past it (and past
// midnight) until the host stops it. Only the clock goes away.
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
    remainingText.value = null;
    endTimeWarning.value = false;
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
  stopEndTimeInterval();
  if (statusInterval) {
    clearInterval(statusInterval);
    statusInterval = null;
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
        <p v-if="goLiveError" class="stream-error">{{ goLiveError }}</p>
        <button class="btn-primary" @click="goToDashboard">Done</button>
      </div>
    </template>

    <!-- ═══════════════════════════════════════════════════════════════════ -->
    <!-- STREAMING PHASE (stream is active)                                 -->
    <!-- ═══════════════════════════════════════════════════════════════════ -->
    <template v-else-if="streamActive">
      <!-- Live Panel 2.0 shell (#273). Reading order: status bar → analyzers →
           connection & upload → live chat. Stop is the only prominent control. -->

      <!-- 1 · Status bar -->
      <div class="panel-card">
        <div class="status-bar">
          <span class="status-dot live"></span>
          <span class="status-label">LIVE</span>
          <span class="status-spacer"></span>
          <span class="clock-group">
            <span class="clock-label">On air</span>
            <span class="clock-value">{{ elapsedText }}</span>
          </span>
          <template v-if="remainingText !== null">
            <span class="clock-sep">·</span>
            <span :class="['clock-group', { warning: endTimeWarning }]">
              <span class="clock-label">{{ endTimeWarning ? '⚠ Ends in' : 'Ends in' }}</span>
              <span class="clock-value">{{ remainingText }}</span>
            </span>
          </template>
          <button
            class="btn-icon"
            title="Stop and change settings"
            aria-label="Stop and change settings"
            :disabled="stopping || changingSettings"
            @click="handleStopAndChangeSettings"
          >
            ⚙
          </button>
          <button class="btn-stop-danger" :disabled="stopping" @click="handleStopUpload">
            {{ stopping ? 'Stopping…' : '⏹ Stop' }}
          </button>
        </div>
        <p v-if="goLiveError" class="stream-error">{{ goLiveError }}</p>
        <div v-if="progressPct !== null" class="show-progress">
          <div class="show-progress-fill" :style="{ width: progressPct + '%' }"></div>
        </div>
      </div>

      <!-- Upload mode: passive monitoring -->
      <div class="panel-card slot-card">
        <div class="upload-streaming-status">
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
      </div>

      <!-- 4 · Live chat — Telegram discussion group bridge (#278) -->
      <LiveChatCard :active="streamActive" />
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
          ><template v-if="show?.end_time">–{{ show.end_time }}</template> · 📁 Pre-recorded ·
          Berlin time
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

        <div class="waiting-status-row">
          <span class="upload-status">
            <span class="upload-ready-icon">✓</span>
            <span>Your pre-recorded set is ready to go</span>
          </span>
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
  max-width: 720px;
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
/* ── Status bar (Live Panel 2.0) ── */
.status-bar {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  flex-wrap: wrap;
  padding: var(--spacing-sm) var(--spacing-md);
}

.status-spacer {
  flex: 1 1 auto;
}

.listener-chip {
  font-family: var(--font-ui);
  font-size: var(--font-size-sm);
  color: var(--color-text);
  white-space: nowrap;
}

.listener-chip.muted {
  color: var(--color-text-muted);
}

.clock-group {
  display: inline-flex;
  align-items: baseline;
  gap: var(--spacing-xs);
}

.clock-label {
  font-family: var(--font-ui);
  font-size: var(--font-size-xs);
  color: var(--color-text-muted);
  white-space: nowrap;
}

.clock-value {
  font-size: var(--font-size-lg);
  font-weight: var(--font-weight-bold);
  font-variant-numeric: tabular-nums;
  color: var(--color-text);
}

.clock-sep {
  color: var(--color-border-light);
}

/* Amber warning carries over from the old end-time banner (< 5 min). */
.clock-group.warning .clock-label,
.clock-group.warning .clock-value {
  color: var(--color-warning);
}

.btn-icon {
  background: none;
  border: 1px solid var(--color-border-light);
  border-radius: var(--radius-md);
  color: var(--color-text);
  padding: 4px 10px;
  font-size: var(--font-size-md);
  line-height: 1.2;
  cursor: pointer;
  transition: all var(--transition-fast);
}

.btn-icon:hover:not(:disabled) {
  background: var(--color-surface-alt);
}

.btn-icon:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* The only prominent stream control (locked design decision). */
.btn-stop-danger {
  background: var(--color-error);
  color: #fff;
  border: none;
  padding: var(--spacing-xs) var(--spacing-md);
  border-radius: var(--radius-md);
  font-family: var(--font-family);
  font-size: var(--font-size-sm);
  font-weight: var(--font-weight-bold);
  cursor: pointer;
  transition: opacity var(--transition-fast);
}

.btn-stop-danger:hover:not(:disabled) {
  opacity: 0.85;
}

.btn-stop-danger:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.stream-error {
  margin: 0;
  padding: 0 var(--spacing-md) var(--spacing-sm);
  font-size: var(--font-size-xs);
  color: var(--color-error);
}

/* 4 px show-progress bar: on-air elapsed vs scheduled duration. */
.show-progress {
  height: 4px;
  background: var(--color-surface-alt);
}

.show-progress-fill {
  height: 100%;
  background: var(--color-error);
  transition: width 1s linear;
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

/* REC chip = the record toggle (no separate control card). */
.rec-chip {
  display: inline-flex;
  align-items: center;
  gap: var(--spacing-xs);
  padding: 2px 10px;
  border: none;
  border-radius: var(--radius-full);
  background: var(--color-error-bg);
  color: var(--color-error);
  font-family: var(--font-ui);
  font-size: var(--font-size-xs);
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  cursor: pointer;
  transition: opacity var(--transition-fast);
}

.rec-chip.off {
  background: var(--color-surface-alt);
  color: var(--color-text-muted);
  font-weight: var(--font-weight-medium);
}

.rec-chip:hover:not(:disabled) {
  opacity: 0.8;
}

.rec-chip:disabled {
  opacity: 0.5;
  cursor: not-allowed;
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

/* ── Card slots (analyzers · connection & upload · live chat) ── */
.analyzer-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: var(--spacing-lg);
  margin-bottom: var(--spacing-lg);
}

.analyzer-grid .panel-card {
  margin-bottom: 0;
}

.slot-card {
  padding: var(--spacing-md) var(--spacing-lg);
}

.slot-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--spacing-sm);
  margin-bottom: var(--spacing-sm);
}

.slot-title {
  font-family: var(--font-ui);
  font-size: var(--font-size-sm);
  font-weight: var(--font-weight-medium);
  color: var(--color-text-muted);
}

.slot-badge {
  font-size: var(--font-size-xs);
  color: var(--color-text-muted);
  padding: 2px 8px;
  border-radius: var(--radius-full);
  border: 1px solid var(--color-border);
  white-space: nowrap;
}

.slot-hint {
  margin: 0;
  font-family: var(--font-ui);
  font-size: var(--font-size-xs);
  color: var(--color-text-muted);
}

.slot-meta {
  margin: var(--spacing-sm) 0 0;
  font-family: var(--font-ui);
  font-size: var(--font-size-xs);
  color: var(--color-text-muted);
}

.slot-readout {
  font-size: var(--font-size-xs);
  font-variant-numeric: tabular-nums;
  color: var(--color-text-muted);
  white-space: nowrap;
}

/* Fader row under an analyzer (input gain / monitor volume). */
.fader-row {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  margin-top: var(--spacing-sm);
}

.fader-label {
  font-family: var(--font-ui);
  font-size: var(--font-size-xs);
  color: var(--color-text-muted);
  white-space: nowrap;
}

.fader-range {
  flex: 1 1 auto;
  accent-color: var(--color-primary);
}

.fader-value {
  min-width: 42px;
  text-align: right;
  font-size: var(--font-size-xs);
  font-variant-numeric: tabular-nums;
  color: var(--color-text);
}

/* dB meter shown during the waiting countdown */
.waiting-meter {
  margin-top: var(--spacing-lg);
  max-width: 360px;
  margin-left: auto;
  margin-right: auto;
}

/* Upload streaming status (pre-recorded monitoring row) */
.upload-streaming-status {
  display: flex;
  align-items: center;
  gap: var(--spacing-md);
}

.upload-status-body {
  flex: 1 1 auto;
  min-width: 0;
}

.upload-status-text {
  font-weight: var(--font-weight-bold);
  color: var(--color-text);
  margin: 0;
}

.upload-status-hint {
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
  margin: 0;
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
