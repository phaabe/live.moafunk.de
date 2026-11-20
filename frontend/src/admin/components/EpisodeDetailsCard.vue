<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { BaseButton } from '@shared/components';
import { seriesApi, showsApi, type SeriesSlot, type ShowDetail } from '../api';
import { slotDate, slotTime } from '../episodeRow';
import { berlinToUtcDate } from '../showTime';
import BoardIcon from './BoardIcon.vue';

// Details of the owner's episode: Title, Description, Date, Time and Mode,
// saved with one Save. Date and Time offer Book Slot's free choices. Date,
// Time and Mode change only before the start (the backend refuses later).
// The default slot holds more buttons next to Save.

const props = defineProps<{ show: ShowDetail }>();
const emit = defineEmits<{ saved: [] }>();

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
  if (!Object.keys(changes).length) return;
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
  <section class="board-card">
    <div class="board-card-head">
      <h2 class="board-card-title"><BoardIcon name="details" />Details</h2>
    </div>
    <div class="board-card-body">
      <label class="field">
        <span class="label">Title</span>
        <input v-model="title" class="input" type="text" />
      </label>
      <label class="field">
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
      <div v-else class="pair">
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
      </div>

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

      <p v-if="error" class="field-error" role="alert">{{ error }}</p>
      <div>
        <BaseButton variant="secondary" :loading="saving" @click="save">Save</BaseButton>
      </div>
      <!-- More buttons (Cancel booking) sit apart from Save. -->
      <div v-if="$slots.default" class="extra">
        <slot />
      </div>
    </div>
  </section>
</template>

<style scoped>
.field {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
}

.input {
  color: var(--color-text);
  padding: var(--spacing-sm) var(--spacing-md);
}

.time-range {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--spacing-xs);
}

.extra {
  padding-top: var(--spacing-md);
  border-top: 1px solid #22223a;
}

.field-error {
  color: var(--color-error);
  font-size: var(--font-size-sm);
  margin: 0;
}
</style>
