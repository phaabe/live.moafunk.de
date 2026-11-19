<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { BaseButton } from '@shared/components';
import {
  announcementsApi,
  seriesApi,
  showsApi,
  type Announcement,
  type AnnouncementImage,
  type AnnouncementTime,
  type ShowDetail,
} from '../api';
import BoardIcon from '../components/BoardIcon.vue';
import { episodeDay } from '../episodeRow';
import {
  CAPTION_MAX,
  EPISODE_LIMIT,
  HEADLINE_MAX,
  inSlot,
  INSTAGRAM_OFF_NOTE,
  PAUSED_NOTE,
  PHOTO_MAX_MB,
  photoError as checkPhoto,
  postTime,
} from '../promotion';

// Flow screen `announce`: the composer of the pre- or post-announcement
// (/shows/<id>/announce/<pre|post>). The post slot uses the same screen.

const route = useRoute();
const router = useRouter();

const id = computed(() => Number(route.params.id));
const slot = computed(() => (route.params.slot === 'post' ? 'post' : 'pre'));

const show = ref<ShowDetail | null>(null);
const handle = ref<string | null>(null);
const list = ref<Announcement[]>([]);
const times = ref<AnnouncementTime[]>([]);
const error = ref<string | null>(null);
const saving = ref(false);
/** Until load() is done its values would overwrite what the host typed. */
const loading = ref(true);
const paused = ref(false);
const instagramOff = ref(false);
const needsApproval = ref(false);

const image = ref<AnnouncementImage>('cover');
/** The uploaded photo; only used with `image === 'upload'`. */
const imageKey = ref<string | null>(null);
const uploading = ref(false);
const photoError = ref<string | null>(null);
const headline = ref('');
const caption = ref('');
const collaborator = ref(true);
const when = ref('');
const layout = ref<'band' | 'split'>('band');
/** `when` value of the "Post now" choice (pre slot only). */
const NOW = 'now';

const current = computed(() => inSlot(list.value, slot.value));
/** After it is sent the caption is frozen. */
const sent = computed(() => current.value?.state === 'sent');
const showName = computed(() => show.value?.series_name ?? show.value?.title ?? '');
const title = computed(() => (slot.value === 'post' ? 'Post-announcement' : 'Pre-announcement'));
const used = computed(
  () => list.value.filter((a) => a.state !== 'cancelled' && a.slot !== slot.value).length + 1
);

const headlineError = computed(() =>
  headline.value.length > HEADLINE_MAX ? `Headline: max ${HEADLINE_MAX} characters.` : null
);
const captionError = computed(() =>
  caption.value.length > CAPTION_MAX ? `Caption: max ${CAPTION_MAX} characters.` : null
);
/** The post slot may have no time yet: it waits for the public SoundCloud track. */
const needsTime = computed(() => slot.value === 'pre');
const valid = computed(
  () =>
    !headlineError.value &&
    !captionError.value &&
    headline.value.trim() !== '' &&
    caption.value.trim() !== '' &&
    (image.value !== 'upload' || !!imageKey.value) &&
    !uploading.value &&
    !loading.value &&
    (!needsTime.value || when.value !== '')
);

async function load() {
  error.value = null;
  loading.value = true;
  try {
    const [loaded, announcements, offered, meta] = await Promise.all([
      showsApi.get(id.value),
      announcementsApi.list(id.value),
      announcementsApi.times(id.value, slot.value),
      announcementsApi.meta(id.value),
    ]);
    show.value = loaded;
    paused.value = meta.paused;
    instagramOff.value = !meta.services.instagram;
    needsApproval.value = meta.needs_approval;
    list.value = announcements;
    times.value = offered;
    const series = loaded.series_slug ? await seriesApi.get(loaded.series_slug) : null;
    handle.value = series?.instagram_handle || null;

    const a = current.value;
    image.value = a?.image_source ?? 'cover';
    imageKey.value = a?.image_key ?? null;
    headline.value = a?.headline ?? '';
    // The caption draft comes from the episode description.
    caption.value = a?.caption ?? loaded.description ?? '';
    collaborator.value = a ? a.collaborator : !!handle.value;
    layout.value = a?.layout ?? 'band';
    const free = offered.filter((t) => t.free);
    when.value = a?.send_now
      ? NOW
      : ((a?.scheduled_at && offered.some((t) => t.scheduled_at === a.scheduled_at)
          ? a.scheduled_at
          : (free.find((t) => t.default) ?? free[0])?.scheduled_at) ?? '');
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Could not load the announcement';
  } finally {
    loading.value = false;
  }
}

watch([id, slot], load, { immediate: true });

// The preview is rendered by the backend; wait until the typing stops.
const preview = ref('');
let previewTimer: ReturnType<typeof setTimeout> | null = null;
watch(
  [headline, layout, image, imageKey, id, slot],
  () => {
    if (previewTimer) clearTimeout(previewTimer);
    previewTimer = setTimeout(() => {
      preview.value = announcementsApi.previewUrl(
        id.value,
        slot.value,
        headline.value,
        layout.value,
        image.value,
        image.value === 'upload' ? imageKey.value : null
      );
    }, 400);
  },
  { immediate: true }
);
onUnmounted(() => {
  if (previewTimer) clearTimeout(previewTimer);
});

/** Check the picked photo, then upload it at once. */
async function pickPhoto(event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file) return;
  photoError.value = checkPhoto(file);
  if (photoError.value) {
    input.value = '';
    return;
  }
  uploading.value = true;
  try {
    imageKey.value = (await announcementsApi.upload(id.value, file)).image_key;
  } catch (e) {
    photoError.value = e instanceof Error ? e.message : 'Could not upload the photo';
    input.value = '';
  } finally {
    uploading.value = false;
  }
}

const back = () => router.push(`/shows/${id.value}`);

async function schedule() {
  if (!valid.value || saving.value || sent.value) return;
  saving.value = true;
  error.value = null;
  try {
    await announcementsApi.save(id.value, slot.value, {
      headline: headline.value.trim(),
      caption: caption.value.trim(),
      image_source: image.value,
      ...(image.value === 'upload' && imageKey.value ? { image_key: imageKey.value } : {}),
      layout: layout.value,
      collaborator: !!handle.value && collaborator.value,
      ...(when.value === NOW ? { send_now: true } : when.value ? { scheduled_at: when.value } : {}),
    });
    await back();
  } catch (e) {
    // A 409 names the taken slot or the limit.
    error.value = e instanceof Error ? e.message : 'Could not schedule the post';
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <div class="announce-page host-page">
    <router-link :to="`/shows/${id}`" class="back-link"
      ><BoardIcon name="back" :size="16" /><span class="sep">&lt; </span>Episode</router-link
    >
    <template v-if="show">
      <h1 class="page-title">{{ title }}</h1>
      <p class="pill status-banner">
        <span class="dot" aria-hidden="true"></span>Feed post 4:5 · {{ episodeDay(show.date) }} ·
        {{ showName }}
      </p>
      <div class="cols">
        <section class="board-card main">
          <div class="board-card-body">
            <fieldset class="group" :disabled="sent || loading">
              <h2 class="board-card-title"><BoardIcon name="image" />Image</h2>
              <div class="choices">
                <label class="check"
                  ><input v-model="image" type="radio" value="cover" />Episode cover</label
                >
                <label class="check"
                  ><input v-model="image" type="radio" value="upload" />Upload a photo</label
                >
                <label class="check"
                  ><input v-model="image" type="radio" value="guest" />Guest photo</label
                >
              </div>
              <template v-if="image === 'upload'">
                <input
                  type="file"
                  accept="image/jpeg,image/png"
                  aria-label="Photo file"
                  :disabled="uploading"
                  @change="pickPhoto"
                />
                <p class="hint">
                  {{
                    uploading
                      ? 'Uploading photo…'
                      : imageKey
                        ? 'Photo uploaded.'
                        : `JPEG or PNG, max ${PHOTO_MAX_MB} MB.`
                  }}
                </p>
                <p v-if="photoError" class="field-error">{{ photoError }}</p>
              </template>
              <p class="hint">The station frame, font and filter are added.</p>
              <label class="field">
                <span class="label">Headline on the image</span>
                <input v-model="headline" type="text" class="input" />
                <span class="hint"
                  >Show name, date and time are added below it. {{ headline.length }} /
                  {{ HEADLINE_MAX }}</span
                >
              </label>
              <p v-if="headlineError" class="field-error">{{ headlineError }}</p>
              <label class="field">
                <span class="label">Caption</span>
                <textarea v-model="caption" class="input" rows="5"></textarea>
                <span class="hint"
                  >Date, time, the link and the station hashtags are added at the end.
                  {{ caption.length }} / {{ CAPTION_MAX }}</span
                >
              </label>
              <p v-if="captionError" class="field-error">{{ captionError }}</p>
              <label v-if="handle" class="check"
                ><input v-model="collaborator" type="checkbox" />Invite @{{ handle }} as
                collaborator</label
              >
            </fieldset>

            <fieldset class="group" :disabled="sent || loading">
              <h2 class="board-card-title"><BoardIcon name="calendar" />When</h2>
              <div v-if="times.length || slot === 'pre'" class="choices">
                <label v-for="t in times" :key="t.scheduled_at" class="check"
                  ><input
                    v-model="when"
                    type="radio"
                    :value="t.scheduled_at"
                    :disabled="!t.free"
                  />{{ postTime(t.scheduled_at)
                  }}<span v-if="!t.free" class="taken"> · {{ t.reason || 'Full' }}</span></label
                >
                <label v-if="slot === 'pre'" class="check"
                  ><input v-model="when" type="radio" :value="NOW" />Post now</label
                >
              </div>
              <p v-if="when === NOW" class="hint">
                Post now sends it within a minute, for example when there is not enough time before
                the show. The station limits per day and week still apply, and a new host's post
                still waits for an admin.
              </p>
              <p v-if="!times.length" class="hint">
                {{ slot === 'post' ? 'Waiting for SoundCloud' : 'No free time left.' }}
              </p>
            </fieldset>
          </div>
        </section>

        <section class="board-card side">
          <div class="board-card-head">
            <h2 class="board-card-title"><BoardIcon name="eye" />Preview</h2>
          </div>
          <div class="board-card-body">
            <img
              v-if="preview"
              class="preview"
              :src="preview"
              :alt="`Preview · 4:5 post by moafunk_radio${handle && collaborator ? ` and ${handle}` : ''}`"
            />
            <p v-if="instagramOff" class="notice">{{ INSTAGRAM_OFF_NOTE }}</p>
            <p v-if="paused" class="notice">{{ PAUSED_NOTE }}</p>
            <p v-if="needsApproval" class="hint">
              An admin checks this post first. It is scheduled after the approval.
            </p>
            <p class="notice">
              After it is posted, the caption cannot be changed. Until then you can edit or cancel
              it here.
            </p>
            <p class="hint">
              Uses {{ used }} of {{ EPISODE_LIMIT }} announcements for this episode.
            </p>
            <p v-if="error" class="field-error" role="alert">{{ error }}</p>
            <div class="actions">
              <BaseButton variant="secondary" @click="back">Cancel</BaseButton>
              <BaseButton
                class="xl"
                :disabled="!valid || sent || instagramOff"
                :loading="saving"
                @click="schedule"
                >{{ needsApproval ? 'Send for approval' : 'Schedule post' }}</BaseButton
              >
            </div>
          </div>
        </section>
      </div>
    </template>
    <p v-else-if="error" class="field-error" role="alert">{{ error }}</p>
  </div>
</template>

<style scoped>
.announce-page {
  max-width: 1120px;
}

.group {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
  margin: 0;
  padding: 0;
  border: 0;
}

.field {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
}

.input {
  color: var(--color-text);
  padding: var(--spacing-sm) var(--spacing-md);
}

.field-error {
  color: var(--color-error);
  font-size: var(--font-size-sm);
  margin: 0;
}

.choices {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 20px;
}

.hint,
.taken {
  margin: 0;
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
  font-weight: 400;
}

.notice {
  margin: 0;
  padding: 12px 14px;
  border: 1px solid #6b5a1e;
  border-radius: var(--radius-lg);
  color: #f3d77a;
}

.preview {
  width: 100%;
  aspect-ratio: 4 / 5;
  object-fit: cover;
  border-radius: var(--radius-lg);
  background: #07071a;
}

.actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: var(--spacing-md);
}
</style>
