<script setup lang="ts">
import { computed } from 'vue';
import type { ShowDetail, SoundCloudStatus } from '../../api';
import type { ShowPhase } from '../../composables/useShowPhase';

/**
 * The status strip of the redesigned show dashboard: three compact metric
 * cards under the header.
 *
 *   1. Clock    — countdown to air / time on air / finished duration
 *   2. Mode     — streaming-mode toggle + preparation readiness (prep/broadcast)
 *                 or publishing progress (wrap-up)
 *   3. Announcements — slot overview (composer wiring lands with the
 *                 scheduled-announcements backend)
 *
 * Pure presentation; all side effects live on ShowDetailPage.
 */
const props = defineProps<{
  show: ShowDetail;
  phase: ShowPhase;
  mode: 'live' | 'upload';
  canManage: boolean;
  countdown: string;
  elapsed: string;
  /** Actual duration of the finished show, or null to fall back to schedule. */
  duration: string | null;
  recordingState: 'ready' | 'processing' | 'failed' | null;
  scStatus: SoundCloudStatus | null;
  /** Number of composed announcement slots (of 4). */
  announcementsReady: number;
  /** Render the streaming-mode card (false for UNHEARD, which has no host flow). */
  modeCard?: boolean;
}>();

const emit = defineEmits<{
  'select-live': [];
  'select-upload': [];
}>();

const clock = computed(() => {
  switch (props.phase) {
    case 'broadcast':
      return { label: 'On air since', value: props.elapsed, hint: null };
    case 'wrapup': {
      if (props.duration) {
        return {
          label: 'Duration',
          value: props.duration,
          hint: scheduledLabel.value ? `Scheduled ${scheduledLabel.value}` : null,
        };
      }
      // No recording length yet — fall back to the scheduled duration rather
      // than rendering a bare dash.
      return {
        label: 'Duration',
        value: scheduledLabel.value ?? '—',
        hint: scheduledLabel.value ? 'Scheduled — recording still processing' : null,
      };
    }
    default:
      return { label: 'On air in', value: props.countdown, hint: null };
  }
});

/** "2h" / "1h 30m" from the scheduled start/end times. */
const scheduledLabel = computed(() => {
  const s = props.show;
  if (!s.start_time || !s.end_time) return null;
  const [sh, sm] = s.start_time.split(':').map(Number);
  const [eh, em] = s.end_time.split(':').map(Number);
  let mins = eh * 60 + em - (sh * 60 + sm);
  if (mins <= 0) mins += 24 * 60; // overnight show
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  if (h && m) return `${h}h ${m}m`;
  if (h) return `${h}h`;
  return `${m}m`;
});

// ── Card 2: preparation readiness (prep/broadcast) ─────────────────────────
const hasFile = computed(() => !!props.show.prerecorded_key);
const confirmed = computed(() => !!props.show.prerecorded_confirmed_at);

const prepStatus = computed<{ label: string; done: boolean }>(() => {
  if (props.mode === 'live') {
    return { label: 'Ready to stream at air time', done: true };
  }
  if (!hasFile.value) return { label: 'No file uploaded yet', done: false };
  return confirmed.value
    ? { label: 'File uploaded & confirmed', done: true }
    : { label: 'Uploaded — not confirmed', done: false };
});

// ── Card 2: publishing progress (wrap-up) ──────────────────────────────────
const publishing = computed<{ label: string; cls: string; hint: string | null }>(() => {
  if (props.recordingState === 'failed') {
    return { label: 'Recording failed', cls: 'error', hint: null };
  }
  if (props.recordingState === 'processing') {
    return { label: 'Converting audio…', cls: 'busy', hint: 'Download unlocks when done' };
  }
  if (props.show.soundcloud_url) {
    return props.show.soundcloud_public
      ? { label: 'Published on SoundCloud', cls: 'done', hint: null }
      : { label: '🔒 Private on SoundCloud', cls: 'warn', hint: '1 step left — publish below' };
  }
  if (props.recordingState === 'ready') {
    return { label: 'Audio ready', cls: 'done', hint: 'Not on SoundCloud yet' };
  }
  return { label: 'No recording', cls: 'muted', hint: null };
});
</script>

<template>
  <div class="status-strip">
    <div class="ss-card">
      <p class="ss-label">{{ clock.label }}</p>
      <p class="ss-clock">{{ clock.value }}</p>
      <p v-if="clock.hint" class="ss-hint">{{ clock.hint }}</p>
    </div>

    <div v-if="phase === 'wrapup' || modeCard !== false" class="ss-card">
      <template v-if="phase !== 'wrapup'">
        <p class="ss-label">Streaming mode</p>
        <div v-if="canManage" class="ss-modetoggle" role="group" aria-label="Streaming mode">
          <button
            type="button"
            :class="['ss-modebtn', { active: mode === 'upload' }]"
            @click="emit('select-upload')"
          >
            ☁️ Pre-recorded
          </button>
          <button
            type="button"
            :class="['ss-modebtn', { active: mode === 'live' }]"
            @click="emit('select-live')"
          >
            📡 Live
          </button>
        </div>
        <p v-else class="ss-value">{{ mode === 'live' ? '📡 Live' : '☁️ Pre-recorded' }}</p>
        <p class="ss-hint" :class="{ done: prepStatus.done, warn: !prepStatus.done }">
          {{ prepStatus.label }}
        </p>
      </template>
      <template v-else>
        <p class="ss-label">Publishing</p>
        <p class="ss-value" :class="publishing.cls">{{ publishing.label }}</p>
        <p v-if="publishing.hint" class="ss-hint">{{ publishing.hint }}</p>
      </template>
    </div>

    <div class="ss-card">
      <p class="ss-label">Announcements</p>
      <p class="ss-value">{{ announcementsReady }} of 4 {{ phase === 'wrapup' ? 'sent' : 'scheduled' }}</p>
      <p class="ss-hint">Instagram · Telegram</p>
    </div>
  </div>
</template>

<style scoped>
.status-strip {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: var(--spacing-md);
  margin-bottom: var(--spacing-lg);
}

.ss-card {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: var(--spacing-md) var(--spacing-lg);
  min-width: 0;
}

.ss-label {
  margin: 0 0 var(--spacing-xs);
  font-family: var(--font-ui);
  font-size: var(--font-size-xs);
  letter-spacing: 0.05em;
  text-transform: uppercase;
  color: var(--color-text-muted);
}

.ss-clock {
  margin: 0;
  font-size: var(--font-size-2xl);
  font-weight: var(--font-weight-bold);
  color: var(--color-text);
  font-variant-numeric: tabular-nums;
}

.ss-value {
  margin: 0;
  font-family: var(--font-ui);
  font-size: var(--font-size-md);
  font-weight: 600;
  color: var(--color-text);
}

.ss-value.done {
  color: var(--color-success);
}

.ss-value.warn {
  color: var(--color-warning);
}

.ss-value.error {
  color: var(--color-error);
}

.ss-value.busy {
  color: #3b82f6;
}

.ss-value.muted {
  color: var(--color-text-muted);
  font-weight: 400;
}

.ss-hint {
  margin: var(--spacing-xs) 0 0;
  font-family: var(--font-ui);
  font-size: var(--font-size-xs);
  color: var(--color-text-muted);
}

.ss-hint.done {
  color: var(--color-success);
}

.ss-hint.warn {
  color: var(--color-warning);
}

.ss-modetoggle {
  display: inline-flex;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  overflow: hidden;
}

.ss-modebtn {
  padding: 5px 12px;
  border: none;
  background: transparent;
  color: var(--color-text-muted);
  font-family: var(--font-ui);
  font-size: var(--font-size-sm);
  cursor: pointer;
  transition: background var(--transition-fast);
}

.ss-modebtn:hover {
  background: var(--color-surface-hover);
}

.ss-modebtn.active {
  background: var(--color-surface-hover);
  color: var(--color-primary);
}
</style>
