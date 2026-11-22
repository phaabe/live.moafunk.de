<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { BaseButton } from '@shared/components';
import { seriesApi, showsApi, type SeriesSlot, type ShowDetail } from '../api';
import { slotDate, slotTime } from '../episodeRow';
import { berlinToUtcDate } from '../showTime';
import BoardIcon from './BoardIcon.vue';
import EpisodeIcon from './episode/EpisodeIcon.vue';

// Edit mode of the episode's heading box (#1052): Title, Description, Date,
// Time and Mode, saved with one Save. Date and Time offer Book Slot's free
// choices. Date, Time and Mode change only before the start (the backend
// refuses later). The default slot holds the danger zone (Cancel booking).

const props = defineProps<{ show: ShowDetail; hosts?: string[] }>();
const emit = defineEmits<{ saved: []; cancel: [] }>();

const title = ref('');
const description = ref('');
const mode = ref<'live' | 'prerecorded'>('live');
const date = ref('');
const time = ref('');
const freeStart = ref('');
const freeEnd = ref('');
/** null: the Show has no fixed rhythm, so Date and Time are free. */
const slots = ref<SeriesSlot[] | null>(null);
const saving = ref(false);
const error = ref<string | null>(null);

const current = computed<SeriesSlot | null>(() =>
  props.show.start_time && props.show.end_time
    ? { date: props.show.date, start_time: props.show.start_time, end_time: props.show.end_time }
    : null
);
const choices = computed(() => [
  ...(current.value ? [current.value] : []),
  ...(slots.value ?? []).filter(
    (s) => !(s.date === current.value?.date && s.start_time === current.value.start_time)
  ),
]);
const dates = computed(() => [...new Set(choices.value.map((s) => s.date))].sort());
const times = computed(() =>
  choices.value.filter((s) => s.date === date.value).map((s) => slotTime(s))
);
watch(times, (list) => {
  if (!list.includes(time.value)) time.value = list[0] ?? '';
});

const started = computed(
  () =>
    !!props.show.on_air ||
    (!!props.show.start_time &&
      Date.now() >= berlinToUtcDate(props.show.date, props.show.start_time).getTime())
);
const today = new Intl.DateTimeFormat('en-CA', { timeZone: 'Europe/Berlin' }).format(new Date());
const cover = computed(() => props.show.series_cover_url ?? props.show.cover_url ?? null);
const coverSrcset = computed(
  () =>
    (props.show.series_cover_url ? props.show.series_cover_srcset : props.show.cover_srcset) ??
    undefined
);

/** The chosen date and time, or null while the free time is not valid. */
const slot = computed<SeriesSlot | null>(() => {
  if (slots.value === null) {
    if (!date.value || !freeStart.value || !freeEnd.value || freeEnd.value <= freeStart.value) {
      return null;
    }
    return { date: date.value, start_time: freeStart.value, end_time: freeEnd.value };
  }
  return choices.value.find((s) => s.date === date.value && slotTime(s) === time.value) ?? null;
});

function reset() {
  const s = props.show;
  title.value = s.title;
  description.value = s.description ?? '';
  mode.value = s.stream_mode === 'prerecorded' ? 'prerecorded' : 'live';
  date.value = s.date;
  time.value = current.value ? slotTime(current.value) : '';
  freeStart.value = s.start_time ?? '';
  freeEnd.value = s.end_time ?? '';
}

async function loadChoices() {
  const slug = props.show.series_slug;
  const series = slug ? await seriesApi.get(slug) : null;
  slots.value = series?.rhythm ? (await seriesApi.slots(series.slug)).slots : null;
}

watch(
  () => props.show,
  () => {
    reset();
    loadChoices().catch((e) => (error.value = e instanceof Error ? e.message : String(e)));
  },
  { immediate: true }
);

async function save() {
  const s = props.show;
  const changes: Parameters<typeof showsApi.update>[1] = {};
  if (title.value.trim() && title.value.trim() !== s.title) changes.title = title.value.trim();
  if (description.value.trim() !== (s.description ?? '')) {
    changes.description = description.value.trim();
  }
  if (!started.value) {
    const next = slot.value;
    if (next && (next.date !== s.date || next.start_time !== s.start_time)) {
      changes.date = next.date;
      changes.start_time = next.start_time;
      changes.end_time = next.end_time;
    } else if (next && next.end_time !== s.end_time) {
      changes.end_time = next.end_time;
    }
    if (mode.value !== (s.stream_mode ?? 'live')) changes.stream_mode = mode.value;
  }
  // Nothing changed: back to the read-only details.
  if (!Object.keys(changes).length) {
    emit('cancel');
    return;
  }
  saving.value = true;
  error.value = null;
  try {
    await showsApi.update(s.id, changes);
    emit('saved');
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Could not save the episode';
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <form class="ep-edit" @submit.prevent="save">
    <div class="ebar">
      <b><EpisodeIcon name="edit" />Editing episode details</b>
      <BaseButton variant="secondary" :disabled="saving" @click="emit('cancel')">Cancel</BaseButton>
      <BaseButton type="submit" :loading="saving">Save</BaseButton>
    </div>
    <div class="top">
      <div class="cover ep-edit-cover">
        <img
          v-if="cover"
          :src="cover"
          :srcset="coverSrcset"
          sizes="140px"
          alt="Cover of the show"
        />
        <BoardIcon v-else name="image" :size="40" />
      </div>
      <div class="form">
        <label class="field wide">
          <span class="label">Title</span>
          <input v-model="title" class="input big" type="text" />
        </label>
        <label class="field wide">
          <span class="label">Description</span>
          <textarea v-model="description" class="input" rows="2"></textarea>
        </label>

        <template v-if="slots === null">
          <label class="field">
            <span class="label">Date</span>
            <input v-model="date" type="date" class="input" :min="today" :disabled="started" />
          </label>
          <div class="field">
            <span class="label">Time</span>
            <div class="time-range">
              <input
                v-model="freeStart"
                type="time"
                class="input"
                aria-label="Start time"
                :disabled="started"
              />
              <span>–</span>
              <input
                v-model="freeEnd"
                type="time"
                class="input"
                aria-label="End time"
                :disabled="started"
              />
            </div>
          </div>
        </template>
        <template v-else>
          <label class="field">
            <span class="label">Date</span>
            <select v-model="date" class="input" :disabled="started">
              <option v-for="d in dates" :key="d" :value="d">{{ slotDate(d) }}</option>
            </select>
          </label>
          <label class="field">
            <span class="label">Time</span>
            <select v-model="time" class="input" :disabled="started">
              <option v-for="t in times" :key="t" :value="t">{{ t }}</option>
            </select>
          </label>
        </template>

        <fieldset class="field mode-field">
          <legend class="legend">Mode</legend>
          <div class="mode">
            <label>
              <input
                v-model="mode"
                type="radio"
                :name="`mode-${show.id}`"
                value="live"
                :disabled="started"
              />
              Live
            </label>
            <label>
              <input
                v-model="mode"
                type="radio"
                :name="`mode-${show.id}`"
                value="prerecorded"
                :disabled="started"
              />
              Pre-recorded
            </label>
          </div>
        </fieldset>

        <div v-if="hosts?.length" class="field hosts">
          <span class="legend">{{ hosts.length > 1 ? 'Hosts' : 'Host' }}</span>
          <span class="hosts-line"><BoardIcon name="user" :size="16" />{{ hosts.join(', ') }}</span>
        </div>

        <p class="help wide">
          Date and Time offer the same free choices as Book Slot. Save refuses a time that clashes
          with another show.
        </p>
      </div>
    </div>
    <p v-if="error" class="field-error edit-error" role="alert">{{ error }}</p>
    <!-- Cancel booking sits apart from Save. -->
    <div v-if="$slots.default" class="danger">
      <p>Cancel booking frees the date. It asks once to confirm, then opens the Show page.</p>
      <slot />
    </div>
  </form>
</template>

<style scoped>
.ebar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  padding: 12px var(--spacing-md);
  border-bottom: 1px solid #5c5620;
  background: #221f00;
}

.ebar b {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  margin-right: auto;
  font: 700 13px var(--font-family);
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--color-primary);
}

.top {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  gap: 20px 28px;
  padding: var(--spacing-lg);
}

.ep-edit-cover {
  width: 140px;
  height: 140px;
}

.form {
  flex: 1 1 480px;
  min-width: 0;
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(100%, 240px), 1fr));
  gap: 16px 24px;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}

.wide {
  grid-column: 1 / -1;
}

.label,
.legend {
  padding: 0;
  font: 700 11px var(--font-ui);
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: #a8a8bc;
}

.input {
  width: 100%;
  min-height: 44px;
  padding: var(--spacing-sm) var(--spacing-md);
  border: 1px solid #34344c;
  border-radius: var(--radius-lg);
  background: #06060f;
  color: var(--color-text);
  font: 15px var(--font-ui);
}

textarea.input {
  resize: vertical;
  line-height: 1.4;
}

.input.big {
  min-height: 52px;
  font: 700 1.4rem var(--font-family);
  color: #fff;
}

.time-range {
  display: flex;
  align-items: center;
  gap: var(--spacing-xs);
}

.time-range .input {
  min-width: 0;
}

.mode-field {
  margin: 0;
  padding: 0;
  border: 0;
}

.mode-field .legend {
  margin-bottom: 6px;
}

.mode-field .mode label {
  padding: 0 8px;
  white-space: nowrap;
}

.hosts-line {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-height: 44px;
  font-weight: 700;
  color: #d0d0dc;
}

.help {
  margin: 0;
  font-size: var(--font-size-sm);
  color: #a0a0b4;
}

.edit-error {
  padding: 0 var(--spacing-lg) var(--spacing-md);
}

.danger {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px 16px;
  padding: var(--spacing-md) var(--spacing-lg);
  border-top: 1px solid #2a2a42;
}

.danger p {
  flex: 1 1 300px;
  margin: 0;
  font-size: var(--font-size-sm);
  color: #a0a0b4;
}

@media (max-width: 640px) {
  .top {
    padding: var(--spacing-md);
  }

  .ep-edit-cover {
    width: 88px;
    height: 88px;
  }

  .danger {
    padding: var(--spacing-md);
  }
}
</style>
