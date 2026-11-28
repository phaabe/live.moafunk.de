<script setup lang="ts">
import { computed, ref } from 'vue';
import { BaseButton } from '@shared/components';
import type { SeriesEpisode, SeriesSlot } from '../../api';
import BoardIcon from '../BoardIcon.vue';
import { episodeDay } from '../../episodeRow';
import { socialItems } from '../episode/tabs';
import PostIcons from './PostIcons.vue';
import { countdownParts, episodeTime, hasStarted, nextStep, type ScheduleItem } from './showPage';

// The Show's dates in one card (issue 1051, design D1): a strip of the
// upcoming episodes and, for whoever may book, the free slots; the selected
// date below. The next episode is selected first. A booked date opens its
// episode; a free one books it. Other hosts see the episodes read-only.

const props = defineProps<{
  items: ScheduleItem[];
  /** The viewer manages this Show: books, gets the next step, opens episodes to edit. */
  mine: boolean;
  /** Clock (ms) for the countdown. */
  now: number;
  /** The free slots could not load. */
  error?: string | null;
  /** The Show has no fixed rhythm, so it has no free slots. */
  noRhythm?: boolean;
}>();
const emit = defineEmits<{ open: [episode: SeriesEpisode]; book: [slot: SeriesSlot | null] }>();

const picked = ref<string | null>(null);
const nextKey = computed(() => props.items.find((i) => i.kind === 'episode')?.key ?? null);
const selected = computed(
  () =>
    props.items.find((i) => i.key === picked.value) ??
    props.items.find((i) => i.key === nextKey.value) ??
    props.items[0] ??
    null
);

const tiles = computed(() =>
  props.items.map((item) => {
    const [weekday, date] = episodeDay(item.date).split(' ');
    let label = 'Free';
    let tone = 'free';
    if (item.kind === 'episode') {
      const e = item.episode;
      const live = hasStarted(e, props.now);
      tone = live ? 'live' : 'booked';
      if (live) label = 'On air';
      else if (item.key === nextKey.value) label = 'Next';
      else label = e.mode === 'live' ? 'Live' : 'Pre-rec';
    }
    return { key: item.key, weekday, date, label, tone, sel: item.key === selected.value?.key };
  })
);

/** The selected booked episode, with what the card says about it. */
const shown = computed(() => {
  const item = selected.value;
  if (item?.kind !== 'episode') return null;
  const e = item.episode;
  const live = hasStarted(e, props.now);
  const isNext = item.key === nextKey.value;
  return {
    episode: e,
    eyebrow: live ? 'On air now' : isNext ? 'Next episode' : 'Upcoming episode',
    live,
    day: episodeDay(e.date),
    meta: [episodeTime(e), e.mode_label].filter(Boolean).join(' · '),
    hint: props.mine && isNext ? nextStep(e, props.now) : '',
    needsFile: e.mode === 'prerecorded' && e.has_file === false,
    count: isNext ? countdownParts(e, props.now) : null,
    posts: socialItems(e.instagram ?? [], e.soundcloud === 'public'),
  };
});

const free = computed(() => {
  const item = selected.value;
  if (item?.kind !== 'free') return null;
  return {
    slot: item.slot,
    day: episodeDay(item.date),
    meta: `${item.slot.start_time}–${item.slot.end_time} · not booked yet`,
  };
});

const empty = computed(() =>
  props.mine && props.noRhythm
    ? 'No fixed rhythm. Book another date picks any free date.'
    : 'No upcoming episodes.'
);
</script>

<template>
  <section class="board-card schedule" aria-labelledby="schedule-title">
    <div class="board-card-head">
      <h2 id="schedule-title" class="board-card-title"><BoardIcon name="clock" />Schedule</h2>
      <BaseButton v-if="mine" variant="ghost" class="book-any" @click="emit('book', null)"
        >Book another date</BaseButton
      >
    </div>

    <p v-if="error" class="empty field-error" role="alert">{{ error }}</p>
    <p v-if="!items.length" class="empty">{{ empty }}</p>

    <div v-if="items.length > 1" class="strip">
      <button
        v-for="t in tiles"
        :key="t.key"
        type="button"
        :class="['tile', t.tone, { sel: t.sel }]"
        :aria-pressed="t.sel"
        @click="picked = t.key"
      >
        <span class="wd">{{ t.weekday }}</span
        ><span class="sep">{{ ' ' }}</span> <b class="dt">{{ t.date }}</b
        ><span class="sep"> · </span>
        <span class="lb">{{ t.label }}</span>
      </button>
    </div>

    <div v-if="shown" :class="['detail', { split: items.length > 1 }]">
      <div class="when">
        <p :class="['eyebrow', { live: shown.live }]">{{ shown.eyebrow }}</p>
        <h3 class="day">{{ shown.day }}</h3>
        <p class="meta">{{ shown.meta }}</p>
        <p v-if="shown.hint" :class="['hint', { warn: shown.needsFile }]">{{ shown.hint }}</p>
      </div>
      <div class="posts">
        <span class="posts-label">Instagram</span>
        <PostIcons :items="shown.posts" />
      </div>
      <div class="actions">
        <div v-if="shown.count" class="count">
          <span class="count-label">{{ shown.count.label }}</span>
          <b :class="['count-value', { live: shown.live }]">{{ shown.count.value }}</b>
        </div>
        <BaseButton v-if="mine" class="xl open" @click="emit('open', shown.episode)"
          >Open episode<BoardIcon name="chevron"
        /></BaseButton>
        <BaseButton v-else variant="secondary" class="open" @click="emit('open', shown.episode)"
          ><BoardIcon name="eye" :size="16" />View episode</BaseButton
        >
      </div>
    </div>

    <div v-else-if="free" :class="['detail', { split: items.length > 1 }]">
      <div class="when">
        <p class="eyebrow muted">Free slot</p>
        <h3 class="day">{{ free.day }}</h3>
        <p class="meta">{{ free.meta }}</p>
      </div>
      <div class="actions">
        <BaseButton class="xl" @click="emit('book', free.slot)"
          ><BoardIcon name="plus" />Book this date</BaseButton
        >
      </div>
    </div>
  </section>
</template>

<style scoped>
.book-any {
  margin-left: auto;
  color: var(--color-primary);
}

.empty {
  margin: 0;
  padding: var(--spacing-md);
  color: #a8a8bc;
}

.strip {
  display: flex;
  gap: 10px;
  padding: var(--spacing-md);
  overflow-x: auto;
}

.tile {
  flex: 1 0 96px;
  max-width: 160px;
  min-height: 104px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 2px;
  padding: 10px 6px;
  border: 1.5px solid #34344c;
  border-radius: var(--radius-lg);
  background: var(--color-surface-alt);
  color: #eee;
  font-family: var(--font-ui);
  cursor: pointer;
}

.tile .wd,
.tile .lb {
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}

.tile .dt {
  font: 700 1.35rem/1.15 var(--font-family);
  font-variant-numeric: tabular-nums;
}

.tile.free {
  border-style: dashed;
  border-color: #55556a;
  background: transparent;
  color: #a8a8bc;
}

.tile.live {
  border-color: #ef4444;
  background: #2a1018;
  color: #ffb3b3;
}

.tile.sel {
  border: 1.5px solid var(--color-primary);
  background: var(--color-primary);
  color: #000;
}

.detail {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--spacing-md) var(--spacing-xl);
  padding: var(--spacing-lg) var(--spacing-md);
}

.detail.split {
  border-top: 1px solid #23233a;
}

.when {
  flex: 1 1 300px;
  min-width: 0;
}

.when p {
  margin: 0;
}

.eyebrow {
  font: 700 13px var(--font-family);
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--color-primary);
}

.eyebrow.live {
  color: #ff6b6b;
}

.eyebrow.muted {
  color: #a8a8bc;
}

.day {
  margin: 6px 0 0;
  font: 700 2.5rem/1.1 var(--font-family);
  font-variant-numeric: tabular-nums;
  color: #fff;
}

.meta {
  margin-top: 8px !important;
  font-size: var(--font-size-md);
  color: #c8c8d4;
}

.hint {
  margin-top: 4px !important;
  color: #a8a8bc;
}

.hint.warn {
  color: #fbbf24;
}

.posts {
  flex: none;
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding-left: var(--spacing-lg);
  border-left: 1px solid #2a2a42;
}

.posts-label,
.count-label {
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: #a8a8bc;
}

.actions {
  flex: none;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 14px;
}

.count {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.count-value {
  font: 700 1.75rem/1.1 var(--font-family);
  color: var(--color-primary);
}

.count-value.live {
  color: #ff6b6b;
}

@media (max-width: 640px) {
  .posts {
    padding-left: 0;
    border-left: 0;
  }

  .day {
    font-size: 2rem;
  }
}
</style>
