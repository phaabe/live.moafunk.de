<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { seriesApi, type UpcomingEpisode } from '../api';
import { episodeDay } from '../episodeRow';
import BoardIcon from '../components/BoardIcon.vue';

// Flow screen `dashboard` (host at /dashboard): the upcoming episodes of all
// hosts. Only the host's own rows open the episode.

const episodes = ref<UpcomingEpisode[]>([]);

/** "So 25.10. · 12:00–14:00 · Brunchtime · lena" */
function left(e: UpcomingEpisode): string {
  const time = e.start_time && e.end_time ? `${e.start_time}–${e.end_time}` : e.start_time;
  return [episodeDay(e.date), time, e.show_name, e.host_username].filter(Boolean).join(' · ');
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
  <div class="dashboard-page">
    <h1 class="page-title">Dashboard</h1>

    <section class="board-card">
      <div class="board-card-head">
        <h2 class="board-card-title"><BoardIcon name="calendar" />Upcoming shows</h2>
      </div>
      <div class="board-card-body">
        <ul class="episodes board-rows">
          <li v-for="(e, i) in episodes" :key="i">
            <router-link
              v-if="e.own_episode_id"
              :to="`/shows/${e.own_episode_id}`"
              class="board-row"
            >
              <span>{{ left(e) }}</span>
              <span class="tag tag-upcoming">Upcoming · {{ e.mode_label }}</span>
            </router-link>
            <div v-else class="board-row">
              <span>{{ left(e) }}</span>
              <span class="tag tag-upcoming">Upcoming · {{ e.mode_label }}</span>
            </div>
          </li>
        </ul>
      </div>
    </section>
  </div>
</template>

<style scoped>
.dashboard-page {
  max-width: 960px;
  display: flex;
  flex-direction: column;
  gap: var(--spacing-lg);
}

.page-title {
  margin: 0;
}
</style>
