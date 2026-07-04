<script setup lang="ts">
import type { LatestRecording, ShowDetail, SoundCloudStatus } from '../../../api';
import { BaseButton } from '@shared/components';
import AudioPlayer from '../../AudioPlayer.vue';

/**
 * Wrap-up panel: the post-broadcast surface — live-capture recording status +
 * SoundCloud publish. Extracted verbatim from ShowDetailPage's shared lower
 * cards (Live Recording + the non-UNHEARD SoundCloud card).
 *
 * All state and side-effects stay on the owning page; this panel renders and
 * emits the actions. Only rendered for external shows (UNHEARD keeps its own
 * recording/SoundCloud UI inside its Final Recording card).
 */
const props = defineProps<{
  show: ShowDetail;
  latestRecording: LatestRecording | null;
  recordingState: 'ready' | 'processing' | 'failed' | null;
  /** Whether the viewer may use SoundCloud (connect + upload their show). */
  canUseSoundcloud: boolean;
  scStatus: SoundCloudStatus | null;
  uploadingToSoundCloud: boolean;
  togglingSoundCloudPrivacy: boolean;
}>();

const emit = defineEmits<{
  'upload-soundcloud': [];
  'toggle-privacy': [];
  'connect-soundcloud': [];
  'reconnect-soundcloud': [];
}>();

const RECORDING_STATE_LABELS: Record<'ready' | 'processing' | 'failed', string> = {
  ready: 'Ready',
  processing: 'Processing…',
  failed: 'Failed',
};

function formatDurationMs(ms: number): string {
  const totalSeconds = Math.floor(ms / 1000);
  const mins = Math.floor(totalSeconds / 60);
  const secs = totalSeconds % 60;
  return `${mins}:${secs.toString().padStart(2, '0')}`;
}
</script>

<template>
  <div class="wrapup-panel">
    <!-- Live Recording -->
    <div v-if="props.latestRecording && props.recordingState" class="card">
      <h2 class="section-title">Live Recording</h2>
      <div class="live-recording-head">
        <span :class="['rec-badge', props.recordingState]">
          {{ RECORDING_STATE_LABELS[props.recordingState] }}
        </span>
        <span class="live-recording-meta">
          {{ props.latestRecording.version }}
          <span v-if="props.latestRecording.duration_ms">
            · {{ formatDurationMs(props.latestRecording.duration_ms) }}</span
          >
        </span>
      </div>

      <p v-if="props.recordingState === 'failed'" class="live-recording-error">
        {{ props.latestRecording.error_message || 'Recording failed.' }}
      </p>
      <p v-else-if="props.recordingState === 'processing'" class="text-muted live-recording-hint">
        The recording is being processed. A download will appear here once it's ready.
      </p>

      <template v-else-if="props.latestRecording.download_url">
        <AudioPlayer
          :key="props.latestRecording.download_url"
          :src="props.latestRecording.download_url"
        />
        <div class="recording-actions">
          <a :href="props.latestRecording.download_url" class="dl-btn recording" download>
            ⬇️ Download recording
          </a>
        </div>
      </template>
    </div>

    <!-- SoundCloud -->
    <div v-if="props.canUseSoundcloud && props.scStatus" class="card">
      <h2 class="section-title">SoundCloud</h2>
      <div class="soundcloud-section">
        <template v-if="!props.scStatus.configured">
          <p class="empty-state">
            SoundCloud isn't configured. Set SOUNDCLOUD_CLIENT_ID / SOUNDCLOUD_CLIENT_SECRET.
          </p>
        </template>
        <template v-else-if="!props.scStatus.authorized">
          <p class="text-muted soundcloud-hint">
            Connect the station's SoundCloud account to enable uploads.
          </p>
          <BaseButton size="sm" variant="primary" @click="emit('connect-soundcloud')">
            🔗 Connect SoundCloud
          </BaseButton>
        </template>
        <template v-else-if="show.soundcloud_url">
          <div class="soundcloud-status">
            <span class="soundcloud-label">☁️ SoundCloud</span>
            <a :href="show.soundcloud_url" target="_blank" rel="noopener" class="soundcloud-link">
              {{ show.soundcloud_public ? '🔓 Public' : '🔒 Private' }}
            </a>
            <span v-if="show.soundcloud_uploaded_at" class="text-muted soundcloud-timestamp">
              Uploaded {{ show.soundcloud_uploaded_at }}
            </span>
          </div>
          <div class="soundcloud-actions">
            <BaseButton
              size="sm"
              :variant="show.soundcloud_public ? 'ghost' : 'success'"
              :loading="props.togglingSoundCloudPrivacy"
              @click="emit('toggle-privacy')"
            >
              {{ show.soundcloud_public ? 'Make Private' : 'Make Public' }}
            </BaseButton>
            <BaseButton
              size="sm"
              variant="ghost"
              :loading="props.uploadingToSoundCloud"
              @click="emit('upload-soundcloud')"
            >
              Re-upload
            </BaseButton>
            <BaseButton
              v-if="props.scStatus.auth_url"
              size="sm"
              variant="ghost"
              @click="emit('reconnect-soundcloud')"
            >
              🔗 Reconnect
            </BaseButton>
          </div>
        </template>
        <template v-else>
          <p class="text-muted soundcloud-hint">
            Finalized recordings upload here automatically. You can also upload now.
          </p>
          <div class="soundcloud-actions">
            <BaseButton
              size="sm"
              variant="ghost"
              :loading="props.uploadingToSoundCloud"
              @click="emit('upload-soundcloud')"
            >
              ☁️ Upload to SoundCloud
            </BaseButton>
            <BaseButton
              v-if="props.scStatus.auth_url"
              size="sm"
              variant="ghost"
              @click="emit('reconnect-soundcloud')"
            >
              🔗 Reconnect
            </BaseButton>
          </div>
        </template>
      </div>
    </div>

    <p v-if="!props.latestRecording && !(props.canUseSoundcloud && props.scStatus)" class="empty-state">
      Nothing to wrap up yet — the recording and publish options appear after the show.
    </p>
  </div>
</template>

<style scoped>
.wrapup-panel {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-lg);
}

.wrapup-panel .card {
  margin-bottom: 0;
}

.section-title {
  font-size: 1.2em;
  margin-bottom: var(--spacing-md);
  padding-bottom: var(--spacing-sm);
  border-bottom: 1px solid var(--color-border);
}

.live-recording-head {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  flex-wrap: wrap;
}

.live-recording-meta {
  font-size: 0.85rem;
  color: var(--color-text-muted);
}

.live-recording-hint,
.live-recording-error {
  margin-top: var(--spacing-sm);
}

.live-recording-error {
  color: #ef4444;
}

.rec-badge {
  font-size: 0.75rem;
  padding: 2px 8px;
  border-radius: var(--radius-sm);
  font-weight: 500;
}

.rec-badge.ready {
  background: rgba(34, 197, 94, 0.1);
  color: #22c55e;
}

.rec-badge.processing {
  background: rgba(59, 130, 246, 0.1);
  color: #3b82f6;
}

.rec-badge.failed {
  background: rgba(239, 68, 68, 0.1);
  color: #ef4444;
}

.recording-actions {
  margin-top: var(--spacing-md);
  display: flex;
  flex-direction: row;
  gap: var(--spacing-sm);
}

.recording-actions > * {
  flex: 1;
}

.dl-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: var(--spacing-sm) var(--spacing-md);
  border-radius: var(--radius-md);
  text-decoration: none;
  font-size: var(--font-size-sm);
  transition: all var(--transition-fast);
  background-color: #666666;
  color: #ffffff;
  white-space: nowrap;
}

.dl-btn:hover {
  opacity: 0.8;
}

.dl-btn.recording {
  background-color: #8b5cf6;
}

.soundcloud-section {
  margin-top: 0;
}

.soundcloud-status {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  flex-wrap: wrap;
  margin-bottom: var(--spacing-sm);
}

.soundcloud-label {
  font-weight: 600;
}

.soundcloud-link {
  color: var(--color-primary);
}

.soundcloud-timestamp {
  font-size: var(--font-size-sm);
}

.soundcloud-actions {
  display: flex;
  gap: var(--spacing-sm);
}

.empty-state {
  color: var(--color-text-muted);
  font-style: italic;
  padding: var(--spacing-md) 0;
}

.text-muted {
  color: var(--color-text-muted);
}
</style>
