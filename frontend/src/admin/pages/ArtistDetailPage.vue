<script setup lang="ts">
import { ref, onMounted } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { artistsApi, type ArtistDetail } from '../api';
import { BaseButton, BaseModal } from '@shared/components';
import ImageCropper from '../components/ImageCropper.vue';
import AudioPlayer from '../components/AudioPlayer.vue';
import { useFlash } from '../composables/useFlash';

defineOptions({
  name: 'ArtistDetailPage'
});

const flash = useFlash();
const route = useRoute();
const router = useRouter();

const artist = ref<ArtistDetail | null>(null);
const loading = ref(true);
const error = ref<string | null>(null);
const showDeleteModal = ref(false);
const deleting = ref(false);
const assigning = ref(false);
const selectedShowId = ref<number | null>(null);

// Edit modes
const editingPicture = ref(false);
const editingDetails = ref(false);
const editingAudio = ref(false);
const saving = ref(false);

// Edit form data
const detailsForm = ref({
  mentions: '',
  upcoming_events: '',
  music_description: '',
  soundcloud: '',
  instagram: '',
  bandcamp: '',
  spotify: '',
  other_social: '',
});

const generatingBio = ref(false);
const generatingCaption = ref(false);
const showInstagramModal = ref(false);
const postingToInstagram = ref(false);
const showInstagramConfirmModal = ref(false);
const editingCaption = ref(false);
const editedCaption = ref('');
const savingCaption = ref(false);
const carouselIndex = ref(0);
const regeneratingVideos = ref(false);

const audioForm = ref({
  voice: null as File | null,
  track1: null as File | null,
  track2: null as File | null,
  track1_name: '',
  track2_name: '',
});

async function loadArtist() {
  const id = Number(route.params.id);
  loading.value = true;
  error.value = null;

  try {
    artist.value = await artistsApi.get(id);
    // Initialize form data from artist
    if (artist.value) {
      detailsForm.value = {
        mentions: artist.value.mentions || '',
        upcoming_events: artist.value.upcoming_events || '',
        music_description: artist.value.music_description || '',
        soundcloud: artist.value.soundcloud || '',
        instagram: artist.value.instagram || '',
        bandcamp: artist.value.bandcamp || '',
        spotify: artist.value.spotify || '',
        other_social: artist.value.other_social || '',
      };
      audioForm.value.track1_name = artist.value.track1_name || '';
      audioForm.value.track2_name = artist.value.track2_name || '';
    }
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
    flash.success(`Artist "${artist.value.name}" deleted`);
    router.push('/artists');
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to delete artist';
    showDeleteModal.value = false;
  } finally {
    deleting.value = false;
  }
}

async function assignShow() {
  if (!artist.value || !selectedShowId.value) return;

  assigning.value = true;
  try {
    await artistsApi.assignShow(artist.value.id, selectedShowId.value);
    flash.success('Artist assigned to show');
    selectedShowId.value = null;
    await loadArtist();
  } catch (e) {
    flash.error(e instanceof Error ? e.message : 'Failed to assign show');
  } finally {
    assigning.value = false;
  }
}

async function unassignShow(showId: number) {
  if (!artist.value) return;

  try {
    await artistsApi.unassignShow(artist.value.id, showId);
    flash.success('Artist unassigned from show');
    await loadArtist();
  } catch (e) {
    flash.error(e instanceof Error ? e.message : 'Failed to unassign show');
  }
}

function formatDate(dateStr: string): string {
  return new Date(dateStr).toLocaleDateString();
}

// Picture editing
function startEditPicture() {
  editingPicture.value = true;
}

function cancelEditPicture() {
  editingPicture.value = false;
}

async function savePicture(data: { original: File; cropped: Blob; branded: Blob }) {
  if (!artist.value) return;

  saving.value = true;
  try {
    await artistsApi.updatePicture(artist.value.id, data);
    flash.success('Profile picture updated');
    editingPicture.value = false;
    await loadArtist();
  } catch (e) {
    flash.error(e instanceof Error ? e.message : 'Failed to update picture');
  } finally {
    saving.value = false;
  }
}

// Details editing
function startEditDetails() {
  if (artist.value) {
    detailsForm.value = {
      mentions: artist.value.mentions || '',
      upcoming_events: artist.value.upcoming_events || '',
      music_description: artist.value.music_description || '',
      soundcloud: artist.value.soundcloud || '',
      instagram: artist.value.instagram || '',
      bandcamp: artist.value.bandcamp || '',
      spotify: artist.value.spotify || '',
      other_social: artist.value.other_social || '',
    };
  }
  editingDetails.value = true;
}

function cancelEditDetails() {
  editingDetails.value = false;
}

async function saveDetails() {
  if (!artist.value) return;

  saving.value = true;
  try {
    await artistsApi.updateDetails(artist.value.id, detailsForm.value);
    flash.success('Details updated');
    editingDetails.value = false;
    await loadArtist();
  } catch (e) {
    flash.error(e instanceof Error ? e.message : 'Failed to update details');
  } finally {
    saving.value = false;
  }
}

// AI Bio generation
async function generateBio() {
  if (!artist.value) return;
  generatingBio.value = true;
  try {
    const result = await artistsApi.generateBio(artist.value.id);
    if (result.ai_bio && artist.value) {
      artist.value.ai_bio = result.ai_bio;
    }
    flash.success('AI bio generated successfully');
  } catch (e) {
    flash.error(e instanceof Error ? e.message : 'Failed to generate bio');
  } finally {
    generatingBio.value = false;
  }
}

// Instagram Caption generation
async function generateInstagramCaption() {
  if (!artist.value) return;
  generatingCaption.value = true;
  try {
    const result = await artistsApi.generateInstagramCaption(artist.value.id);
    if (result.instagram_caption && artist.value) {
      artist.value.instagram_caption = result.instagram_caption;
      // Also update ai_bio if it was generated as a side effect
      await loadArtist();
    }
    flash.success('Instagram caption generated successfully');
  } catch (e) {
    flash.error(e instanceof Error ? e.message : 'Failed to generate caption');
  } finally {
    generatingCaption.value = false;
  }
}

async function openInstagramPreview() {
  showInstagramModal.value = true;
  carouselIndex.value = 0;
  // Auto-generate caption if none exists
  if (!artist.value?.instagram_caption) {
    await generateInstagramCaption();
  }
}

function startEditCaption() {
  editedCaption.value = artist.value?.instagram_caption || '';
  editingCaption.value = true;
}

function cancelEditCaption() {
  editingCaption.value = false;
}

async function confirmEditCaption() {
  if (!artist.value) return;
  savingCaption.value = true;
  try {
    const result = await artistsApi.updateInstagramCaption(artist.value.id, editedCaption.value);
    if (result.instagram_caption !== undefined) {
      artist.value.instagram_caption = result.instagram_caption;
    }
    editingCaption.value = false;
    flash.success('Caption updated');
  } catch (e) {
    flash.error(e instanceof Error ? e.message : 'Failed to save caption');
  } finally {
    savingCaption.value = false;
  }
}

async function postToInstagram(force = false) {
  if (!artist.value) return;

  // If already posted and not forcing, show confirmation modal
  if (artist.value.instagram_posted_at && !force) {
    showInstagramConfirmModal.value = true;
    return;
  }

  postingToInstagram.value = true;
  showInstagramConfirmModal.value = false;

  try {
    const result = await artistsApi.postToInstagram(artist.value.id, force);

    if (result.success) {
      flash.success('Posted to Instagram successfully!');
      showInstagramModal.value = false;
      await loadArtist();
    } else {
      flash.error(result.error || 'Failed to post to Instagram');
    }
  } catch (e) {
    flash.error(e instanceof Error ? e.message : 'Failed to post to Instagram');
  } finally {
    postingToInstagram.value = false;
  }
}

async function regenerateVideos() {
  if (!artist.value) return;
  regeneratingVideos.value = true;
  try {
    await artistsApi.generateVideos(artist.value.id);
    flash.success('Videos regenerated successfully');
    await loadArtist();
  } catch (e) {
    flash.error(e instanceof Error ? e.message : 'Failed to regenerate videos');
  } finally {
    regeneratingVideos.value = false;
  }
}

// Audio editing
function startEditAudio() {
  if (artist.value) {
    audioForm.value = {
      voice: null,
      track1: null,
      track2: null,
      track1_name: artist.value.track1_name || '',
      track2_name: artist.value.track2_name || '',
    };
  }
  editingAudio.value = true;
}

function cancelEditAudio() {
  editingAudio.value = false;
  audioForm.value = {
    voice: null,
    track1: null,
    track2: null,
    track1_name: artist.value?.track1_name || '',
    track2_name: artist.value?.track2_name || '',
  };
}

function onAudioFileChange(field: 'voice' | 'track1' | 'track2', event: Event) {
  const target = event.target as HTMLInputElement;
  const file = target.files?.[0];
  if (file) {
    audioForm.value[field] = file;
  }
}

async function saveAudio() {
  if (!artist.value) return;

  saving.value = true;
  try {
    await artistsApi.updateAudio(artist.value.id, {
      voice: audioForm.value.voice || undefined,
      track1: audioForm.value.track1 || undefined,
      track2: audioForm.value.track2 || undefined,
      track1_name: audioForm.value.track1_name || undefined,
      track2_name: audioForm.value.track2_name || undefined,
    });
    flash.success('Audio files updated');
    editingAudio.value = false;
    await loadArtist();
  } catch (e) {
    flash.error(e instanceof Error ? e.message : 'Failed to update audio');
  } finally {
    saving.value = false;
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
        <div class="header-title-row">
          <router-link to="/artists" class="back-link">← Back to Artists</router-link>
          <div class="title-status-row">
            <h1 class="page-title">{{ artist.name }}</h1>
            <span v-if="artist.pronouns" class="pronouns">({{ artist.pronouns }})</span>
            <span :class="['badge', artist.status === 'assigned' ? 'success' : 'warning']">
              {{ artist.status }}
            </span>
          </div>
          <span class="submitted-date">Submitted: {{ new Date(artist.created_at).toLocaleString() }}</span>
        </div>
      </div>

      <!-- Top Row: Assign to Show + Details (left) + Profile Picture (right) -->
      <div class="top-row-grid">
        <!-- Left Column: Show Assignment + Details -->
        <div class="left-column">
          <!-- Assign to Show -->
          <div class="card assign-card">
            <h2 class="section-title">Show Assignment</h2>

            <!-- Already assigned - show current assignment -->
            <template v-if="artist.shows.length > 0">
              <div class="current-assignment">
                <h3 class="subsection-title">📅 Assigned Show</h3>
                <div class="assigned-show-item">
                  <div class="show-info">
                    <router-link :to="`/shows/${artist.shows[0].id}`" class="show-title-link">
                      {{ artist.shows[0].title }}
                    </router-link>
                    <span class="show-date">{{ formatDate(artist.shows[0].date) }}</span>
                  </div>
                  <button class="unassign-btn" @click="unassignShow(artist.shows[0].id)" title="Unassign from show">
                    ✕ Unassign
                  </button>
                </div>
              </div>
            </template>

            <!-- Not assigned - show assignment form -->
            <template v-else>
              <div v-if="artist.available_shows.length > 0" class="assign-form">
                <select v-model="selectedShowId" class="select-input">
                  <option :value="null" disabled>Select a show...</option>
                  <option v-for="show in artist.available_shows" :key="show.id" :value="show.id">
                    {{ show.title }} ({{ formatDate(show.date) }}) - {{ show.artists_left }} slot(s) left
                  </option>
                </select>
                <BaseButton variant="primary" :disabled="!selectedShowId" :loading="assigning" @click="assignShow">
                  Assign
                </BaseButton>
              </div>
              <p v-else class="text-muted">No available shows for assignment</p>
            </template>
          </div>

          <!-- Details Section -->
          <div class="card details-card">
            <div class="card-header">
              <h2 class="section-title">Details</h2>
              <button v-if="!editingDetails" class="edit-btn" @click="startEditDetails" title="Edit">edit</button>
            </div>

            <!-- Edit Mode -->
            <template v-if="editingDetails">
              <div class="edit-form">
                <div class="form-group">
                  <label class="form-label">💬 Mentions</label>
                  <textarea v-model="detailsForm.mentions" class="form-textarea" rows="3"></textarea>
                </div>

                <div class="form-group">
                  <label class="form-label">🎤 Upcoming Events</label>
                  <textarea v-model="detailsForm.upcoming_events" class="form-textarea" rows="3"></textarea>
                </div>

                <div class="form-group">
                  <label class="form-label">🎵 Music Description</label>
                  <textarea v-model="detailsForm.music_description" class="form-textarea" rows="3"
                    placeholder="How does the artist describe their music?"></textarea>
                </div>

                <h3 class="subsection-title">🔗 Social Links</h3>

                <div class="form-group">
                  <label class="form-label">SoundCloud</label>
                  <input v-model="detailsForm.soundcloud" type="url" class="form-input"
                    placeholder="https://soundcloud.com/..." />
                </div>

                <div class="form-group">
                  <label class="form-label">Instagram</label>
                  <input v-model="detailsForm.instagram" type="url" class="form-input"
                    placeholder="https://instagram.com/..." />
                </div>

                <div class="form-group">
                  <label class="form-label">Bandcamp</label>
                  <input v-model="detailsForm.bandcamp" type="url" class="form-input"
                    placeholder="https://bandcamp.com/..." />
                </div>

                <div class="form-group">
                  <label class="form-label">Spotify</label>
                  <input v-model="detailsForm.spotify" type="url" class="form-input"
                    placeholder="https://open.spotify.com/..." />
                </div>

                <div class="form-group">
                  <label class="form-label">Other</label>
                  <input v-model="detailsForm.other_social" type="text" class="form-input" />
                </div>

                <div class="edit-actions">
                  <BaseButton variant="ghost" size="sm" @click="cancelEditDetails">Cancel</BaseButton>
                  <BaseButton variant="primary" size="sm" :loading="saving" @click="saveDetails">
                    Save
                  </BaseButton>
                </div>
              </div>
            </template>

            <!-- View Mode -->
            <template v-else>
              <!-- Mentions -->
              <template v-if="artist.mentions">
                <h3 class="subsection-title">💬 Mentions</h3>
                <p class="mentions-text">{{ artist.mentions }}</p>
              </template>

              <!-- Upcoming Events (from artist submission form) -->
              <div class="details-section">
                <h3 class="subsection-title">🎤 Upcoming Events</h3>
                <p v-if="artist.upcoming_events" class="upcoming-events-text">{{ artist.upcoming_events }}</p>
                <p v-else class="text-muted">No upcoming events listed</p>
              </div>

              <!-- Music Description -->
              <div class="details-section">
                <h3 class="subsection-title">🎵 Music Description</h3>
                <p v-if="artist.music_description" class="mentions-text">{{ artist.music_description }}</p>
                <p v-else class="text-muted">No music description provided</p>
              </div>

              <!-- AI-Generated Bio -->
              <div class="details-section">
                <h3 class="subsection-title">🤖 AI Bio (for Instagram)</h3>
                <div v-if="artist.ai_bio" class="ai-bio-card">
                  <p class="ai-bio-text">{{ artist.ai_bio }}</p>
                </div>
                <p v-else class="text-muted">No AI bio generated yet</p>
                <button class="generate-bio-btn" :disabled="generatingBio || !artist.music_description"
                  @click="generateBio">
                  <template v-if="generatingBio">Generating...</template>
                  <template v-else-if="artist.ai_bio">🔄 Regenerate Bio</template>
                  <template v-else>✨ Generate Bio</template>
                </button>
              </div>

              <!-- Instagram -->
              <div class="details-section">
                <h3 class="subsection-title">📸 Instagram</h3>
                <button class="generate-bio-btn instagram-preview-btn"
                  :disabled="!artist.music_description || artist.shows.length === 0" @click="openInstagramPreview">
                  📸 Instagram Preview
                </button>
                <span v-if="artist.instagram_posted_at" class="instagram-posted-badge">
                  ✅ Posted {{ new Date(artist.instagram_posted_at).toLocaleString() }}
                </span>
                <p v-if="artist.shows.length === 0" class="text-muted"
                  style="font-size: var(--font-size-sm); margin-top: var(--spacing-xs);">
                  Assign artist to a show first
                </p>
              </div>

              <!-- Social Links -->
              <h3 class="subsection-title">🔗 Social Links</h3>
              <dl class="detail-list">
                <dt>SoundCloud</dt>
                <dd>
                  <a v-if="artist.soundcloud" :href="artist.soundcloud" target="_blank" class="social-link">
                    {{ artist.soundcloud }}
                  </a>
                  <span v-else class="text-muted">Not provided</span>
                </dd>
                <dt>Instagram</dt>
                <dd>
                  <a v-if="artist.instagram" :href="artist.instagram" target="_blank" class="social-link">
                    {{ artist.instagram }}
                  </a>
                  <span v-else class="text-muted">Not provided</span>
                </dd>
                <dt>Bandcamp</dt>
                <dd>
                  <a v-if="artist.bandcamp" :href="artist.bandcamp" target="_blank" class="social-link">
                    {{ artist.bandcamp }}
                  </a>
                  <span v-else class="text-muted">Not provided</span>
                </dd>
                <dt>Spotify</dt>
                <dd>
                  <a v-if="artist.spotify" :href="artist.spotify" target="_blank" class="social-link">
                    {{ artist.spotify }}
                  </a>
                  <span v-else class="text-muted">Not provided</span>
                </dd>
                <dt>Other</dt>
                <dd>
                  <span v-if="artist.other_social">{{ artist.other_social }}</span>
                  <span v-else class="text-muted">Not provided</span>
                </dd>
              </dl>
            </template>
          </div>
        </div>

        <!-- Profile Picture -->
        <div class="card profile-picture-card">
          <div class="card-header">
            <h2 class="section-title">Profile Picture</h2>
            <button v-if="!editingPicture" class="edit-btn" @click="startEditPicture" title="Edit">edit</button>
          </div>

          <!-- Edit Mode -->
          <template v-if="editingPicture">
            <ImageCropper :artist-name="artist.name" @cancel="cancelEditPicture" @save="savePicture" />
          </template>

          <!-- View Mode -->
          <template v-else>
            <div class="profile-picture-container">
              <img v-if="artist.file_urls.pic" :src="artist.file_urls.pic" alt="Artist picture" class="profile-picture"
                crossorigin="anonymous" />
              <p v-else class="text-muted">No picture uploaded</p>
            </div>
          </template>
        </div>
      </div>

      <!-- Audio Files -->
      <div class="card audio-files-card">
        <div class="card-header">
          <h2 class="section-title">Audio Files</h2>
          <button v-if="!editingAudio" class="edit-btn" @click="startEditAudio" title="Edit">edit</button>
        </div>

        <!-- Edit Mode -->
        <template v-if="editingAudio">
          <div class="edit-form">
            <div class="form-group">
              <label class="form-label">Voice Message</label>
              <input type="file" accept="audio/*" @change="(e) => onAudioFileChange('voice', e)" class="file-input" />
              <span v-if="audioForm.voice" class="file-selected">{{ audioForm.voice.name }}</span>
              <span v-else-if="artist.file_urls.voice" class="text-muted">Current file will be kept</span>
            </div>

            <div class="form-group">
              <label class="form-label">Track 1 Name</label>
              <input v-model="audioForm.track1_name" type="text" class="form-input" placeholder="Track name" />
            </div>

            <div class="form-group">
              <label class="form-label">Track 1 File</label>
              <input type="file" accept="audio/*" @change="(e) => onAudioFileChange('track1', e)" class="file-input" />
              <span v-if="audioForm.track1" class="file-selected">{{ audioForm.track1.name }}</span>
              <span v-else-if="artist.file_urls.track1" class="text-muted">Current file will be kept</span>
            </div>

            <div class="form-group">
              <label class="form-label">Track 2 Name</label>
              <input v-model="audioForm.track2_name" type="text" class="form-input" placeholder="Track name" />
            </div>

            <div class="form-group">
              <label class="form-label">Track 2 File</label>
              <input type="file" accept="audio/*" @change="(e) => onAudioFileChange('track2', e)" class="file-input" />
              <span v-if="audioForm.track2" class="file-selected">{{ audioForm.track2.name }}</span>
              <span v-else-if="artist.file_urls.track2" class="text-muted">Current file will be kept</span>
            </div>

            <div class="edit-actions">
              <BaseButton variant="ghost" size="sm" @click="cancelEditAudio">Cancel</BaseButton>
              <BaseButton variant="primary" size="sm" :loading="saving" @click="saveAudio">
                Save
              </BaseButton>
            </div>
          </div>
        </template>

        <!-- View Mode -->
        <template v-else>
          <div class="file-list">
            <div v-if="artist.file_urls.voice" class="file-item">
              <span class="file-label">Voice Message</span>
              <AudioPlayer :src="artist.file_urls.voice" />
            </div>

            <div v-if="artist.file_urls.track1" class="file-item">
              <span class="file-label">{{ artist.track1_name || 'Track 1' }}</span>
              <AudioPlayer :src="artist.file_urls.track1" />
            </div>

            <div v-if="artist.file_urls.track2" class="file-item">
              <span class="file-label">{{ artist.track2_name || 'Track 2' }}</span>
              <AudioPlayer :src="artist.file_urls.track2" />
            </div>

            <p v-if="!artist.file_urls.voice && !artist.file_urls.track1 && !artist.file_urls.track2"
              class="text-muted">
              No audio files uploaded
            </p>
          </div>
        </template>
      </div>

      <!-- Downloads -->
      <div class="card downloads-section">
        <h2 class="section-title">📥 Downloads</h2>
        <div class="download-buttons-grid">
          <a v-if="artist.file_urls.voice || artist.file_urls.track1 || artist.file_urls.track2"
            :href="`/artists/${artist.id}/download/audio`" class="dl-btn audio">
            Download Audio
          </a>
          <a v-if="artist.file_urls.pic" :href="`/artists/${artist.id}/download/images`" class="dl-btn images">
            Download Images
          </a>
          <a :href="`/artists/${artist.id}/download/pdf`" class="dl-btn pdf">
            Download Handout
          </a>
          <a :href="`/artists/${artist.id}/download`" class="dl-btn all">
            Download Full Profile
          </a>
        </div>
      </div>

      <!-- Danger Zone -->
      <div class="danger-zone">
        <h2 class="danger-zone-title">⚠️ Danger Zone</h2>
        <div class="danger-zone-content">
          <div class="danger-zone-info">
            <p class="danger-zone-description">
              <strong>Delete this artist</strong><br>
              Once you delete an artist, there is no going back. Please be certain.
            </p>
          </div>
          <BaseButton variant="danger" size="sm" @click="showDeleteModal = true">
            Delete Artist
          </BaseButton>
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

    <!-- Instagram Preview Modal -->
    <BaseModal :open="showInstagramModal" title="📸 Instagram Preview" size="lg"
      @close="showInstagramModal = false; editingCaption = false">
      <div class="ig-preview-layout">
        <div class="ig-preview-media">
          <!-- Carousel -->
          <div class="ig-carousel">
            <div class="ig-carousel-slide">
              <!-- Slide 0: Artist image -->
              <template v-if="carouselIndex === 0">
                <img v-if="artist?.file_urls.pic" :src="artist.file_urls.pic" alt="Artist image"
                  class="ig-preview-img" crossorigin="anonymous" />
                <div v-else class="ig-preview-placeholder">No image</div>
              </template>
              <!-- Slide 1: Track 1 video -->
              <template v-else-if="carouselIndex === 1">
                <video v-if="artist?.file_urls.track1_video" :src="artist.file_urls.track1_video"
                  class="ig-preview-video" controls autoplay muted loop />
                <div v-else class="ig-preview-placeholder">Track 1 video not generated yet</div>
              </template>
              <!-- Slide 2: Track 2 video -->
              <template v-else-if="carouselIndex === 2">
                <video v-if="artist?.file_urls.track2_video" :src="artist.file_urls.track2_video"
                  class="ig-preview-video" controls autoplay muted loop />
                <div v-else class="ig-preview-placeholder">Track 2 video not generated yet</div>
              </template>
            </div>
            <!-- Navigation arrows -->
            <button v-if="carouselIndex > 0" class="ig-carousel-arrow ig-carousel-prev"
              @click="carouselIndex--">&lsaquo;</button>
            <button v-if="carouselIndex < 2" class="ig-carousel-arrow ig-carousel-next"
              @click="carouselIndex++">&rsaquo;</button>
            <!-- Dots -->
            <div class="ig-carousel-dots">
              <button v-for="i in 3" :key="i" class="ig-carousel-dot"
                :class="{ active: carouselIndex === i - 1 }" @click="carouselIndex = i - 1">
                <span v-if="i === 1">🖼️</span>
                <span v-else-if="i === 2">🎵1</span>
                <span v-else>🎵2</span>
              </button>
            </div>
          </div>
          <!-- Regenerate button -->
          <div class="ig-video-actions">
            <BaseButton variant="ghost" size="sm" :loading="regeneratingVideos"
              :disabled="!artist?.file_urls.pic || (!artist?.file_urls.track1 && !artist?.file_urls.track2)"
              @click="regenerateVideos">
              🔄 Regenerate Videos
            </BaseButton>
          </div>
        </div>
        <div class="ig-preview-caption">
          <div v-if="generatingCaption" class="ig-preview-loading">Generating caption...</div>
          <template v-else-if="editingCaption">
            <textarea v-model="editedCaption" class="ig-caption-edit" rows="16"></textarea>
          </template>
          <pre v-else-if="artist?.instagram_caption" class="ig-caption-text">{{ artist.instagram_caption }}</pre>
          <p v-else class="text-muted">No caption generated yet</p>
        </div>
      </div>
      <template #footer>
        <template v-if="editingCaption">
          <BaseButton variant="ghost" @click="cancelEditCaption">Cancel</BaseButton>
          <BaseButton variant="primary" :loading="savingCaption" @click="confirmEditCaption">
            ✅ Confirm
          </BaseButton>
        </template>
        <template v-else>
          <BaseButton variant="ghost" :loading="generatingCaption" @click="generateInstagramCaption">
            🔄 Regenerate
          </BaseButton>
          <BaseButton variant="ghost" :disabled="!artist?.instagram_caption" @click="startEditCaption">
            ✏️ Edit
          </BaseButton>
          <BaseButton variant="primary" :loading="postingToInstagram"
            :disabled="!artist?.instagram_caption || !artist?.file_urls.pic" @click="postToInstagram()">
            📤 Publish
          </BaseButton>
        </template>
      </template>
    </BaseModal>

    <!-- Instagram Re-post Confirmation Modal -->
    <BaseModal :open="showInstagramConfirmModal" title="Post to Instagram Again?"
      @close="showInstagramConfirmModal = false">
      <p>This artist was already posted to Instagram.</p>
      <p class="text-muted">Do you want to post again? This will create a duplicate post.</p>
      <template #footer>
        <BaseButton variant="ghost" @click="showInstagramConfirmModal = false">Cancel</BaseButton>
        <BaseButton variant="primary" :loading="postingToInstagram" @click="postToInstagram(true)">
          Post Again
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

.header-title-row {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
}

.title-status-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--spacing-sm);
}

.title-status-row .page-title {
  margin: 0;
  font-size: 3.5rem;
  text-transform: uppercase;
}

.pronouns {
  color: var(--color-text-muted);
  font-size: var(--font-size-lg);
}

.submitted-date {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}

.dl-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: var(--spacing-xs) var(--spacing-md);
  border-radius: var(--radius-md);
  text-decoration: none;
  font-size: var(--font-size-sm);
  transition: all var(--transition-fast);
  background-color: #666666;
  color: #ffffff;
  white-space: nowrap;
}

.dl-btn:hover {
  opacity: 0.8;
}

.dl-btn.audio {
  background-color: #8b5cf6;
}

.dl-btn.images {
  background-color: #f59e0b;
}

.dl-btn.pdf {
  background-color: #3b82f6;
}

.dl-btn.all {
  background-color: #10b981;
}

/* Card header with edit button */
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: var(--spacing-md);
}

.card-header .section-title {
  margin-bottom: 0;
  padding-bottom: var(--spacing-sm);
  border-bottom: 1px solid var(--color-border);
  flex: 1;
}

.edit-btn {
  background: none;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  padding: var(--spacing-xs) var(--spacing-sm);
  cursor: pointer;
  font-size: var(--font-size-sm);
  transition: all var(--transition-fast);
  margin-left: var(--spacing-sm);
  color: var(--color-border);
}

.edit-btn:hover {
  background-color: var(--color-surface);
  border-color: #ffec44;
  color: #ffec44;
}

/* Edit form styles */
.edit-form {
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
  font-weight: 500;
}

.form-input,
.form-textarea {
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  color: var(--color-text);
  font-family: var(--font-family);
  font-size: var(--font-size-md);
  padding: var(--spacing-sm) var(--spacing-md);
}

.form-textarea {
  resize: vertical;
  min-height: 80px;
}

.form-input:focus,
.form-textarea:focus {
  outline: none;
  border-color: var(--color-primary);
}

.file-input {
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  color: var(--color-text);
  font-size: var(--font-size-sm);
  padding: var(--spacing-sm);
  cursor: pointer;
}

.file-input::file-selector-button {
  background-color: var(--color-primary);
  color: white;
  border: none;
  border-radius: var(--radius-sm);
  padding: var(--spacing-xs) var(--spacing-sm);
  margin-right: var(--spacing-sm);
  cursor: pointer;
}

.file-selected {
  font-size: var(--font-size-sm);
  color: var(--color-success);
}

.file-input-wrapper {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-sm);
}

.preview-container {
  display: flex;
  justify-content: center;
  margin-top: var(--spacing-sm);
}

.edit-actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--spacing-sm);
  margin-top: var(--spacing-md);
  padding-top: var(--spacing-md);
  border-top: 1px solid var(--color-border);
}

/* Top Row: Assign to Show (left) + Profile Picture (right) */
.top-row-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--spacing-lg);
  margin-bottom: var(--spacing-lg);
}

.assign-card {
  display: flex;
  flex-direction: column;
  min-height: 260px;
}

.current-assignment {
  margin-top: var(--spacing-sm);
}

.current-assignment .subsection-title {
  margin-top: 0;
}

.assigned-show-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: var(--spacing-md);
  background-color: rgba(var(--color-success-rgb, 46, 160, 67), 0.1);
  border-radius: var(--radius-md);
  border: 1px solid rgba(var(--color-success-rgb, 46, 160, 67), 0.3);
}

.show-title-link {
  font-size: var(--font-size-lg);
  font-weight: 500;
  color: var(--color-primary);
}

.left-column {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-lg);
}

.details-card {
  flex: 1;
}

.upcoming-events-text {
  color: var(--color-text);
  line-height: 1.6;
  white-space: pre-wrap;
}

.profile-picture-card {
  display: flex;
  flex-direction: column;
}

.profile-picture-container {
  display: flex;
  justify-content: center;
  align-items: center;
  flex: 1;
}

.profile-picture {
  width: 100%;
  border-radius: var(--radius-lg);
  object-fit: contain;
}

.audio-files-card {
  margin-top: var(--spacing-lg);
}

.downloads-section {
  margin-top: var(--spacing-lg);
}

.download-buttons-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: var(--spacing-sm);
}

.details-section {
  margin-top: var(--spacing-lg);
}

.section-title {
  font-size: var(--font-size-lg);
  margin-bottom: var(--spacing-md);
  padding-bottom: var(--spacing-sm);
  border-bottom: 1px solid var(--color-border);
}

.subsection-title {
  font-size: var(--font-size-md);
  margin-top: var(--spacing-lg);
  margin-bottom: var(--spacing-sm);
  color: var(--color-text-muted);
}

.details-section .subsection-title,
.assigned-shows-section .subsection-title {
  margin-top: 0;
}

.detail-list {
  display: grid;
  grid-template-columns: 100px 1fr;
  gap: var(--spacing-sm);
}

.detail-list dt {
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
}

.detail-list dd {
  margin: 0;
  word-break: break-word;
}

.social-link {
  color: var(--color-primary);
  word-break: break-all;
}

.mentions-text {
  color: var(--color-text);
  line-height: 1.6;
}

.ai-bio-card {
  background: var(--color-surface-hover);
  border-left: 3px solid var(--color-primary);
  padding: var(--spacing-md);
  border-radius: var(--radius-sm);
  margin-bottom: var(--spacing-sm);
}

.ai-bio-text {
  color: var(--color-text);
  line-height: 1.6;
  font-style: italic;
}

.generate-bio-btn {
  background: var(--color-surface-hover);
  color: var(--color-text);
  border: 1px solid var(--color-border);
  padding: var(--spacing-xs) var(--spacing-md);
  border-radius: var(--radius-sm);
  cursor: pointer;
  font-size: var(--font-size-sm);
  transition: background 0.2s;
}

.generate-bio-btn:hover:not(:disabled) {
  background: var(--color-primary);
  color: var(--color-on-primary, #fff);
}

.instagram-preview-btn {
  background: linear-gradient(45deg, #f09433, #e6683c, #dc2743, #cc2366, #bc1888) !important;
  color: #fff !important;
  border: none !important;
}

.instagram-preview-btn:hover:not(:disabled) {
  opacity: 0.9;
}

.instagram-posted-badge {
  display: inline-block;
  margin-left: var(--spacing-sm);
  font-size: var(--font-size-sm);
  color: var(--color-success, #22c55e);
}

.ig-preview-layout {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--spacing-lg);
  min-height: 300px;
}

.ig-preview-image,
.ig-preview-media {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--spacing-sm);
}

.ig-preview-img {
  width: 100%;
  max-height: 400px;
  object-fit: contain;
  border-radius: var(--radius-sm);
}

.ig-preview-video {
  width: 100%;
  max-height: 400px;
  border-radius: var(--radius-sm);
  background: #000;
}

.ig-carousel {
  position: relative;
  width: 100%;
}

.ig-carousel-slide {
  min-height: 300px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.ig-carousel-arrow {
  position: absolute;
  top: 50%;
  transform: translateY(-50%);
  background: rgba(0, 0, 0, 0.5);
  color: white;
  border: none;
  width: 36px;
  height: 36px;
  border-radius: 50%;
  font-size: 20px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1;
  transition: background 0.2s;
}

.ig-carousel-arrow:hover {
  background: rgba(0, 0, 0, 0.7);
}

.ig-carousel-prev {
  left: 8px;
}

.ig-carousel-next {
  right: 8px;
}

.ig-carousel-dots {
  display: flex;
  justify-content: center;
  gap: var(--spacing-xs, 4px);
  margin-top: var(--spacing-sm);
}

.ig-carousel-dot {
  background: none;
  border: 2px solid var(--color-border, #555);
  border-radius: var(--radius-sm, 4px);
  padding: 2px 6px;
  font-size: 12px;
  cursor: pointer;
  opacity: 0.5;
  transition: opacity 0.2s, border-color 0.2s;
}

.ig-carousel-dot.active {
  opacity: 1;
  border-color: var(--color-primary, #e1306c);
}

.ig-video-actions {
  text-align: center;
}

.ig-preview-placeholder {
  width: 100%;
  height: 300px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--color-surface-hover);
  border-radius: var(--radius-sm);
  color: var(--color-text-muted);
}

.ig-preview-caption {
  display: flex;
  flex-direction: column;
  overflow-y: auto;
  max-height: 400px;
}

.ig-preview-loading {
  color: var(--color-text-muted);
  font-style: italic;
  padding: var(--spacing-md);
}

.ig-caption-text {
  color: var(--color-text);
  line-height: 1.6;
  font-family: inherit;
  font-size: var(--font-size-sm);
  white-space: pre-wrap;
  word-wrap: break-word;
  margin: 0;
  padding: var(--spacing-sm);
  background: var(--color-surface-hover);
  border-radius: var(--radius-sm);
  border-left: 3px solid #e1306c;
}

.ig-caption-edit {
  width: 100%;
  min-height: 200px;
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  padding: var(--spacing-sm);
  font-family: inherit;
  font-size: var(--font-size-sm);
  line-height: 1.6;
  resize: vertical;
}

.ig-caption-edit:focus {
  outline: none;
  border-color: var(--color-primary);
}

@media (max-width: 768px) {
  .ig-preview-layout {
    grid-template-columns: 1fr;
  }
}

.generate-bio-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.file-list {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
}

.file-item {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
}

.file-label {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}

.audio-player {
  width: 100%;
  max-width: 100%;
}

.audio-row {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
}

.download-link {
  color: var(--color-text-muted);
  text-decoration: none;
  font-size: 16px;
  padding: var(--spacing-xs);
  transition: color var(--transition-fast);
  flex-shrink: 0;
}

.download-link:hover {
  color: var(--color-primary);
}

.show-list {
  list-style: none;
  padding: 0;
  margin: 0;
}

.show-list li {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: var(--spacing-sm) 0;
  border-bottom: 1px solid var(--color-border);
}

.show-list li:last-child {
  border-bottom: none;
}

.show-list li.upcoming-show {
  background-color: rgba(var(--color-success-rgb, 46, 160, 67), 0.1);
  padding: var(--spacing-sm);
  border-radius: var(--radius-sm);
  margin-bottom: var(--spacing-xs);
  border-bottom: none;
}

.show-info {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
}

.show-date {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}

.unassign-btn {
  background: none;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  color: var(--color-text-muted);
  cursor: pointer;
  padding: var(--spacing-xs) var(--spacing-sm);
  font-size: var(--font-size-sm);
  transition: all var(--transition-fast);
}

.unassign-btn:hover {
  color: var(--color-error);
  border-color: var(--color-error);
  background-color: rgba(var(--color-error-rgb, 220, 53, 69), 0.1);
}

.assign-form {
  display: flex;
  gap: var(--spacing-sm);
  align-items: center;
}

.select-input {
  flex: 1;
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  color: var(--color-text);
  font-family: var(--font-family);
  font-size: var(--font-size-md);
  padding: var(--spacing-sm) var(--spacing-md);
}

/* Danger Zone */
.danger-zone {
  margin-top: var(--spacing-xl);
  border: 2px solid var(--color-error);
  border-radius: var(--radius-lg);
  padding: var(--spacing-lg);
  background-color: rgba(var(--color-error-rgb, 220, 53, 69), 0.05);
}

.danger-zone-title {
  font-size: var(--font-size-lg);
  color: var(--color-error);
  margin: 0 0 var(--spacing-md) 0;
}

.danger-zone-content {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: var(--spacing-md);
}

.danger-zone-info {
  flex: 1;
}

.danger-zone-description {
  margin: 0;
  line-height: 1.6;
  color: var(--color-text);
}

.danger-zone-description strong {
  color: var(--color-error);
}

/* Tablet breakpoint */
@media (max-width: 992px) {
  .top-row-grid {
    grid-template-columns: 1fr;
  }
}

/* Mobile breakpoint */
@media (max-width: 768px) {
  .page-header {
    flex-direction: column;
    align-items: flex-start;
    gap: var(--spacing-md);
  }

  .title-status-row {
    flex-direction: column;
    align-items: flex-start;
  }

  .download-buttons-grid {
    grid-template-columns: 1fr;
  }

  .profile-picture {
    max-height: 350px;
  }

  .assign-form {
    flex-direction: column;
    align-items: stretch;
  }

  .select-input {
    width: 100%;
  }

  .detail-list {
    grid-template-columns: 1fr;
    gap: var(--spacing-xs);
  }

  .detail-list dt {
    margin-top: var(--spacing-sm);
  }

  .detail-list dd {
    margin-bottom: var(--spacing-xs);
  }

  .audio-row {
    flex-direction: column;
    align-items: stretch;
  }

  .audio-player {
    width: 100%;
  }

  .assigned-show-item {
    flex-direction: column;
    align-items: flex-start;
    gap: var(--spacing-sm);
  }

  .assigned-show-item .unassign-btn {
    align-self: flex-end;
  }

  .danger-zone-content {
    flex-direction: column;
    align-items: stretch;
  }
}

/* Small mobile */
@media (max-width: 480px) {
  .profile-picture {
    max-height: 280px;
  }

  .show-list li {
    flex-direction: column;
    align-items: flex-start;
    gap: var(--spacing-xs);
  }

  .unassign-btn {
    align-self: flex-end;
  }
}
</style>
