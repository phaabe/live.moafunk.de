<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { BaseButton } from '@shared/components';
import { seriesApi, type Series } from '../api';

// Flow screen `my-shows` (host at /shows): one row per own Show with its
// rhythm, and "New show". UNHEARD is parked, so it has no row here.

const router = useRouter();
const shows = ref<Series[]>([]);
const error = ref<string | null>(null);

onMounted(async () => {
  try {
    shows.value = (await seriesApi.list()).series;
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Could not load your shows';
  }
});
</script>

<template>
  <div class="my-shows-page">
    <h1 class="page-title">My Shows</h1>
    <p v-if="error" class="field-error" role="alert">{{ error }}</p>
    <ul class="shows">
      <li v-for="show in shows" :key="show.slug">
        <router-link :to="`/shows/${show.slug}`" class="show-row">
          <span>{{ show.name }}</span>
          <span class="rhythm">{{ show.rhythm_line }}</span>
        </router-link>
      </li>
    </ul>
    <div>
      <BaseButton @click="router.push('/shows/new')">New show</BaseButton>
    </div>
  </div>
</template>

<style scoped>
.my-shows-page {
  max-width: 560px;
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
}

.page-title {
  margin: 0;
}

.shows {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
}

.show-row {
  display: flex;
  justify-content: space-between;
  gap: var(--spacing-md);
  padding: var(--spacing-sm) var(--spacing-md);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  color: var(--color-text);
}

.show-row:hover {
  border-color: var(--color-primary);
}

.rhythm {
  color: var(--color-text-muted);
}

.field-error {
  color: var(--color-error);
  font-size: var(--font-size-sm);
  margin: 0;
}
</style>
