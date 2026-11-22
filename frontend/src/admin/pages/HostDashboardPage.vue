<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue';
import {
  seriesApi,
  streamApi,
  type PastEpisode,
  type SeriesOverviewItem,
  type UpcomingEpisode,
} from '../api';
import { useAuthStore } from '../stores/auth';
import ChalkboardCard from '../components/dashboard/ChalkboardCard.vue';
import EpisodesCard from '../components/dashboard/EpisodesCard.vue';
import NextShowCard from '../components/dashboard/NextShowCard.vue';
import OnAirCard from '../components/dashboard/OnAirCard.vue';
import ShowsCard from '../components/dashboard/ShowsCard.vue';
import { airsAt, hostsOf, isMine } from '../components/dashboard/dashboard';

// Flow screen `dashboard` (host at /dashboard), redesign #1049: On air and My
// next show on top, all hosts' episodes (Past first, then Upcoming), the
// hosts' chalkboard, and every Show as a tile. The episode page shows the
// owner's screen or a read-only view.

const auth = useAuthStore();
const username = computed(() => auth.user?.username);

const upcoming = ref<UpcomingEpisode[]>([]);
const past = ref<PastEpisode[]>([]);
const shows = ref<SeriesOverviewItem[]>([]);
const upcomingError = ref<string | null>(null);
const pastError = ref<string | null>(null);
const showsError = ref<string | null>(null);
const episodesLoaded = ref(false);
const showsLoaded = ref(false);
const streamActive = ref(false);
const listeners = ref<number | null>(null);
const now = ref(Date.now());

/** Each Show of the Shows grid, by slug: its cover for the episode cards. */
const bySlug = computed(() => new Map(shows.value.map((s) => [s.slug, s])));
const coverOf = (e: UpcomingEpisode | null) =>
  e ? (bySlug.value.get(e.series_slug)?.cover_url ?? null) : null;
const coverSrcsetOf = (e: UpcomingEpisode | null) =>
  e ? (bySlug.value.get(e.series_slug)?.cover_srcset ?? null) : null;

/** The episode on air: the stream is live and its time is now. */
const onAir = computed(
  () => (streamActive.value && upcoming.value.find((e) => airsAt(e, now.value))) || null
);
const notOnAir = computed(() => upcoming.value.filter((e) => e.id !== onAir.value?.id));
const nextEpisode = computed(() => notOnAir.value[0] ?? null);
const myNext = computed(
  () => notOnAir.value.find((e) => isMine(hostsOf(e), username.value)) ?? null
);

function logError(what: string) {
  return (e: unknown) => console.error(`Failed to load ${what}:`, e);
}

async function loadEpisodes() {
  await Promise.all([
    seriesApi
      .upcoming()
      .then((r) => {
        upcoming.value = r.episodes;
        upcomingError.value = null;
      })
      .catch((e) => {
        logError('upcoming episodes')(e);
        upcomingError.value = 'Could not load the upcoming episodes.';
      }),
    seriesApi
      .past()
      .then((r) => {
        past.value = r.episodes;
        pastError.value = null;
      })
      .catch((e) => {
        logError('past episodes')(e);
        pastError.value = 'Could not load the past episodes.';
      }),
  ]);
  episodesLoaded.value = true;
}

async function loadShows() {
  try {
    shows.value = (await seriesApi.overview()).series;
  } catch (e) {
    logError('shows')(e);
    showsError.value = 'Could not load the shows.';
  } finally {
    showsLoaded.value = true;
  }
}

/** Listeners now, read only while the stream is live (null: unknown). */
async function loadListeners() {
  try {
    const metrics = await streamApi.metrics();
    listeners.value = metrics.online ? metrics.total_listeners : null;
  } catch {
    listeners.value = null;
  }
}

/** A stream that starts or stops moves episodes between the lists: reload them. */
async function loadStatus() {
  const active = (await streamApi.status()).active;
  const changed = active !== streamActive.value;
  streamActive.value = active;
  if (active) await loadListeners();
  else listeners.value = null;
  if (changed && episodesLoaded.value) await loadEpisodes();
}

let clockTimer: ReturnType<typeof setInterval> | null = null;
let pollTimer: ReturnType<typeof setInterval> | null = null;

onMounted(() => {
  void loadEpisodes();
  void loadShows();
  loadStatus().catch(logError('stream status'));
  clockTimer = setInterval(() => (now.value = Date.now()), 1000);
  pollTimer = setInterval(() => loadStatus().catch(logError('stream status')), 10000);
});

onUnmounted(() => {
  if (clockTimer) clearInterval(clockTimer);
  if (pollTimer) clearInterval(pollTimer);
  clockTimer = pollTimer = null;
});
</script>

<template>
  <div class="dashboard-page host-page">
    <h1 class="page-title">Dashboard</h1>

    <div class="dash-cols">
      <div class="dash-main">
        <div class="dash-duo">
          <OnAirCard
            :active="streamActive"
            :episode="onAir"
            :next="nextEpisode"
            :cover-url="coverOf(onAir)"
            :cover-srcset="coverSrcsetOf(onAir)"
            :username="username"
            :now="now"
            :listeners="listeners"
          />
          <NextShowCard
            :episode="myNext"
            :cover-url="coverOf(myNext)"
            :cover-srcset="coverSrcsetOf(myNext)"
            :error="upcomingError"
            :loaded="episodesLoaded"
            :now="now"
          />
        </div>
        <EpisodesCard
          :past="past"
          :upcoming="upcoming"
          :past-error="pastError"
          :upcoming-error="upcomingError"
          :loaded="episodesLoaded"
          :username="username"
        />
      </div>
      <aside class="dash-side">
        <ChalkboardCard :username="username" />
      </aside>
    </div>

    <ShowsCard :shows="shows" :error="showsError" :loaded="showsLoaded" :username="username" />
  </div>
</template>

<style scoped>
.dashboard-page {
  max-width: 1200px;
}

.dash-cols {
  display: flex;
  flex-wrap: wrap;
  align-items: stretch;
  gap: var(--spacing-lg);
}

.dash-main {
  flex: 999 1 600px;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: var(--spacing-lg);
}

.dash-side {
  flex: 1 1 320px;
  min-width: 0;
  display: flex;
}

.dash-duo {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 300px), 1fr));
  gap: var(--spacing-lg);
}

@media (max-width: 640px) {
  .dash-cols,
  .dash-main,
  .dash-duo {
    gap: var(--spacing-md);
  }
}
</style>
