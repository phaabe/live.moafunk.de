<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue';
import { BaseButton } from '@shared/components';
import {
  announcementsApi,
  seriesApi,
  showsApi,
  type Announcement,
  type AnnouncementImage,
  type AnnouncementSlot,
  type AnnouncementTime,
  type ShowDetail,
} from '../api';
import BoardIcon from './BoardIcon.vue';
import {
  CAPTION_MAX,
  cleanHandle,
  COLLABORATORS_MAX,
  EPISODE_LIMIT,
  groupTimes,
  handleError,
  HEADLINE_MAX,
  inSlot,
  INSTAGRAM_OFF_NOTE,
  PAUSED_NOTE,
  PHOTO_MAX_MB,
  photoError as checkPhoto,
  postTime,
  TAGS_MAX,
} from '../promotion';

// The post widget of one announcement slot (#1052): photo, text, people,
// time and a live preview. The episode page shows it in a popup; the flow
// screen `announce` (/shows/<id>/announce/<pre|post>) shows it as a page.
// The on-air story has no text and no people: it goes out at go-live.

const props = defineProps<{ showId: number; slot: AnnouncementSlot }>();
const emit = defineEmits<{ done: []; cancel: [] }>();

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
const extras = ref<string[]>([]);
const tags = ref<string[]>([]);
const extraDraft = ref('');
const tagDraft = ref('');
const extraError = ref<string | null>(null);
const tagError = ref<string | null>(null);
const when = ref('');
const layout = ref<'band' | 'split'>('band');
/** `when` value of the "Post now" choice (pre slot only). */
const NOW = 'now';

/** Feed posts have text and people; the story has neither. */
const feed = computed(() => props.slot !== 'onair');
const current = computed(() => inSlot(list.value, props.slot));
/** After it is sent the caption is frozen. */
const sent = computed(() => current.value?.state === 'sent');
const used = computed(
  () => list.value.filter((a) => a.state !== 'cancelled' && a.slot !== props.slot).length + 1
);
const days = computed(() => groupTimes(times.value));
/** A suggested time exists and another one is picked. */
const suggested = computed(() =>
  times.value.some((t) => t.default && t.free && t.scheduled_at !== when.value)
);
/** The show account counts as one of the collaborators when it is invited. */
const collaborators = computed(
  () => extras.value.length + (handle.value && collaborator.value ? 1 : 0)
);

const headlineError = computed(() =>
  headline.value.length > HEADLINE_MAX ? `Headline: max ${HEADLINE_MAX} characters.` : null
);
const captionError = computed(() =>
  caption.value.length > CAPTION_MAX ? `Caption: max ${CAPTION_MAX} characters.` : null
);
/** The post slot may have no time yet: it waits for the public SoundCloud track. */
const needsTime = computed(() => props.slot === 'pre');
const valid = computed(
  () =>
    (!feed.value ||
      (!headlineError.value &&
        !captionError.value &&
        headline.value.trim() !== '' &&
        caption.value.trim() !== '')) &&
    (image.value !== 'upload' || !!imageKey.value) &&
    !uploading.value &&
    !loading.value &&
    (!needsTime.value || when.value !== '')
);
const action = computed(() => {
  if (needsApproval.value) return 'Send for approval';
  return feed.value ? 'Schedule post' : 'Schedule story';
});
/** When the post goes out, in one line. */
const whenLine = computed(() => {
  if (props.slot === 'onair')
    return 'Goes out when you go live. A pre-recorded show sends it when its file starts playing.';
  if (when.value === NOW)
    return 'Post now sends it within a minute, for example when there is not enough time before the show. The station limits per day and week still apply, and a new host’s post still waits for an admin.';
  if (!when.value) return '';
  const at = `Goes out ${postTime(when.value)}.`;
  return props.slot === 'post' ? `${at} Only once the SoundCloud track is public.` : at;
});

async function load() {
  error.value = null;
  loading.value = true;
  try {
    const [loaded, announcements, offered, meta] = await Promise.all([
      showsApi.get(props.showId),
      announcementsApi.list(props.showId),
      feed.value ? announcementsApi.times(props.showId, props.slot) : [],
      announcementsApi.meta(props.showId),
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
    extras.value = [...(a?.extra_collaborators ?? [])];
    tags.value = [...(a?.user_tags ?? [])];
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

watch(() => [props.showId, props.slot], load, { immediate: true });

// The preview is rendered by the backend; wait until the typing stops.
const preview = ref('');
let previewTimer: ReturnType<typeof setTimeout> | null = null;
watch(
  [headline, layout, image, imageKey, () => props.showId, () => props.slot],
  () => {
    if (previewTimer) clearTimeout(previewTimer);
    previewTimer = setTimeout(() => {
      preview.value = announcementsApi.previewUrl(
        props.showId,
        props.slot,
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
const previewAlt = computed(() => {
  if (!feed.value) return 'Preview · 9:16 story by moafunk_radio';
  const names = [
    ...(handle.value && collaborator.value ? [handle.value] : []),
    ...extras.value,
  ].join(' and ');
  return `Preview · 4:5 post by moafunk_radio${names ? ` and ${names}` : ''}`;
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
    imageKey.value = (await announcementsApi.upload(props.showId, file)).image_key;
  } catch (e) {
    photoError.value = e instanceof Error ? e.message : 'Could not upload the photo';
    input.value = '';
  } finally {
    uploading.value = false;
  }
}

/** Add a typed handle to a list; null when it was added (or already there). */
function addHandle(draft: string, into: string[], others: string[]): string | null {
  const h = cleanHandle(draft).toLowerCase();
  if (!h) return null;
  const wrong = handleError(h);
  if (wrong) return wrong;
  if (!into.includes(h) && !others.includes(h)) into.push(h);
  return null;
}

function addExtra() {
  if (collaborators.value >= COLLABORATORS_MAX) return;
  const own = handle.value && collaborator.value ? [handle.value.toLowerCase()] : [];
  extraError.value = addHandle(extraDraft.value, extras.value, own);
  if (!extraError.value) extraDraft.value = '';
}

function addTag() {
  if (tags.value.length >= TAGS_MAX) return;
  tagError.value = addHandle(tagDraft.value, tags.value, []);
  if (!tagError.value) tagDraft.value = '';
}

const remove = (from: string[], h: string) => from.splice(from.indexOf(h), 1);

async function schedule() {
  if (!valid.value || saving.value || sent.value) return;
  saving.value = true;
  error.value = null;
  const photo = image.value === 'upload' && imageKey.value ? { image_key: imageKey.value } : {};
  try {
    await announcementsApi.save(
      props.showId,
      props.slot,
      feed.value
        ? {
            headline: headline.value.trim(),
            caption: caption.value.trim(),
            image_source: image.value,
            ...photo,
            layout: layout.value,
            collaborator: !!handle.value && collaborator.value,
            extra_collaborators: extras.value,
            user_tags: tags.value,
            ...(when.value === NOW
              ? { send_now: true }
              : when.value
                ? { scheduled_at: when.value }
                : {}),
          }
        : {
            headline: '',
            caption: '',
            image_source: image.value,
            ...photo,
            layout: 'band',
            // A story takes no collaborator.
            collaborator: false,
          }
    );
    emit('done');
  } catch (e) {
    // A 409 names the taken slot or the limit.
    error.value = e instanceof Error ? e.message : 'Could not schedule the post';
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <div class="composer cols">
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
          <template v-if="feed">
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
          </template>
        </fieldset>

        <fieldset v-if="feed" class="group" :disabled="sent || loading">
          <h2 class="board-card-title"><BoardIcon name="users" />People</h2>
          <label v-if="handle" class="check"
            ><input
              v-model="collaborator"
              type="checkbox"
              :disabled="!collaborator && collaborators >= COLLABORATORS_MAX"
            />Invite @{{ handle }} as collaborator</label
          >
          <div class="people">
            <label class="field">
              <span class="label">More collaborators</span>
              <span class="add">
                <input
                  v-model="extraDraft"
                  type="text"
                  class="input"
                  placeholder="@account"
                  :disabled="collaborators >= COLLABORATORS_MAX"
                  @keydown.enter.prevent="addExtra"
                />
                <BaseButton
                  variant="secondary"
                  :disabled="collaborators >= COLLABORATORS_MAX || !extraDraft.trim()"
                  @click="addExtra"
                  >Invite</BaseButton
                >
              </span>
            </label>
            <ul v-if="extras.length" class="chips">
              <li v-for="h in extras" :key="h">
                @{{ h }}
                <button
                  type="button"
                  class="chip-x"
                  :aria-label="`Remove @${h}`"
                  @click="remove(extras, h)"
                >
                  <BoardIcon name="close" :size="14" />
                </button>
              </li>
            </ul>
            <p class="hint">
              Up to {{ COLLABORATORS_MAX }} in total. Instagram asks each one to accept; then the
              post also shows on their profile.
            </p>
            <p v-if="extraError" class="field-error">{{ extraError }}</p>
          </div>
          <div class="people">
            <label class="field">
              <span class="label">Tag people</span>
              <span class="add">
                <input
                  v-model="tagDraft"
                  type="text"
                  class="input"
                  placeholder="@guest"
                  :disabled="tags.length >= TAGS_MAX"
                  @keydown.enter.prevent="addTag"
                />
                <BaseButton
                  variant="secondary"
                  :disabled="tags.length >= TAGS_MAX || !tagDraft.trim()"
                  @click="addTag"
                  >Tag</BaseButton
                >
              </span>
            </label>
            <ul v-if="tags.length" class="chips">
              <li v-for="h in tags" :key="h">
                @{{ h }}
                <button
                  type="button"
                  class="chip-x"
                  :aria-label="`Remove @${h}`"
                  @click="remove(tags, h)"
                >
                  <BoardIcon name="close" :size="14" />
                </button>
              </li>
            </ul>
            <p class="hint">Guests, artists or labels on the image. No invite needed.</p>
            <p v-if="tagError" class="field-error">{{ tagError }}</p>
          </div>
        </fieldset>

        <fieldset class="group" :disabled="sent || loading">
          <h2 class="board-card-title"><BoardIcon name="calendar" />When</h2>
          <template v-if="feed">
            <div v-if="times.length || slot === 'pre'" class="week">
              <fieldset v-for="d in days" :key="d.day" class="day">
                <legend>{{ d.day }}</legend>
                <label
                  v-for="t in d.times"
                  :key="t.scheduled_at"
                  :class="[
                    'time',
                    { on: when === t.scheduled_at, suggested: t.default, taken: !t.free },
                  ]"
                  ><input
                    v-model="when"
                    class="sr-only"
                    type="radio"
                    name="when"
                    :value="t.scheduled_at"
                    :disabled="!t.free"
                  /><span class="sr-only">{{ d.day }} · </span><span class="at">{{ t.time }}</span
                  ><span v-if="!t.free" class="taken-why"> · {{ t.reason || 'Full' }}</span></label
                >
              </fieldset>
            </div>
            <p v-if="suggested" class="hint">The dashed time is the suggested one.</p>
            <label v-if="slot === 'pre'" class="check"
              ><input v-model="when" type="radio" :value="NOW" />Post now</label
            >
            <p v-if="!times.length" class="hint">
              {{ slot === 'post' ? 'Waiting for SoundCloud' : 'No free time left.' }}
            </p>
          </template>
          <p v-if="whenLine" class="when-line" role="status">{{ whenLine }}</p>
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
          :class="['preview', { story: !feed }]"
          :src="preview"
          :alt="previewAlt"
        />
        <p v-if="feed && caption.trim()" class="caption-preview">
          <b>moafunk_radio</b> {{ caption.trim() }}
        </p>
        <p v-if="instagramOff" class="notice">{{ INSTAGRAM_OFF_NOTE }}</p>
        <p v-if="paused" class="notice">{{ PAUSED_NOTE }}</p>
        <p v-if="needsApproval" class="hint">
          An admin checks this post first. It is scheduled after the approval.
        </p>
        <p class="notice">
          After it is posted, the {{ feed ? 'caption' : 'story' }} cannot be changed. Until then you
          can edit or cancel it here.
        </p>
        <p class="hint">Uses {{ used }} of {{ EPISODE_LIMIT }} announcements for this episode.</p>
        <p v-if="error" class="field-error" role="alert">{{ error }}</p>
        <div class="actions">
          <BaseButton variant="secondary" @click="emit('cancel')">Cancel</BaseButton>
          <BaseButton
            class="xl"
            :disabled="!valid || sent || instagramOff"
            :loading="saving"
            @click="schedule"
            >{{ action }}</BaseButton
          >
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.group {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
  margin: 0 0 var(--spacing-lg);
  padding: 0;
  border: 0;
  min-width: 0;
}

.group:last-child {
  margin-bottom: 0;
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

.choices {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 20px;
}

.hint {
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

/* People: a text field with its button, then the added accounts as chips. */
.people {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
}

.add {
  display: flex;
  gap: var(--spacing-sm);
}

.add .input {
  flex: 1;
  min-width: 0;
}

.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.chips li {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  min-height: 36px;
  padding: 0 2px 0 12px;
  border-radius: 999px;
  background: #23233d;
  font-size: var(--font-size-sm);
}

.chip-x {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  padding: 0;
  border: 0;
  border-radius: 50%;
  background: none;
  color: #a0a0b4;
  cursor: pointer;
}

.chip-x:focus-visible {
  outline: 2px solid var(--color-primary);
}

/* Book a slot: one column per day, its times as pills. */
.week {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(92px, 1fr));
  gap: 8px;
}

.day {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
  margin: 0;
  padding: 0;
  border: 0;
}

.day legend {
  padding: 0 0 6px;
  font-size: var(--font-size-sm);
  font-weight: 700;
  text-align: center;
}

.time {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  min-height: 44px;
  padding: 4px;
  border: 1px solid #3a3a55;
  border-radius: 10px;
  font-weight: 700;
  text-align: center;
  cursor: pointer;
}

.time.suggested {
  border-style: dashed;
  border-color: var(--color-primary);
  color: var(--color-primary);
}

.time.on {
  border-style: solid;
  border-color: var(--color-primary);
  background: var(--color-primary);
  color: #0f0f23;
}

.time.taken {
  border-color: #23233d;
  background: #15152a;
  color: #8a8aa0;
  font-weight: 400;
  cursor: not-allowed;
}

.time.taken .at {
  text-decoration: line-through;
}

.time:has(input:focus-visible) {
  outline: 2px solid var(--color-primary);
  outline-offset: 2px;
}

.taken-why {
  font-size: 11px;
}

.when-line {
  margin: 0;
  padding: 12px 14px;
  border-radius: var(--radius-lg);
  background: #1a1a33;
}

.preview {
  width: 100%;
  aspect-ratio: 4 / 5;
  object-fit: cover;
  border-radius: var(--radius-lg);
  background: #07071a;
}

.preview.story {
  aspect-ratio: 9 / 16;
  max-height: 480px;
  object-fit: contain;
}

.caption-preview {
  margin: 0;
  color: #d4d4de;
  font-size: var(--font-size-sm);
  line-height: 1.45;
  white-space: pre-line;
  display: -webkit-box;
  -webkit-line-clamp: 4;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: var(--spacing-md);
}

.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}
</style>
