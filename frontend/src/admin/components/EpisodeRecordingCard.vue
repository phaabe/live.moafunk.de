<script setup lang="ts">
import { computed, ref } from 'vue';
import { BaseButton } from '@shared/components';
import { showsApi, type ShowDetail } from '../api';
import BoardIcon from './BoardIcon.vue';

// Flow block "Recording" of the Episode screen: a picked file uploads at once
// and becomes the episode recording; Upload to SoundCloud sends it as a
// private track. Only an admin upload starts SoundCloud on its own. Hosts
// and admins switch the track public or private; public unlocks the
// post-announcement.

const props = defineProps<{ show: ShowDetail }>();
const emit = defineEmits<{ uploaded: []; changed: [] }>();

const uploading = ref(false);
const progress = ref('');
const sending = ref(false);
const trackUrl = ref<string | null>(null);
const error = ref<string | null>(null);
const switching = ref(false);
/** Set after a switch here, until the parent reloads the show. */
const publicNow = ref<boolean | null>(null);

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
  <section class="board-card">
    <div class="board-card-head">
      <h2 class="board-card-title"><BoardIcon name="music" />Recording</h2>
    </div>
    <div class="board-card-body">
      <label class="field">
        <span class="label">Recording file</span>
        <input
          type="file"
          class="input"
          accept=".mp3,.wav,audio/mpeg,audio/wav"
          :disabled="uploading"
          @change="upload"
        />
      </label>
      <p v-if="uploading" class="status">{{ progress || 'Uploading…' }}</p>
      <p v-else-if="show.recording_filename" class="status">{{ show.recording_filename }}</p>
      <p v-if="link" class="status">
        {{ isPublic ? 'Public on SoundCloud' : 'Private on SoundCloud' }}:
        <a :href="link" target="_blank" rel="noopener">{{ link }}</a>
      </p>
      <p v-if="error" class="field-error" role="alert">{{ error }}</p>
      <div>
        <BaseButton
          variant="secondary"
          :disabled="!hasRecording || uploading"
          :loading="sending"
          @click="sendToSoundCloud"
        >
          Upload to SoundCloud
        </BaseButton>
        <BaseButton
          v-if="link"
          variant="secondary"
          :disabled="sending"
          :loading="switching"
          @click="switchPublic"
        >
          {{ isPublic ? 'Make track private' : 'Make track public' }}
        </BaseButton>
      </div>
    </div>
  </section>
</template>

<style scoped>
.status {
  margin: 0;
  overflow-wrap: anywhere;
  font-size: var(--font-size-sm);
}
</style>
