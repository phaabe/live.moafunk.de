<script setup lang="ts">
import { ref, onMounted } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { showsApi, type Show } from '../api';
import { BaseButton, FormInput } from '@shared/components';

const route = useRoute();
const router = useRouter();

const show = ref<Show | null>(null);
const loading = ref(true);
const saving = ref(false);
const error = ref<string | null>(null);

const editForm = ref({
  title: '',
  date: '',
  description: '',
});

async function loadShow() {
  const id = Number(route.params.id);
  loading.value = true;
  error.value = null;

  try {
    show.value = await showsApi.get(id);
    editForm.value = {
      title: show.value.title,
      date: show.value.date,
      description: show.value.description || '',
    };
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to load show';
  } finally {
    loading.value = false;
  }
}

async function saveShow() {
  if (!show.value) return;

  saving.value = true;
  error.value = null;

  try {
    await showsApi.update(show.value.id, editForm.value);
    router.push('/shows');
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to save show';
  } finally {
    saving.value = false;
  }
}

onMounted(loadShow);
</script>

<template>
  <div class="show-detail-page">
    <div v-if="loading" class="loading-spinner"></div>

    <div v-else-if="error" class="flash-message error">{{ error }}</div>

    <template v-else-if="show">
      <div class="page-header">
        <div>
          <router-link to="/shows" class="back-link">← Back to Shows</router-link>
          <h1 class="page-title">Edit: {{ show.title }}</h1>
        </div>
      </div>

      <div class="card">
        <form class="edit-form" @submit.prevent="saveShow">
          <FormInput v-model="editForm.title" label="Title" required />
          <FormInput v-model="editForm.date" label="Date" placeholder="YYYY-MM-DD" required />
          <FormInput v-model="editForm.description" label="Description" />

          <div class="form-actions">
            <BaseButton type="button" variant="ghost" @click="router.push('/shows')">
              Cancel
            </BaseButton>
            <BaseButton type="submit" variant="primary" :loading="saving">
              Save Changes
            </BaseButton>
          </div>
        </form>
      </div>

      <div class="card" style="margin-top: var(--spacing-lg);">
        <h2 class="section-title">Assigned Artists</h2>
        <ul v-if="show.artists.length > 0" class="artist-list">
          <li v-for="artist in show.artists" :key="artist.id">
            <router-link :to="`/artists/${artist.id}`">{{ artist.name }}</router-link>
          </li>
        </ul>
        <p v-else class="text-muted">No artists assigned to this show</p>
      </div>
    </template>
  </div>
</template>

<style scoped>
.back-link {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
  display: inline-block;
  margin-bottom: var(--spacing-sm);
}

.edit-form {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
  max-width: 600px;
}

.form-actions {
  display: flex;
  gap: var(--spacing-md);
  justify-content: flex-end;
  margin-top: var(--spacing-md);
}

.section-title {
  font-size: var(--font-size-lg);
  margin-bottom: var(--spacing-md);
  padding-bottom: var(--spacing-sm);
  border-bottom: 1px solid var(--color-border);
}

.artist-list {
  list-style: none;
  padding: 0;
  margin: 0;
}

.artist-list li {
  padding: var(--spacing-sm) 0;
  border-bottom: 1px solid var(--color-border);
}

.artist-list li:last-child {
  border-bottom: none;
}
</style>
