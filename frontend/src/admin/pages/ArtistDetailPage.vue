<script setup lang="ts">
import { ref, onMounted } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { artistsApi, type ArtistDetail } from '../api';
import { BaseButton, BaseModal } from '@shared/components';

const route = useRoute();
const router = useRouter();

const artist = ref<ArtistDetail | null>(null);
const loading = ref(true);
const error = ref<string | null>(null);
const showDeleteModal = ref(false);
const deleting = ref(false);

async function loadArtist() {
  const id = Number(route.params.id);
  loading.value = true;
  error.value = null;

  try {
    artist.value = await artistsApi.get(id);
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to load artist';
  } finally {
    loading.value = false;
  }
}

async function deleteArtist() {
  if (!artist.value) return;

  deleting.value = true;
  try {
    await artistsApi.delete(artist.value.id);
    router.push('/artists');
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to delete artist';
    showDeleteModal.value = false;
  } finally {
    deleting.value = false;
  }
}

onMounted(loadArtist);
</script>

<template>
  <div class="artist-detail-page">
    <div v-if="loading" class="loading-spinner"></div>

    <div v-else-if="error" class="flash-message error">{{ error }}</div>

    <template v-else-if="artist">
      <div class="page-header">
        <div>
          <router-link to="/artists" class="back-link">← Back to Artists</router-link>
          <h1 class="page-title">{{ artist.name }}</h1>
        </div>
        <div class="actions">
          <BaseButton variant="danger" size="sm" @click="showDeleteModal = true">
            Delete
          </BaseButton>
        </div>
      </div>

      <div class="content-grid">
        <div class="card">
          <h2 class="section-title">Details</h2>
          <dl class="detail-list">
            <dt>Status</dt>
            <dd>
              <span :class="['badge', artist.status === 'assigned' ? 'success' : 'warning']">
                {{ artist.status }}
              </span>
            </dd>
            <dt>Bio</dt>
            <dd>{{ artist.bio || 'No bio provided' }}</dd>
            <dt>SoundCloud</dt>
            <dd>
              <a v-if="artist.soundcloud_url" :href="artist.soundcloud_url" target="_blank">
                {{ artist.soundcloud_url }}
              </a>
              <span v-else class="text-muted">Not provided</span>
            </dd>
            <dt>Instagram</dt>
            <dd>
              <a v-if="artist.instagram_url" :href="artist.instagram_url" target="_blank">
                {{ artist.instagram_url }}
              </a>
              <span v-else class="text-muted">Not provided</span>
            </dd>
            <dt>Submitted</dt>
            <dd>{{ new Date(artist.created_at).toLocaleString() }}</dd>
          </dl>
        </div>

        <div class="card">
          <h2 class="section-title">Assigned Shows</h2>
          <ul v-if="artist.shows.length > 0" class="show-list">
            <li v-for="show in artist.shows" :key="show.id">
              <router-link :to="`/shows/${show.id}`">{{ show.title }}</router-link>
            </li>
          </ul>
          <p v-else class="text-muted">No shows assigned</p>
        </div>
      </div>
    </template>

    <BaseModal :open="showDeleteModal" title="Delete Artist" @close="showDeleteModal = false">
      <p>Are you sure you want to delete <strong>{{ artist?.name }}</strong>?</p>
      <p class="text-muted">This action cannot be undone.</p>
      <template #footer>
        <BaseButton variant="ghost" @click="showDeleteModal = false">Cancel</BaseButton>
        <BaseButton variant="danger" :loading="deleting" @click="deleteArtist">
          Delete
        </BaseButton>
      </template>
    </BaseModal>
  </div>
</template>

<style scoped>
.back-link {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
  display: inline-block;
  margin-bottom: var(--spacing-sm);
}

.content-grid {
  display: grid;
  grid-template-columns: 2fr 1fr;
  gap: var(--spacing-lg);
}

.section-title {
  font-size: var(--font-size-lg);
  margin-bottom: var(--spacing-md);
  padding-bottom: var(--spacing-sm);
  border-bottom: 1px solid var(--color-border);
}

.detail-list {
  display: grid;
  grid-template-columns: 120px 1fr;
  gap: var(--spacing-sm);
}

.detail-list dt {
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
}

.detail-list dd {
  margin: 0;
}

.show-list {
  list-style: none;
  padding: 0;
  margin: 0;
}

.show-list li {
  padding: var(--spacing-sm) 0;
  border-bottom: 1px solid var(--color-border);
}

.show-list li:last-child {
  border-bottom: none;
}

@media (max-width: 768px) {
  .content-grid {
    grid-template-columns: 1fr;
  }
}
</style>
