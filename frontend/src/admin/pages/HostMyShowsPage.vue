<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { BaseButton } from '@shared/components';
import { seriesApi, type Series } from '../api';
import BoardIcon from '../components/BoardIcon.vue';

// Flow screen `my-shows` (host at /shows): one row per own Show with its
// rhythm, and "New show". UNHEARD is parked, so it has no row here.

const router = useRouter();
const shows = ref<Series[]>([]);
const error = ref<string | null>(null);

onMounted(async () => {
  try {
    shows.value = (await seriesApi.list()).series;
  } catch (e) {
    console.error('Failed to load shows:', e);
    error.value = 'Could not load your shows.';
  }
});
</script>

<template>
  <div class="my-shows-page host-page">
    <div class="page-head">
      <h1 class="page-title">My Shows</h1>
      <BaseButton variant="secondary" @click="router.push('/shows/new')"
        ><BoardIcon name="plus" :size="16" />New show</BaseButton
      >
    </div>
    <p v-if="error" class="field-error" role="alert">{{ error }}</p>
    <section class="board-card">
      <ul class="shows board-rows">
        <li v-for="show in shows" :key="show.slug">
          <router-link :to="`/shows/${show.slug}`" class="board-row show-row">
            <div class="cover thumb">
              <img v-if="show.cover_url" :src="show.cover_url" alt="" />
              <BoardIcon v-else name="image" :size="24" />
            </div>
            <span class="name row-title big">{{ show.name }}</span>
            <span class="rhythm row-sub"
              ><BoardIcon v-if="show.rhythm_line" name="repeat" :size="14" />{{
                show.rhythm_line
              }}</span
            >
            <BoardIcon name="chevron" class="row-icon" />
          </router-link>
        </li>
      </ul>
    </section>
  </div>
</template>

<style scoped>
.my-shows-page {
  max-width: 1040px;
}

.page-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: var(--spacing-md);
}

/* Name over rhythm, chevron on the right. Both stay direct children of the
   link, so the row reads "Brunchtime | Sundays · 12:00–14:00". */
.host-page .board-card > .board-rows .show-row {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  grid-template-areas:
    'cover name icon'
    'cover rhythm icon';
  gap: 4px var(--spacing-md);
  min-height: 84px;
}

.thumb {
  grid-area: cover;
  width: 64px;
  height: 64px;
  border-radius: var(--radius-lg);
}

.name {
  grid-area: name;
  align-self: end;
}

.rhythm {
  grid-area: rhythm;
  align-self: start;
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.rhythm svg {
  color: var(--color-primary);
}

.show-row .row-icon {
  grid-area: icon;
}
</style>
