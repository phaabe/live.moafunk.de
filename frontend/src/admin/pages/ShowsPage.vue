<script setup lang="ts">
import { ref, computed, onMounted } from 'vue';
import { showsApi, type Show, type Artist } from '../api';
import { BaseButton, BaseModal, FormInput } from '@shared/components';
import { useFlash } from '../composables/useFlash';

const flash = useFlash();
const shows = ref<Show[]>([]);
const artists = ref<Artist[]>([]);
const loading = ref(true);
const error = ref<string | null>(null);

type FilterType = 'all' | 'past' | 'upcoming';
const filter = ref<FilterType>('upcoming');

const filteredAndSortedShows = computed(() => {
  let filtered = shows.value;
  
  if (filter.value === 'upcoming') {
    filtered = shows.value.filter(show => getDaysUntil(show.date) >= 0);
  } else if (filter.value === 'past') {
    filtered = shows.value.filter(show => getDaysUntil(show.date) < 0);
  }
  
  return [...filtered].sort((a, b) => {
    return new Date(a.date).getTime() - new Date(b.date).getTime();
  });
});

function getDaysUntil(dateStr: string): number {
  const showDate = new Date(dateStr);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  showDate.setHours(0, 0, 0, 0);
  const diffTime = showDate.getTime() - today.getTime();
  return Math.ceil(diffTime / (1000 * 60 * 60 * 24));
}

function getDaysClass(days: number): string {
  if (days < 0) return 'days-completed';
  if (days <= 7) return 'days-critical';
  if (days <= 15) return 'days-warning';
  return 'days-ok';
}

const showCreateModal = ref(false);
const creating = ref(false);
const newShow = ref({
  title: '',
  date: '',
  description: '',
});

async function loadShows() {
  loading.value = true;
  error.value = null;

  try {
    const response = await showsApi.list();
    shows.value = response.shows;
    artists.value = response.artists;
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to load shows';
  } finally {
    loading.value = false;
  }
}

async function createShow() {
  creating.value = true;
  error.value = null;

  try {
    await showsApi.create(newShow.value);
    flash.success('Show created successfully');
    showCreateModal.value = false;
    newShow.value = { title: '', date: '', description: '' };
    await loadShows();
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to create show';
  } finally {
    creating.value = false;
  }
}

async function deleteShow(id: number) {
  if (!confirm('Are you sure you want to delete this show?')) return;

  try {
    await showsApi.delete(id);
    flash.success('Show deleted');
    await loadShows();
  } catch (e) {
    flash.error(e instanceof Error ? e.message : 'Failed to delete show');
  }
}

onMounted(loadShows);
</script>

<template>
  <div class="shows-page">
    <div class="page-header">
      <h1 class="page-title">Shows</h1>
      <div class="filters">
        <select v-model="filter" class="filter-select">
          <option value="all">All Shows</option>
          <option value="upcoming">Upcoming</option>
          <option value="past">Past</option>
        </select>
      </div>
    </div>

    <div v-if="error" class="flash-message error">{{ error }}</div>

    <div v-if="loading" class="loading-spinner"></div>

    <div v-else class="card">
      <table class="data-table">
        <thead>
          <tr>
            <th>Title</th>
            <th>Days Until</th>
            <th>Date</th>
            <th>Assigned</th>
            <th>Artists</th>
            <th>Downloads</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="show in filteredAndSortedShows" :key="show.id">
            <td>
              <router-link :to="`/shows/${show.id}`" class="show-title">
                {{ show.title }}
              </router-link>
            </td>
            <td>
              <span :class="['badge', 'days-until', getDaysClass(getDaysUntil(show.date))]">
                {{ getDaysUntil(show.date) < 0 ? '✓' : getDaysUntil(show.date) + 'd' }}
              </span>
            </td>
            <td>{{ show.date }}</td>
            <td>
              <span :class="['badge', 'artist-count', {
                'count-empty': show.artists.length === 0,
                'count-partial': show.artists.length > 0 && show.artists.length < 4,
                'count-full': show.artists.length >= 4
              }]">
                {{ show.artists.length }}/4
              </span>
            </td>
            <td class="text-muted">
              {{ show.artists.map((a) => a.name).join(', ') || '-' }}
            </td>
            <td class="download-cell">
              <template v-if="show.artists.length > 0">
                <a :href="`/shows/${show.id}/download/recording`" class="dl-btn recording" title="Recording Package">REC</a>
                <a :href="`/shows/${show.id}/download/social-media`" class="dl-btn social" title="Social Media Package">SM</a>
                <a :href="`/shows/${show.id}/download/all-data`" class="dl-btn all" title="All Material">ALL</a>
              </template>
              <span v-else class="text-muted">-</span>
            </td>
          </tr>
          <tr v-if="filteredAndSortedShows.length === 0">
            <td colspan="6" class="text-muted" style="text-align: center; padding: 2rem;">
              No shows found
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="page-footer">
      <BaseButton variant="primary" @click="showCreateModal = true">
        + New Show
      </BaseButton>
    </div>

    <BaseModal :open="showCreateModal" title="Create New Show" @close="showCreateModal = false">
      <form class="create-form" @submit.prevent="createShow">
        <FormInput v-model="newShow.title" label="Title" required />
        <FormInput v-model="newShow.date" label="Date" type="date" required />
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
.show-title {
  color: var(--color-primary);
  font-weight: var(--font-weight-medium);
}

.filters {
  display: flex;
  gap: var(--spacing-md);
}

.filter-select {
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  color: var(--color-text);
  font-family: var(--font-family);
  padding: var(--spacing-sm) var(--spacing-md);
}

.page-footer {
  margin-top: var(--spacing-lg);
  display: flex;
  justify-content: flex-end;
}

/* Ensure consistent row heights across all table rows */
:deep(.data-table td) {
  height: 48px;
  vertical-align: middle;
}

.download-cell {
  display: flex;
  gap: var(--spacing-xs);
  justify-content: flex-end;
  align-items: center;
}

.dl-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: var(--spacing-xs) var(--spacing-sm);
  border-radius: var(--radius-md);
  text-decoration: none;
  font-size: var(--font-size-m);
  transition: all var(--transition-fast);
  background-color: #666666;
  color: #ffffff;
  white-space: nowrap;
}

.dl-btn:hover {
  opacity: 0.8;
}

.dl-btn.recording {
  background-color: #00cc03;
}

.dl-btn.social {
  background-color: #cc008a;
}

.dl-btn.all {
  background-color: #888888;
}

.create-form {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
}

/* Artist count badges */
.artist-count {
  font-weight: var(--font-weight-bold);
  font-size: var(--font-size-sm);
  padding: 0.25rem 0.5rem;
  border-radius: var(--radius-sm);
}

.count-empty {
  background-color: rgba(255, 59, 48, 0.2);
  color: #ff3b30;
  border: 1px solid #ff3b30;
}

.count-partial {
  background-color: rgba(255, 149, 0, 0.2);
  color: #ff9500;
  border: 1px solid #ff9500;
}

.count-full {
  background-color: rgba(52, 199, 89, 0.2);
  color: #34c759;
  border: 1px solid #34c759;
}

/* Days until show badges */
.days-until {
  font-weight: var(--font-weight-bold);
  font-size: var(--font-size-sm);
  padding: 0.25rem 0.5rem;
  border-radius: var(--radius-sm);
  min-width: 50px;
  text-align: center;
  display: inline-block;
}

.days-critical {
  background-color: rgba(255, 59, 48, 0.2);
  color: #ff3b30;
  border: 1px solid #ff3b30;
}

.days-warning {
  background-color: rgba(255, 149, 0, 0.2);
  color: #ff9500;
  border: 1px solid #ff9500;
}

.days-ok {
  background-color: rgba(52, 199, 89, 0.2);
  color: #34c759;
  border: 1px solid #34c759;
}

.days-completed {
  background-color: rgba(128, 128, 128, 0.2);
  color: #888888;
  border: 1px solid #888888;
}

/* Mobile responsive - show only Title, Status, and Assigned columns */
@media (max-width: 768px) {
  .data-table th:nth-child(3),
  .data-table th:nth-child(5),
  .data-table th:nth-child(6),
  .data-table td:nth-child(3),
  .data-table td:nth-child(5),
  .data-table td:nth-child(6) {
    display: none;
  }
}
</style>
