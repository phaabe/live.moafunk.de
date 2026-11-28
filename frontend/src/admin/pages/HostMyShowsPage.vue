<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import {
  onBeforeRouteLeave,
  onBeforeRouteUpdate,
  useRoute,
  useRouter,
  type LocationQuery,
} from 'vue-router';
import { BaseButton } from '@shared/components';
import { seriesApi, type Series, type SeriesHost, type UpcomingEpisode } from '../api';
import BoardIcon from '../components/BoardIcon.vue';
import MyShowCard from '../components/my-show/MyShowCard.vue';

// Flow screen `my-shows` (host at /shows), reworked as "My Show" (#1050).
// One Show fills the page as one card; more Shows sit in a grid, two per
// row. A card reads as plain text until Edit turns it into the Show form.
// `?edit=<slug>` opens that card in edit mode (the Show page links here).
// The Shows the host owns or co-hosts are listed, so every card may be edited.
// UNHEARD is parked, so it has no card here.

const route = useRoute();
const router = useRouter();

const shows = ref<Series[]>([]);
/** All hosts' upcoming episodes, by date and start time; null when they could not load. */
const upcoming = ref<UpcomingEpisode[] | null>(null);
const loaded = ref(false);
const error = ref<string | null>(null);
/** The card in edit mode has unsaved changes. */
const dirty = ref(false);

const editOf = (query: LocationQuery) => (typeof query.edit === 'string' ? query.edit : null);

/** The slug of the card in edit mode, when it is one of the host's Shows. */
const editing = computed(() => {
  const slug = editOf(route.query);
  return shows.value.some((s) => s.slug === slug) ? slug : null;
});
const grid = computed(() => shows.value.length > 1);

/** A Show's next episode; null when none is booked, undefined when the episodes could not load. */
const nextOf = (slug: string) =>
  upcoming.value ? (upcoming.value.find((e) => e.series_slug === slug) ?? null) : undefined;

/** Edit mode lives in the URL, so a reload keeps it and the Show page can link to it. */
function setEditing(slug: string | null) {
  const query: LocationQuery = { ...route.query };
  delete query.edit;
  if (slug) query.edit = slug;
  return router.replace({ query });
}

function onSaved(saved: Series) {
  shows.value = shows.value.map((s) => (s.slug === saved.slug ? saved : s));
  dirty.value = false;
  void setEditing(null);
}

function onHosts(slug: string, hosts: SeriesHost[]) {
  shows.value = shows.value.map((s) => (s.slug === slug ? { ...s, hosts } : s));
}

/** Leave show: the Show is no longer the host's. */
function onLeft(slug: string) {
  shows.value = shows.value.filter((s) => s.slug !== slug);
  dirty.value = false;
  void setEditing(null);
}

/** Leaving edit mode with unsaved changes asks once. */
const mayLeave = () => !dirty.value || window.confirm('Discard changes?');

onBeforeRouteUpdate((to, from) => editOf(to.query) === editOf(from.query) || mayLeave());
onBeforeRouteLeave(() => mayLeave());

/** A reload or a closed tab with unsaved changes asks too (the browser's own prompt). */
function onBeforeUnload(event: BeforeUnloadEvent) {
  if (!dirty.value) return;
  event.preventDefault();
  event.returnValue = '';
}
window.addEventListener('beforeunload', onBeforeUnload);
onBeforeUnmount(() => window.removeEventListener('beforeunload', onBeforeUnload));

onMounted(async () => {
  // The cards still show without the episodes; their Next episode says "Could not load".
  const [list, episodes] = await Promise.allSettled([seriesApi.list(), seriesApi.upcoming()]);
  if (list.status === 'fulfilled') {
    shows.value = list.value.series;
  } else {
    console.error('Failed to load shows:', list.reason);
    error.value = 'Could not load your shows.';
  }
  if (episodes.status === 'fulfilled') upcoming.value = episodes.value.episodes;
  else console.error('Failed to load upcoming episodes:', episodes.reason);
  loaded.value = true;
  // An edit link to a Show that is not the host's: drop it.
  if (editOf(route.query) && !editing.value) void setEditing(null);
});
</script>

<template>
  <div :class="['my-show-page', 'host-page', { grid }]">
    <div class="page-head">
      <h1 class="page-title">{{ grid ? 'Your shows' : 'My Show' }}</h1>
      <BaseButton variant="secondary" @click="router.push('/shows/new')"
        ><BoardIcon name="plus" :size="16" />New show</BaseButton
      >
    </div>
    <p v-if="error" class="field-error" role="alert">{{ error }}</p>
    <section v-else-if="loaded && shows.length === 0" class="board-card">
      <p class="empty">You have no show yet. Create one, then book its episodes.</p>
    </section>
    <div v-else :class="grid ? 'show-grid' : 'show-one'">
      <MyShowCard
        v-for="show in shows"
        :key="show.slug"
        v-model:dirty="dirty"
        :show="show"
        :next="nextOf(show.slug)"
        :editing="editing === show.slug"
        :compact="grid"
        @edit="setEditing(show.slug)"
        @cancel="setEditing(null)"
        @saved="onSaved"
        @hosts="onHosts(show.slug, $event)"
        @left="onLeft(show.slug)"
      />
    </div>
  </div>
</template>

<style scoped>
.my-show-page {
  max-width: 1120px;
}

.my-show-page.grid {
  max-width: 1200px;
}

.page-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: var(--spacing-md);
}

/* Two per row; one on narrow screens. Cards in a row share a height, but
   not with a card in edit mode: the form is much taller. */
.show-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 440px), 1fr));
  gap: 24px;
}

.show-grid:has(.editing) {
  align-items: start;
}

@media (max-width: 640px) {
  .show-grid {
    gap: 16px;
  }
}
</style>
