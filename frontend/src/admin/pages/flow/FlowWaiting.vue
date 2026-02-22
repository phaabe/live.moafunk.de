<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue';
import { useRouter } from 'vue-router';
import { useHostFlow, useAudioCapture, useStreamSocket } from '@admin/composables';

const router = useRouter();
const flow = useHostFlow();
const show = computed(() => flow.show.value);
const isLive = computed(() => flow.uploadMode.value === 'live');

// ─── Countdown ──────────────────────────────────────────────────────────────
const remaining = ref<number>(0); // seconds remaining
const countdownText = ref('--:--:--');
type AlertState = 'normal' | 'warning' | 'critical';
const alertState = ref<AlertState>('normal');
let countdownInterval: ReturnType<typeof setInterval> | null = null;

function getTargetDate(): Date | null {
  if (!show.value?.date || !show.value?.start_time) return null;
  // Parse as Europe/Berlin timezone
  const dateStr = show.value.date; // "YYYY-MM-DD"
  const timeStr = show.value.start_time; // "HH:MM"
  try {
    // Create a string that the Date constructor can parse in the target timezone
    const isoStr = `${dateStr}T${timeStr}:00`;
    // Use Intl to determine the UTC offset for Europe/Berlin at this datetime
    const formatter = new Intl.DateTimeFormat('en-US', {
      timeZone: 'Europe/Berlin',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false,
    });
    // Parse the local date in Europe/Berlin by comparing with UTC
    const localDate = new Date(isoStr);
    // Get what "now" looks like in Berlin
    const berlinNow = new Date(
      new Date().toLocaleString('en-US', { timeZone: 'Europe/Berlin' })
    );
    const utcNow = new Date();
    const offsetMs = berlinNow.getTime() - utcNow.getTime();
    // Target in UTC = local target - offset
    return new Date(localDate.getTime() - offsetMs);
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
    return;
  }

  // Update alert state
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
  countdownText.value =
    `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
}

// ─── Beep (Web Audio API oscillator) ────────────────────────────────────────
let beepCtx: AudioContext | null = null;
let lastBeepSecond = -1;

function playBeep() {
  const sec = remaining.value;
  if (sec === lastBeepSecond) return; // don't repeat same second
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

// ─── Audio device status (live mode) ────────────────────────────────────────
// If in live mode, the audioCapture from FlowLive is still active.
// We create a new instance just to check device status — the actual stream
// was set up in FlowLive and persists.
const audioCapture = isLive.value ? useAudioCapture() : null;
const audioDeviceOk = computed(() => audioCapture?.isCapturing.value ?? false);

// ─── Stream socket (for live → go live) ─────────────────────────────────────
const streamSocket = useStreamSocket({
  onLive: () => {
    // Socket connected and streaming — navigate to streaming room
    flow.goToStep('streaming');
    router.push('/stream/streaming');
  },
  onError: (msg) => {
    goLiveError.value = msg;
  },
});

const goLiveLoading = ref(false);
const goLiveError = ref<string | null>(null);

const canGoLive = computed(() => remaining.value <= 0);

async function handleGoLive() {
  goLiveLoading.value = true;
  goLiveError.value = null;

  try {
    if (isLive.value) {
      // Live mode: connect WebSocket, wire audio data, navigate
      await streamSocket.connect();

      // Wire audioCapture data to stream socket
      // The audioCapture was created in FlowLive and the stream is still running.
      // We need to start a MediaRecorder and pipe data to streamSocket.
      if (audioCapture?.processedStream.value || audioCapture?.mediaStream.value) {
        const stream = audioCapture.processedStream.value || audioCapture.mediaStream.value;
        if (stream) {
          const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
            ? 'audio/webm;codecs=opus'
            : 'audio/webm';
          const recorder = new MediaRecorder(stream, {
            mimeType,
            audioBitsPerSecond: 192000,
          });
          recorder.ondataavailable = async (event) => {
            if (event.data.size > 0) {
              const buffer = await event.data.arrayBuffer();
              streamSocket.send(buffer);
            }
          };
          recorder.start(250);
        }
      }

      // Navigation happens via onLive callback when server confirms
    } else {
      // Upload mode: just navigate to streaming page
      // Backend handles the prerecorded playback
      flow.goToStep('streaming');
      router.push('/stream/streaming');
    }
  } catch (err) {
    goLiveError.value = err instanceof Error ? err.message : 'Failed to go live';
    goLiveLoading.value = false;
  }
}

// ─── Show info formatting ───────────────────────────────────────────────────
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

// ─── Lifecycle ──────────────────────────────────────────────────────────────
onMounted(() => {
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
});
</script>

<template>
  <div class="flow-waiting">
    <h1 class="waiting-title">Waiting Room</h1>

    <!-- Show info card -->
    <div class="show-card">
      <div class="show-card-row">
        <span class="show-card-label">Show</span>
        <span class="show-card-value">{{ show?.title }}</span>
      </div>
      <div class="show-card-row">
        <span class="show-card-label">Date</span>
        <span class="show-card-value">{{ formattedDate }}</span>
      </div>
      <div v-if="show?.start_time" class="show-card-row">
        <span class="show-card-label">Time</span>
        <span class="show-card-value">{{ show.start_time }} (Berlin)</span>
      </div>
      <div class="show-card-row">
        <span class="show-card-label">Mode</span>
        <span class="show-card-value">{{ isLive ? '🎙️ Live' : '📁 Pre-recorded' }}</span>
      </div>
    </div>

    <!-- Countdown -->
    <div :class="['countdown-section', alertState]">
      <p class="countdown-label">
        {{ remaining > 0 ? 'Show starts in' : 'Show time!' }}
      </p>
      <div class="countdown-display">{{ countdownText }}</div>
      <p v-if="alertState === 'warning'" class="countdown-alert warning-text">
        Less than 1 minute!
      </p>
      <p v-if="alertState === 'critical' && remaining > 0" class="countdown-alert critical-text">
        Starting soon!
      </p>
    </div>

    <!-- Mode-specific status -->
    <div class="mode-status">
      <template v-if="isLive">
        <div class="audio-status">
          <span :class="['status-dot', audioDeviceOk ? 'ok' : 'lost']"></span>
          <span v-if="audioDeviceOk">Audio device active</span>
          <span v-else class="status-lost-text">Audio device disconnected — return to setup</span>
        </div>
      </template>
      <template v-else>
        <div class="upload-status">
          <span class="upload-ready-icon">✓</span>
          <span>Your pre-recorded set is ready to go</span>
        </div>
      </template>
    </div>

    <!-- Go Live button -->
    <div class="go-live-section">
      <button :class="['btn-go-live', { ready: canGoLive }]" :disabled="!canGoLive || goLiveLoading"
        @click="handleGoLive">
        {{ goLiveLoading ? 'Connecting...' : isLive ? '🎙️ Go Live' : '▶ Start Show' }}
      </button>
      <p v-if="!canGoLive" class="go-live-hint">
        Button activates when countdown reaches zero
      </p>
      <p v-if="goLiveError" class="go-live-error">{{ goLiveError }}</p>
    </div>
  </div>
</template>

<style scoped>
.flow-waiting {
  max-width: 560px;
  margin: 0 auto;
  text-align: center;
}

.waiting-title {
  font-size: var(--font-size-2xl);
  font-weight: var(--font-weight-bold);
  margin: 0 0 var(--spacing-xl);
}

/* ─── Show card ──────────────────────────────────────────────────────────── */
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

/* ─── Countdown ──────────────────────────────────────────────────────────── */
.countdown-section {
  background: var(--color-surface);
  border: 2px solid var(--color-border);
  border-radius: var(--radius-xl);
  padding: var(--spacing-2xl) var(--spacing-xl);
  margin-bottom: var(--spacing-2xl);
  transition: all 0.3s ease;
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

/* ─── Mode status ────────────────────────────────────────────────────────── */
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

.status-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
}

.status-dot.ok {
  background: #22c55e;
  box-shadow: 0 0 6px rgba(34, 197, 94, 0.5);
}

.status-dot.lost {
  background: #ef4444;
  box-shadow: 0 0 6px rgba(239, 68, 68, 0.5);
}

.status-lost-text {
  color: #ef4444;
}

.upload-ready-icon {
  color: #22c55e;
  font-weight: var(--font-weight-bold);
}

/* ─── Go Live button ─────────────────────────────────────────────────────── */
.go-live-section {
  margin-top: var(--spacing-xl);
}

.btn-go-live {
  background: var(--color-surface-alt);
  color: var(--color-text-muted);
  border: 2px solid var(--color-border);
  padding: var(--spacing-lg) var(--spacing-3xl);
  border-radius: var(--radius-xl);
  font-family: var(--font-family);
  font-size: var(--font-size-xl);
  font-weight: var(--font-weight-bold);
  cursor: not-allowed;
  transition: all var(--transition-fast);
}

.btn-go-live.ready {
  background: var(--color-primary);
  color: var(--color-primary-text, #fff);
  border-color: var(--color-primary);
  cursor: pointer;
  animation: glow-primary 2s ease-in-out infinite;
}

.btn-go-live.ready:hover:not(:disabled) {
  transform: scale(1.02);
}

.btn-go-live:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

@keyframes glow-primary {

  0%,
  100% {
    box-shadow: 0 0 0 0 rgba(255, 152, 0, 0);
  }

  50% {
    box-shadow: 0 0 20px 4px rgba(255, 152, 0, 0.3);
  }
}

.go-live-hint {
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
  margin: var(--spacing-md) 0 0;
}

.go-live-error {
  color: #ef4444;
  font-size: var(--font-size-sm);
  margin: var(--spacing-md) 0 0;
}
</style>
