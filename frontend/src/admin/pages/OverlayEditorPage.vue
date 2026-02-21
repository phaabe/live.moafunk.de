<script setup lang="ts">
import { ref, watch, computed, onMounted, onUnmounted, nextTick } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import Cropper from 'cropperjs';
import 'cropperjs/dist/cropper.css';
import { artistsApi, showsApi, presetsApi, type Artist, type ArtistDetail, type Show, type ShowDetail, type OverlayParams } from '../api';
import { BaseModal } from '@shared/components';
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
// Entity type toggle (artist / show)
// ---------------------------------------------------------------------------

type EntityType = 'artist' | 'show';
const entityType = ref<EntityType>('artist');

// ---------------------------------------------------------------------------
// Artist selection
// ---------------------------------------------------------------------------

const artists = ref<Artist[]>([]);
const artistsLoading = ref(false);
const selectedArtistId = ref<number | null>(null);
const artist = ref<ArtistDetail | null>(null);
const artistLoading = ref(false);
const artistError = ref<string | null>(null);

const activePresetId = computed(() => {
  if (entityType.value === 'artist') return artist.value?.active_overlay_preset_id ?? undefined;
  return show.value?.active_overlay_preset_id ?? undefined;
});
const currentPresetId = ref<number | null>(null);

// ---------------------------------------------------------------------------
// Activation modal state (shows only)
// ---------------------------------------------------------------------------

const showActivateModal = ref(false);
const activateChoice = ref<'overwrite' | 'new'>('overwrite');
const activateNewPresetName = ref('');
const activating = ref(false);

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
    router.replace({ params: { id: String(selectedArtistId.value) } });
  }
}

// ---------------------------------------------------------------------------
// Show selection
// ---------------------------------------------------------------------------

const shows = ref<Show[]>([]);
const showsLoading = ref(false);
const selectedShowId = ref<number | null>(null);
const show = ref<ShowDetail | null>(null);
const showLoading = ref(false);
const showError = ref<string | null>(null);

async function loadShowList(): Promise<void> {
  showsLoading.value = true;
  try {
    const res = await showsApi.list();
    shows.value = res.shows;
  } catch (err) {
    console.error('Failed to load shows:', err);
  } finally {
    showsLoading.value = false;
  }
}

async function loadShowDetail(id: number): Promise<void> {
  showLoading.value = true;
  showError.value = null;
  try {
    show.value = await showsApi.get(id);
  } catch (err) {
    showError.value = err instanceof Error ? err.message : 'Failed to load show';
    show.value = null;
  } finally {
    showLoading.value = false;
  }
}

/** Computed helpers for entity-agnostic template bindings */
const entityLoading = computed(() =>
  entityType.value === 'artist' ? artistLoading.value : showLoading.value
);
const entityError = computed(() =>
  entityType.value === 'artist' ? artistError.value : showError.value
);
const entityName = computed(() => {
  if (entityType.value === 'artist') return artist.value?.name ?? '';
  return show.value?.title ?? '';
});
const hasEntity = computed(() =>
  entityType.value === 'artist' ? !!artist.value : !!show.value
);
const entityId = computed(() =>
  entityType.value === 'artist' ? artist.value?.id : show.value?.id
);

function onEntityTypeChange(): void {
  // Reset both selections when switching entity type
  destroyCropper();
  selectedArtistId.value = null;
  selectedShowId.value = null;
  artist.value = null;
  show.value = null;
  activeKey.value = null;
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

/**
 * Scale factor: ratio of the live overlay container width to the 1024px
 * canvas that renderPreview / Save-to-R2 produces.  All absolute-pixel
 * values (font sizes, shadow offsets) in the live DOM overlay are
 * multiplied by this so the editor matches the final output.
 */
const overlayScale = ref(1);
let overlayResizeObserver: ResizeObserver | null = null;

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

  // Start tracking the overlay container size for scale calculations
  if (!overlayResizeObserver && overlayEl.value) {
    const target = overlayEl.value.parentElement ?? overlayEl.value;
    overlayResizeObserver = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const w = entry.contentRect.width;
        if (w > 0) overlayScale.value = w / 1024;
      }
    });
    overlayResizeObserver.observe(target);
    // Seed initial value
    if (target.clientWidth > 0) overlayScale.value = target.clientWidth / 1024;
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
  const s = overlayScale.value;
  return `${el.shadow.offsetX * s}px ${el.shadow.offsetY * s}px 0px ${el.shadow.color}`;
}

/** Per-tile shadow: uses tileShadowColors[idx] when available, otherwise the shared shadow color. */
function tileShadowCss(idx: number): string {
  const shadow = params.value.artistName.shadow;
  if (!shadow) return 'none';
  const s = overlayScale.value;
  const color = params.value.tileShadowColors?.[idx] ?? shadow.color;
  return `${shadow.offsetX * s}px ${shadow.offsetY * s}px 0px ${color}`;
}

const overlayUnStyle = computed(() => ({
  display: params.value.un.visible ? 'block' : 'none',
  left: `${params.value.un.x}%`,
  top: `${params.value.un.y}%`,
  fontSize: `${params.value.un.size * overlayScale.value}px`,
  color: params.value.un.color,
  fontWeight: params.value.un.fontWeight ?? '600',
  fontStyle: params.value.un.fontStyle ?? 'italic',
  textShadow: textShadowCss(params.value.un),
}));

const overlayHeardStyle = computed(() => ({
  display: params.value.heard.visible ? 'block' : 'none',
  left: `${params.value.heard.x}%`,
  top: `${params.value.heard.y}%`,
  fontSize: `${params.value.heard.size * overlayScale.value}px`,
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
  fontSize: `${params.value.artistName.size * overlayScale.value}px`,
  color: params.value.artistName.color,
  fontWeight: params.value.artistName.fontWeight ?? '700',
  fontStyle: params.value.artistName.fontStyle ?? 'normal',
  transform: 'translate(-50%, -50%)',
  textShadow: textShadowCss(params.value.artistName),
}));

const displayArtistName = computed(() =>
  entityName.value?.toUpperCase() ?? '',
);

/** For show mode: list of up to 4 artist names for tile overlay */
const showTileNames = computed<string[]>(() => {
  if (entityType.value !== 'show' || !show.value) return [];
  return show.value.artists.slice(0, 4).map((a) => a.name);
});

// ---------------------------------------------------------------------------
// Preview modal
// ---------------------------------------------------------------------------

const previewUrl = ref<string | null>(null);
const showPreview = ref(false);
const previewing = ref(false);

async function openPreview(): Promise<void> {
  if (!cropper || !hasEntity.value) return;
  previewing.value = true;
  try {
    const tiles = entityType.value === 'show' ? showTileNames.value : undefined;
    const { brandedBlob } = await renderPreview(cropper, params.value, entityName.value, tiles);
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
// Save to R2 (artists only)
// ---------------------------------------------------------------------------

const saving = ref(false);

const galleryRef = ref<InstanceType<typeof OverlayGallery> | null>(null);
const overlayControlsRef = ref<InstanceType<typeof OverlayControls> | null>(null);

async function saveToR2(): Promise<void> {
  if (!cropper || !artist.value) return;
  saving.value = true;
  try {
    const { brandedBlob } = await renderPreview(cropper, params.value, entityName.value);
    await artistsApi.saveOverlay(artist.value.id, brandedBlob);
    // Sync active preset if one is selected
    if (currentPresetId.value != null) {
      await artistsApi.setActivePreset(artist.value.id, currentPresetId.value);
    }
    flash.success('Overlay saved to R2');
    galleryRef.value?.refresh();
  } catch (err) {
    flash.error(err instanceof Error ? err.message : 'Save failed');
  } finally {
    saving.value = false;
  }
}

// ---------------------------------------------------------------------------
// Activate Current Parameters (shows only)
// ---------------------------------------------------------------------------

function activateCurrentParams(): void {
  if (!show.value) return;
  if (currentPresetId.value == null) {
    // No preset selected → go straight to "save as new" prompt
    activateChoice.value = 'new';
    activateNewPresetName.value = '';
    showActivateModal.value = true;
  } else {
    // Preset selected → show choice modal
    activateChoice.value = 'overwrite';
    activateNewPresetName.value = '';
    showActivateModal.value = true;
  }
}

const canConfirmActivation = computed(() => {
  if (activateChoice.value === 'overwrite') return true;
  return activateNewPresetName.value.trim().length > 0;
});

async function confirmActivation(): Promise<void> {
  if (!show.value) return;
  activating.value = true;
  try {
    let presetId: number;

    if (activateChoice.value === 'overwrite' && currentPresetId.value != null) {
      // Overwrite the current preset's params
      await presetsApi.update(currentPresetId.value, {
        params: JSON.parse(JSON.stringify(params.value)),
      });
      presetId = currentPresetId.value;
    } else {
      // Create a new preset
      const name = activateNewPresetName.value.trim();
      if (!name) return;
      const created = await presetsApi.create(name, JSON.parse(JSON.stringify(params.value)), 'show');
      presetId = created.id;
      currentPresetId.value = presetId;
    }

    // Set as active preset → triggers server-side cover regeneration
    await showsApi.setActivePreset(show.value.id, presetId);

    // Refresh preset list in OverlayControls so the new preset appears
    await overlayControlsRef.value?.refreshPresets(presetId);

    flash.success('Parameters activated — cover is regenerating');
    showActivateModal.value = false;
  } catch (err) {
    flash.error(err instanceof Error ? err.message : 'Activation failed');
  } finally {
    activating.value = false;
  }
}

// ---------------------------------------------------------------------------
// Gallery active overlay
// ---------------------------------------------------------------------------

const activeKey = ref<string | null>(null);

async function handleSetActive(key: string): Promise<void> {
  if (entityType.value !== 'artist' || !artist.value) return;
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
      const blob = await artistsApi.getImageBlob(id, 'cropped');
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      objectUrl = URL.createObjectURL(blob);
      cropperImg.value.src = objectUrl;
    } catch (err) {
      console.error('Failed to load artist image:', err);
      flash.error('Failed to load artist image');
    }
  }
});

// ---------------------------------------------------------------------------
// Watch show selection
// ---------------------------------------------------------------------------

watch(selectedShowId, async (id) => {
  destroyCropper();
  show.value = null;
  activeKey.value = null;
  if (!id) return;

  await loadShowDetail(id);

  const loaded = show.value as ShowDetail | null;
  if (!loaded) return;

  // Load show cover into cropper via same-origin proxy
  await nextTick();
  if (cropperImg.value) {
    try {
      const blob = await showsApi.getImageBlob(id, 'collage');
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      objectUrl = URL.createObjectURL(blob);
      cropperImg.value.src = objectUrl;
    } catch (err) {
      console.error('Failed to load show cover:', err);
      flash.error('Failed to load show cover image');
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
  // Load both lists upfront so the user can toggle freely
  await Promise.all([loadArtistList(), loadShowList()]);

  // If route has :id param, pre-select the artist (default entity type)
  const routeId = Number(route.params.id);
  if (routeId && !isNaN(routeId)) {
    selectedArtistId.value = routeId;
  }

  window.addEventListener('resize', handleResize);
});

onUnmounted(() => {
  overlayResizeObserver?.disconnect();
  overlayResizeObserver = null;
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
      <div class="entity-selector">
        <div class="entity-toggle">
          <button type="button" :class="['toggle-btn', { active: entityType === 'artist' }]"
            @click="entityType = 'artist'; onEntityTypeChange()">Artist</button>
          <button type="button" :class="['toggle-btn', { active: entityType === 'show' }]"
            @click="entityType = 'show'; onEntityTypeChange()">Show</button>
        </div>
        <div v-if="entityType === 'artist'" class="selector-dropdown">
          <label for="artist-select">Artist</label>
          <select id="artist-select" v-model="selectedArtistId" class="form-input" :disabled="artistsLoading"
            @change="onArtistChange">
            <option :value="null" disabled>Select an artist…</option>
            <option v-for="a in artists" :key="a.id" :value="a.id">
              {{ a.name }}
            </option>
          </select>
        </div>
        <div v-else class="selector-dropdown">
          <label for="show-select">Show</label>
          <select id="show-select" v-model="selectedShowId" class="form-input" :disabled="showsLoading">
            <option :value="null" disabled>Select a show…</option>
            <option v-for="s in shows" :key="s.id" :value="s.id">
              {{ s.title }} ({{ s.date }})
            </option>
          </select>
        </div>
      </div>
    </div>

    <!-- Loading / Error -->
    <div v-if="entityLoading" class="loading-spinner"></div>
    <div v-if="entityError" class="flash-message error">{{ entityError }}</div>

    <!-- Editor -->
    <div v-if="hasEntity" class="editor-layout">
      <!-- Left: Canvas + Actions -->
      <div class="editor-canvas-panel">
        <div ref="cropperWrapper" class="cropper-wrapper">
          <div class="cropper-frame">
            <img ref="cropperImg" :alt="entityType === 'artist' ? 'Original artist image' : 'Show cover image'"
              crossorigin="anonymous" @load="onImageLoad" />

            <!-- Live DOM overlay (positioned by params) -->
            <div ref="overlayEl" class="live-overlay" aria-hidden="true">
              <span class="live-overlay-un" :style="overlayUnStyle">UN</span>
              <span class="live-overlay-heard" :style="overlayHeardStyle">HEARD</span>
              <img class="live-overlay-logo overlay-logo" src="/moafunk.png" alt="" crossorigin="anonymous"
                :style="overlayLogoStyle" />
              <!-- Artist mode: single centred name -->
              <span v-if="entityType === 'artist'" class="live-overlay-name" :style="overlayNameStyle">
                {{ displayArtistName }}
              </span>
              <!-- Show mode: 4 tile-centred artist names -->
              <template v-if="entityType === 'show' && params.artistName.visible">
                <span v-for="(tName, tIdx) in showTileNames" :key="tIdx" class="live-overlay-tile-name" :style="{
                  left: `${(tIdx % 2) * 50 + 25}%`,
                  top: `${(tIdx < 2 ? 0 : 50) + 25}%`,
                  fontSize: `${params.artistName.size * overlayScale}px`,
                  color: params.tileColors?.[tIdx] ?? params.artistName.color,
                  fontWeight: params.artistName.fontWeight ?? '700',
                  fontStyle: params.artistName.fontStyle ?? 'normal',
                  textShadow: tileShadowCss(tIdx),
                }">{{ tName.toUpperCase() }}</span>
              </template>
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
          <button v-if="entityType === 'artist'" type="button" class="btn-primary" :disabled="saving" @click="saveToR2">
            {{ saving ? 'Saving…' : 'Save to R2' }}
          </button>
          <button v-else type="button" class="btn-primary" :disabled="activating" @click="activateCurrentParams">
            {{ activating ? 'Activating…' : 'Activate Current Parameters' }}
          </button>
        </div>

        <!-- No image hint (artist) -->
        <div v-if="entityType === 'artist' && !imageLoaded && !entityLoading && artist" class="no-image-hint">
          <p v-if="!artist.file_urls?.pic_original" class="text-muted">
            This artist has no original image uploaded yet. Upload one from the
            <router-link :to="`/artists/${artist.id}`">artist detail page</router-link>.
          </p>
          <div v-else class="loading-image">
            <div class="loading-spinner small"></div>
            <span class="text-muted">Loading original image…</span>
          </div>
        </div>

        <!-- No image hint (show) -->
        <div v-if="entityType === 'show' && !imageLoaded && !entityLoading && show" class="no-image-hint">
          <p v-if="!show.cover_url" class="text-muted">
            This show has no cover image yet. It will be generated when artists are assigned.
          </p>
          <div v-else class="loading-image">
            <div class="loading-spinner small"></div>
            <span class="text-muted">Loading show cover…</span>
          </div>
        </div>
      </div>

      <!-- Right: Controls panel -->
      <div class="editor-controls-panel">
        <OverlayControls ref="overlayControlsRef" v-model="params" :artist-name="entityName"
          :initial-preset-id="activePresetId" :entity-type="entityType"
          @update:selected-preset-id="currentPresetId = $event" />
      </div>
    </div>

    <!-- Gallery section (artists only) -->
    <div v-if="hasEntity && entityId && entityType === 'artist'" class="gallery-section">
      <h2 class="section-title">Saved Overlays</h2>
      <OverlayGallery v-if="artist" ref="galleryRef" :artist-id="artist.id" :active-key="activeKey"
        @set-active="handleSetActive" />
    </div>

    <!-- Activate Current Parameters modal (shows only) -->
    <BaseModal :open="showActivateModal" title="Activate Current Parameters" size="sm"
      @close="showActivateModal = false">
      <div class="activate-modal-body">
        <p class="activate-description">Save overlay parameters before activating. The show cover will be regenerated
          with
          the selected preset.</p>

        <!-- Option: Overwrite (only shown when a preset is selected) -->
        <label v-if="currentPresetId != null" class="activate-option">
          <input v-model="activateChoice" type="radio" name="activate-choice" value="overwrite" />
          <span>Overwrite current preset</span>
        </label>

        <!-- Option: Save as new -->
        <label class="activate-option">
          <input v-model="activateChoice" type="radio" name="activate-choice" value="new" />
          <span>Save as new preset</span>
        </label>

        <!-- Name input (shown when "new" is selected) -->
        <div v-if="activateChoice === 'new'" class="activate-name-input">
          <input v-model="activateNewPresetName" type="text" class="form-input" placeholder="Preset name…"
            @keydown.enter="canConfirmActivation && confirmActivation()" />
        </div>
      </div>

      <template #footer>
        <button type="button" class="btn-ghost" @click="showActivateModal = false">Cancel</button>
        <button type="button" class="btn-primary" :disabled="!canConfirmActivation || activating"
          @click="confirmActivation">
          {{ activating ? 'Activating…' : 'Activate' }}
        </button>
      </template>
    </BaseModal>

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
            <button v-if="entityType === 'artist'" type="button" class="btn-primary" :disabled="saving"
              @click="saveToR2(); closePreview()">
              {{ saving ? 'Saving…' : 'Save to R2' }}
            </button>
            <button v-else type="button" class="btn-primary" :disabled="activating"
              @click="closePreview(); activateCurrentParams()">
              {{ activating ? 'Activating…' : 'Activate Current Parameters' }}
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

.artist-selector,
.entity-selector {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
}

.entity-toggle {
  display: flex;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  overflow: hidden;
}

.toggle-btn {
  padding: var(--spacing-xs) var(--spacing-md);
  background: var(--color-surface);
  color: var(--color-text-muted);
  border: none;
  cursor: pointer;
  font-size: var(--font-size-sm);
  font-weight: 500;
  transition: all var(--transition-fast);
}

.toggle-btn:not(:last-child) {
  border-right: 1px solid var(--color-border);
}

.toggle-btn.active {
  background: var(--color-primary);
  color: var(--color-primary-text);
}

.toggle-btn:hover:not(.active) {
  background: var(--color-surface-hover);
}

.selector-dropdown {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
}

.selector-dropdown label {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
  white-space: nowrap;
}

.selector-dropdown .form-input,
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

.live-overlay-tile-name {
  position: absolute;
  text-transform: uppercase;
  line-height: 1;
  letter-spacing: 0.02em;
  white-space: nowrap;
  transform: translate(-50%, -50%);
  text-align: center;
  max-width: 43%;
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

/* ---- Activate modal ---- */
.activate-modal-body {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
}

.activate-description {
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
  margin: 0;
}

.activate-option {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  cursor: pointer;
  padding: var(--spacing-xs) 0;
  font-size: var(--font-size-md);
}

.activate-option input[type="radio"] {
  accent-color: var(--color-primary);
}

.activate-name-input {
  padding-left: var(--spacing-lg);
}

.activate-name-input .form-input {
  width: 100%;
  padding: var(--spacing-xs) var(--spacing-sm);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  color: var(--color-text);
  font-size: var(--font-size-sm);
}
</style>
