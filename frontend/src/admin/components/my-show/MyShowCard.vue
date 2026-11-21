<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import { BaseButton } from '@shared/components';
import { seriesApi, type Series, type UpcomingEpisode } from '../../api';
import { draftOf, draftValid, toNewSeries, type SeriesDraft } from '../../seriesDraft';
import { cleanHandle, handleError } from '../../promotion';
import { getBerlinNowISO } from '../../showTime';
import BoardIcon from '../BoardIcon.vue';
import CoverDrop from '../CoverDrop.vue';
import SeriesFields from '../SeriesFields.vue';
import { coverCountdown, showFacts } from './showFacts';

// One Show on My Show (#1050, design option C). Read mode: the cover on top
// (with "in 3 days" when the next episode is close), then the name and the
// details as fixed rows; the whole card opens the Show page. Edit turns the
// same card into the Show form. The page decides which card edits
// (`editing`) and asks before unsaved changes (`dirty`) are dropped.

const FACT_ICONS = {
  'Next episode': 'calendar',
  Rhythm: 'repeat',
  Instagram: 'instagram',
} as const;
// Read once: after midnight the label is a day off until the page reloads.
const today = getBerlinNowISO().slice(0, 10);

const props = defineProps<{
  show: Series;
  /** Who hosts the Show. Today only its owner; a list, so more hosts fit later (#1047). */
  hosts: string[];
  /** The next episode; null when none is booked, undefined when the episodes could not load. */
  next?: UpcomingEpisode | null;
  editing: boolean;
  /** In the grid: a 2:1 cover (one Show: a 3:1 banner) and a smaller name. */
  compact?: boolean;
}>();
const emit = defineEmits<{
  edit: [];
  cancel: [];
  saved: [show: Series];
}>();
/** The form has changes that are not saved. */
const dirty = defineModel<boolean>('dirty', { default: false });

const router = useRouter();

const draft = ref<SeriesDraft>(draftOf(props.show));
const cover = ref<File | null>(null);
/** The host's Instagram account; announcements invite it as collaborator. */
const handle = ref('');
const saving = ref(false);
const error = ref<string | null>(null);

const showPath = computed(() => `/shows/${props.show.slug}`);
const facts = computed(() => showFacts(props.show, props.next));
const soon = computed(() => coverCountdown(props.next, today));
const coverSize = computed(() => (props.compact ? 120 : 180));
const handleProblem = computed(() => handleError(cleanHandle(handle.value)));
const canSave = computed(() => draftValid(draft.value) && !handleProblem.value);

/** What Save would send, so an edit that ends where it started is no change. */
const sent = (d: SeriesDraft, h: string) =>
  JSON.stringify({ ...toNewSeries(d, null), instagram_handle: cleanHandle(h) });
const savedForm = computed(() => sent(draftOf(props.show), props.show.instagram_handle ?? ''));
const changed = computed(
  () =>
    props.editing && (cover.value !== null || sent(draft.value, handle.value) !== savedForm.value)
);
watch(changed, (now) => (dirty.value = now));

// Every edit starts from the saved Show.
watch(
  () => props.editing,
  (on) => {
    if (!on) return;
    draft.value = draftOf(props.show);
    handle.value = props.show.instagram_handle ? `@${props.show.instagram_handle}` : '';
    cover.value = null;
    error.value = null;
  },
  { immediate: true }
);

async function save() {
  if (!canSave.value || saving.value) return;
  saving.value = true;
  error.value = null;
  try {
    const saved = await seriesApi.update(props.show.slug, {
      ...toNewSeries(draft.value, cover.value),
      instagram_handle: cleanHandle(handle.value),
    });
    emit('saved', saved);
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Could not save the show';
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <section
    :class="['board-card', 'my-show-card', { editing, compact }]"
    :aria-label="editing ? `${show.name}, editing` : show.name"
  >
    <template v-if="editing">
      <div class="edit-bar">
        <span class="edit-title"><BoardIcon name="pencil" :size="16" />Editing show details</span>
        <BaseButton variant="secondary" :disabled="saving" @click="emit('cancel')"
          >Cancel</BaseButton
        >
        <BaseButton :disabled="!canSave" :loading="saving" @click="save">Save</BaseButton>
      </div>
      <div class="top">
        <div class="cover-edit">
          <CoverDrop v-model="cover" :current-url="show.cover_url" :size="coverSize" />
          <span v-if="show.cover_url || cover" class="drop-hint" aria-hidden="true"
            ><BoardIcon name="upload" :size="16" />Drop an image or pick a file</span
          >
        </div>
        <div class="text about">
          <SeriesFields v-model="draft" part="about" />
        </div>
      </div>
      <div class="details">
        <div class="form">
          <SeriesFields v-model="draft" part="rhythm" />
          <label class="field handle">
            <span class="label">Instagram handle</span>
            <input v-model="handle" type="text" class="input" placeholder="@brunchtime" />
          </label>
          <!-- Not editable yet: more hosts per show come with #1047. -->
          <div class="field hosts">
            <span class="label">Hosts</span>
            <span class="static">{{ hosts.join(', ') || '—' }}</span>
          </div>
        </div>
        <p v-if="handleProblem" class="field-error">{{ handleProblem }}</p>
        <p class="hint">
          Booked episodes keep their date and time. A new rhythm only changes the free dates in Book
          Slot.
        </p>
        <p v-if="error" class="field-error" role="alert">{{ error }}</p>
      </div>
    </template>

    <template v-else>
      <div class="cover">
        <img v-if="show.cover_url" :src="show.cover_url" alt="" />
        <BoardIcon v-else name="image" :size="40" />
        <span v-if="soon" class="soon"><BoardIcon name="clock" :size="14" />{{ soon }}</span>
      </div>
      <div class="body">
        <h2 class="show-name">
          <!-- The one link of the card; it stretches over the whole card. -->
          <router-link :to="showPath" class="name-link"
            ><span>{{ show.name }}</span
            ><BoardIcon name="chevron" :size="compact ? 22 : 28"
          /></router-link>
        </h2>
        <p v-if="show.description" class="description">{{ show.description }}</p>
        <dl class="facts">
          <div v-for="fact in facts" :key="fact.label" :class="fact.tone">
            <dt>{{ fact.label }}</dt>
            <dd><BoardIcon :name="FACT_ICONS[fact.label]" :size="16" />{{ fact.value }}</dd>
          </div>
        </dl>
        <div class="actions">
          <BaseButton variant="secondary" class="book" @click="router.push(`${showPath}/book`)"
            ><BoardIcon name="calendar-plus" :size="16" />Book Slot</BaseButton
          >
          <BaseButton variant="secondary" class="edit" @click="emit('edit')"
            ><BoardIcon name="pencil" :size="16" />Edit</BaseButton
          >
        </div>
      </div>
    </template>
  </section>
</template>

<style scoped>
.my-show-card {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.my-show-card.editing {
  border: 2px solid var(--color-primary);
}

/* Read mode: the name link covers the whole card (::after), so a click
   anywhere opens the Show page. The buttons sit above it. */
.my-show-card:not(.editing) {
  position: relative;
  transition: border-color 0.15s;
}

.my-show-card:not(.editing):hover,
.my-show-card:not(.editing):focus-within {
  border-color: #5c5620;
}

.name-link::after {
  content: '';
  position: absolute;
  inset: 0;
}

/* ---------- edit bar ---------- */
.edit-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  padding: 12px 20px;
  background: #221f00;
  border-bottom: 1px solid #5c5620;
}

.edit-title {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  margin-right: auto;
  font: 700 13px var(--font-family);
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--color-primary);
}

/* ---------- read mode: cover on top, then name, rows and buttons ---------- */
.my-show-card > .cover {
  position: relative;
  aspect-ratio: 3 / 1;
  border-radius: 0;
}

.compact > .cover {
  aspect-ratio: 2 / 1;
}

/* "in 3 days": black with a yellow frame, so it reads on any cover image. */
.soon {
  position: absolute;
  top: 14px;
  left: 14px;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 32px;
  padding: 0 12px;
  border: 1px solid var(--color-primary);
  border-radius: 16px;
  background: #000;
  color: var(--color-primary);
  font: 700 13px/1 var(--font-family);
  letter-spacing: 0.06em;
  text-transform: uppercase;
}

.body {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 20px 24px 24px;
}

.compact .body {
  padding: 18px 20px 20px;
}

/* ---------- edit mode: cover beside Name and Description ---------- */
.top {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  gap: 24px 28px;
  padding: 24px;
}

.compact .top {
  flex-wrap: nowrap;
  gap: 18px;
  padding: 20px;
}

.text {
  flex: 1 1 360px;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.compact .text {
  flex: 1 1 0;
  gap: 8px;
}

.show-name {
  margin: 0;
  font: 700 36px/1.1 var(--font-family);
  color: #fff;
  overflow-wrap: anywhere;
}

.compact .show-name {
  font-size: 28px;
}

.name-link {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: inherit;
  text-decoration: none;
}

.name-link svg {
  flex: none;
  color: var(--color-primary);
}

.my-show-card:hover .name-link span,
.name-link:focus-visible span {
  text-decoration: underline;
  text-underline-offset: 4px;
}

.description {
  margin: -6px 0 0;
  font-size: 16px;
  color: #c8c8d4;
}

.compact .description {
  font-size: 15px;
}

/* Fixed rows: Next episode, Rhythm, Instagram. */
.facts {
  display: flex;
  flex-direction: column;
  margin: 0;
  border-top: 1px solid #2a2a42;
}

.facts > div {
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 42px;
  padding: 6px 0;
  border-bottom: 1px solid #2a2a42;
}

.facts dt {
  flex: none;
  width: 120px;
  font: 700 11px var(--font-ui);
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: #a8a8bc;
}

.facts dd {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
  margin: 0;
  font-size: 15px;
  font-weight: 600;
  color: #e4e4ee;
  overflow-wrap: anywhere;
}

.facts dd svg {
  flex: none;
  color: #8e8ea4;
}

.facts .accent dd,
.facts .accent dd svg {
  color: var(--color-primary);
}

.facts .muted dd {
  color: #a8a8bc;
  font-weight: 500;
}

/* Book Slot and Edit share the card's full width, each in its own quiet color. */
.actions {
  position: relative;
  z-index: 1;
  display: flex;
  gap: 10px;
  margin-top: auto;
}

.actions :deep(.base-button) {
  flex: 1 1 0;
}

.my-show-card .actions :deep(button.base-button.book) {
  background: #221f00;
  border: 1px solid #5c5620;
  color: var(--color-primary);
}

.my-show-card .actions :deep(button.base-button.book:hover) {
  background: #2e2a00;
  border-color: #8f8536;
}

.my-show-card .actions :deep(button.base-button.edit) {
  background: #1a1a33;
  border: 1px solid #34344c;
  color: #c8c8d8;
}

.my-show-card .actions :deep(button.base-button.edit:hover) {
  border-color: #4a4a60;
  color: #fff;
}

/* ---------- edit form ---------- */
.details {
  flex: 1;
  padding: 20px 24px 24px;
  border-top: 1px solid #2a2a42;
}

.compact .details {
  padding: 16px 20px 20px;
}

.cover-edit {
  position: relative;
  flex: none;
  align-self: flex-start;
}

.drop-hint {
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 8px;
  border-radius: 0 0 var(--radius-md) var(--radius-md);
  background: rgba(255, 236, 68, 0.9);
  color: #000;
  font: 700 12px/1.3 var(--font-ui);
  text-align: center;
  pointer-events: none;
}

.about {
  gap: 14px;
}

.my-show-card .about :deep(.form-input .input) {
  min-height: 52px;
  font: 700 26px var(--font-family);
  color: #fff;
}

/* One row on desktop, like the details in read mode; Time needs room for
   two time inputs. Fields wrap on narrow cards and phones. */
.form {
  display: flex;
  flex-wrap: wrap;
  gap: 16px 24px;
  color-scheme: dark;
}

/* SeriesFields' Repeats, Day and Time sit in the same row as the fields below. */
.form :deep(.rhythm) {
  display: contents;
}

.form :deep(.rhythm > .field),
.form > .field {
  flex: 1 1 170px;
}

.form :deep(.rhythm > div.field) {
  flex-basis: 260px;
}

.form > .handle {
  flex-basis: 200px;
}

.form > .hosts {
  flex-basis: 120px;
}

.form :deep(.field-error) {
  flex-basis: 100%;
}

.form :deep(.time-range) {
  display: flex;
  align-items: center;
  gap: 8px;
}

.form :deep(.time-range .input) {
  flex: 1 1 0;
  min-width: 0;
  padding: 0 8px;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}

/* The Instagram handle (SeriesFields styles its own inputs). */
.field > .input {
  width: 100%;
  padding: 0 12px;
  color: var(--color-text);
}

.field > .input:focus {
  outline: none;
  border-color: var(--color-primary);
}

.static {
  display: flex;
  align-items: center;
  min-height: 44px;
  font-size: 17px;
  font-weight: 600;
  color: #fff;
}

.details > .field-error,
.hint {
  margin: 16px 0 0;
}

.hint {
  font-size: var(--font-size-sm);
  color: #a0a0b4;
}

/* ---------- phones ---------- */
@media (max-width: 640px) {
  .my-show-card > .cover {
    aspect-ratio: 2 / 1;
  }

  .body,
  .compact .body {
    padding: 16px;
  }

  .show-name {
    font-size: 30px;
  }

  .compact .show-name {
    font-size: 24px;
  }

  .facts dt {
    width: 104px;
  }

  /* Two buttons side by side: keep each label on one line. */
  .actions :deep(.base-button) {
    min-height: 48px;
    padding: 0 10px;
    white-space: nowrap;
  }

  .details,
  .compact .details {
    padding: 16px;
  }

  .edit-bar {
    padding: 12px 16px;
  }

  .edit-title {
    flex: 1 0 100%;
  }

  /* Edit mode, also in the grid: cover above Name and Description. */
  .my-show-card.editing .top {
    flex-direction: column;
    flex-wrap: nowrap;
    align-items: stretch;
    gap: 16px;
    padding: 16px;
  }

  .my-show-card.editing .text {
    flex: none;
    padding: 0;
  }
}
</style>
