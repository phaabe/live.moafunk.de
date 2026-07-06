<script setup lang="ts">
import { computed } from 'vue';
import type { LatestRecording, ShowDetail, SoundCloudStatus } from '../../api';
import { BaseButton } from '@shared/components';
import AudioPlayer from '../AudioPlayer.vue';
import { fmtClock } from '../../composables/useShowClocks';

/**
 * Post-production pipeline for finished shows: a step list from
 * "stream ended" through audio conversion (→ download) to SoundCloud
 * upload (→ publish). Each step shows its own state, so a missing duration
 * or pending conversion reads as "processing" rather than broken.
 *
 * Pure presentation: all side effects live on ShowDetailPage.
 */
const props = defineProps<{
  show: ShowDetail;
  latestRecording: LatestRecording | null;
  recordingState: 'ready' | 'processing' | 'failed' | null;
  canManage: boolean;
  canPublish: boolean;
  canGoLiveAgain: boolean;
  scStatus: SoundCloudStatus | null;
  uploadingToSoundCloud: boolean;
  togglingSoundCloudPrivacy: boolean;
}>();

const emit = defineEmits<{
  publish: [];
  'upload-soundcloud': [];
  'connect-soundcloud': [];
  'live-panel': [];
}>();

const durationLabel = computed(() =>
  props.latestRecording?.duration_ms ? fmtClock(props.latestRecording.duration_ms) : null
);

type StepState = 'done' | 'busy' | 'failed' | 'pending';

const conversionStep = computed<{ state: StepState; hint: string }>(() => {
  switch (props.recordingState) {
    case 'ready':
      return {
        state: 'done',
        hint: durationLabel.value ? `Recorded ${durationLabel.value}` : 'Ready to download',
      };
    case 'processing':
      return { state: 'busy', hint: 'Converting — download unlocks when done' };
    case 'failed':
      return {
        state: 'failed',
        hint: props.latestRecording?.error_message || 'Recording failed.',
      };
    default:
      return { state: 'pending', hint: 'No live recording for this show' };
  }
});

const soundcloudStep = computed<{ state: StepState; hint: string }>(() => {
  if (props.show.soundcloud_url) {
    return props.show.soundcloud_public
      ? { state: 'done', hint: 'Published — visible to listeners' }
      : { state: 'busy', hint: 'Private — not visible to listeners yet' };
  }
  if (!props.scStatus?.configured) return { state: 'pending', hint: 'Not configured' };
  if (!props.scStatus.authorized) return { state: 'pending', hint: 'Connect SoundCloud first' };
  return { state: 'pending', hint: 'Not uploaded yet' };
});

const STEP_ICONS: Record<StepState, string> = {
  done: '✓',
  busy: '◌',
  failed: '✕',
  pending: '·',
};
</script>

<template>
  <div class="card wrapup-card">
    <div class="wp-head">
      <h2 class="wp-title">Post-production</h2>
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

    <!-- Step 1: recording / conversion → download -->
    <div class="wp-step">
      <span class="wp-step-ico" :class="conversionStep.state">
        {{ STEP_ICONS[conversionStep.state] }}
      </span>
      <div class="wp-step-body">
        <p class="wp-step-label">Audio file</p>
        <p class="wp-step-hint" :class="conversionStep.state">{{ conversionStep.hint }}</p>
      </div>
      <a
        v-if="recordingState === 'ready' && latestRecording?.download_url"
        :href="latestRecording.download_url"
        class="wp-dl"
        download
      >
        ⬇ Download
      </a>
    </div>

    <AudioPlayer
      v-if="recordingState === 'ready' && latestRecording?.download_url"
      :key="latestRecording.download_url"
      :src="latestRecording.download_url"
    />

    <!-- Step 2: SoundCloud upload → publish -->
    <div v-if="canPublish && scStatus" class="wp-step">
      <span class="wp-step-ico" :class="soundcloudStep.state">
        {{ STEP_ICONS[soundcloudStep.state] }}
      </span>
      <div class="wp-step-body">
        <p class="wp-step-label">
          SoundCloud
          <a
            v-if="show.soundcloud_url"
            :href="show.soundcloud_url"
            target="_blank"
            rel="noopener"
            class="wp-link"
            >open ↗</a
          >
        </p>
        <p class="wp-step-hint" :class="soundcloudStep.state">{{ soundcloudStep.hint }}</p>
      </div>

      <template v-if="!scStatus.configured"></template>
      <BaseButton
        v-else-if="!scStatus.authorized"
        size="sm"
        variant="primary"
        @click="emit('connect-soundcloud')"
      >
        🔗 Connect
      </BaseButton>
      <BaseButton
        v-else-if="show.soundcloud_url && !show.soundcloud_public"
        size="sm"
        variant="success"
        :loading="togglingSoundCloudPrivacy"
        @click="emit('publish')"
      >
        🚀 Publish
      </BaseButton>
      <BaseButton
        v-else-if="!show.soundcloud_url"
        size="sm"
        variant="ghost"
        :loading="uploadingToSoundCloud"
        @click="emit('upload-soundcloud')"
      >
        ☁️ Upload to SoundCloud
      </BaseButton>
    </div>
  </div>
</template>

<style scoped>
.wrapup-card {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
  min-height: 100%;
  font-family: var(--font-ui);
}

.wp-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--spacing-sm);
}

.wp-title {
  margin: 0;
  font-size: var(--font-size-sm);
  font-weight: 700;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  color: var(--color-text-muted);
}

.wp-step {
  display: flex;
  align-items: center;
  gap: var(--spacing-md);
  padding: var(--spacing-sm) 0;
  border-bottom: 1px solid var(--color-border);
}

.wp-step:last-child {
  border-bottom: none;
}

.wp-step-ico {
  flex: 0 0 auto;
  width: 24px;
  height: 24px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--radius-full);
  font-size: var(--font-size-sm);
  font-weight: 700;
  background: var(--color-surface-alt);
  color: var(--color-text-muted);
}

.wp-step-ico.done {
  background: var(--color-success-bg);
  color: var(--color-success);
}

.wp-step-ico.busy {
  background: rgba(59, 130, 246, 0.12);
  color: #3b82f6;
  animation: wp-spin 1.6s linear infinite;
}

.wp-step-ico.failed {
  background: var(--color-error-bg);
  color: var(--color-error);
}

@keyframes wp-spin {
  to {
    transform: rotate(360deg);
  }
}

.wp-step-body {
  flex: 1 1 auto;
  min-width: 0;
}

.wp-step-label {
  margin: 0;
  font-size: var(--font-size-sm);
  font-weight: 600;
  color: var(--color-text);
}

.wp-step-hint {
  margin: 0;
  font-size: var(--font-size-xs);
  color: var(--color-text-muted);
}

.wp-step-hint.failed {
  color: var(--color-error);
}

.wp-step-hint.done {
  color: var(--color-success);
}

.wp-link {
  margin-left: var(--spacing-xs);
  font-size: var(--font-size-xs);
  font-weight: 400;
  color: var(--color-link);
}

.wp-link:hover {
  color: var(--color-link-hover);
}

.wp-dl {
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

.wp-dl:hover {
  opacity: 0.85;
}
</style>
