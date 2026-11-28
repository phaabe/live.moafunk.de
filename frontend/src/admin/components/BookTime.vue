<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { BaseButton } from '@shared/components';
import { announcementsApi, type AnnouncementTime, type ShowDetail } from '../api';
import { episodeDay } from '../episodeRow';
import { groupTimes, postTime, showDistance, SLOT_NAMES, untilText } from '../promotion';

// Book a time (flow screen `book-time`, #1091): the feed times of the pre
// (7 days before the show) or post slot (14 days after it), one column per
// day, the show as a marker. Opened from the Social media tile and from the
// composer's When part; the parent saves the picked time.

const props = defineProps<{
  showId: number;
  slot: 'pre' | 'post';
  show: Pick<ShowDetail, 'date' | 'start_time'>;
  /** The time booked now, picked when it is still offered. */
  picked?: string | null;
  /** Offer "Post now" (pre slot, Instagram test account only). */
  postNow?: boolean;
  /** Why the parent could not save the pick. */
  error?: string | null;
  busy?: boolean;
}>();
const emit = defineEmits<{ pick: [value: string]; cancel: [] }>();

/** `pick` value of "Post now". */
const NOW = 'now';

const times = ref<AnnouncementTime[]>([]);
const loading = ref(true);
const loadError = ref<string | null>(null);
const pick = ref('');

const days = computed(() =>
  groupTimes(times.value).map((d) => ({ ...d, full: d.times.every((t) => !t.free) }))
);
const anyFree = computed(() => times.value.some((t) => t.free));
const relation = computed(() =>
  pick.value ? `${untilText(pick.value)} · ${showDistance(pick.value, props.show.date)}` : ''
);

async function load() {
  loading.value = true;
  loadError.value = null;
  try {
    times.value = await announcementsApi.times(props.showId, props.slot);
    const free = times.value.filter((t) => t.free);
    pick.value =
      (free.find((t) => t.scheduled_at === props.picked) ?? free.find((t) => t.default) ?? free[0])
        ?.scheduled_at ?? '';
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : 'Could not load the free times';
  } finally {
    loading.value = false;
  }
}

watch(() => [props.showId, props.slot], load, { immediate: true });
</script>

<template>
  <div class="book-time">
    <h1 class="bt-title">Book a time</h1>
    <p class="pill">{{ SLOT_NAMES[slot] }} · {{ slot === 'pre' ? 'before' : 'after' }} your show</p>

    <p v-if="loading" class="hint">Loading the free times…</p>
    <fieldset v-else :class="['bt-grid', slot]" :disabled="busy">
      <legend class="sr-only">Times</legend>
      <div class="bt-show">
        <span class="bt-show-label">Your show</span>
        <b>{{ episodeDay(show.date) }}</b>
        <span>{{ show.start_time }}</span>
      </div>
      <fieldset v-for="d in days" :key="d.day" class="bt-day">
        <legend>{{ d.day }}<span v-if="d.full" class="bt-full">Full</span></legend>
        <label
          v-for="t in d.times"
          :key="t.scheduled_at"
          :class="[
            'bt-time',
            { on: pick === t.scheduled_at, suggested: t.default, taken: !t.free },
          ]"
          ><input
            v-model="pick"
            class="sr-only"
            type="radio"
            name="book-time"
            :value="t.scheduled_at"
            :disabled="!t.free"
          /><span class="sr-only">{{ d.day }} · </span><span class="at">{{ t.time }}</span></label
        >
      </fieldset>
    </fieldset>
    <p v-if="!loading && !anyFree" class="hint">No free time left.</p>
    <p class="bt-legend">
      <span><i class="sw on" aria-hidden="true"></i>Your pick</span>
      <span><i class="sw suggested" aria-hidden="true"></i>Suggested</span>
      <span><i class="sw taken" aria-hidden="true"></i>Full: another show posts that day</span>
    </p>
    <p v-if="slot === 'post'" class="notice">
      It goes out only if your SoundCloud track is public by then. If not, the time is unset and you
      book a new one.
    </p>
    <div v-if="postNow && slot === 'pre'" class="bt-now">
      <span class="bt-badge">Staging only</span>
      <span class="hint">Sends within a minute. For tests, not on the live server.</span>
      <BaseButton variant="secondary" :disabled="busy" @click="emit('pick', NOW)"
        >Post now</BaseButton
      >
    </div>

    <h2 class="bt-head">Goes out</h2>
    <p class="bt-at">{{ pick ? postTime(pick) : 'No time picked' }}</p>
    <p v-if="relation" class="hint">{{ relation }}</p>
    <p v-if="loadError || error" class="field-error" role="alert">{{ loadError || error }}</p>
    <div class="actions">
      <BaseButton variant="secondary" @click="emit('cancel')">Cancel</BaseButton>
      <BaseButton :disabled="!pick" :loading="busy" @click="emit('pick', pick)"
        >Book this time</BaseButton
      >
    </div>
  </div>
</template>

<style scoped>
.book-time {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
}

.bt-title {
  margin: 0;
  font-size: 20px;
}

.pill {
  align-self: flex-start;
  margin: 0;
  color: #a8a8bc;
}

/* One column per day; the show sits after the days (pre) or before them (post). */
.bt-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(84px, 1fr));
  gap: 14px 8px;
  margin: 0;
  padding: 0;
  border: 0;
  min-width: 0;
}

.bt-grid.pre .bt-show {
  order: 99;
}

.bt-show {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 2px;
  padding: 8px 4px;
  border: 1px solid #8f8536;
  border-radius: 10px;
  background: #26220f;
  color: #ffe98a;
  text-align: center;
}

.bt-show-label {
  font-family: var(--font-family-mono, 'Courier New', monospace);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.1em;
  text-transform: uppercase;
}

.bt-show b {
  color: #fff;
}

.bt-day {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
  margin: 0;
  padding: 0;
  border: 0;
}

.bt-day legend {
  display: flex;
  flex-direction: column;
  align-items: center;
  width: 100%;
  padding: 0 0 6px;
  font-size: var(--font-size-sm);
  font-weight: 700;
}

.bt-full {
  color: #a8a8bc;
  font-size: 12px;
  font-weight: 400;
}

.bt-time {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 44px;
  border: 1.5px solid #3a3a58;
  border-radius: 8px;
  font-weight: 700;
  cursor: pointer;
}

.bt-time.suggested {
  border-style: dashed;
  border-color: var(--color-primary);
  color: var(--color-primary);
}

.bt-time.on {
  border-style: solid;
  border-color: var(--color-primary);
  background: var(--color-primary);
  color: #0f0f23;
}

.bt-time.taken {
  border-color: #2a2a44;
  background: #1c1c33;
  color: #8a8aa0;
  font-weight: 400;
  cursor: not-allowed;
}

.bt-time.taken .at {
  text-decoration: line-through;
}

.bt-time:has(input:focus-visible) {
  outline: 2px solid var(--color-primary);
  outline-offset: 2px;
}

.bt-legend {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 18px;
  margin: 0;
  color: #a8a8bc;
  font-size: var(--font-size-sm);
}

.bt-legend span {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.sw {
  width: 18px;
  height: 12px;
  box-sizing: border-box;
  border-radius: 3px;
}

.sw.on {
  background: var(--color-primary);
}

.sw.suggested {
  border: 1.5px dashed var(--color-primary);
}

.sw.taken {
  border: 1px solid #2a2a44;
  background: #1c1c33;
}

.notice {
  margin: 0;
  padding: 12px 14px;
  border: 1px solid #6b5a1e;
  border-radius: var(--radius-lg);
  color: #f3d77a;
}

/* Staging only: dashed purple, so it never looks like a real option. */
.bt-now {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  padding: 10px 14px;
  border: 1px dashed #a78bfa;
  border-radius: var(--radius-lg);
  background: #1a1533;
}

.bt-badge {
  padding: 3px 6px;
  border-radius: 4px;
  background: #c4b5fd;
  color: #0f0f23;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.1em;
  text-transform: uppercase;
}

.bt-now :deep(button) {
  margin-left: auto;
}

.bt-head {
  margin: var(--spacing-sm) 0 0;
  padding-top: var(--spacing-md);
  border-top: 1px solid #24243e;
  color: #a8a8bc;
  font-size: 12px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
}

.bt-at {
  margin: 0;
  font-size: 22px;
  font-weight: 600;
}

.hint {
  margin: 0;
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
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
