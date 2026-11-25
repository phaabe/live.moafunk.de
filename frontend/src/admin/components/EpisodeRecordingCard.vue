<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import { BaseButton } from '@shared/components';
import {
  recordingEditApi,
  seriesApi,
  showsApi,
  type RecordingEditState,
  type RecordingFile,
  type ShowDetail,
} from '../api';
import { inSlot, postTime, slotState, type SlotReading } from '../promotion';
import { span } from './recording-editor/edit';
import RecordingPlayer from './recording-editor/RecordingPlayer.vue';
import SnippetEditor from './recording-editor/SnippetEditor.vue';
import EpisodeCard from './episode/EpisodeCard.vue';
import EpisodeIcon from './episode/EpisodeIcon.vue';
import type { EpisodePhase, Status, StepState } from './episode/phase';
import type { RecordingPill } from './episode/useEpisode';

// Recording & SoundCloud card of the episode page (step 4, the Recording tab).
// Design option C (#1082): a player on top, then three rows, one open at a time:
// File (show recording or own file; both can be edited), SoundCloud (the track,
// which file it has, private or public) and Instagram (the post-announcement
// waits for a public track; the snippets for it, flow `episode-recording-instagram`). A picked own file uploads at once and becomes the
// file for SoundCloud. Only an admin upload starts SoundCloud on its own. The
// post-announcement goes out at its booked time only while public. Publish episode turns "Finished · wrap-up
// open" into "Finished". Other hosts see the status and a public track's link.

const props = defineProps<{
  show: ShowDetail;
  phase: EpisodePhase;
  /** Where the Publish recording step is. */
  state: StepState;
  /** Where the recording is after the show. */
  recording: RecordingPill;
  /** An admin turned SoundCloud off. */
  soundcloudOff?: boolean;
  /** Another host's episode: status and public links only. */
  readOnly?: boolean;
  /** The episode's announcements, for the post-announcement's time. */
  announcements?: SlotReading[];
}>();
const emit = defineEmits<{ uploaded: []; changed: []; social: [] }>();
const router = useRouter();

type Row = 'file' | 'soundcloud' | 'instagram';

const uploading = ref(false);
const progress = ref('');
const sending = ref(false);
const trackUrl = ref<string | null>(null);
const error = ref<string | null>(null);
const switching = ref(false);
const publishing = ref(false);
const picking = ref(false);
/** Set after a switch here, until the parent reloads the show. */
const publicNow = ref<boolean | null>(null);
const fileInput = ref<HTMLInputElement | null>(null);
const editState = ref<RecordingEditState | null>(null);

const s = computed(() => props.show);
const post = computed(() => props.phase === 'post');
const prerecorded = computed(() => s.value.stream_mode === 'prerecorded');
const hasRecording = computed(
  () =>
    !!s.value.recording_filename ||
    !!s.value.recording_url ||
    s.value.latest_recording?.status === 'finalized'
);
const link = computed(() => trackUrl.value ?? s.value.soundcloud_url ?? null);
// A fresh upload is always private.
const isPublic = computed(
  () => publicNow.value ?? (!trackUrl.value && !!s.value.soundcloud_public)
);

// ---- The two files --------------------------------------------------------

const ownName = computed(() => s.value.recording_filename ?? null);
const picked = computed<RecordingFile>(
  () => s.value.recording_source ?? (ownName.value ? 'own' : 'show')
);
/** The show recording only counts after the show. */
const showRec = computed(() => (post.value ? props.recording.state : 'none'));
const hasFile = computed(() => (picked.value === 'own' ? !!ownName.value : showRec.value === 'ok'));
const rawUrl = computed(() => {
  if (picked.value === 'own') return s.value.recording_url ?? null;
  return (
    s.value.latest_recording?.download_url ??
    (prerecorded.value ? s.value.prerecorded_url : null) ??
    null
  );
});
const fileKey = computed(() =>
  hasFile.value && !props.readOnly
    ? `${picked.value}:${picked.value === 'own' ? ownName.value : (s.value.latest_recording?.id ?? '')}`
    : ''
);

// The edit of the picked file: its marks.
async function loadEdit() {
  editState.value = null;
  if (!fileKey.value) return;
  try {
    editState.value = await recordingEditApi.get(s.value.id);
  } catch {
    editState.value = null;
  }
}
watch(() => [fileKey.value, s.value.recording_edit_status, s.value.recording_edited_at], loadEdit, {
  immediate: true,
});

const editedUrl = computed(() => s.value.edited_recording_url ?? null);
/** The applied edit: the player shows its marks and plays the edited file. */
const appliedEdit = computed(() =>
  s.value.recording_edit_status === 'done' && editedUrl.value
    ? (editState.value?.edit ?? null)
    : null
);
const editPart = computed(() => {
  switch (s.value.recording_edit_status) {
    case 'rendering':
      return 'making the edited file…';
    case 'failed':
      return 'edit failed';
    case 'done':
      return editedUrl.value ? `edited · ${span(s.value.recording_edited_seconds ?? 0)}` : null;
    case 'draft':
      return editedUrl.value ? 'edit not applied yet' : null;
    default:
      return null;
  }
});
const showLength = computed(() => {
  const ms = s.value.latest_recording?.duration_ms;
  if (ms) return span(ms / 1000);
  const set = prerecorded.value ? s.value.prerecorded_duration_secs : null;
  return set ? span(set) : null;
});

const playerTitle = computed(() => {
  const name = picked.value === 'own' ? `Own file · ${ownName.value}` : 'Show recording';
  return appliedEdit.value
    ? `${name} · edited · ${span(s.value.recording_edited_seconds ?? 0)}`
    : name;
});
const downloads = computed(() => {
  const main = s.value.recording_download_url ?? editedUrl.value ?? rawUrl.value;
  const original =
    s.value.recording_original_download_url ?? (editedUrl.value ? rawUrl.value : null);
  return [
    ...(main ? [{ label: 'Download', url: main }] : []),
    ...(original && original !== main ? [{ label: 'Download original', url: original }] : []),
  ];
});
/** Why the player has no waveform after the show. */
const playerMessage = computed(() => {
  if (props.readOnly || hasFile.value) return null;
  if (uploading.value) return progress.value || 'Uploading…';
  if (!post.value) return null;
  if (picked.value === 'own') return 'No own file yet. Upload one under File.';
  if (showRec.value === 'processing')
    return 'Processing the recording. The waveform shows up when it is ready.';
  // Says why, e.g. "No usable recording · Shorter than 1 minute."
  return `${recordingLine.value.replace(/\.?$/, '.')} Upload your own file under File.`;
});

const fileSummary = computed(() => {
  if (uploading.value) return `Own file · ${progress.value || 'uploading…'}`;
  if (picked.value === 'own') {
    return ['Own file', ownName.value, editPart.value].filter(Boolean).join(' · ');
  }
  switch (showRec.value) {
    case 'none':
      return s.value.on_air
        ? 'Show recording · recording now'
        : 'Show recording · starts when you go live';
    case 'processing':
      return 'Show recording · processing';
    case 'failed':
      return ownName.value
        ? 'No usable show recording'
        : 'No usable show recording · upload your own file';
    default:
      return ['Show recording', editPart.value ?? showLength.value].filter(Boolean).join(' · ');
  }
});
const fileWarn = computed(() => showRec.value === 'failed' && picked.value === 'show');
const editNote = computed(() =>
  s.value.recording_edit_status === 'failed'
    ? 'Edit failed. Open Edit recording to try again.'
    : null
);

async function pick(file: RecordingFile) {
  if (file === picked.value || picking.value) return;
  picking.value = true;
  error.value = null;
  try {
    await recordingEditApi.setSource(s.value.id, file);
    emit('changed');
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Could not pick the file';
  } finally {
    picking.value = false;
  }
}

// ---- SoundCloud -----------------------------------------------------------

const serverTime = (v?: string | null) => (v ? Date.parse(v.replace(' ', 'T') + 'Z') : NaN);
/** SoundCloud has another file than the one that would go up now. */
const stale = computed(() => {
  if (!link.value || trackUrl.value) return false;
  if (s.value.soundcloud_current != null) return !s.value.soundcloud_current;
  // Tracks from before the file was stored: only an edit after the upload tells.
  return serverTime(s.value.soundcloud_uploaded_at) < serverTime(s.value.recording_edited_at);
});
const scFile = computed(() => {
  const src = s.value.soundcloud_source;
  if (!src || trackUrl.value) return null;
  const name = src === 'own' ? 'Own file' : 'Show recording';
  return s.value.soundcloud_edited ? `${name}, edited` : name;
});
const scFileLong = computed(() => {
  const sec = s.value.recording_edited_seconds;
  return scFile.value && s.value.soundcloud_edited && !stale.value && sec
    ? `${scFile.value} (${span(sec)})`
    : scFile.value;
});
const uploadedText = computed(() => {
  const at = s.value.soundcloud_uploaded_at;
  return at && !trackUrl.value ? postTime(at.replace(' ', 'T') + 'Z') : null;
});
const scRest = computed(() => {
  const parts = [scFile.value, uploadedText.value].filter(Boolean);
  return parts.length ? ` · ${parts.join(' · ')}` : '';
});
const scSummary = computed(() => {
  if (sending.value) return 'Uploading to SoundCloud…';
  if (hasFile.value) return 'No track yet';
  if (!post.value) return 'After the show';
  if (picked.value === 'show' && showRec.value === 'processing')
    return 'After the recording is ready';
  return 'Needs a file first';
});
const canSend = computed(() => !props.readOnly && hasFile.value);

async function sendToSoundCloud() {
  // The row the host works in stays open.
  chosen.value = 'soundcloud';
  sending.value = true;
  error.value = null;
  try {
    const result = await showsApi.uploadToSoundCloud(s.value.id);
    if (result.success && result.track_url) {
      trackUrl.value = result.track_url;
      publicNow.value = false;
      emit('changed');
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

async function setPublic(makePublic: boolean) {
  if (makePublic === isPublic.value) return;
  chosen.value = 'soundcloud';
  switching.value = true;
  error.value = null;
  try {
    const result = await showsApi.setSoundCloudPrivacy(s.value.id, makePublic);
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

// ---- Instagram ------------------------------------------------------------

const igSummary = computed(() => {
  if (!post.value) return 'Goes out after the show, once the track is public';
  if (!isPublic.value || !link.value) return 'Post-announcement waits for a public track';
  // Its time, like the Social media card. Going public books no time: the host books it.
  const list = props.announcements ?? [];
  return inSlot(list, 'post')
    ? `Post-announcement: ${slotState(list, 'post')}`
    : 'Post-announcement not set up';
});

// ---- Rows -----------------------------------------------------------------

/** The row with the next step, until the host opens or closes one. */
const autoOpen = computed<Row | null>(() => {
  if (props.readOnly) return null;
  if (!hasFile.value) return post.value && showRec.value === 'failed' ? 'file' : null;
  return link.value && isPublic.value ? 'instagram' : 'soundcloud';
});
const chosen = ref<Row | 'none' | null>(null);
const open = computed(() =>
  chosen.value === null ? autoOpen.value : chosen.value === 'none' ? null : chosen.value
);
function toggle(row: Row) {
  chosen.value = open.value === row ? 'none' : row;
}
const rowId = (row: Row) => `rec-${s.value.id}-${row}`;

// ---- Status, upload, publish ----------------------------------------------

// While the server makes the edited file, look again every 5 s.
let poll: ReturnType<typeof setInterval> | undefined;
watch(
  () => s.value.recording_edit_status === 'rendering',
  (rendering) => {
    clearInterval(poll);
    if (rendering) poll = setInterval(() => emit('changed'), 5000);
  },
  { immediate: true }
);
onBeforeUnmount(() => clearInterval(poll));

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
  if (post.value) {
    if (props.recording.state === 'ok' || hasRecording.value)
      return { tone: 'green', text: 'Recording ready' };
    if (props.recording.state === 'processing') return { tone: 'amber', text: 'Processing' };
    return { tone: 'red', text: 'No recording' };
  }
  if (prerecorded.value) {
    return s.value.prerecorded_filename
      ? { tone: 'green', text: 'Set ready' }
      : { tone: 'grey', text: 'No set yet' };
  }
  if (hasRecording.value) return { tone: 'green', text: 'File uploaded' };
  if (s.value.on_air) return { tone: 'red', text: 'Rec' };
  return { tone: 'grey', text: 'Not started' };
});

const lead = computed(() => {
  if (post.value) return '';
  if (prerecorded.value) return 'The uploaded set is the recording of this episode.';
  if (s.value.on_air)
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
    await showsApi.uploadRecording(s.value.id, file, (p) => {
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

/** Close the episode; the server waits for the final recording (processing). */
async function publish() {
  publishing.value = true;
  error.value = null;
  try {
    await seriesApi.publish(s.value.id);
    emit('changed');
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Could not publish the episode';
  } finally {
    publishing.value = false;
  }
}
</script>

<template>
  <EpisodeCard
    title="Recording & SoundCloud"
    short="Recording"
    :step="4"
    :state="state"
    :status="status"
    class="recording"
  >
    <template #icon><EpisodeIcon name="cloud" /></template>

    <p v-if="lead" class="ep-lead">{{ lead }}</p>

    <!-- Another host's episode: the status and a public track's link. -->
    <dl v-if="readOnly" class="ep-kv">
      <div v-if="post">
        <dt>Recording</dt>
        <dd :class="{ ok: recording.state === 'ok', bad: recording.state === 'failed' }">
          {{ recordingLine }}
        </dd>
      </div>
      <div>
        <dt>SoundCloud</dt>
        <dd v-if="link && isPublic" class="wrap">
          Public track · <a :href="link" target="_blank" rel="noopener">Open on SoundCloud</a>
        </dd>
        <dd v-else-if="link">Private track</dd>
        <dd v-else class="mut">{{ post ? 'No track yet' : 'After the show' }}</dd>
      </div>
    </dl>

    <template v-else>
      <RecordingPlayer
        v-if="fileKey"
        :show-id="show.id"
        :file-key="fileKey"
        :title="playerTitle"
        :src="appliedEdit ? editedUrl : rawUrl"
        :edit="appliedEdit"
        :downloads="downloads"
      />
      <div v-else-if="playerMessage" class="rec-msg">
        <p :class="{ warn: showRec === 'failed' && picked === 'show' }">{{ playerMessage }}</p>
        <span v-if="showRec === 'processing' || uploading" class="rec-busy" aria-hidden="true"
          ><span></span
        ></span>
      </div>

      <p v-if="soundcloudOff" class="notice">SoundCloud is temporarily not available.</p>
      <p v-if="error" class="field-error" role="alert">{{ error }}</p>
      <input
        ref="fileInput"
        type="file"
        class="rec-file"
        accept=".mp3,.wav,audio/mpeg,audio/wav"
        aria-label="Own file"
        :disabled="uploading"
        @change="upload"
      />

      <div class="rec-rows">
        <!-- File -->
        <div class="rec-row">
          <button
            type="button"
            class="rec-toggle"
            :aria-expanded="open === 'file'"
            :aria-controls="rowId('file')"
            @click="toggle('file')"
          >
            <svg
              class="rec-icon file"
              width="22"
              height="22"
              viewBox="0 0 24 24"
              aria-hidden="true"
            >
              <path d="M9 18V5l12-2v13" />
              <circle cx="6" cy="18" r="3" />
              <circle cx="18" cy="16" r="3" />
            </svg>
            <span class="rec-name">{{ 'File ' }}</span>
            <span :class="['rec-sum', { warn: fileWarn }]">{{ fileSummary }}</span>
            <EpisodeIcon name="down" :size="18" class="rec-chev" />
          </button>
        </div>
        <div v-if="open === 'file'" :id="rowId('file')" class="rec-detail">
          <span class="rec-label">This file goes to SoundCloud</span>
          <label class="rec-choice">
            <input
              type="radio"
              name="rec-file"
              :checked="picked === 'show'"
              :disabled="showRec !== 'ok' || picking"
              @change="pick('show')"
            />
            Show recording
          </label>
          <p class="rec-help">
            {{
              showRec === 'failed'
                ? 'There is no usable recording from the show.'
                : 'Recorded on air.'
            }}
          </p>
          <label class="rec-choice">
            <input
              type="radio"
              name="rec-file"
              :checked="picked === 'own'"
              :disabled="!ownName || picking"
              @change="pick('own')"
            />
            Own file
          </label>
          <p class="rec-help">
            {{
              ownName
                ? `${ownName}. It goes to SoundCloud as it is, or with its edit.`
                : 'A finished mix from you, MP3 or WAV. It uploads to our storage first.'
            }}
          </p>
          <p v-if="editNote" class="rec-help bad">{{ editNote }}</p>
          <div class="rec-actions">
            <BaseButton
              v-if="hasFile"
              variant="secondary"
              @click="router.push(`/shows/${show.id}/edit-recording`)"
              >Edit recording</BaseButton
            >
            <BaseButton variant="secondary" :loading="uploading" @click="fileInput?.click()">{{
              ownName ? 'Replace file' : 'Choose file'
            }}</BaseButton>
          </div>
        </div>

        <!-- SoundCloud -->
        <div class="rec-row">
          <button
            type="button"
            class="rec-toggle"
            :aria-expanded="open === 'soundcloud'"
            :aria-controls="rowId('soundcloud')"
            @click="toggle('soundcloud')"
          >
            <svg class="rec-icon sc" width="24" height="22" viewBox="0 0 26 24" aria-hidden="true">
              <path d="M3 14v3M6.5 11v6M10 9v8M13.5 7.5V17" />
              <path d="M16.5 8a5 5 0 0 1 5 4.6 2.6 2.6 0 0 1-1 4.4h-3.5" />
            </svg>
            <span class="rec-name">{{ 'SoundCloud ' }}</span>
            <span v-if="link" class="rec-sum"
              ><span :class="['rec-vis', isPublic ? 'pub' : 'priv']">{{
                isPublic ? 'Public' : 'Private'
              }}</span
              >{{ scRest }}<span v-if="stale" class="rec-older">{{ ' · older file' }}</span></span
            >
            <span v-else class="rec-sum">{{ scSummary }}</span>
            <EpisodeIcon name="down" :size="18" class="rec-chev" />
          </button>
          <BaseButton
            v-if="!link && canSend"
            variant="secondary"
            class="rec-row-action"
            :disabled="soundcloudOff"
            :loading="sending"
            @click="sendToSoundCloud"
            >Upload to SoundCloud</BaseButton
          >
        </div>
        <div v-if="open === 'soundcloud'" :id="rowId('soundcloud')" class="rec-detail">
          <template v-if="link">
            <dl class="ep-kv">
              <div v-if="scFileLong">
                <dt>File</dt>
                <dd>{{ scFileLong }}</dd>
              </div>
              <div v-if="uploadedText">
                <dt>Uploaded</dt>
                <dd>{{ uploadedText }}</dd>
              </div>
            </dl>
            <a :href="link" target="_blank" rel="noopener" class="rec-link">Open on SoundCloud</a>
            <div class="rec-seg" role="group" aria-label="Who can listen">
              <button
                type="button"
                :aria-pressed="!isPublic"
                :disabled="switching || sending || soundcloudOff"
                @click="setPublic(false)"
              >
                Private
              </button>
              <button
                type="button"
                :aria-pressed="isPublic"
                :disabled="switching || sending || soundcloudOff"
                @click="setPublic(true)"
              >
                Public
              </button>
            </div>
            <p class="rec-help">
              {{
                isPublic
                  ? 'Everyone can listen. The post-announcement goes out at its time.'
                  : 'Only people with the secret link can listen.'
              }}
            </p>
            <div v-if="stale" class="rec-stale">
              <p>SoundCloud has an older file. Upload again sends the picked file.</p>
              <BaseButton
                variant="secondary"
                :disabled="soundcloudOff"
                :loading="sending"
                @click="sendToSoundCloud"
                >Upload again</BaseButton
              >
              <p class="rec-help">It makes a new private track and deletes the old one.</p>
            </div>
          </template>
          <p v-else class="rec-help">
            {{
              canSend
                ? 'The picked file goes up as a private track. You make it public when you are ready.'
                : 'Upload works once there is a file.'
            }}
          </p>
        </div>

        <!-- Instagram -->
        <div class="rec-row">
          <button
            type="button"
            class="rec-toggle"
            :aria-expanded="open === 'instagram'"
            :aria-controls="rowId('instagram')"
            @click="toggle('instagram')"
          >
            <svg class="rec-icon ig" width="22" height="22" viewBox="0 0 24 24" aria-hidden="true">
              <rect x="3" y="3" width="18" height="18" rx="5" />
              <circle cx="12" cy="12" r="4" />
            </svg>
            <span class="rec-name">{{ 'Instagram ' }}</span>
            <span :class="['rec-sum', { ok: isPublic && link }]">{{ igSummary }}</span>
            <EpisodeIcon name="down" :size="18" class="rec-chev" />
          </button>
        </div>
        <div v-if="open === 'instagram'" :id="rowId('instagram')" class="rec-detail">
          <p class="rec-text">
            The post-announcement goes out at its booked time once the SoundCloud track is public.
            You set it up in step 1 Social media.
          </p>
          <p class="rec-help">
            Snippets are up to 3 short clips from your set, 5 to 30 s each. They go into the post as
            video slides after the image, so pick the moments that make people want to listen.
          </p>
          <SnippetEditor v-if="fileKey" :key="fileKey" :show-id="show.id" />
          <p v-else class="rec-help">Snippets can be placed once the recording is ready.</p>
          <BaseButton variant="ghost" @click="emit('social')">Go to Social media</BaseButton>
        </div>
      </div>
    </template>

    <template v-if="!readOnly" #actions>
      <BaseButton
        v-if="phase === 'post' && show.episode_status !== 'finished'"
        class="xl"
        :disabled="recording.state === 'processing'"
        :loading="publishing"
        @click="publish"
        >Publish episode</BaseButton
      >
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

.wrap {
  overflow-wrap: anywhere;
}

.rec-file {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip-path: inset(50%);
  white-space: nowrap;
}

.rec-msg {
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 12px;
  min-height: 120px;
  padding: 14px;
  border: 1px solid #2a2a42;
  border-radius: 10px;
  background: #06060f;
}

.rec-msg p {
  margin: 0;
  font-size: 15px;
  line-height: 1.5;
  color: #e4e4ee;
}

.rec-msg p.warn {
  color: #fcd34d;
}

.rec-busy {
  display: block;
  max-width: 420px;
  height: 6px;
  border-radius: 3px;
  background: #2a2a42;
  overflow: hidden;
}

.rec-busy span {
  display: block;
  width: 30%;
  height: 100%;
  background: #fbbf24;
  animation: rec-slide 1.4s linear infinite;
}

@keyframes rec-slide {
  from {
    transform: translateX(-100%);
  }
  to {
    transform: translateX(340%);
  }
}

.rec-rows {
  border-bottom: 1px solid #2a2a42;
}

.rec-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
  padding: 4px 0;
  border-top: 1px solid #2a2a42;
}

.rec-toggle {
  flex: 1 1 320px;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 14px;
  min-height: 60px;
  padding: 8px 10px;
  border: 0;
  border-radius: var(--radius-lg);
  background: transparent;
  color: var(--color-text);
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.rec-toggle:hover {
  background: #151530;
}

.rec-toggle:focus-visible,
.rec-seg button:focus-visible,
.rec-link:focus-visible {
  outline: 2px solid var(--color-primary);
  outline-offset: 2px;
}

.rec-icon {
  flex: none;
  fill: none;
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.rec-icon.file {
  stroke: var(--color-primary);
}

.rec-icon.sc {
  stroke: #ff8a3d;
}

.rec-icon.ig {
  stroke: #f472b6;
}

.rec-name {
  flex: none;
  width: 110px;
  font: 700 11px var(--font-family);
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: #ddd;
}

.rec-sum {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  font-size: 14px;
  color: #c4c4d4;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.rec-sum.warn,
.rec-older {
  color: #fcd34d;
}

.rec-sum.ok {
  color: #4ade80;
}

.rec-vis {
  display: inline-flex;
  margin-right: 2px;
  padding: 2px 8px;
  border-radius: 999px;
  font: 700 11px var(--font-family);
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.rec-vis.priv {
  background: #3b2410;
  color: #fb923c;
}

.rec-vis.pub {
  background: #183b18;
  color: #4ade80;
}

.rec-chev {
  flex: none;
  color: #a8a8bc;
  transition: transform 0.15s;
}

.rec-toggle[aria-expanded='true'] .rec-chev {
  transform: rotate(180deg);
}

.rec-row-action {
  margin-right: 10px;
}

.rec-detail {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 12px;
  padding: 4px 10px 20px 58px;
}

.rec-label {
  font: 700 11px var(--font-family);
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: #a8a8bc;
}

.rec-choice {
  display: flex;
  align-items: center;
  gap: 12px;
  min-height: 32px;
  font-size: 15px;
  font-weight: 600;
  color: #fff;
  cursor: pointer;
}

.rec-choice input {
  width: 20px;
  height: 20px;
  margin: 0;
  accent-color: var(--color-primary);
}

.rec-choice:has(input:disabled) {
  color: #8e8ea4;
  cursor: default;
}

.rec-help,
.rec-text {
  margin: 0;
  max-width: 640px;
  font-size: 13px;
  line-height: 1.5;
  color: #a8a8bc;
}

.rec-choice + .rec-help {
  margin: -8px 0 0 32px;
}

.rec-text {
  font-size: 14px;
  color: #e4e4ee;
}

.rec-help.bad {
  color: #f87171;
}

.rec-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}

.rec-link {
  color: var(--color-primary);
  font-size: 14px;
}

.rec-link:hover {
  color: #fff;
}

.rec-seg {
  display: flex;
}

.rec-seg button {
  min-width: 104px;
  min-height: 44px;
  padding: 0 14px;
  border: 1px solid #34344c;
  background: transparent;
  color: #c4c4d4;
  font: 700 12px var(--font-family);
  letter-spacing: 0.05em;
  text-transform: uppercase;
  cursor: pointer;
}

.rec-seg button:first-child {
  border-radius: 8px 0 0 8px;
}

.rec-seg button:last-child {
  border-left: 0;
  border-radius: 0 8px 8px 0;
}

.rec-seg button[aria-pressed='true'] {
  border-color: var(--color-primary);
  background: var(--color-primary);
  color: #000;
}

.rec-seg button:disabled {
  opacity: 0.4;
  cursor: default;
}

.rec-stale {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
  padding: 10px 12px;
  border: 1px solid #6b5a1e;
  border-radius: 8px;
  background: rgba(251, 191, 36, 0.07);
}

.rec-stale > p:first-child {
  flex: 1 1 220px;
  margin: 0;
  font-size: 14px;
  color: #fcd34d;
}

.rec-stale .rec-help {
  flex-basis: 100%;
}

@media (max-width: 640px) {
  .rec-detail {
    padding-left: 10px;
  }

  /* The summary gets its own line under the row name. */
  .rec-toggle {
    flex-wrap: wrap;
    row-gap: 4px;
  }

  .rec-name {
    flex: 1;
    width: auto;
  }

  .rec-chev {
    order: 2;
  }

  .rec-sum {
    flex-basis: 100%;
    order: 3;
    padding-left: 36px;
    white-space: normal;
  }
}
</style>
