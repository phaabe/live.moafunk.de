<script setup lang="ts">
import { ref, watch, computed, onMounted, onUnmounted, nextTick } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import Cropper from 'cropperjs';
import 'cropperjs/dist/cropper.css';
import { artistsApi, type Artist, type ArtistDetail, type OverlayParams } from '../api';
import {
  getDefaultOverlayParams,
  buildFilterString,
  renderPreview,
} from '../composables/useOverlayRenderer';
import { useFlash } from '../composables/useFlash';
import OverlayControls from '../components/OverlayControls.vue';
import OverlayGallery from '../components/OverlayGallery.vue';

defineOptions({
  name: 'OverlayEditorPage',
});

const route = useRoute();
const router = useRouter();
const flash = useFlash();

// ---------------------------------------------------------------------------
// Artist selection
// ---------------------------------------------------------------------------

const artists = ref<Artist[]>([]);
const artistsLoading = ref(false);
const selectedArtistId = ref<number | null>(null);
const artist = ref<ArtistDetail | null>(null);
const artistLoading = ref(false);
const artistError = ref<string | null>(null);

async function loadArtistList(): Promise<void> {
  artistsLoading.value = true;
  try {
    const res = await artistsApi.list({ sort: 'name', dir: 'asc' });
    artists.value = res.artists;
  } catch (err) {
    console.error('Failed to load artists:', err);
  } finally {
    artistsLoading.value = false;
  }
}

async function loadArtistDetail(id: number): Promise<void> {
  artistLoading.value = true;
  artistError.value = null;
  try {
    artist.value = await artistsApi.get(id);
  } catch (err) {
    artistError.value = err instanceof Error ? err.message : 'Failed to load artist';
    artist.value = null;
  } finally {
    artistLoading.value = false;
  }
}

function onArtistChange(): void {
  if (selectedArtistId.value) {
    // Update the URL to reflect artist selection
    router.replace({ params: { id: String(selectedArtistId.value) } });
  }
}

// ---------------------------------------------------------------------------
// CropperJS
// ---------------------------------------------------------------------------

const cropperWrapper = ref<HTMLElement | null>(null);
const cropperImg = ref<HTMLImageElement | null>(null);
const overlayEl = ref<HTMLElement | null>(null);

let cropper: Cropper | null = null;
let objectUrl: string | null = null;
let destroyed = false;

const imageLoaded = ref(false);

function initCropper(): void {
  if (!cropperImg.value) return;
  destroyed = false;

  cropper = new Cropper(cropperImg.value, {
    aspectRatio: 1,
    viewMode: 1,
    dragMode: 'move',
    autoCropArea: 1.0,
    background: false,
    minContainerWidth: 200,
    minContainerHeight: 200,
    guides: false,
    center: false,
    highlight: false,
    cropBoxMovable: false,
    cropBoxResizable: false,
    toggleDragModeOnDblclick: false,
    responsive: true,
    zoomOnWheel: true,
    ready() {
      if (destroyed) return;
      attachOverlay();
      requestAnimationFrame(() => {
        if (destroyed || !cropper) return;
        zoomImageToContainer();
        requestAnimationFrame(() => {
          if (destroyed || !cropper) return;
          setCropBoxPadding(0.05);
        });
      });
    },
  });
}

function destroyCropper(): void {
  destroyed = true;
  detachOverlay();
  if (cropper) {
    cropper.destroy();
    cropper = null;
  }
  if (objectUrl) {
    URL.revokeObjectURL(objectUrl);
    objectUrl = null;
  }
  imageLoaded.value = false;
}

function attachOverlay(): void {
  if (!overlayEl.value || !cropperWrapper.value) return;
  const viewBox =
    cropperWrapper.value.querySelector<HTMLElement>('.cropper-view-box') ||
    cropperWrapper.value.querySelector<HTMLElement>('.cropper-crop-box');
  if (viewBox && overlayEl.value.parentElement !== viewBox) {
    viewBox.appendChild(overlayEl.value);
  }
}

function detachOverlay(): void {
  const frame = cropperWrapper.value?.querySelector<HTMLElement>('.cropper-frame');
  if (frame && overlayEl.value && overlayEl.value.parentElement !== frame) {
    frame.appendChild(overlayEl.value);
  }
}

function setCropBoxPadding(paddingFraction = 0.05): void {
  if (!cropper) return;
  const container = cropper.getContainerData() as unknown as {
    left?: number; top?: number; width: number; height: number;
  };
  if (!container || container.width <= 0 || container.height <= 0) return;

  const containerSize = container.width;
  const pad = containerSize * paddingFraction;
  const cropSide = Math.max(1, containerSize - 2 * pad);
  const leftBase = container.left ?? 0;
  const topBase = container.top ?? 0;
  const left = leftBase + pad;
  const top = topBase + (container.height - cropSide) / 2;
  cropper.setCropBoxData({ left, top, width: cropSide, height: cropSide });
}

function zoomImageToContainer(): void {
  if (!cropper) return;
  const container = cropper.getContainerData();
  const canvasData = cropper.getCanvasData();
  if (!container || !canvasData || container.width <= 0 || container.height <= 0) return;

  const scaleX = container.width / canvasData.width;
  const scaleY = container.height / canvasData.height;
  const scale = Math.max(scaleX, scaleY);

  if (scale > 1) {
    const newWidth = canvasData.width * scale;
    const newHeight = canvasData.height * scale;
    const newLeft = (container.width - newWidth) / 2;
    const newTop = (container.height - newHeight) / 2;
    cropper.setCanvasData({ left: newLeft, top: newTop, width: newWidth, height: newHeight });
  }
}

function isCropperReady(): boolean {
  if (!cropper || destroyed) return false;
  try {
    const cd = cropper.getCanvasData();
    return cd != null && cd.width > 0;
  } catch {
    return false;
  }
}

function zoomIn(): void {
  if (isCropperReady()) cropper!.zoom(0.1);
}

function zoomOut(): void {
  if (isCropperReady()) cropper!.zoom(-0.1);
}

function onImageLoad(): void {
  imageLoaded.value = true;
  if (cropperImg.value?.src) {
    initCropper();
  }
}

// ---------------------------------------------------------------------------
// Overlay params (two-way bound to OverlayControls)
// ---------------------------------------------------------------------------

const params = ref<OverlayParams>(getDefaultOverlayParams());

/** CSS filter applied live to the cropper image elements */
const liveFilterString = computed(() => buildFilterString(params.value.filter));

/** Apply filter to CropperJS image elements reactively */
watch(liveFilterString, (filterStr) => {
  if (!cropperWrapper.value) return;
  const imgs = cropperWrapper.value.querySelectorAll<HTMLImageElement>(
    '.cropper-view-box img:not(.overlay-logo), .cropper-canvas img',
  );
  imgs.forEach((img) => {
    img.style.filter = filterStr;
  });
});

// ---------------------------------------------------------------------------
// Live DOM overlay (positioned using params percentages)
// ---------------------------------------------------------------------------

function textShadowCss(el: { shadow?: { offsetX: number; offsetY: number; color: string } }): string {
  if (!el.shadow) return 'none';
  return `${el.shadow.offsetX}px ${el.shadow.offsetY}px 0px ${el.shadow.color}`;
}

const overlayUnStyle = computed(() => ({
  display: params.value.un.visible ? 'block' : 'none',
  left: `${params.value.un.x}%`,
  top: `${params.value.un.y}%`,
  fontSize: `${params.value.un.size}px`,
  color: params.value.un.color,
  fontWeight: params.value.un.fontWeight ?? '600',
  fontStyle: params.value.un.fontStyle ?? 'italic',
  textShadow: textShadowCss(params.value.un),
}));

const overlayHeardStyle = computed(() => ({
  display: params.value.heard.visible ? 'block' : 'none',
  left: `${params.value.heard.x}%`,
  top: `${params.value.heard.y}%`,
  fontSize: `${params.value.heard.size}px`,
  color: params.value.heard.color,
  fontWeight: params.value.heard.fontWeight ?? '400',
  fontStyle: params.value.heard.fontStyle ?? 'italic',
  textShadow: textShadowCss(params.value.heard),
}));

const overlayLogoStyle = computed(() => ({
  display: params.value.logo.visible ? 'block' : 'none',
  left: `${params.value.logo.x}%`,
  top: `${params.value.logo.y}%`,
  width: `${params.value.logo.size}%`,
  height: `${params.value.logo.size}%`,
  transform: 'translate(-50%, -50%)',
}));

const overlayNameStyle = computed(() => ({
  display: params.value.artistName.visible ? 'flex' : 'none',
  left: `${params.value.artistName.x}%`,
  top: `${params.value.artistName.y}%`,
  fontSize: `${params.value.artistName.size}px`,
  color: params.value.artistName.color,
  fontWeight: params.value.artistName.fontWeight ?? '700',
  fontStyle: params.value.artistName.fontStyle ?? 'normal',
  transform: 'translate(-50%, -50%)',
  textShadow: textShadowCss(params.value.artistName),
}));

const displayArtistName = computed(() =>
  artist.value?.name?.toUpperCase() ?? '',
);

// ---------------------------------------------------------------------------
// Preview modal
// ---------------------------------------------------------------------------

const previewUrl = ref<string | null>(null);
const showPreview = ref(false);
const previewing = ref(false);

async function openPreview(): Promise<void> {
  if (!cropper || !artist.value) return;
  previewing.value = true;
  try {
    const { brandedBlob } = await renderPreview(cropper, params.value, artist.value.name);
    if (previewUrl.value) URL.revokeObjectURL(previewUrl.value);
    previewUrl.value = URL.createObjectURL(brandedBlob);
    showPreview.value = true;
  } catch (err) {
    flash.error(err instanceof Error ? err.message : 'Preview failed');
  } finally {
    previewing.value = false;
  }
}

function closePreview(): void {
  showPreview.value = false;
}

// ---------------------------------------------------------------------------
// Save to R2
// ---------------------------------------------------------------------------

const saving = ref(false);

const galleryRef = ref<InstanceType<typeof OverlayGallery> | null>(null);

async function saveToR2(): Promise<void> {
  if (!cropper || !artist.value) return;
  saving.value = true;
  try {
    const { brandedBlob } = await renderPreview(cropper, params.value, artist.value.name);
    await artistsApi.saveOverlay(artist.value.id, brandedBlob);
    flash.success('Overlay saved to R2');
    galleryRef.value?.refresh();
  } catch (err) {
    flash.error(err instanceof Error ? err.message : 'Save failed');
  } finally {
    saving.value = false;
  }
}

// ---------------------------------------------------------------------------
// Gallery active overlay
// ---------------------------------------------------------------------------

const activeKey = ref<string | null>(null);

async function handleSetActive(key: string): Promise<void> {
  if (!artist.value) return;
  try {
    await artistsApi.setActiveOverlay(artist.value.id, key);
    activeKey.value = key;
    flash.success('Active overlay updated');
    galleryRef.value?.refresh();
  } catch (err) {
    flash.error(err instanceof Error ? err.message : 'Failed to set active overlay');
  }
}

// ---------------------------------------------------------------------------
// Watch artist selection / route changes
// ---------------------------------------------------------------------------

watch(selectedArtistId, async (id) => {
  destroyCropper();
  artist.value = null;
  activeKey.value = null;
  if (!id) return;

  await loadArtistDetail(id);

  // Capture loaded artist (TS can't see loadArtistDetail mutated the ref)
  const loaded = artist.value as ArtistDetail | null;
  if (!loaded) return;

  // Load the overlay list to determine the active key
  try {
    const data = await artistsApi.listOverlays(id);
    activeKey.value = data.active_key;
  } catch {
    // non-critical
  }

  // Load original image into cropper via same-origin proxy (avoids R2 CORS)
  await nextTick();
  if (cropperImg.value) {
    try {
      const blob = await artistsApi.getImageBlob(id, 'original');
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      objectUrl = URL.createObjectURL(blob);
      cropperImg.value.src = objectUrl;
    } catch (err) {
      console.error('Failed to load artist image:', err);
      flash.error('Failed to load artist image');
    }
  }
});

// Handle resize
function handleResize(): void {
  requestAnimationFrame(() => {
    if (destroyed || !cropper) return;
    setCropBoxPadding(0.05);
  });
}

onMounted(async () => {
  await loadArtistList();

  // If route has :id param, pre-select the artist
  const routeId = Number(route.params.id);
  if (routeId && !isNaN(routeId)) {
    selectedArtistId.value = routeId;
  }

  window.addEventListener('resize', handleResize);
});

onUnmounted(() => {
  destroyCropper();
  if (previewUrl.value) URL.revokeObjectURL(previewUrl.value);
  window.removeEventListener('resize', handleResize);
});
</script>

<template>
  <div class="overlay-editor-page">
    <!-- Header -->
    <div class="page-header">
      <h1 class="page-title">Overlay Editor</h1>
      <div class="artist-selector">
        <label for="artist-select">Artist</label>
        <select id="artist-select" v-model="selectedArtistId" class="form-input" :disabled="artistsLoading"
          @change="onArtistChange">
          <option :value="null" disabled>Select an artist…</option>
          <option v-for="a in artists" :key="a.id" :value="a.id">
            {{ a.name }}
          </option>
        </select>
      </div>
    </div>

    <!-- Loading / Error -->
    <div v-if="artistLoading" class="loading-spinner"></div>
    <div v-if="artistError" class="flash-message error">{{ artistError }}</div>

    <!-- Editor -->
    <div v-if="artist" class="editor-layout">
      <!-- Left: Canvas + Actions -->
      <div class="editor-canvas-panel">
        <div ref="cropperWrapper" class="cropper-wrapper">
          <div class="cropper-frame">
            <img ref="cropperImg" alt="Original artist image" crossorigin="anonymous" @load="onImageLoad" />

            <!-- Live DOM overlay (positioned by params) -->
            <div ref="overlayEl" class="live-overlay" aria-hidden="true">
              <span class="live-overlay-un" :style="overlayUnStyle">UN</span>
              <span class="live-overlay-heard" :style="overlayHeardStyle">HEARD</span>
              <img class="live-overlay-logo overlay-logo" src="/moafunk.png" alt="" crossorigin="anonymous"
                :style="overlayLogoStyle" />
              <span class="live-overlay-name" :style="overlayNameStyle">
                {{ displayArtistName }}
              </span>
            </div>
          </div>

          <!-- Zoom controls -->
          <div v-if="imageLoaded" class="cropper-controls">
            <button type="button" @click="zoomOut" aria-label="Zoom out">−</button>
            <button type="button" @click="zoomIn" aria-label="Zoom in">+</button>
            <span class="cropper-note">Drag to move. Use +/- to zoom.</span>
          </div>
        </div>

        <!-- Action buttons -->
        <div v-if="imageLoaded" class="action-bar">
          <button type="button" class="btn-secondary" :disabled="previewing" @click="openPreview">
            {{ previewing ? 'Rendering…' : 'Preview' }}
          </button>
          <button type="button" class="btn-primary" :disabled="saving" @click="saveToR2">
            {{ saving ? 'Saving…' : 'Save to R2' }}
          </button>
        </div>

        <!-- No image hint -->
        <div v-if="!imageLoaded && !artistLoading" class="no-image-hint">
          <p v-if="!artist.file_urls?.pic_original" class="text-muted">
            This artist has no original image uploaded yet. Upload one from the
            <router-link :to="`/artists/${artist.id}`">artist detail page</router-link>.
          </p>
          <div v-else class="loading-image">
            <div class="loading-spinner small"></div>
            <span class="text-muted">Loading original image…</span>
          </div>
        </div>
      </div>

      <!-- Right: Controls panel -->
      <div class="editor-controls-panel">
        <OverlayControls v-model="params" :artist-name="artist.name" />
      </div>
    </div>

    <!-- Gallery section -->
    <div v-if="artist" class="gallery-section">
      <h2 class="section-title">Saved Overlays</h2>
      <OverlayGallery ref="galleryRef" :artist-id="artist.id" :active-key="activeKey" @set-active="handleSetActive" />
    </div>

    <!-- Preview modal -->
    <Teleport to="body">
      <div v-if="showPreview" class="preview-backdrop" @click.self="closePreview">
        <div class="preview-modal">
          <div class="preview-header">
            <h3>Preview (1024 × 1024)</h3>
            <button type="button" class="btn-ghost btn-close" @click="closePreview">✕</button>
          </div>
          <div class="preview-body">
            <img v-if="previewUrl" :src="previewUrl" alt="Overlay preview" class="preview-image" />
          </div>
          <div class="preview-footer">
            <button type="button" class="btn-ghost" @click="closePreview">Close</button>
            <button type="button" class="btn-primary" :disabled="saving" @click="saveToR2(); closePreview()">
              {{ saving ? 'Saving…' : 'Save to R2' }}
            </button>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.overlay-editor-page {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-lg);
}

/* ---- Header ---- */
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: var(--spacing-md);
}

.page-title {
  font-size: var(--font-size-2xl);
  font-weight: var(--font-weight-bold);
  margin: 0;
}

.artist-selector {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
}

.artist-selector label {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
  white-space: nowrap;
}

.artist-selector .form-input {
  min-width: 240px;
  background-color: var(--color-surface);
  color: var(--color-text);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  padding: var(--spacing-xs) var(--spacing-sm);
  font-size: var(--font-size-sm);
}

/* ---- Two-panel editor layout ---- */
.editor-layout {
  display: grid;
  grid-template-columns: 1fr 380px;
  gap: var(--spacing-lg);
  align-items: start;
  justify-content: center;
  max-width: calc(80vh + 380px + var(--spacing-lg));
  margin: 0 auto;
}

@media (max-width: 1024px) {
  .editor-layout {
    grid-template-columns: 1fr;
  }
}

/* ---- Canvas panel ---- */
.editor-canvas-panel {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--spacing-md);
}

.cropper-wrapper {
  width: 100%;
  max-width: 80vh;
}

.cropper-frame {
  position: relative;
  width: 100%;
  aspect-ratio: 1;
  background-color: #1a1a1a;
  overflow: hidden;
}

.cropper-frame img {
  display: block;
  max-width: 100%;
}

.cropper-controls {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  padding: var(--spacing-sm) 0;
}

.cropper-controls button {
  width: 36px;
  height: 36px;
  border: 1px solid var(--color-border);
  background: var(--color-surface);
  color: var(--color-text);
  border-radius: var(--radius-sm);
  font-size: 18px;
  cursor: pointer;
  transition: all var(--transition-fast);
}

.cropper-controls button:hover {
  background: var(--color-primary);
  border-color: var(--color-primary);
  color: var(--color-primary-text);
}

.cropper-note {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}

/* ---- Action bar ---- */
.action-bar {
  display: flex;
  gap: var(--spacing-sm);
}

.btn-primary {
  background: var(--color-bg);
  border: 1px solid var(--color-primary);
  color: var(--color-primary);
  padding: var(--spacing-xs) var(--spacing-md);
  border-radius: var(--radius-md);
  cursor: pointer;
  font-size: var(--font-size-sm);
  font-weight: 500;
  transition: all var(--transition-fast);
}

.btn-primary:hover:not(:disabled) {
  background: var(--color-primary);
  color: var(--color-primary-text);
}

.btn-primary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn-secondary {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  color: var(--color-text);
  padding: var(--spacing-xs) var(--spacing-md);
  border-radius: var(--radius-md);
  cursor: pointer;
  font-size: var(--font-size-sm);
  font-weight: 500;
  transition: all var(--transition-fast);
}

.btn-secondary:hover:not(:disabled) {
  background: var(--color-surface-hover);
  border-color: var(--color-border-light);
}

.btn-secondary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn-ghost {
  background: none;
  border: 1px solid var(--color-border);
  color: var(--color-text);
  padding: var(--spacing-xs) var(--spacing-md);
  border-radius: var(--radius-md);
  cursor: pointer;
  font-size: var(--font-size-sm);
}

.btn-ghost:hover {
  background: var(--color-surface);
}

.btn-close {
  border: none;
  font-size: var(--font-size-lg);
  padding: var(--spacing-xs);
  line-height: 1;
}

/* ---- No-image hint ---- */
.no-image-hint {
  text-align: center;
  padding: var(--spacing-xl);
  background: var(--color-surface);
  border-radius: var(--radius-md);
  border: 1px dashed var(--color-border);
}

.loading-image {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--spacing-sm);
}

/* ---- Controls panel ---- */
.editor-controls-panel {
  max-height: 80vh;
  overflow-y: auto;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  padding: var(--spacing-md);
}

/* ---- Gallery section ---- */
.gallery-section {
  border-top: 1px solid var(--color-border);
  padding-top: var(--spacing-lg);
}

.section-title {
  font-size: var(--font-size-xl);
  font-weight: var(--font-weight-bold);
  margin: 0 0 var(--spacing-md) 0;
}

/* ---- Live overlay (positioned via params percentages) ---- */
.live-overlay {
  position: absolute;
  inset: 0;
  pointer-events: none;
  z-index: 2000;
  font-family: 'Shoika', system-ui, -apple-system, 'Segoe UI', Roboto, Arial, sans-serif;
  overflow: hidden;
}

.live-overlay-un,
.live-overlay-heard,
.live-overlay-name {
  position: absolute;
  text-transform: uppercase;
  line-height: 1;
  letter-spacing: 0.02em;
  white-space: nowrap;
}

.live-overlay-logo {
  position: absolute;
  object-fit: contain;
}

.live-overlay-name {
  display: flex;
  align-items: center;
  justify-content: center;
  max-width: 35%;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* ---- Preview modal ---- */
.preview-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.8);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 10000;
  padding: var(--spacing-lg);
}

.preview-modal {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  max-width: 1080px;
  width: 100%;
  max-height: 95vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.preview-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--spacing-md);
  border-bottom: 1px solid var(--color-border);
}

.preview-header h3 {
  margin: 0;
  font-size: var(--font-size-lg);
}

.preview-body {
  flex: 1;
  overflow: auto;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--spacing-md);
  background: #000;
}

.preview-image {
  max-width: 100%;
  max-height: 70vh;
  border-radius: var(--radius-sm);
}

.preview-footer {
  display: flex;
  justify-content: flex-end;
  gap: var(--spacing-sm);
  padding: var(--spacing-md);
  border-top: 1px solid var(--color-border);
}

/* ---- Utilities ---- */
.text-muted {
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
}

.flash-message.error {
  background: var(--color-error-bg);
  color: var(--color-error);
  border: 1px solid var(--color-error);
  border-radius: var(--radius-md);
  padding: var(--spacing-sm) var(--spacing-md);
}

.loading-spinner {
  width: 40px;
  height: 40px;
  border: 3px solid rgba(255, 236, 68, 0.3);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: spin 1s linear infinite;
  margin: var(--spacing-lg) auto;
}

.loading-spinner.small {
  width: 20px;
  height: 20px;
  border-width: 2px;
  margin: 0;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

/* ---- CropperJS overrides ---- */
:deep(.cropper-view-box) {
  position: relative;
  outline: none;
}

/* CropperJS applies width/height: 100% to img inside .cropper-view-box.
   Override for our overlay logo. */
:deep(.cropper-view-box img.overlay-logo) {
  position: absolute !important;
  max-width: none !important;
  max-height: none !important;
}

/* Reduce brightness of inactive area */
:deep(.cropper-modal) {
  background-color: #000 !important;
  opacity: 0.8 !important;
}
</style>
