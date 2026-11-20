<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { seriesApi, type SeriesDetail, type SeriesSlot } from '../api';
import { useAuthStore } from '../stores/auth';
import BoardIcon from '../components/BoardIcon.vue';
import EpisodeRowsCard from '../components/show-page/EpisodeRowsCard.vue';
import FreeSlotsCard from '../components/show-page/FreeSlotsCard.vue';
import NextEpisodeCard from '../components/show-page/NextEpisodeCard.vue';
import OnAirStrip from '../components/show-page/OnAirStrip.vue';
import ShowCard from '../components/show-page/ShowCard.vue';
import { splitEpisodes } from '../components/show-page/showPage';

// Flow screen `show` (#/shows/<slug>, issue 1051): one Show (series) with
// what is on air, its next episode, upcoming and past episodes and, for its
// owner, the free slots (Schedule's content lives here now). Every host may
// open it: other hosts read it, only the owner edits (on My Show) and books.

/** Free slots shown; Book Slot books any other date. */
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
const next = computed(() => episodes.value.upcoming[0] ?? null);
/** Free slots and Book Slot: only for whoever may book. */
const showSlots = computed(() => canEdit.value);

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

function openEpisode() {
  if (next.value) router.push(`/shows/${next.value.id}`);
}

/** Book a free slot, or (null) any date on Book Slot. */
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

      <div :class="['show-grid', { 'with-side': showSlots }]">
        <NextEpisodeCard
          v-if="next"
          class="in-main"
          :episode="next"
          :mine="canEdit"
          :now="now"
          @open="openEpisode"
        />
        <EpisodeRowsCard
          class="in-main"
          title="Upcoming episodes"
          kind="upcoming"
          :episodes="episodes.upcoming"
          :mine="canEdit"
          empty="No upcoming episodes."
        />
        <FreeSlotsCard
          v-if="showSlots"
          class="in-side"
          :slots="slots"
          :error="slotsError"
          :no-rhythm="!series.rhythm"
          @book="book"
        />
        <EpisodeRowsCard
          class="in-main"
          title="Past episodes"
          kind="past"
          :episodes="episodes.past"
          :mine="canEdit"
          empty="No past episodes yet."
        />
      </div>
    </template>
  </div>
</template>

<style scoped>
.show-series-page {
  max-width: 1200px;
}

/* One column on phones (free slots after the upcoming episodes); on desktop
   the free slots sit beside the episodes. The last row takes the side card's
   extra height, so the episode cards stay together. */
.show-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  align-items: start;
  gap: var(--spacing-lg);
}

@media (min-width: 960px) {
  .show-grid.with-side {
    grid-template-columns: minmax(0, 1fr) minmax(300px, 360px);
    grid-template-rows: auto auto auto 1fr;
  }

  .show-grid.with-side > .in-main {
    grid-column: 1;
  }

  .show-grid.with-side > .in-side {
    grid-column: 2;
    grid-row: 1 / -1;
  }
}
</style>
