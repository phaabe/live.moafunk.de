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
            <th>Actions</th>
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
            <td>
              <router-link :to="`/shows/${show.id}`" class="action-link">Edit</router-link>
              <button class="action-link danger" @click="deleteShow(show.id)">Delete</button>
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
        <FormInput v-model="newShow.date" label="Date" type="text" placeholder="YYYY-MM-DD" required />
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

.action-link {
  background: none;
  border: none;
  color: var(--color-link);
  cursor: pointer;
  font-family: var(--font-family);
  font-size: inherit;
  padding: 0;
  margin-right: var(--spacing-md);
}

.action-link:hover {
  color: var(--color-primary);
}

.action-link.danger:hover {
  color: var(--color-error);
}

.create-form {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
}
</style>
