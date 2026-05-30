<script setup lang="ts">
import { ref, computed, onMounted } from 'vue';
import { useRouter } from 'vue-router';
import { showsApi, type Show } from '../api';
import { BaseButton } from '@shared/components';
import ShowList from '../components/ShowList.vue';
import ShowCreateModal from '../components/ShowCreateModal.vue';

const router = useRouter();
const shows = ref<Show[]>([]);
const loading = ref(true);
const error = ref<string | null>(null);

type ListFilter = 'all' | 'upcoming' | 'past';
const listFilter = ref<ListFilter>('upcoming');

function getDaysUntil(dateStr: string): number {
  const showDate = new Date(dateStr);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  showDate.setHours(0, 0, 0, 0);
  const diffTime = showDate.getTime() - today.getTime();
  return Math.ceil(diffTime / (1000 * 60 * 60 * 24));
}

const showCount = computed(() => {
  let filtered = shows.value;
  if (listFilter.value === 'upcoming') {
    filtered = shows.value.filter((s) => getDaysUntil(s.date) >= 0);
  } else if (listFilter.value === 'past') {
    filtered = shows.value.filter((s) => getDaysUntil(s.date) < 0);
  }
  return filtered.length;
});

const showCreateModal = ref(false);

function goToShow(show: Show) {
  router.push(`/shows/${show.id}`);
}

async function loadShows() {
  loading.value = true;
  error.value = null;
  try {
    const response = await showsApi.list();
    shows.value = response.shows;
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to load shows';
  } finally {
    loading.value = false;
  }
}

async function onShowCreated(show: Show) {
  showCreateModal.value = false;
  await loadShows();
  if (show?.id) {
    router.push(`/shows/${show.id}`);
  }
}

onMounted(loadShows);
</script>

<template>
  <div class="shows-page">
    <div class="page-header">
      <h1 class="page-title">Shows</h1>
      <div class="page-header-actions">
        <BaseButton variant="primary" @click="showCreateModal = true">+ New Show</BaseButton>
      </div>
    </div>

    <div v-if="error" class="flash-message error">{{ error }}</div>
    <div v-if="loading" class="loading-spinner"></div>

    <template v-else>
      <div class="list-toolbar">
        <div class="list-filters">
          <button :class="['filter-btn', { active: listFilter === 'upcoming' }]"
            @click="listFilter = 'upcoming'">Upcoming</button>
          <button :class="['filter-btn', { active: listFilter === 'all' }]" @click="listFilter = 'all'">All</button>
          <button :class="['filter-btn', { active: listFilter === 'past' }]" @click="listFilter = 'past'">Past</button>
        </div>
        <span class="list-count text-muted">{{ showCount }} shows</span>
      </div>

      <ShowList :shows="shows" :filter="listFilter" @show-click="goToShow" />
    </template>

    <ShowCreateModal
      :open="showCreateModal"
      @close="showCreateModal = false"
      @created="onShowCreated"
    />
  </div>
</template>

<style scoped>
.shows-page {
  max-width: 1200px;
}

.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--spacing-lg);
}

.list-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--spacing-lg);
}

.list-filters {
  display: flex;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  overflow: hidden;
}

.filter-btn {
  background: transparent;
  border: none;
  color: var(--color-text-muted);
  font-family: var(--font-family);
  font-size: var(--font-size-lg);
  padding: var(--spacing-sm) var(--spacing-xl);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.filter-btn:hover {
  background: var(--color-surface-hover);
  color: var(--color-text);
}

.filter-btn.active {
  background: var(--color-primary);
  color: var(--color-primary-text);
  font-weight: var(--font-weight-bold);
}

.list-count {
  font-size: var(--font-size-sm);
}
</style>
