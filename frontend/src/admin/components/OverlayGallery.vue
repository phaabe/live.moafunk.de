<script setup lang="ts">
import { ref, watch, onMounted } from 'vue';
import { artistsApi } from '../api';
import type { OverlayImage } from '../api';

const props = defineProps<{
  artistId: number;
  activeKey: string | null;
}>();

const emit = defineEmits<{
  (e: 'set-active', key: string): void;
}>();

const overlays = ref<OverlayImage[]>([]);
const loading = ref(false);
const error = ref<string | null>(null);

async function fetchOverlays(): Promise<void> {
  loading.value = true;
  error.value = null;
  try {
    const data = await artistsApi.listOverlays(props.artistId);
    // Sort newest first by last_modified
    overlays.value = data.overlays.sort(
      (a, b) => new Date(b.last_modified).getTime() - new Date(a.last_modified).getTime(),
    );
  } catch (err) {
    error.value = err instanceof Error ? err.message : 'Failed to load overlays';
    console.error('Failed to fetch overlays:', err);
  } finally {
    loading.value = false;
  }
}

function formatDate(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleDateString('de-DE', {
      day: '2-digit',
      month: '2-digit',
      year: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    });
  } catch {
    return iso;
  }
}

function openFullSize(url: string): void {
  window.open(url, '_blank');
}

/** Called by parent to refresh after saving a new overlay */
function refresh(): void {
  void fetchOverlays();
}

onMounted(fetchOverlays);

watch(() => props.artistId, fetchOverlays);

defineExpose({ refresh });
</script>

<template>
  <div class="overlay-gallery">
    <div class="gallery-header">
      <h3 class="gallery-title">Saved Overlays</h3>
      <span v-if="overlays.length" class="gallery-count">{{ overlays.length }}</span>
    </div>

    <div v-if="loading" class="gallery-loading">
      <div class="loading-spinner"></div>
      <span>Loading overlays…</span>
    </div>

    <div v-else-if="error" class="gallery-error">
      {{ error }}
      <button class="btn-sm btn-ghost" @click="fetchOverlays">Retry</button>
    </div>

    <div v-else-if="overlays.length === 0" class="gallery-empty">
      No overlay images saved yet for this artist.
    </div>

    <div v-else class="gallery-grid">
      <div
        v-for="overlay in overlays"
        :key="overlay.key"
        class="gallery-item"
        :class="{ active: overlay.key === activeKey }"
      >
        <div class="item-image-wrapper">
          <img
            :src="overlay.url"
            :alt="overlay.key"
            class="item-image"
            loading="lazy"
            @click="openFullSize(overlay.url)"
          />
          <div v-if="overlay.key === activeKey" class="active-badge">Active</div>
        </div>

        <div class="item-meta">
          <span class="item-date">{{ formatDate(overlay.last_modified) }}</span>
        </div>

        <div class="item-actions">
          <button
            class="btn-sm btn-ghost"
            @click="openFullSize(overlay.url)"
          >Preview</button>
          <button
            v-if="overlay.key !== activeKey"
            class="btn-sm btn-primary"
            @click="emit('set-active', overlay.key)"
          >Set Active</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.overlay-gallery {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
}

.gallery-header {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
}

.gallery-title {
  font-size: var(--font-size-md);
  font-weight: var(--font-weight-medium);
  color: var(--color-text);
  margin: 0;
}

.gallery-count {
  background: var(--color-surface-alt);
  color: var(--color-text-muted);
  font-size: var(--font-size-xs);
  padding: 1px 6px;
  border-radius: var(--radius-full);
}

.gallery-loading {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
  padding: var(--spacing-md) 0;
}

.loading-spinner {
  width: 18px;
  height: 18px;
  border: 2px solid rgba(255, 236, 68, 0.3);
  border-top-color: #ffec44;
  border-radius: 50%;
  animation: spin 1s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

.gallery-error {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  color: var(--color-error);
  font-size: var(--font-size-sm);
  padding: var(--spacing-sm);
  background: var(--color-error-bg);
  border-radius: var(--radius-md);
}

.gallery-empty {
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
  padding: var(--spacing-lg) 0;
  text-align: center;
}

.gallery-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
  gap: var(--spacing-md);
}

.gallery-item {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-surface);
  overflow: hidden;
  transition: border-color var(--transition-fast);
}

.gallery-item.active {
  border-color: #ffec44;
  box-shadow: 0 0 0 1px #ffec44;
}

.item-image-wrapper {
  position: relative;
  aspect-ratio: 1;
  overflow: hidden;
  background: #1a1a1a;
}

.item-image {
  width: 100%;
  height: 100%;
  object-fit: cover;
  cursor: pointer;
  transition: transform var(--transition-fast);
}

.item-image:hover {
  transform: scale(1.03);
}

.active-badge {
  position: absolute;
  top: var(--spacing-xs);
  right: var(--spacing-xs);
  background: #ffec44;
  color: #000;
  font-size: 10px;
  font-weight: var(--font-weight-bold);
  padding: 1px 6px;
  border-radius: var(--radius-sm);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.item-meta {
  padding: 0 var(--spacing-sm);
}

.item-date {
  font-size: var(--font-size-xs);
  color: var(--color-text-muted);
}

.item-actions {
  display: flex;
  gap: var(--spacing-xs);
  padding: 0 var(--spacing-sm) var(--spacing-sm);
}

.btn-sm {
  padding: 3px 8px;
  border-radius: var(--radius-sm);
  font-size: var(--font-size-xs);
  cursor: pointer;
  transition: all var(--transition-fast);
  white-space: nowrap;
}

.btn-primary {
  background: var(--color-bg);
  border: 1px solid #ffec44;
  color: #ffec44;
}

.btn-primary:hover {
  opacity: 0.85;
}

.btn-ghost {
  background: none;
  border: 1px solid var(--color-border);
  color: var(--color-text);
}

.btn-ghost:hover {
  background: var(--color-surface-hover);
}
</style>
