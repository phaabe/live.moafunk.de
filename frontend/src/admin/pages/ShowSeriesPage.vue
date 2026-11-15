<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { BaseButton } from '@shared/components';
import { seriesApi, type SeriesDetail } from '../api';
import { episodeDay, episodeTarget } from '../episodeRow';

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
      <div class="cover">
        <img v-if="series.cover_url" :src="series.cover_url" alt="Show cover" />
      </div>
      <h1 class="page-title">{{ series.name }}</h1>
      <p v-if="series.description" class="description">{{ series.description }}</p>
      <div>
        <BaseButton @click="router.push(`/shows/${series.slug}/book`)">Book Slot</BaseButton>
      </div>

      <h2 class="section-title">Episodes</h2>
      <ul class="episodes">
        <li v-for="row in rows" :key="row.id">
          <router-link v-if="row.to" :to="row.to" class="episode">
            <span>{{ row.left }}</span>
            <span class="status">{{ row.right }}</span>
          </router-link>
          <div v-else class="episode">
            <span>{{ row.left }}</span>
            <span class="status">{{ row.right }}</span>
          </div>
        </li>
      </ul>
    </template>
  </div>
</template>

<style scoped>
.show-series-page {
  max-width: 560px;
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
}

.back-link {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}

.cover {
  width: 220px;
  height: 220px;
  border-radius: var(--radius-md);
  overflow: hidden;
  background-color: var(--color-surface);
}

.cover img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.page-title {
  margin: 0;
}

.description {
  margin: 0;
  color: var(--color-text-muted);
}

.section-title {
  font-size: var(--font-size-lg);
  margin: var(--spacing-md) 0 0;
}

.episodes {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
}

.episode {
  display: flex;
  justify-content: space-between;
  gap: var(--spacing-md);
  padding: var(--spacing-sm) var(--spacing-md);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  color: var(--color-text);
}

a.episode:hover {
  border-color: var(--color-primary);
}

.status {
  color: var(--color-text-muted);
}

.field-error {
  color: var(--color-error);
  font-size: var(--font-size-sm);
  margin: 0;
}
</style>
