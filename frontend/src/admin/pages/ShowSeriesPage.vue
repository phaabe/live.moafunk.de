<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { BaseButton } from '@shared/components';
import { seriesApi, type SeriesDetail } from '../api';
import { episodeDay, episodeTarget } from '../episodeRow';
import BoardIcon from '../components/BoardIcon.vue';

// Flow screen `show`: one Show (series) with its episodes, newest first.

const route = useRoute();
const router = useRouter();

const series = ref<SeriesDetail | null>(null);
const error = ref<string | null>(null);

const slug = computed(() => String(route.params.slug));

const rows = computed(() =>
  (series.value?.episodes ?? []).map((episode) => ({
    id: episode.id,
    left: `${episodeDay(episode.date)} · ${episode.mode_label}`,
    right: episode.status_label,
    status: episode.status,
    to: episodeTarget(episode),
  }))
);

async function load() {
  error.value = null;
  try {
    const found = await seriesApi.get(slug.value);
    if (!found) {
      // Not this host's show (or no such show).
      await router.replace('/shows');
      return;
    }
    series.value = found;
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Could not load the show';
  }
}

watch(slug, load, { immediate: true });
</script>

<template>
  <div class="show-series-page">
    <router-link to="/shows" class="back-link">&lt; My Shows</router-link>
    <p v-if="error" class="field-error" role="alert">{{ error }}</p>

    <template v-if="series">
      <div class="show-head">
        <div class="cover">
          <img v-if="series.cover_url" :src="series.cover_url" alt="Show cover" />
        </div>
        <div class="show-text">
          <h1 class="page-title">{{ series.name }}</h1>
          <p v-if="series.description" class="description">{{ series.description }}</p>
        </div>
        <BaseButton @click="router.push(`/shows/${series.slug}/book`)">Book Slot</BaseButton>
      </div>

      <section class="board-card">
        <div class="board-card-head">
          <h2 class="board-card-title"><BoardIcon name="list" />Episodes</h2>
        </div>
        <div class="board-card-body">
          <ul class="episodes board-rows">
            <li v-for="row in rows" :key="row.id">
              <router-link v-if="row.to" :to="row.to" class="board-row">
                <span>{{ row.left }}</span>
                <span :class="['tag', `tag-${row.status}`]">{{ row.right }}</span>
              </router-link>
              <div v-else class="board-row">
                <span>{{ row.left }}</span>
                <span :class="['tag', `tag-${row.status}`]">{{ row.right }}</span>
              </div>
            </li>
          </ul>
        </div>
      </section>
    </template>
  </div>
</template>

<style scoped>
.show-series-page {
  max-width: 960px;
  display: flex;
  flex-direction: column;
  gap: var(--spacing-lg);
}

.back-link {
  align-self: flex-start;
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}

.show-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--spacing-lg);
  padding-bottom: var(--spacing-lg);
  border-bottom: 1px solid var(--color-border);
}

.cover {
  width: 72px;
  height: 72px;
  flex-shrink: 0;
  border-radius: var(--radius-lg);
  overflow: hidden;
  background-color: var(--color-surface-alt);
}

.cover img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.show-text {
  flex: 1;
  min-width: 200px;
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
}

.page-title {
  margin: 0;
}

.description {
  margin: 0;
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
}

.field-error {
  color: var(--color-error);
  font-size: var(--font-size-sm);
  margin: 0;
}
</style>
