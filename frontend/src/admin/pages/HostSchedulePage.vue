<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { seriesApi, showsApi, streamApi, type ShowOverviewItem } from '../api';
import { episodeDay } from '../episodeRow';
import BoardIcon from '../components/BoardIcon.vue';
import { isShowRunning } from '../showTime';

// Flow screen `schedule` (host at /dashboard): what is on air now and the
// host's next episodes. Free slots come in a later release.

interface Row {
  id: number;
  sortKey: string;
  left: string;
  right: string;
}

const onAir = ref(false);
const schedule = ref<ShowOverviewItem[]>([]);
const rows = ref<Row[]>([]);
/** Show (series) name of each of the host's episodes. */
const showNameOf = ref(new Map<number, string>());
let pollTimer: ReturnType<typeof setInterval> | null = null;

async function loadEpisodes() {
  const { series } = await seriesApi.list();
  const details = await Promise.all(series.map((s) => seriesApi.get(s.slug)));
  const names = new Map<number, string>();
  const next: Row[] = [];
  for (const show of details) {
    if (!show) continue;
    for (const episode of show.episodes) {
      names.set(episode.id, show.name);
      if (episode.status !== 'upcoming') continue;
      next.push({
        id: episode.id,
        sortKey: `${episode.date} ${episode.start_time ?? ''}`,
        left: `${episodeDay(episode.date)} · ${show.name}`,
        right: `${episode.status_label} · ${episode.mode_label}`,
      });
    }
  }
  next.sort((a, b) => a.sortKey.localeCompare(b.sortKey));
  rows.value = next;
  showNameOf.value = names;
}

async function loadSchedule() {
  schedule.value = (await showsApi.overview()).shows;
}

async function loadStatus() {
  const wasOnAir = onAir.value;
  onAir.value = (await streamApi.status()).active;
  if (onAir.value && !wasOnAir) await loadSchedule();
}

/** "On air · <Show> · until <HH:MM>", from the schedule entry airing now. */
const onAirLine = computed(() => {
  const running = schedule.value.find((s) => isShowRunning(s));
  if (!running) return 'On air';
  const name = showNameOf.value.get(running.id) ?? running.title;
  return `On air · ${name} · until ${running.end_time}`;
});

function logError(what: string) {
  return (e: unknown) => console.error(`Failed to load ${what}:`, e);
}

onMounted(() => {
  loadEpisodes().catch(logError('episodes'));
  loadStatus().catch(logError('stream status'));
  pollTimer = setInterval(() => loadStatus().catch(logError('stream status')), 10000);
});

onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
});
</script>

<template>
  <div class="schedule-page">
    <h1 class="page-title">Schedule</h1>

    <section v-if="onAir" class="status-banner live">
      <h2 class="board-card-title">On air now</h2>
      <span class="on-air">{{ onAirLine }}</span>
    </section>

    <section class="board-card">
      <div class="board-card-head">
        <h2 class="board-card-title"><BoardIcon name="calendar" />Your next episodes</h2>
      </div>
      <div class="board-card-body">
        <ul class="episodes board-rows">
          <li v-for="row in rows" :key="row.id">
            <router-link :to="`/shows/${row.id}`" class="board-row">
              <span>{{ row.left }}</span>
              <span class="tag tag-upcoming">{{ row.right }}</span>
            </router-link>
          </li>
        </ul>
      </div>
    </section>
  </div>
</template>

<style scoped>
.schedule-page {
  max-width: 960px;
  display: flex;
  flex-direction: column;
  gap: var(--spacing-lg);
}

.page-title {
  margin: 0;
}

.on-air {
  font-size: var(--font-size-lg);
}
</style>
