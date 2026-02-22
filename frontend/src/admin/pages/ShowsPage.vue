<script setup lang="ts">
import { ref, computed, onMounted } from 'vue';
import { useRouter } from 'vue-router';
import { showsApi, type Show } from '../api';
import { BaseButton, BaseModal, FormInput } from '@shared/components';
import { useFlash } from '../composables/useFlash';
import ShowList from '../components/ShowList.vue';

const router = useRouter();
const flash = useFlash();
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
const creating = ref(false);
const newShow = ref({
  title: '',
  date: '',
  start_time: '',
  description: '',
  show_type: 'unheard',
});

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

async function createShow() {
  creating.value = true;
  try {
    const created = await showsApi.create(newShow.value);
    flash.success('Show created successfully');
    showCreateModal.value = false;
    newShow.value = { title: '', date: '', start_time: '', description: '', show_type: 'unheard' };
    await loadShows();
    if (created?.id) {
      router.push(`/shows/${created.id}`);
    }
  } catch (e) {
    flash.error(e instanceof Error ? e.message : 'Failed to create show');
  } finally {
    creating.value = false;
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

    <BaseModal :open="showCreateModal" title="Create New Show" @close="showCreateModal = false">
      <form class="create-form" @submit.prevent="createShow">
        <div class="form-group">
          <label class="form-label">Show Type</label>
          <select v-model="newShow.show_type" class="type-select">
            <option value="unheard">UNHEARD</option>
            <option value="brunchtime">Brunchtime</option>
            <option value="external">External</option>
          </select>
        </div>
        <FormInput v-model="newShow.title" label="Title" required />
        <FormInput v-model="newShow.date" label="Date" type="date" required />
        <FormInput v-model="newShow.start_time" label="Start Time" type="time" />
        <FormInput v-model="newShow.description" label="Description" />
      </form>
      <template #footer>
        <BaseButton variant="ghost" @click="showCreateModal = false">Cancel</BaseButton>
        <BaseButton variant="primary" :loading="creating" @click="createShow">
          Create Show
        </BaseButton>
      </template>
    </BaseModal>
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

.create-form {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
}

.form-group {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
}

.form-label {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
  font-weight: var(--font-weight-medium);
}

.type-select {
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  color: var(--color-text);
  font-family: var(--font-family);
  padding: var(--spacing-sm) var(--spacing-md);
}
</style>
