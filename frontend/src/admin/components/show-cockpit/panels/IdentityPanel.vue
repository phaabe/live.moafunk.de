<script setup lang="ts">
import { ref } from 'vue';
import type { ShowDetail } from '../../../api';
import { FormInput } from '@shared/components';

/**
 * Hero panel: cover image + title + description with inline editing.
 *
 * Extracted verbatim from ShowDetailPage's external/brunchtime dashboard.
 * Edit state (editMode + form values) stays on the owning page/shell so the
 * combined Save/Cancel header keeps working unchanged; this panel only renders
 * and forwards input.
 */
const props = defineProps<{
  show: ShowDetail;
  /** Whether the combined dashboard edit mode is active. */
  editMode: boolean;
  /** Whether the viewer may edit (gates the cover-replace button). */
  canEdit: boolean;
  /** Cover upload in flight (disables the replace button). */
  uploadingCover: boolean;
  /** Title form value while editing. */
  title: string;
  /** Description form value while editing. */
  description: string;
}>();

const emit = defineEmits<{
  'update:title': [value: string];
  'update:description': [value: string];
  /** A new cover file was picked. */
  'cover-selected': [file: File];
}>();

const coverInput = ref<HTMLInputElement | null>(null);

function onCoverChange(event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  if (file) {
    emit('cover-selected', file);
  }
  input.value = '';
}
</script>

<template>
  <div class="card hero-card">
    <div class="hero-cover">
      <img v-if="show.cover_url" :src="show.cover_url" alt="Cover" class="hero-cover-img" />
      <div v-else class="hero-cover-placeholder">
        <span class="hero-cover-ico">🎙</span>
        <span>COVER</span>
      </div>
      <button
        v-if="canEdit"
        type="button"
        class="cover-replace"
        :disabled="uploadingCover"
        @click="coverInput?.click()"
      >
        ⬆ {{ show.cover_url ? 'Replace' : 'Upload' }}
      </button>
      <input
        ref="coverInput"
        type="file"
        accept="image/*"
        class="upload-input"
        @change="onCoverChange"
      />
    </div>

    <div class="hero-body">
      <p class="dash-label">TITLE</p>
      <FormInput
        v-if="editMode"
        :model-value="props.title"
        required
        @update:model-value="emit('update:title', $event)"
      />
      <h2 v-else class="hero-title">{{ show.title }}</h2>

      <p class="dash-label">DESCRIPTION</p>
      <textarea
        v-if="editMode"
        :value="props.description"
        class="text-field"
        rows="4"
        placeholder="Brief description..."
        @input="emit('update:description', ($event.target as HTMLTextAreaElement).value)"
      ></textarea>
      <p v-else-if="show.description" class="hero-desc">{{ show.description }}</p>
      <p v-else class="empty-state">No description.</p>
    </div>
  </div>
</template>

<style scoped>
.hero-card {
  display: flex;
  gap: var(--spacing-xl);
  align-items: flex-start;
}

.hero-cover {
  position: relative;
  flex: 0 0 auto;
  width: 200px;
  height: 200px;
  border-radius: var(--radius-lg);
  overflow: hidden;
  background: var(--color-surface-alt);
}

.hero-cover-img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.hero-cover-placeholder {
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--spacing-sm);
  color: #fff;
  background: repeating-linear-gradient(
    45deg,
    var(--color-primary) 0,
    var(--color-primary) 10px,
    var(--color-surface-alt) 10px,
    var(--color-surface-alt) 20px
  );
  font-weight: 700;
  letter-spacing: 0.1em;
}

.hero-cover-ico {
  font-size: 2rem;
}

.cover-replace {
  position: absolute;
  left: var(--spacing-sm);
  bottom: var(--spacing-sm);
  border: none;
  border-radius: var(--radius-md);
  padding: 6px 10px;
  font-size: var(--font-size-sm);
  background: rgba(0, 0, 0, 0.6);
  color: #fff;
  cursor: pointer;
}

.cover-replace:disabled {
  opacity: 0.6;
  cursor: default;
}

.hero-body {
  flex: 1 1 auto;
  min-width: 0;
}

.hero-title {
  margin: 0 0 var(--spacing-lg);
  font-size: 1.8em;
  font-weight: 700;
}

.hero-desc {
  margin: 0;
  line-height: var(--line-height-relaxed, 1.6);
  color: var(--color-text);
}

.dash-label {
  margin: 0 0 var(--spacing-xs);
  font-size: var(--font-size-xs);
  font-weight: 700;
  letter-spacing: 0.05em;
  color: var(--color-text-muted);
}

.upload-input {
  display: none;
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

.empty-state {
  color: var(--color-text-muted);
  font-style: italic;
  padding: var(--spacing-md) 0;
}

@media (max-width: 768px) {
  .hero-card {
    flex-direction: column;
  }

  .hero-cover {
    width: 100%;
    height: auto;
    aspect-ratio: 1;
  }
}
</style>
