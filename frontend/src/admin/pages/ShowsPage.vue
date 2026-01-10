<script setup lang="ts">
import { ref, onMounted } from 'vue';
import { showsApi, type Show, type Artist } from '../api';
import { BaseButton, BaseModal, FormInput } from '@shared/components';
import { useFlash } from '../composables/useFlash';

const flash = useFlash();
const shows = ref<Show[]>([]);
const artists = ref<Artist[]>([]);
const loading = ref(true);
const error = ref<string | null>(null);

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
      <BaseButton variant="primary" @click="showCreateModal = true">
        + New Show
      </BaseButton>
    </div>

    <div v-if="error" class="flash-message error">{{ error }}</div>

    <div v-if="loading" class="loading-spinner"></div>

    <div v-else class="card">
      <table class="data-table">
        <thead>
          <tr>
            <th>Title</th>
            <th>Date</th>
            <th>Status</th>
            <th>Artists</th>
            <th>Downloads</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="show in shows" :key="show.id">
            <td>
              <router-link :to="`/shows/${show.id}`" class="show-title">
                {{ show.title }}
              </router-link>
            </td>
            <td>{{ show.date }}</td>
            <td>
              <span :class="['badge', show.status === 'completed' ? 'success' : 'warning']">
                {{ show.status }}
              </span>
            </td>
            <td class="text-muted">
              {{ show.artists.map((a) => a.name).join(', ') || '-' }}
            </td>
            <td class="download-cell">
              <template v-if="show.artists.length > 0">
                <a :href="`/shows/${show.id}/download/recording`" class="dl-btn recording" title="Recording Package">📼</a>
                <a :href="`/shows/${show.id}/download/social-media`" class="dl-btn social" title="Social Media Package">📱</a>
                <a :href="`/shows/${show.id}/download/all-data`" class="dl-btn all" title="All Material">📦</a>
              </template>
              <span v-else class="text-muted">-</span>
            </td>
          </tr>
          <tr v-if="shows.length === 0">
            <td colspan="5" class="text-muted" style="text-align: center; padding: 2rem;">
              No shows found
            </td>
          </tr>
        </tbody>
      </table>
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

.download-cell {
  display: flex;
  gap: var(--spacing-xs);
}

.dl-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border-radius: var(--radius-sm);
  text-decoration: none;
  font-size: 14px;
  transition: all var(--transition-fast);
  border: 1px solid;
}

.dl-btn.recording {
  border-color: #00ff04;
}

.dl-btn.recording:hover {
  background-color: #00ff04;
}

.dl-btn.social {
  border-color: #ff00aa;
}

.dl-btn.social:hover {
  background-color: #ff00aa;
}

.dl-btn.all {
  border-color: #bbbbbb;
}

.dl-btn.all:hover {
  background-color: #bbbbbb;
}

.create-form {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
}
</style>
