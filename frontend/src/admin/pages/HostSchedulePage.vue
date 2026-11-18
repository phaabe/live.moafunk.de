<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { RouterLink } from 'vue-router';
import { seriesApi, showsApi, streamApi, type ShowOverviewItem } from '../api';
import BoardIcon from '../components/BoardIcon.vue';
import DateChip from '../components/DateChip.vue';
import { isShowRunning } from '../showTime';

// Flow screen `schedule` (host at /schedule): what is on air now and the
// host's next episodes. Free slots come in a later release.

interface Row {
  id: number;
  sortKey: string;
  date: string;
  show: string;
  status: string;
  mode: string;
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
        date: episode.date,
        show: show.name,
        status: episode.status_label,
        mode: episode.mode_label,
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
/** The schedule entry airing now. */
const running = computed(() => schedule.value.find((s) => isShowRunning(s)) ?? null);

/** "On air · <Show> · until <HH:MM>", from the schedule entry airing now. */
const onAirLine = computed(() => {
  const show = running.value;
  if (!show) return 'On air';
  const name = showNameOf.value.get(show.id) ?? show.title;
  return `On air · ${name} · until ${show.end_time}`;
});

/** The strip opens On Air when the show on air is the host's own. */
const onAirLink = computed(() =>
  running.value && showNameOf.value.has(running.value.id)
    ? { path: '/stream/on-air', query: { show: String(running.value.id) } }
    : null
);
const episodesError = ref<string | null>(null);

function logError(what: string) {
  return (e: unknown) => console.error(`Failed to load ${what}:`, e);
}

onMounted(() => {
  loadEpisodes().catch((e) => {
    logError('episodes')(e);
    episodesError.value = 'Could not load your episodes.';
  });
  loadStatus().catch(logError('stream status'));
  pollTimer = setInterval(() => loadStatus().catch(logError('stream status')), 10000);
});

onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
});
</script>

<template>
  <div class="schedule-page host-page">
    <h1 class="page-title">Schedule</h1>

    <component
      :is="onAirLink ? RouterLink : 'section'"
      v-if="onAir"
      v-bind="onAirLink ? { to: onAirLink } : {}"
      class="status-banner live on-air-strip"
    >
      <h2 class="strip-title"><span class="dot" aria-hidden="true"></span>On air now</h2>
      <span class="on-air">{{ onAirLine }}</span>
    </component>

    <section class="board-card">
      <div class="board-card-head">
        <h2 class="board-card-title"><BoardIcon name="calendar" />Your next episodes</h2>
      </div>
      <p v-if="episodesError" class="empty field-error" role="alert">{{ episodesError }}</p>
      <ul class="episodes board-rows">
        <li v-for="row in rows" :key="row.id">
          <router-link :to="`/shows/${row.id}`" class="board-row">
            <span class="row-main">
              <DateChip :date="row.date" tone="mine" />
              <span class="sep"> · </span>
              <span class="row-title">{{ row.show }}</span>
            </span>
            <span class="row-tags">
              <span class="tag tag-upcoming">{{ row.status }}</span>
              <span class="sep"> · </span>
              <span class="tag">{{ row.mode }}</span>
            </span>
            <BoardIcon name="chevron" class="row-icon" />
          </router-link>
        </li>
      </ul>
    </section>
  </div>
</template>

<style scoped>
.schedule-page {
  max-width: 1040px;
}

.on-air-strip {
  flex-direction: column;
  align-items: flex-start;
  gap: var(--spacing-sm);
  padding: var(--spacing-md) var(--spacing-lg);
}

.strip-title {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 0;
  font: 700 12px var(--font-family);
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: #ffb3ab;
}

a.on-air-strip:hover {
  background-color: rgba(239, 68, 68, 0.16);
}

.on-air {
  font: 700 1.4rem/1.25 var(--font-family);
  color: #fff;
}
</style>
