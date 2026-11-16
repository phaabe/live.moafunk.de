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
    <div class="page-head">
      <h1 class="page-title">My Shows</h1>
      <BaseButton @click="router.push('/shows/new')">New show</BaseButton>
    </div>
    <p v-if="error" class="field-error" role="alert">{{ error }}</p>
    <section class="board-card">
      <div class="board-card-body">
        <ul class="shows board-rows">
          <li v-for="show in shows" :key="show.slug">
            <router-link :to="`/shows/${show.slug}`" class="board-row">
              <span class="name">{{ show.name }}</span>
              <span class="rhythm">{{ show.rhythm_line }}</span>
            </router-link>
          </li>
        </ul>
      </div>
    </section>
  </div>
</template>

<style scoped>
.my-shows-page {
  max-width: 960px;
  display: flex;
  flex-direction: column;
  gap: var(--spacing-lg);
}

.page-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: var(--spacing-md);
}

.page-title {
  margin: 0;
}

.name {
  font-weight: var(--font-weight-bold);
}

.rhythm {
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
}

.field-error {
  color: var(--color-error);
  font-size: var(--font-size-sm);
  margin: 0;
}
</style>
