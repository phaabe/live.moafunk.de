<script setup lang="ts">
import { ref, onMounted } from 'vue';
import { artistsApi, type Artist } from '../api';

const artists = ref<Artist[]>([]);
const loading = ref(true);
const error = ref<string | null>(null);

const filter = ref<string>('');
const sort = ref<string>('name');
const dir = ref<string>('asc');

async function loadArtists() {
  loading.value = true;
  error.value = null;

  try {
    const response = await artistsApi.list({
      filter: filter.value || undefined,
      sort: sort.value,
      dir: dir.value,
    });
    artists.value = response.artists;
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to load artists';
  } finally {
    loading.value = false;
  }
}

function toggleSort(column: string) {
  if (sort.value === column) {
    dir.value = dir.value === 'asc' ? 'desc' : 'asc';
  } else {
    sort.value = column;
    dir.value = 'asc';
  }
  loadArtists();
}

onMounted(loadArtists);
</script>

<template>
  <div class="artists-page">
    <div class="page-header">
      <h1 class="page-title">Artists</h1>
      <div class="filters">
        <select v-model="filter" class="filter-select" @change="loadArtists">
          <option value="">All Artists</option>
          <option value="assigned">Assigned</option>
          <option value="unassigned">Unassigned</option>
        </select>
      </div>
    </div>

    <div v-if="error" class="flash-message error">{{ error }}</div>

    <div v-if="loading" class="loading-spinner"></div>

    <div v-else class="card">
      <table class="data-table">
        <thead>
          <tr>
            <th class="sortable" @click="toggleSort('name')">
              Name
              <span v-if="sort === 'name'">{{ dir === 'asc' ? '↑' : '↓' }}</span>
            </th>
            <th class="sortable" @click="toggleSort('status')">
              Status
              <span v-if="sort === 'status'">{{ dir === 'asc' ? '↑' : '↓' }}</span>
            </th>
            <th>Shows</th>
            <th class="sortable" @click="toggleSort('submitted')">
              Submitted
              <span v-if="sort === 'submitted'">{{ dir === 'asc' ? '↑' : '↓' }}</span>
            </th>
            <th>Download</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="artist in artists" :key="artist.id">
            <td>
              <router-link :to="`/artists/${artist.id}`" class="artist-name">
                {{ artist.name }}
              </router-link>
            </td>
            <td>
              <span :class="['badge', artist.status === 'assigned' ? 'success' : 'warning']">
                {{ artist.status }}
              </span>
            </td>
            <td class="text-muted">{{ artist.show_titles || '-' }}</td>
            <td class="text-muted">{{ new Date(artist.created_at).toLocaleDateString() }}</td>
            <td>
              <a :href="`/artists/${artist.id}/download`" class="dl-btn" title="Download Artist Package">📦</a>
            </td>
          </tr>
          <tr v-if="artists.length === 0">
            <td colspan="5" class="text-muted" style="text-align: center; padding: 2rem;">
              No artists found
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
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

.sortable {
  cursor: pointer;
  user-select: none;
}

.sortable:hover {
  color: var(--color-text);
}

.artist-name {
  color: var(--color-primary);
  font-weight: var(--font-weight-medium);
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
  border: 1px solid #bbbbbb;
}

.dl-btn:hover {
  background-color: #bbbbbb;
}
</style>
