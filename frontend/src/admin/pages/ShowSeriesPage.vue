<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { seriesApi, type SeriesDetail, type SeriesEpisode, type SeriesSlot } from '../api';
import { useAuthStore } from '../stores/auth';
import BoardIcon from '../components/BoardIcon.vue';
import EpisodeRowsCard from '../components/show-page/EpisodeRowsCard.vue';
import OnAirStrip from '../components/show-page/OnAirStrip.vue';
import ScheduleCard from '../components/show-page/ScheduleCard.vue';
import ShowCard from '../components/show-page/ShowCard.vue';
import { scheduleItems, splitEpisodes } from '../components/show-page/showPage';

// Flow screen `show` (#/shows/<slug>, issue 1051, design D1): one Show
// (series) with what is on air, its Show card, one Schedule card (upcoming
// episodes and, for its owner, the free slots) and the past episodes. Every
// host may open it: other hosts read it, only the owner edits (on My Show)
// and books.

/** Free slots shown; Book another date books any other date. */
const SLOTS_SHOWN = 3;

const route = useRoute();
const router = useRouter();
const auth = useAuthStore();

const series = ref<SeriesDetail | null>(null);
const error = ref<string | null>(null);
const slots = ref<SeriesSlot[]>([]);
const slotsError = ref<string | null>(null);
const now = ref(Date.now());
const clock = setInterval(() => (now.value = Date.now()), 30000);
onUnmounted(() => clearInterval(clock));

const slug = computed(() => String(route.params.slug));
/** The owner, or an admin: books and manages the episodes. */
const canEdit = computed(() => series.value?.can_edit === true);
/** Edit lives on My Show, a host page: the owner gets it there. */
const editable = computed(() => canEdit.value && auth.user?.role === 'host');
const episodes = computed(() => splitEpisodes(series.value?.episodes ?? []));
/** Upcoming episodes and, for whoever may book, the free slots. */
const schedule = computed(() =>
  scheduleItems(episodes.value.upcoming, canEdit.value ? slots.value : [])
);

const back = computed(() => {
  if (auth.user?.role !== 'host') return { to: '/shows', label: 'Shows' };
  return canEdit.value
    ? { to: '/shows', label: 'My Show' }
    : { to: '/dashboard', label: 'Dashboard' };
});

async function loadSlots(found: SeriesDetail) {
  slots.value = [];
  slotsError.value = null;
  if (!found.can_edit || !found.rhythm) return;
  try {
    slots.value = (await seriesApi.slots(found.slug)).slots.slice(0, SLOTS_SHOWN);
  } catch (e) {
    console.error('Failed to load free slots:', e);
    slotsError.value = 'Could not load the free slots.';
  }
}

async function load() {
  error.value = null;
  try {
    const found = await seriesApi.get(slug.value);
    if (!found) {
      // No such show.
      await router.replace('/shows');
      return;
    }
    series.value = found;
    await loadSlots(found);
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Could not load the show';
  }
}

function edit() {
  if (series.value) router.push({ path: '/shows', query: { edit: series.value.slug } });
}

function openEpisode(episode: SeriesEpisode) {
  router.push(`/shows/${episode.id}`);
}

/** Book a free slot, or (null) any date. */
function book(slot: SeriesSlot | null) {
  if (!series.value) return;
  router.push({
    path: `/shows/${series.value.slug}/book`,
    query: slot ? { date: slot.date } : {},
  });
}

watch(slug, load, { immediate: true });
</script>

<template>
  <div class="show-series-page host-page">
    <router-link :to="back.to" class="back-link"
      ><BoardIcon name="back" :size="16" /><span class="sep">&lt; </span
      >{{ back.label }}</router-link
    >
    <p v-if="error" class="field-error" role="alert">{{ error }}</p>
    <template v-if="series">
      <OnAirStrip :series="series" :owner="canEdit" />
      <ShowCard :series="series" :editable="editable" @edit="edit" />

      <ScheduleCard
        :items="schedule"
        :mine="canEdit"
        :now="now"
        :error="slotsError"
        :no-rhythm="!series.rhythm"
        @open="openEpisode"
        @book="book"
      />
      <EpisodeRowsCard
        title="Past episodes"
        kind="past"
        :episodes="episodes.past"
        :mine="canEdit"
        empty="No past episodes yet."
      />
    </template>
  </div>
</template>

<style scoped>
.show-series-page {
  max-width: 1040px;
}
</style>
