<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue';
import type { LatestRecording, ShowDetail, SoundCloudStatus } from '../../../api';
import { BaseButton } from '@shared/components';
import type { ShowPhase } from '../../../composables/useShowPhase';
import { isShowEnded } from '../../../showTime';

/**
 * The "Live" card of the 2×2 show dashboard (docs/stream-rework/show-cockpit-plan.md).
 * A three-state machine keyed off the show's soft phase:
 *
 * - `prep`      — countdown to air · preparation status · stream-type · "Prep. Show"
 * - `broadcast` — time since on air · ON AIR · stream-type · "Live Panel"
 * - `wrapup`    — show duration · optional "Go live again" (still inside air window)
 *                 · audio-conversion status + Download · SoundCloud status + Publish
 *
 * Pure presentation: all side-effects live on ShowDetailPage; this card renders
 * and emits. Owns only a 1 s ticker for the live counters.
 */
const props = defineProps<{
  show: ShowDetail;
  phase: ShowPhase;
  /** Selected media mode (owned by the page so it survives reloads). */
  mode: 'live' | 'upload';
  /** Air datetime (UTC) for the countdown / elapsed counters, or null if unknown. */
  airTarget: Date | null;
  latestRecording: LatestRecording | null;
  recordingState: 'ready' | 'processing' | 'failed' | null;
  /** The assigned host may manage media (enter the stream flow). */
  canManage: boolean;
  /** Host/admin may publish on SoundCloud. */
  canPublish: boolean;
  scStatus: SoundCloudStatus | null;
  uploadingToSoundCloud: boolean;
  togglingSoundCloudPrivacy: boolean;
}>();

const emit = defineEmits<{
  /** Change selected media type (does not navigate) — prep only. */
  'select-live': [];
  'select-upload': [];
  /** Enter the stream preparation flow (upload or live) for the chosen mode. */
  prep: [];
  /** Jump to the live panel (go on air / go live again). */
  'live-panel': [];
  /** Publish the SoundCloud track (make public). */
  publish: [];
  /** Upload the recording to SoundCloud. */
  'upload-soundcloud': [];
  'connect-soundcloud': [];
}>();

// ── Live counters ───────────────────────────────────────────────────────────
const now = ref(Date.now());
let timer: ReturnType<typeof setInterval> | null = null;
onMounted(() => {
  timer = setInterval(() => (now.value = Date.now()), 1000);
});
onUnmounted(() => {
  if (timer) clearInterval(timer);
});

/** "04d 12h 33m 38s" — days segment dropped once under a day. */
function fmtDuration(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const d = Math.floor(total / 86400);
  const h = Math.floor((total % 86400) / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n: number) => String(n).padStart(2, '0');
  return d > 0 ? `${d}d ${pad(h)}h ${pad(m)}m ${pad(s)}s` : `${pad(h)}h ${pad(m)}m ${pad(s)}s`;
}

const countdown = computed(() =>
  props.airTarget ? fmtDuration(props.airTarget.getTime() - now.value) : '—'
);

const elapsed = computed(() =>
  props.airTarget ? fmtDuration(now.value - props.airTarget.getTime()) : '—'
);

/** "HH:MM:SS" clock — used for the finished-show duration. */
function fmtClock(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(h)}:${pad(m)}:${pad(s)}`;
}

/**
 * Duration of the finished show. Prefers the recording's stored length; for
 * legacy rows that predate duration persistence, a failed short capture still
 * carries its length in the reason string ("… is only 3s …").
 */
const duration = computed(() => {
  const rec = props.latestRecording?.duration_ms;
  if (rec) return fmtClock(rec);
  const shortMatch = props.latestRecording?.error_message?.match(/only (\d+)s/);
  if (shortMatch) return fmtClock(Number(shortMatch[1]) * 1000);
  return '—';
});

const streamTypeLabel = computed(() => (props.mode === 'live' ? '📡 Live' : '☁️ Upload'));

// ── Prep status ───────────────────────────────────────────────────────────
const hasFile = computed(() => !!props.show.prerecorded_key);
const confirmed = computed(() => !!props.show.prerecorded_confirmed_at);

/** Preparation readiness for the current media mode. */
const prepStatus = computed<{ label: string; done: boolean }>(() => {
  if (props.mode === 'live') {
    return { label: 'Ready to stream at air time', done: true };
  }
  if (!hasFile.value) return { label: 'No file uploaded yet', done: false };
  return confirmed.value
    ? { label: 'File uploaded & confirmed', done: true }
    : { label: 'File uploaded — not confirmed', done: false };
});

// ── Wrap-up ─────────────────────────────────────────────────────────────────
const RECORDING_STATE_LABELS: Record<'ready' | 'processing' | 'failed', string> = {
  ready: 'Ready',
  processing: 'Converting…',
  failed: 'Failed',
};

/** Still inside the show's air window → offer to resume streaming. */
const canGoLiveAgain = computed(() => !isShowEnded(props.show));
</script>

<template>
  <div class="card live-card">
    <!-- ── PREP: countdown + preparation status ─────────────────────────── -->
    <template v-if="phase === 'prep'">
      <p class="lc-eyebrow">Countdown</p>
      <p class="lc-clock">{{ countdown }}</p>

      <div class="lc-row">
        <span class="lc-label">Preparation status</span>
        <span class="lc-value" :class="{ done: prepStatus.done }">{{ prepStatus.label }}</span>
      </div>

      <div class="lc-row">
        <span class="lc-label">Stream type</span>
        <div v-if="canManage" class="lc-modetoggle" role="group" aria-label="Stream type">
          <button
            type="button"
            :class="['lc-modebtn', { active: mode === 'live' }]"
            @click="emit('select-live')"
          >
            📡 Live
          </button>
          <button
            type="button"
            :class="['lc-modebtn', { active: mode === 'upload' }]"
            @click="emit('select-upload')"
          >
            ☁️ Upload
          </button>
        </div>
        <span v-else class="lc-value">{{ streamTypeLabel }}</span>
      </div>

      <BaseButton v-if="canManage" variant="primary" class="lc-action" @click="emit('prep')">
        {{ mode === 'live' ? '📡 Prepare live show' : '⬆ Prepare show' }}
      </BaseButton>
    </template>

    <!-- ── BROADCAST: on air ────────────────────────────────────────────── -->
    <template v-else-if="phase === 'broadcast'">
      <p class="lc-eyebrow">Time since on air</p>
      <p class="lc-clock">{{ elapsed }}</p>

      <div class="lc-row">
        <span class="lc-label">Status</span>
        <span class="lc-value onair"><span class="lc-dot"></span> On air</span>
      </div>

      <div class="lc-row">
        <span class="lc-label">Stream type</span>
        <span class="lc-value">{{ streamTypeLabel }}</span>
      </div>

      <BaseButton v-if="canManage" variant="primary" class="lc-action" @click="emit('live-panel')">
        🎛 Live panel
      </BaseButton>
    </template>

    <!-- ── WRAP-UP: finished ────────────────────────────────────────────── -->
    <template v-else>
      <p class="lc-eyebrow">Show duration</p>
      <p class="lc-clock">{{ duration }}</p>

      <div class="lc-row">
        <span class="lc-label">Status</span>
        <span class="lc-value done">Show finished</span>
        <BaseButton
          v-if="canManage && canGoLiveAgain"
          variant="ghost"
          size="sm"
          title="Still inside the show window — resume streaming"
          @click="emit('live-panel')"
        >
          ↻ Go live again
        </BaseButton>
      </div>

      <!-- Audio conversion + download -->
      <div v-if="recordingState" class="lc-row">
        <span class="lc-label">Audio file</span>
        <span :class="['lc-badge', recordingState]">{{ RECORDING_STATE_LABELS[recordingState] }}</span>
        <a
          v-if="recordingState === 'ready' && latestRecording?.download_url"
          :href="latestRecording.download_url"
          class="lc-dl"
          download
        >
          ⬇️ Download
        </a>
      </div>
      <p v-if="recordingState === 'failed'" class="lc-error">
        {{ latestRecording?.error_message || 'Recording failed.' }}
      </p>

      <!-- SoundCloud upload + publish -->
      <div v-if="canPublish && scStatus" class="lc-row">
        <span class="lc-label">SoundCloud</span>
        <template v-if="!scStatus.configured">
          <span class="lc-value muted">Not configured</span>
        </template>
        <template v-else-if="!scStatus.authorized">
          <BaseButton size="sm" variant="primary" @click="emit('connect-soundcloud')">
            🔗 Connect
          </BaseButton>
        </template>
        <template v-else-if="show.soundcloud_url">
          <a :href="show.soundcloud_url" target="_blank" rel="noopener" class="lc-value link">
            {{ show.soundcloud_public ? '🔓 Published' : '🔒 Uploaded (private)' }}
          </a>
          <BaseButton
            v-if="!show.soundcloud_public"
            size="sm"
            variant="success"
            :loading="togglingSoundCloudPrivacy"
            @click="emit('publish')"
          >
            🚀 Publish
          </BaseButton>
        </template>
        <template v-else>
          <BaseButton
            size="sm"
            variant="ghost"
            :loading="uploadingToSoundCloud"
            @click="emit('upload-soundcloud')"
          >
            ☁️ Upload to SoundCloud
          </BaseButton>
        </template>
      </div>
    </template>
  </div>
</template>

<style scoped>
.live-card {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
  min-height: 100%;
}

.lc-eyebrow {
  margin: 0;
  font-size: var(--font-size-xs);
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--color-text-muted);
}

.lc-clock {
  margin: 0;
  font-size: var(--font-size-2xl, 1.8rem);
  font-weight: 700;
  color: var(--color-text);
  font-variant-numeric: tabular-nums;
}

.lc-row {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  flex-wrap: wrap;
}

.lc-label {
  flex: 0 0 auto;
  min-width: 120px;
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}

.lc-value {
  font-size: var(--font-size-sm);
  font-weight: 600;
  color: var(--color-text);
}

.lc-value.done {
  color: var(--color-success);
}

.lc-value.muted {
  color: var(--color-text-muted);
  font-weight: 400;
}

.lc-value.link {
  color: var(--color-primary);
}

.lc-value.onair {
  display: inline-flex;
  align-items: center;
  gap: var(--spacing-xs);
  color: var(--color-error, #ef4444);
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.lc-dot {
  width: 9px;
  height: 9px;
  border-radius: var(--radius-full);
  background: var(--color-error, #ef4444);
  animation: lc-pulse 1.4s ease-in-out infinite;
}

@keyframes lc-pulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.3;
  }
}

.lc-modetoggle {
  display: inline-flex;
  gap: var(--spacing-xs);
}

.lc-modebtn {
  padding: 4px 10px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-surface);
  color: var(--color-text);
  font-family: var(--font-family);
  font-size: var(--font-size-sm);
  cursor: pointer;
  transition:
    border-color var(--transition-fast),
    background var(--transition-fast);
}

.lc-modebtn:hover {
  border-color: var(--color-primary);
}

.lc-modebtn.active {
  border-color: var(--color-primary);
  background: var(--color-surface-hover);
  box-shadow: inset 0 0 0 1px var(--color-primary);
}

.lc-badge {
  font-size: 0.75rem;
  padding: 2px 8px;
  border-radius: var(--radius-sm);
  font-weight: 500;
}

.lc-badge.ready {
  background: rgba(34, 197, 94, 0.1);
  color: #22c55e;
}

.lc-badge.processing {
  background: rgba(59, 130, 246, 0.1);
  color: #3b82f6;
}

.lc-badge.failed {
  background: rgba(239, 68, 68, 0.1);
  color: #ef4444;
}

.lc-dl {
  display: inline-flex;
  align-items: center;
  padding: var(--spacing-xs) var(--spacing-md);
  border-radius: var(--radius-md);
  text-decoration: none;
  font-size: var(--font-size-sm);
  background-color: #8b5cf6;
  color: #fff;
  white-space: nowrap;
}

.lc-dl:hover {
  opacity: 0.85;
}

.lc-error {
  margin: 0;
  font-size: var(--font-size-sm);
  color: #ef4444;
}

.lc-action {
  width: 100%;
  margin-top: auto;
}
</style>
