<script setup lang="ts">
import { computed, ref } from 'vue';
import { BaseButton } from '@shared/components';
import { showsApi, type ShowDetail } from '../api';
import AudioPlayer from './AudioPlayer.vue';
import EpisodeCard from './episode/EpisodeCard.vue';
import EpisodeIcon from './episode/EpisodeIcon.vue';
import type { EpisodePhase, Status } from './episode/phase';
import type { RecordingPill } from './episode/useEpisode';

// Recording & SoundCloud card of the episode page (post-show, #1052): where the
// recording is, its player, the SoundCloud track and its link. A picked file
// uploads at once and becomes the episode recording; Upload to SoundCloud
// sends it as a private track. Only an admin upload starts SoundCloud on its
// own. Hosts and admins switch the track public or private; public unlocks
// the post-announcement. Other hosts see the status and a public track's link.

const props = defineProps<{
  show: ShowDetail;
  phase: EpisodePhase;
  /** Where the recording is, as the Wrap-up says it. */
  recording: RecordingPill;
  /** An admin turned SoundCloud off. */
  soundcloudOff?: boolean;
  /** Another host's episode: status and public links only. */
  readOnly?: boolean;
}>();
const emit = defineEmits<{ uploaded: []; changed: []; openWrapup: [] }>();

const uploading = ref(false);
const progress = ref('');
const sending = ref(false);
const trackUrl = ref<string | null>(null);
const error = ref<string | null>(null);
const switching = ref(false);
/** Set after a switch here, until the parent reloads the show. */
const publicNow = ref<boolean | null>(null);

const prerecorded = computed(() => props.show.stream_mode === 'prerecorded');
const hasRecording = computed(
  () =>
    !!props.show.recording_filename ||
    !!props.show.recording_url ||
    props.show.latest_recording?.status === 'finalized'
);
const link = computed(() => trackUrl.value ?? props.show.soundcloud_url ?? null);
// A fresh upload is always private.
const isPublic = computed(
  () => publicNow.value ?? (!trackUrl.value && !!props.show.soundcloud_public)
);
/** The recording to play back after the show (only the episode's hosts get it). */
const playback = computed(() => {
  if (props.readOnly || props.phase !== 'post') return null;
  const s = props.show;
  if (prerecorded.value) return s.prerecorded_url ?? null;
  return s.latest_recording?.download_url ?? s.recording_url ?? null;
});

/** "Ready · 1:58:40", "Processing…", "No usable recording · …" */
const recordingLine = computed(() =>
  props.recording.text.replace(/^Recording (\w)/, (_, c: string) => c.toUpperCase())
);

const status = computed<Status>(() => {
  if (link.value) {
    return isPublic.value
      ? { tone: 'green', text: 'Public on SoundCloud' }
      : { tone: 'orange', text: 'Private on SoundCloud' };
  }
  if (props.phase === 'post') {
    if (props.recording.state === 'ok' || hasRecording.value)
      return { tone: 'green', text: 'Recording ready' };
    if (props.recording.state === 'processing') return { tone: 'amber', text: 'Processing' };
    return { tone: 'red', text: 'No recording' };
  }
  if (prerecorded.value) {
    return props.show.prerecorded_filename
      ? { tone: 'green', text: 'Set ready' }
      : { tone: 'grey', text: 'No set yet' };
  }
  if (hasRecording.value) return { tone: 'green', text: 'File uploaded' };
  if (props.show.on_air) return { tone: 'red', text: 'Rec' };
  return { tone: 'grey', text: 'Not started' };
});

const lead = computed(() => {
  if (props.phase === 'post') return '';
  if (prerecorded.value) return 'The uploaded set is the recording of this episode.';
  if (props.show.on_air)
    return 'The server records the show while it is on air. The recording ends when you end the episode.';
  return 'The recording starts when you go live. After the show it goes to SoundCloud as a private track.';
});

async function upload(event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file) return;
  uploading.value = true;
  error.value = null;
  try {
    await showsApi.uploadRecording(props.show.id, file, (p) => {
      progress.value = p.phase === 'uploading' ? `Uploading… ${p.percent}%` : 'Uploading…';
    });
    emit('uploaded');
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Could not upload the recording';
  } finally {
    uploading.value = false;
    progress.value = '';
    input.value = '';
  }
}

async function sendToSoundCloud() {
  sending.value = true;
  error.value = null;
  try {
    const result = await showsApi.uploadToSoundCloud(props.show.id);
    if (result.success && result.track_url) {
      trackUrl.value = result.track_url;
      publicNow.value = false;
    } else error.value = result.error || 'SoundCloud did not take the track';
  } catch (e) {
    const message = e instanceof Error ? e.message : 'Could not upload to SoundCloud';
    // Only an admin can connect the station's SoundCloud account.
    error.value = /not authorized/i.test(message)
      ? 'SoundCloud is not connected. An admin must connect the account again.'
      : message;
  } finally {
    sending.value = false;
  }
}

async function switchPublic() {
  const makePublic = !isPublic.value;
  switching.value = true;
  error.value = null;
  try {
    const result = await showsApi.setSoundCloudPrivacy(props.show.id, makePublic);
    if (!result.success) {
      error.value = result.error || 'SoundCloud did not change the track';
      return;
    }
    publicNow.value = makePublic;
    emit('changed');
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Could not change the track on SoundCloud';
  } finally {
    switching.value = false;
  }
}
</script>

<template>
  <EpisodeCard
    title="Recording & SoundCloud"
    short="Recording"
    phase="Post-show"
    :status="status"
    :current="phase === 'post' && show.episode_status !== 'finished'"
    class="recording"
  >
    <template #icon><EpisodeIcon name="cloud" /></template>

    <p v-if="lead" class="ep-lead">{{ lead }}</p>
    <AudioPlayer v-if="playback" :src="playback" />
    <dl class="ep-kv">
      <div v-if="phase === 'post'">
        <dt>Recording</dt>
        <dd :class="{ ok: recording.state === 'ok', bad: recording.state === 'failed' }">
          {{ recordingLine }}
        </dd>
      </div>
      <div v-if="!readOnly && (uploading || show.recording_filename)">
        <dt>File</dt>
        <dd class="status">{{ uploading ? progress || 'Uploading…' : show.recording_filename }}</dd>
      </div>
      <div>
        <dt>SoundCloud</dt>
        <dd v-if="link && (!readOnly || isPublic)" class="status">
          {{ isPublic ? 'Public track' : 'Private track' }} ·
          <a :href="link" target="_blank" rel="noopener">Open on SoundCloud</a>
        </dd>
        <dd v-else-if="link">Private track</dd>
        <dd v-else class="mut">
          {{ phase === 'post' ? 'No track yet' : 'After the show' }}
        </dd>
      </div>
    </dl>

    <template v-if="!readOnly">
      <label class="field upload">
        <span class="label">Recording file</span>
        <input
          type="file"
          class="input"
          accept=".mp3,.wav,audio/mpeg,audio/wav"
          :disabled="uploading"
          @change="upload"
        />
      </label>
      <p v-if="soundcloudOff" class="notice">SoundCloud is temporarily not available.</p>
      <p v-if="error" class="field-error" role="alert">{{ error }}</p>
    </template>

    <template v-if="!readOnly" #actions>
      <BaseButton
        v-if="phase === 'post' && show.episode_status !== 'finished'"
        class="xl"
        @click="emit('openWrapup')"
        >Open Wrap-up</BaseButton
      >
      <BaseButton
        v-if="hasRecording"
        variant="secondary"
        :disabled="!hasRecording || uploading || soundcloudOff"
        :loading="sending"
        @click="sendToSoundCloud"
      >
        Upload to SoundCloud
      </BaseButton>
      <BaseButton
        v-if="link"
        variant="secondary"
        :disabled="sending || soundcloudOff"
        :loading="switching"
        @click="switchPublic"
      >
        {{ isPublic ? 'Make track private' : 'Make track public' }}
      </BaseButton>
    </template>
  </EpisodeCard>
</template>

<style scoped>
.notice {
  margin: 0;
  padding: 12px 14px;
  border: 1px solid #6b5a1e;
  border-radius: var(--radius-lg);
  color: #f3d77a;
}

.status {
  overflow-wrap: anywhere;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.label {
  font: 700 11px var(--font-ui);
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: #a8a8bc;
}

.input {
  min-height: 44px;
  padding: var(--spacing-sm) var(--spacing-md);
  border: 1px solid #34344c;
  border-radius: var(--radius-lg);
  background: #06060f;
  color: var(--color-text);
  font: 15px var(--font-ui);
}
</style>
