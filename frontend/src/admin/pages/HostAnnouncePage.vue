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
import { CAPTION_MAX, EPISODE_LIMIT, HEADLINE_MAX, inSlot, postTime } from '../promotion';

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

const image = ref<AnnouncementImage>('cover');
const headline = ref('');
const caption = ref('');
const collaborator = ref(true);
const when = ref('');
const layout = ref<'band' | 'split'>('band');

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
    (!needsTime.value || when.value !== '')
);

async function load() {
  error.value = null;
  try {
    const [loaded, announcements, offered] = await Promise.all([
      showsApi.get(id.value),
      announcementsApi.list(id.value),
      announcementsApi.times(id.value, slot.value),
    ]);
    show.value = loaded;
    list.value = announcements;
    times.value = offered;
    const series = loaded.series_slug ? await seriesApi.get(loaded.series_slug) : null;
    handle.value = series?.instagram_handle || null;

    const a = current.value;
    image.value = a?.image_source ?? 'cover';
    headline.value = a?.headline ?? '';
    // The caption draft comes from the episode description.
    caption.value = a?.caption ?? loaded.description ?? '';
    collaborator.value = a ? a.collaborator : !!handle.value;
    layout.value = a?.layout ?? 'band';
    const free = offered.filter((t) => t.free);
    when.value =
      (a?.scheduled_at && offered.some((t) => t.scheduled_at === a.scheduled_at)
        ? a.scheduled_at
        : (free.find((t) => t.default) ?? free[0])?.scheduled_at) ?? '';
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Could not load the announcement';
  }
}

watch([id, slot], load, { immediate: true });

// The preview is rendered by the backend; wait until the typing stops.
const preview = ref('');
let previewTimer: ReturnType<typeof setTimeout> | null = null;
watch(
  [headline, layout, id, slot],
  () => {
    if (previewTimer) clearTimeout(previewTimer);
    previewTimer = setTimeout(() => {
      preview.value = announcementsApi.previewUrl(
        id.value,
        slot.value,
        headline.value,
        layout.value
      );
    }, 400);
  },
  { immediate: true }
);
onUnmounted(() => {
  if (previewTimer) clearTimeout(previewTimer);
});

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
      layout: layout.value,
      collaborator: !!handle.value && collaborator.value,
      ...(when.value ? { scheduled_at: when.value } : {}),
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
            <fieldset class="group" :disabled="sent">
              <h2 class="board-card-title"><BoardIcon name="image" />Image</h2>
              <div class="choices">
                <label class="check"
                  ><input v-model="image" type="radio" value="cover" />Episode cover</label
                >
                <!-- No upload endpoint yet. -->
                <label class="check"
                  ><input v-model="image" type="radio" value="upload" disabled />Upload a
                  photo</label
                >
                <label class="check"
                  ><input v-model="image" type="radio" value="guest" />Guest photo</label
                >
              </div>
              <p class="hint">
                The station frame, font and filter are added. Photo upload comes later.
              </p>
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

            <fieldset class="group" :disabled="sent">
              <h2 class="board-card-title"><BoardIcon name="calendar" />When</h2>
              <div v-if="times.length" class="choices">
                <label v-for="t in times" :key="t.scheduled_at" class="check"
                  ><input
                    v-model="when"
                    type="radio"
                    :value="t.scheduled_at"
                    :disabled="!t.free"
                  />{{ postTime(t.scheduled_at)
                  }}<span v-if="!t.free" class="taken"> · {{ t.reason || 'Full' }}</span></label
                >
              </div>
              <p v-else class="hint">
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
              <BaseButton class="xl" :disabled="!valid || sent" :loading="saving" @click="schedule"
                >Schedule post</BaseButton
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
