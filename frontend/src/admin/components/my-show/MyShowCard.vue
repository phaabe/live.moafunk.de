<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import { BaseButton } from '@shared/components';
import { seriesApi, type Series } from '../../api';
import { draftOf, draftValid, toNewSeries, type SeriesDraft } from '../../seriesDraft';
import { cleanHandle, handleError } from '../../promotion';
import BoardIcon from '../BoardIcon.vue';
import CoverDrop from '../CoverDrop.vue';
import SeriesFields from '../SeriesFields.vue';
import { showFacts } from './showFacts';

// One Show on My Show (#1050). Read mode lists the details as plain text;
// Edit turns the same card into the Show form. The page decides which card
// edits (`editing`) and asks before unsaved changes (`dirty`) are dropped.

const props = defineProps<{
  show: Series;
  /** Who hosts the Show. Today only its owner; a list, so more hosts fit later (#1047). */
  hosts: string[];
  editing: boolean;
  /** In the grid: a smaller cover, Day and Time as one item, a plain Book Slot. */
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
const facts = computed(() => showFacts(props.show, props.hosts, props.compact));
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
      <div class="top">
        <!-- The name is the link for screen readers; the cover is a bigger target. -->
        <router-link :to="showPath" class="cover" tabindex="-1" aria-hidden="true">
          <img v-if="show.cover_url" :src="show.cover_url" alt="" />
          <BoardIcon v-else name="image" :size="compact ? 32 : 40" />
        </router-link>
        <div class="text">
          <h2 class="show-name">
            <router-link :to="showPath" class="name-link"
              ><span>{{ show.name }}</span
              ><BoardIcon name="chevron" :size="compact ? 22 : 28"
            /></router-link>
          </h2>
          <p v-if="show.description" class="description">{{ show.description }}</p>
          <div v-if="!compact" class="actions">
            <BaseButton @click="router.push(`${showPath}/book`)"
              ><BoardIcon name="calendar-plus" />Book Slot</BaseButton
            >
            <BaseButton variant="secondary" @click="emit('edit')"
              ><BoardIcon name="pencil" :size="16" />Edit</BaseButton
            >
          </div>
        </div>
      </div>
      <div class="details">
        <div class="details-head">
          <h3>Details</h3>
          <span class="read-only"><BoardIcon name="lock" :size="12" />Read only</span>
        </div>
        <dl class="facts">
          <div v-for="fact in facts" :key="fact.label">
            <dt>{{ fact.label }}</dt>
            <dd>{{ fact.value }}</dd>
          </div>
        </dl>
      </div>
      <div v-if="compact" class="actions">
        <BaseButton variant="secondary" @click="router.push(`${showPath}/book`)"
          ><BoardIcon name="calendar-plus" :size="16" />Book Slot</BaseButton
        >
        <BaseButton variant="secondary" @click="emit('edit')"
          ><BoardIcon name="pencil" :size="16" />Edit</BaseButton
        >
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

/* ---------- cover, name, description ---------- */
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

.top > .cover {
  width: 180px;
  height: 180px;
  border-radius: 10px;
}

.compact .top > .cover {
  width: 120px;
  height: 120px;
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
  font: 700 44px/1.08 var(--font-family);
  color: #fff;
  overflow-wrap: anywhere;
}

.compact .show-name {
  font-size: 30px;
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

.name-link:hover span,
.name-link:focus-visible span {
  text-decoration: underline;
  text-underline-offset: 4px;
}

.description {
  margin: 0;
  font-size: 17px;
  color: #c8c8d4;
}

.compact .description {
  font-size: 15px;
}

.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}

.compact > .actions {
  padding: 0 20px 20px;
}

/* ---------- details ---------- */
.details {
  flex: 1;
  padding: 20px 24px 24px;
  border-top: 1px solid #2a2a42;
}

.compact .details {
  padding: 16px 20px 20px;
}

.details-head {
  display: flex;
  align-items: center;
  gap: 12px;
  margin: 0 0 16px;
}

.details-head h3 {
  margin: 0;
  font: 700 12px var(--font-family);
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: #c8c8d8;
}

.read-only {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 8px;
  border: 1px solid #34344c;
  border-radius: 6px;
  color: #a8a8bc;
  font: 600 12px var(--font-ui);
}

.facts {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(190px, 1fr));
  gap: 18px 24px;
  margin: 0;
}

.compact .facts {
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px 20px;
}

.facts > div {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}

.facts dt {
  font: 700 11px var(--font-ui);
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: #a8a8bc;
}

.facts dd {
  margin: 0;
  font-size: 17px;
  font-weight: 600;
  color: #fff;
  overflow-wrap: anywhere;
}

.compact .facts dd {
  font-size: 16px;
}

/* ---------- edit form ---------- */
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
  .my-show-card:not(.compact) .top {
    flex-direction: column;
    align-items: stretch;
    gap: 16px;
    padding: 0 0 16px;
  }

  .my-show-card:not(.compact) .top > .cover {
    width: 100%;
    height: 150px;
    border-radius: 0;
  }

  .my-show-card:not(.compact) .text {
    flex: none;
    padding: 0 16px;
    gap: 10px;
  }

  .show-name {
    font-size: 32px;
  }

  .compact .top {
    padding: 16px;
    gap: 14px;
  }

  .compact .top > .cover {
    width: 88px;
    height: 88px;
  }

  .compact .show-name {
    font-size: 24px;
  }

  .my-show-card:not(.compact) .actions {
    flex-direction: column;
    gap: 8px;
    padding-top: 4px;
  }

  .actions :deep(.base-button) {
    flex: 1 1 auto;
    min-height: 48px;
  }

  .details,
  .compact .details {
    padding: 16px;
  }

  .facts,
  .compact .facts {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 14px 16px;
  }

  .facts dd {
    font-size: 16px;
  }

  .compact > .actions {
    padding: 0 16px 16px;
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
