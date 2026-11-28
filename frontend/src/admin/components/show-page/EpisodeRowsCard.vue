<script setup lang="ts">
import { computed } from 'vue';
import { RouterLink } from 'vue-router';
import type { SeriesEpisode } from '../../api';
import BoardIcon from '../BoardIcon.vue';
import DateChip from '../DateChip.vue';
import { socialItems } from '../episode/tabs';
import PostIcons from './PostIcons.vue';
import { episodeTime, soundcloudLabel } from './showPage';

// Upcoming or Past episodes of the show detail page, as M46 rows: date,
// time, Live / Pre-recorded and the status (past rows add SoundCloud).
// Every row opens its episode, also a finished one; rows of a Show the
// viewer does not manage open it read-only.

const props = defineProps<{
  title: string;
  kind: 'upcoming' | 'past';
  episodes: SeriesEpisode[];
  /** The viewer manages this Show's episodes. */
  mine: boolean;
  empty: string;
}>();

/** Date chip per status: yellow while upcoming, amber while the wrap-up is open. */
const CHIP = { upcoming: 'mine', wrapup_open: 'wrapup', finished: '' } as const;

const rows = computed(() =>
  props.episodes.map((e) => ({
    id: e.id,
    date: e.date,
    time: episodeTime(e),
    mode: e.mode_label,
    live: e.mode === 'live',
    status: e.status,
    label: e.status_label,
    soundcloud: props.kind === 'past' ? soundcloudLabel(e.soundcloud) : null,
    tone: props.mine ? CHIP[e.status] : '',
    posts: socialItems(e.instagram ?? [], e.soundcloud === 'public'),
  }))
);
</script>

<template>
  <section :class="['board-card', 'episodes-card', kind]">
    <div class="board-card-head">
      <h2 class="board-card-title">
        <BoardIcon v-if="kind === 'upcoming'" name="calendar" /><svg
          v-else
          width="18"
          height="18"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
          stroke-linejoin="round"
          aria-hidden="true"
        >
          <path d="M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18zM12 7v5l3 2" /></svg
        >{{ title }}
      </h2>
    </div>
    <p v-if="!rows.length" class="empty">{{ empty }}</p>
    <!-- Row text stays "So 25.10. · 12:00–14:00 · Live | Upcoming". -->
    <ul v-else :class="['episodes', 'board-rows', `${kind}-episodes`]">
      <li v-for="row in rows" :key="row.id">
        <RouterLink :to="`/shows/${row.id}`" class="board-row">
          <span class="row-main">
            <DateChip :date="row.date" :tone="row.tone" />
            <span class="row-body">
              <template v-if="row.time">
                <span class="sep"> · </span>
                <span class="row-time">{{ row.time }}</span>
              </template>
              <span class="sep"> · </span>
              <span class="row-title row-mode"
                ><BoardIcon :name="row.live ? 'mic' : 'disc'" :size="16" />{{ row.mode }}</span
              >
            </span>
          </span>
          <span class="row-tags">
            <span :class="['tag', `tag-${row.status}`]">{{ row.label }}</span>
            <template v-if="row.soundcloud">
              <span class="sep"> · </span>
              <span class="tag tag-soundcloud">{{ row.soundcloud }}</span>
            </template>
          </span>
          <PostIcons :items="row.posts" small class="row-posts" />
          <BoardIcon
            :name="mine ? 'chevron' : 'eye'"
            :class="['row-icon', { 'read-only': !mine }]"
          />
        </RouterLink>
      </li>
    </ul>
  </section>
</template>

<style scoped>
.row-mode {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.row-mode svg {
  color: #a0a0b4;
}

.row-posts {
  flex: none;
  padding: 0;
}

@media (max-width: 640px) {
  .row-posts {
    display: none;
  }
}

.tag-soundcloud {
  border-color: #ff7a2f;
  background-color: rgba(255, 122, 47, 0.14);
  color: #ffb38a;
}
</style>
