<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { seriesApi, type UpcomingEpisode } from '../api';
import { useAuthStore } from '../stores/auth';
import BoardIcon from '../components/BoardIcon.vue';
import DateChip from '../components/DateChip.vue';

// Flow screen `dashboard` (host at /dashboard): the upcoming episodes of all
// hosts. The episode page shows the owner's screen or a read-only view.

const auth = useAuthStore();
const episodes = ref<UpcomingEpisode[]>([]);

/** "12:00–14:00" */
function time(e: UpcomingEpisode): string | null {
  return e.start_time && e.end_time ? `${e.start_time}–${e.end_time}` : e.start_time;
}

/** The host's own rows open the episode; other hosts' rows open it read-only. */
function mine(e: UpcomingEpisode): boolean {
  return !!auth.user && e.host_username === auth.user.username;
}

onMounted(async () => {
  try {
    episodes.value = (await seriesApi.upcoming()).episodes;
  } catch (e) {
    console.error('Failed to load upcoming shows:', e);
  }
});
</script>

<template>
  <div class="dashboard-page host-page">
    <h1 class="page-title">Dashboard</h1>

    <section class="board-card">
      <div class="board-card-head">
        <h2 class="board-card-title"><BoardIcon name="calendar" />Upcoming shows</h2>
      </div>
      <!-- Row text stays "So 25.10. · 12:00–14:00 · Brunchtime · lena | Upcoming · Live". -->
      <ul class="episodes board-rows">
        <li v-for="e in episodes" :key="e.id">
          <router-link :to="`/shows/${e.id}`" class="board-row">
            <span class="row-main">
              <DateChip :date="e.date" :tone="mine(e) ? 'mine' : ''" />
              <span class="row-body">
                <template v-if="time(e)">
                  <span class="sep"> · </span>
                  <span class="row-time">{{ time(e) }}</span>
                </template>
                <span class="sep"> · </span>
                <span class="row-title">{{ e.show_name }}</span>
                <span class="sep"> · </span>
                <span :class="['row-host', { mine: mine(e) }]">{{ e.host_username }}</span>
              </span>
            </span>
            <span class="row-tags">
              <span class="tag tag-upcoming">Upcoming</span>
              <span class="sep"> · </span>
              <span class="tag">{{ e.mode_label }}</span>
            </span>
            <BoardIcon
              :name="mine(e) ? 'chevron' : 'eye'"
              :class="['row-icon', { 'read-only': !mine(e) }]"
            />
          </router-link>
        </li>
      </ul>
    </section>
  </div>
</template>

<style scoped>
.dashboard-page {
  max-width: 1040px;
}
</style>
