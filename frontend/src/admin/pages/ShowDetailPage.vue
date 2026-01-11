<script setup lang="ts">
import { ref, computed, onMounted } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { showsApi, type ShowDetail } from '../api';
import { BaseButton, BaseModal } from '@shared/components';
import AudioPlayer from '../components/AudioPlayer.vue';
import { useFlash } from '../composables/useFlash';

defineOptions({
  name: 'ShowDetailPage'
});

const flash = useFlash();
const route = useRoute();
const router = useRouter();

const show = ref<ShowDetail | null>(null);
const loading = ref(true);
const error = ref<string | null>(null);

// UI state
const showDeleteModal = ref(false);
const deleting = ref(false);
const editingDate = ref(false);
const editingDescription = ref(false);
const saving = ref(false);
const assigning = ref(false);

// Form data
const dateForm = ref('');
const descriptionForm = ref('');
const selectedArtistId = ref<number | null>(null);

// Computed
const hasArtists = computed(() => show.value && show.value.artists.length > 0);
const artistsLeft = computed(() => show.value?.artists_left ?? 0);

async function loadShow() {
  const id = Number(route.params.id);
  loading.value = true;
  error.value = null;

  try {
    show.value = await showsApi.get(id);
    dateForm.value = show.value.date;
    descriptionForm.value = show.value.description || '';
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to load show';
  } finally {
    loading.value = false;
  }
}

// Date editing
function startEditDate() {
  if (show.value) {
    dateForm.value = show.value.date;
  }
  editingDate.value = true;
}

async function saveDate() {
  if (!show.value) return;
  
  saving.value = true;
  try {
    await showsApi.update(show.value.id, { date: dateForm.value });
    flash.success('Date updated');
    editingDate.value = false;
    await loadShow();
  } catch (e) {
    flash.error(e instanceof Error ? e.message : 'Failed to update date');
  } finally {
    saving.value = false;
  }
}

// Description editing
function startEditDescription() {
  if (show.value) {
    descriptionForm.value = show.value.description || '';
  }
  editingDescription.value = true;
}

async function saveDescription() {
  if (!show.value) return;
  
  saving.value = true;
  try {
    await showsApi.update(show.value.id, { description: descriptionForm.value });
    flash.success('Description updated');
    editingDescription.value = false;
    await loadShow();
  } catch (e) {
    flash.error(e instanceof Error ? e.message : 'Failed to update description');
  } finally {
    saving.value = false;
  }
}

// Artist assignment
async function assignArtist() {
  if (!show.value || !selectedArtistId.value) return;

  const artistIdToAssign = selectedArtistId.value;
  
  // Find the artist in available_artists before we remove it
  const selectedAvailableArtist = show.value.available_artists.find(
    a => a.id === artistIdToAssign
  );
  
  if (!selectedAvailableArtist) {
    flash.error('Artist not found in available list');
    return;
  }

  assigning.value = true;
  try {
    const response = await showsApi.assignArtist(show.value.id, artistIdToAssign);
    
    // Use response artist if available, otherwise construct from available_artists
    const newArtist = response?.artist ?? {
      id: selectedAvailableArtist.id,
      name: selectedAvailableArtist.name,
      pronouns: selectedAvailableArtist.pronouns,
      has_pic: false,
      // No audio URLs - they'll load on next page visit
    };
    
    // Update local state surgically
    show.value.artists = [...show.value.artists, newArtist];
    show.value.available_artists = show.value.available_artists.filter(
      a => a.id !== artistIdToAssign
    );
    show.value.artists_left = Math.max(0, show.value.artists_left - 1);
    
    flash.success('Artist assigned to show');
    selectedArtistId.value = null;
  } catch (e) {
    flash.error(e instanceof Error ? e.message : 'Failed to assign artist');
  } finally {
    assigning.value = false;
  }
}

async function unassignArtist(artistId: number) {
  if (!show.value) return;

  try {
    // Find artist before removing for the available_artists update
    const removedArtist = show.value.artists.find(a => a.id === artistId);
    
    await showsApi.unassignArtist(show.value.id, artistId);
    
    // Update local state surgically using spread for reactivity
    show.value.artists = show.value.artists.filter(a => a.id !== artistId);
    show.value.artists_left = Math.min(4, show.value.artists_left + 1);
    
    // Add back to available_artists if we have the info
    if (removedArtist) {
      const newAvailable = {
        id: removedArtist.id,
        name: removedArtist.name,
        pronouns: removedArtist.pronouns
      };
      show.value.available_artists = [...show.value.available_artists, newAvailable].sort(
        (a, b) => a.name.localeCompare(b.name)
      );
    }
    
    flash.success('Artist removed from show');
  } catch (e) {
    flash.error(e instanceof Error ? e.message : 'Failed to remove artist');
  }
}

// Delete show
async function deleteShow() {
  if (!show.value) return;

  deleting.value = true;
  try {
    await showsApi.delete(show.value.id);
    flash.success(`Show "${show.value.title}" deleted`);
    router.push('/shows');
  } catch (e) {
    flash.error(e instanceof Error ? e.message : 'Failed to delete show');
    showDeleteModal.value = false;
  } finally {
    deleting.value = false;
  }
}

function formatDateTime(dateStr: string): string {
  return new Date(dateStr).toLocaleString();
}

onMounted(loadShow);
</script>

<template>
  <div class="show-detail-page">
    <div v-if="loading" class="loading-spinner"></div>

    <div v-else-if="error" class="flash-message error">{{ error }}</div>

    <template v-else-if="show">
      <!-- Header -->
      <div class="page-header">
        <router-link to="/shows" class="back-link">← Back to Shows</router-link>
        <div class="header-content">
          <h1 class="page-title">{{ show.title }}</h1>
        </div>
      </div>

      <!-- Information Section -->
      <div class="card">
        <h2 class="section-title">Information</h2>
        <div class="info-grid">
          <div class="info-label">Status</div>
          <div class="info-value">
            <span :class="['status-badge', show.status]">{{ show.status }}</span>
          </div>
          
          <div class="info-label">Date</div>
          <div class="info-value">{{ show.date }}</div>
        </div>

        <div class="edit-toggle-container">
          <button type="button" class="btn-edit" @click="startEditDate" v-if="!editingDate">
            Edit Date
          </button>
        </div>

        <div v-if="editingDate" class="edit-panel">
          <div class="edit-row">
            <input type="date" v-model="dateForm" class="date-input" />
            <BaseButton variant="primary" size="sm" :loading="saving" @click="saveDate">
              Save Date
            </BaseButton>
            <BaseButton variant="ghost" size="sm" @click="editingDate = false">
              Cancel
            </BaseButton>
          </div>
        </div>
      </div>

      <!-- Assigned Artists Section -->
      <div class="card">
        <h2 class="section-title">Assigned Artists ({{ show.artists.length }})</h2>
        <p class="slots-info">{{ artistsLeft }} slot{{ artistsLeft !== 1 ? 's' : '' }} left (max 4)</p>

        <!-- Assignment Form -->
        <template v-if="artistsLeft > 0 && show.available_artists.length > 0">
          <div class="assign-form">
            <select v-model="selectedArtistId" class="select-input">
              <option :value="null" disabled>-- Select an unassigned artist --</option>
              <option v-for="artist in show.available_artists" :key="artist.id" :value="artist.id">
                {{ artist.name }} ({{ artist.pronouns }})
              </option>
            </select>
            <BaseButton variant="success" :disabled="!selectedArtistId" :loading="assigning" @click="assignArtist">
              Assign
            </BaseButton>
          </div>
        </template>
        <p v-else-if="artistsLeft === 0" class="text-muted assign-note">
          This show already has 4 assigned artists.
        </p>
        <p v-else class="text-muted assign-note">
          No unassigned artists available. All artists are already assigned to shows.
        </p>

        <!-- Artists Table -->
        <div v-if="show.artists.length > 0" class="artists-list">
          <div v-for="artist in show.artists" :key="artist.id" class="artist-card">
            <div class="artist-header">
              <img 
                v-if="artist.pic_url" 
                :src="artist.pic_url" 
                :alt="artist.name" 
                class="artist-thumb"
                crossorigin="anonymous"
              />
              <div v-else class="artist-thumb-placeholder"></div>
              <div class="artist-info">
                <router-link :to="`/artists/${artist.id}`" class="artist-name">
                  {{ artist.name }}
                </router-link>
                <span class="artist-pronouns">{{ artist.pronouns }}</span>
              </div>
              <button class="remove-btn remove-btn-desktop" @click="unassignArtist(artist.id)">
                Remove
              </button>
            </div>
            
            <div class="download-btns">
              <a 
                v-if="artist.voice_url || artist.track1_url || artist.track2_url"
                :href="`/api/artists/${artist.id}/download/audio`" 
                class="tbl-dl-btn audio" 
                title="Download Audio"
              >AUD</a>
              <a 
                v-if="artist.has_pic"
                :href="`/api/artists/${artist.id}/download/images`" 
                class="tbl-dl-btn images" 
                title="Download Images"
              >IMG</a>
              <a 
                :href="`/api/artists/${artist.id}/download/pdf`" 
                class="tbl-dl-btn pdf" 
                title="Download Handout"
              >PDF</a>
              <a 
                :href="`/api/artists/${artist.id}/download`" 
                class="tbl-dl-btn all" 
                title="Download Full Profile"
              >ALL</a>
            </div>
            
            <div class="audio-players">
              <div class="audio-row">
                <span class="audio-label">Voice Memo</span>
                <AudioPlayer v-if="artist.voice_url" :src="artist.voice_url" />
                <span v-else class="audio-placeholder">No voice memo</span>
              </div>
              <div v-if="artist.track1_url" class="audio-row">
                <span class="audio-label">Track 1</span>
                <AudioPlayer :src="artist.track1_url" />
              </div>
              <div v-if="artist.track2_url" class="audio-row">
                <span class="audio-label">Track 2</span>
                <AudioPlayer :src="artist.track2_url" />
              </div>
            </div>
            
            <div class="artist-card-footer">
              <button class="remove-btn remove-btn-mobile" @click="unassignArtist(artist.id)">
                Remove
              </button>
            </div>
          </div>
        </div>
        <p v-else class="empty-state">No artists assigned to this show yet.</p>
      </div>

      <!-- Description Section -->
      <div class="card">
        <h2 class="section-title">Description</h2>
        
        <template v-if="!editingDescription">
          <div v-if="show.description" class="description-view">{{ show.description }}</div>
          <p v-else class="empty-state">No description.</p>
          
          <div class="edit-toggle-container">
            <button type="button" class="btn-edit" @click="startEditDescription">
              Edit Description
            </button>
          </div>
        </template>

        <div v-else class="edit-panel">
          <textarea 
            v-model="descriptionForm" 
            class="text-field" 
            rows="4" 
            placeholder="Brief description..."
          ></textarea>
          <div class="edit-actions">
            <BaseButton variant="ghost" size="sm" @click="editingDescription = false">
              Cancel
            </BaseButton>
            <BaseButton variant="primary" size="sm" :loading="saving" @click="saveDescription">
              Save Description
            </BaseButton>
          </div>
        </div>
      </div>

      <!-- Download Section -->
      <div class="card">
        <h2 class="section-title">Download</h2>
        <template v-if="hasArtists">
          <div class="download-row">
            <a :href="`/shows/${show.id}/download/recording`" class="download-btn recording">
              Recording Package
            </a>
            <a :href="`/shows/${show.id}/download/social-media`" class="download-btn social">
              Social Media Package
            </a>
            <a :href="`/shows/${show.id}/download/all-data`" class="download-btn all">
              All Material Package
            </a>
          </div>
        </template>
        <p v-else class="empty-state">Assign at least one artist to enable downloads.</p>
      </div>

      <!-- Metadata Section -->
      <div class="card">
        <h2 class="section-title">Metadata</h2>
        <div class="info-grid">
          <div class="info-label">Created</div>
          <div class="info-value">{{ formatDateTime(show.created_at) }}</div>

          <div class="info-label">Last Updated</div>
          <div class="info-value">
            <template v-if="show.updated_at">{{ formatDateTime(show.updated_at) }}</template>
            <span v-else class="text-muted">Never</span>
          </div>

          <div class="info-label">Show ID</div>
          <div class="info-value">{{ show.id }}</div>
        </div>
      </div>

      <!-- Danger Zone -->
      <div class="danger-zone">
        <h2 class="danger-zone-title">Danger</h2>
        <div class="danger-note">
          ATTENTION: Deleted shows cannot be restored!
        </div>
        <BaseButton variant="danger" @click="showDeleteModal = true">
          Delete Show
        </BaseButton>
      </div>
    </template>

    <!-- Delete Confirmation Modal -->
    <BaseModal :open="showDeleteModal" title="Delete Show" @close="showDeleteModal = false">
      <p>Really delete this show?</p>
      <p class="text-muted">This will remove all artist assignments from it. This cannot be undone.</p>
      <template #footer>
        <BaseButton variant="ghost" @click="showDeleteModal = false">Cancel</BaseButton>
        <BaseButton variant="danger" :loading="deleting" @click="deleteShow">
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

.header-content {
  display: flex;
  align-items: center;
  gap: var(--spacing-md);
}

.page-title {
  font-size: 2.4em;
  margin: 0;
}

.card {
  margin-bottom: var(--spacing-lg);
}

.section-title {
  font-size: 1.2em;
  margin-bottom: var(--spacing-md);
  padding-bottom: var(--spacing-sm);
  border-bottom: 1px solid var(--color-border);
}

.info-grid {
  display: grid;
  grid-template-columns: 150px 1fr;
  gap: var(--spacing-md);
}

.info-label {
  color: var(--color-text-muted);
  font-weight: 500;
}

.info-value {
  color: var(--color-text);
}

.status-badge {
  display: inline-block;
  padding: 4px 12px;
  border-radius: 20px;
  font-size: 0.9em;
  font-weight: 500;
}

.status-badge.scheduled {
  background: #cecece;
  color: #000;
}

.status-badge.completed {
  background: #48bb78;
  color: #000;
}

.edit-toggle-container {
  margin-top: var(--spacing-md);
}

.btn-edit {
  padding: 8px 12px;
  font-size: 0.9em;
  background: transparent;
  border: 1px solid var(--color-primary);
  border-radius: var(--radius-md);
  color: var(--color-primary);
  cursor: pointer;
  transition: all var(--transition-fast);
  font-family: var(--font-family);
}

.btn-edit:hover {
  background: var(--color-primary);
  color: var(--color-bg);
}

.edit-panel {
  margin-top: var(--spacing-md);
}

.edit-row {
  display: flex;
  gap: var(--spacing-sm);
  align-items: center;
  flex-wrap: wrap;
}

.date-input {
  padding: 8px 10px;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  color: var(--color-text);
  font-size: 1em;
  font-family: var(--font-family);
}

.date-input:focus {
  outline: none;
  border-color: var(--color-primary);
}

.slots-info {
  color: var(--color-text-muted);
  margin: 6px 0 var(--spacing-md);
}

.assign-form {
  display: flex;
  gap: var(--spacing-sm);
  margin-bottom: var(--spacing-md);
}

.assign-note {
  margin-bottom: var(--spacing-md);
}

.select-input {
  flex: 1;
  padding: 8px 10px;
  background: #ffec44;
  border: 1px solid #111;
  border-radius: var(--radius-md);
  color: #111;
  font-size: 1em;
  font-family: var(--font-family);
}

.select-input:focus {
  outline: none;
  border-color: #111;
  box-shadow: 0 0 0 2px rgba(255, 236, 68, 0.35);
}

/* Artists List */
.artists-list {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: var(--spacing-lg);
}

@media (max-width: 768px) {
  .artists-list {
    grid-template-columns: 1fr;
    gap: var(--spacing-md);
  }

  .artist-card {
    padding: var(--spacing-sm);
  }

  .artist-header {
    gap: var(--spacing-sm);
    margin-bottom: var(--spacing-sm);
    padding-bottom: var(--spacing-sm);
    border-bottom: 1px solid var(--color-border);
  }

  .artist-thumb,
  .artist-thumb-placeholder {
    width: 40px;
    height: 40px;
  }

  .artist-name {
    font-size: 0.95em;
  }

  .download-btns {
    margin-bottom: var(--spacing-sm);
    padding-bottom: var(--spacing-sm);
    border-bottom: 1px solid var(--color-border);
    display: flex;
    gap: var(--spacing-xs);
  }

  .download-btns .tbl-dl-btn {
    flex: 1;
    text-align: center;
  }

  .audio-players {
    padding-top: var(--spacing-sm);
    border-top: none;
  }

  .audio-row {
    grid-template-columns: 70px 1fr;
    gap: var(--spacing-sm);
  }

  .audio-label {
    font-size: 0.8em;
  }

  .artist-card-footer {
    margin-top: var(--spacing-sm);
    padding-top: var(--spacing-sm);
    border-top: 1px solid var(--color-border);
  }

  .artist-card-footer .remove-btn {
    width: 100%;
    margin-top: var(--spacing-xs);
  }

  .remove-btn-desktop {
    display: none;
  }

  .remove-btn-mobile {
    display: inline-block;
  }

  .artist-card-footer {
    display: block;
  }
}

.artist-card {
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  padding: var(--spacing-md);
}

.artist-header {
  display: flex;
  align-items: center;
  gap: var(--spacing-md);
  margin-bottom: var(--spacing-sm);
}

.artist-thumb {
  width: 48px;
  height: 48px;
  border-radius: var(--radius-md);
  object-fit: cover;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  flex: 0 0 auto;
}

.artist-thumb-placeholder {
  width: 48px;
  height: 48px;
  border-radius: var(--radius-md);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  flex: 0 0 auto;
}

.artist-info {
  display: flex;
  flex-direction: column;
  gap: 2px;
  flex: 1;
  min-width: 0;
}

.artist-name {
  color: var(--color-primary);
  text-decoration: none;
  font-weight: 500;
  font-size: 1.05em;
}

.artist-name:hover {
  text-decoration: underline;
}

.artist-pronouns {
  color: var(--color-text-muted);
  font-size: 0.85em;
}

.audio-players {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-sm);
  border-top: 1px solid var(--color-border);
  padding-top: var(--spacing-md);
}

.audio-row {
  display: grid;
  grid-template-columns: 80px 1fr;
  gap: var(--spacing-md);
  align-items: center;
}

.audio-label {
  font-size: 0.85em;
  color: var(--color-text-muted);
  font-weight: 500;
}

.audio-placeholder {
  color: var(--color-text-muted);
  font-size: 0.9em;
}

.no-audio {
  color: var(--color-text-muted);
  font-size: 0.9em;
  margin: 0;
}

.download-btns {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
  margin-bottom: var(--spacing-sm);
}

.tbl-dl-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 4px 8px;
  border-radius: var(--radius-sm);
  background: transparent;
  border: 1px solid;
  text-decoration: none;
  font-size: 0.75em;
  font-weight: 600;
  transition: all var(--transition-fast);
}

.tbl-dl-btn.audio {
  border-color: #9b59b6;
  color: #9b59b6;
}

.tbl-dl-btn.audio:hover {
  background: #9b59b6;
  color: #000;
}

.tbl-dl-btn.images {
  border-color: #3498db;
  color: #3498db;
}

.tbl-dl-btn.images:hover {
  background: #3498db;
  color: #000;
}

.tbl-dl-btn.pdf {
  border-color: #e74c3c;
  color: #e74c3c;
}

.tbl-dl-btn.pdf:hover {
  background: #e74c3c;
  color: #000;
}

.tbl-dl-btn.all {
  border-color: #95a5a6;
  color: #95a5a6;
}

.tbl-dl-btn.all:hover {
  background: #95a5a6;
  color: #000;
}

.remove-btn {
  padding: 6px 12px;
  background: transparent;
  border: 1px solid #fc8181;
  border-radius: var(--radius-md);
  color: #fc8181;
  cursor: pointer;
  font-size: 0.85em;
  font-family: var(--font-family);
  transition: all var(--transition-fast);
}

.remove-btn:hover {
  background: #fc8181;
  color: #000;
}

/* Desktop: show header button, hide footer */
.remove-btn-desktop {
  display: inline-block;
}

.remove-btn-mobile {
  display: none;
}

.artist-card-footer {
  display: none;
}

.empty-state {
  color: var(--color-text-muted);
  font-style: italic;
  padding: var(--spacing-md) 0;
}

.description-view {
  color: var(--color-text);
  line-height: 1.5;
  white-space: pre-wrap;
}

.text-field {
  width: 100%;
  padding: 10px;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  color: var(--color-text);
  font-size: 1em;
  font-family: var(--font-family);
  line-height: 1.4;
  resize: vertical;
}

.text-field:focus {
  outline: none;
  border-color: var(--color-primary);
}

.edit-actions {
  display: flex;
  gap: var(--spacing-sm);
  justify-content: flex-end;
  margin-top: var(--spacing-sm);
}

.download-row {
  display: flex;
  flex-wrap: wrap;
  gap: var(--spacing-md);
}

.download-btn {
  display: inline-block;
  padding: var(--spacing-sm) var(--spacing-lg);
  border: 1px solid;
  border-radius: var(--radius-md);
  font-family: var(--font-family);
  font-size: var(--font-size-md);
  font-weight: var(--font-weight-medium);
  text-decoration: none;
  transition: all var(--transition-fast);
}

.download-btn.recording {
  border-color: #00ff04;
  color: #00ff04;
}

.download-btn.recording:hover {
  background-color: #00ff04;
  color: var(--color-bg);
}

.download-btn.social {
  border-color: #ff00aa;
  color: #ff00aa;
}

.download-btn.social:hover {
  background-color: #ff00aa;
  color: var(--color-bg);
}

.download-btn.all {
  border-color: #bbbbbb;
  color: #bbbbbb;
}

.download-btn.all:hover {
  background-color: #bbbbbb;
  color: var(--color-bg);
}

.danger-zone {
  background: var(--color-card-bg);
  padding: var(--spacing-lg);
  border-radius: var(--radius-md);
  margin-top: var(--spacing-lg);
}

.danger-zone-title {
  font-size: 1.2em;
  margin-bottom: var(--spacing-md);
  padding-bottom: var(--spacing-sm);
  border-bottom: 1px solid var(--color-border);
}

.danger-note {
  padding: 12px 14px;
  border-radius: var(--radius-md);
  border: 1px solid #fc8181;
  color: #000;
  background: #fc8181;
  margin-bottom: var(--spacing-md);
}

.text-muted {
  color: var(--color-text-muted);
}
</style>
