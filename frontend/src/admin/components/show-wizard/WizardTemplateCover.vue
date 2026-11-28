<script setup lang="ts">
import { ref } from 'vue';
import { useShowWizard } from '../../composables';
import { BaseButton } from '@shared/components';
import ImageCropDialog from '../ImageCropDialog.vue';

const wizard = useShowWizard();
const fileInput = ref<HTMLInputElement | null>(null);
const cropDialog = ref<InstanceType<typeof ImageCropDialog> | null>(null);

async function onPick(e: Event) {
  const target = e.target as HTMLInputElement;
  const file = target.files?.[0];
  // Lets the same file be picked again after Cancel.
  target.value = '';
  // A cover that is not square opens "Crop cover" first; Cancel keeps the old one.
  const picked = file && (await cropDialog.value?.crop(file));
  if (picked) wizard.setCover(picked);
}

function clearCover() {
  wizard.setCover(null);
  if (fileInput.value) fileInput.value.value = '';
}
</script>

<template>
  <div class="step">
    <h2 class="step-title">Add a cover photo</h2>
    <p class="step-hint">Optional — you can skip this and add one later.</p>

    <div class="cover-area">
      <div v-if="wizard.coverPreviewUrl.value" class="cover-preview">
        <img :src="wizard.coverPreviewUrl.value" alt="Cover preview" />
      </div>
      <div v-else class="cover-placeholder" @click="fileInput?.click()">
        <span>Click to choose an image</span>
      </div>

      <input ref="fileInput" type="file" accept="image/*" class="file-input" @change="onPick" />
      <ImageCropDialog ref="cropDialog" />

      <div class="cover-actions">
        <BaseButton variant="secondary" size="sm" @click="fileInput?.click()">
          {{ wizard.coverPreviewUrl.value ? 'Change' : 'Choose image' }}
        </BaseButton>
        <BaseButton
          v-if="wizard.coverPreviewUrl.value"
          variant="ghost"
          size="sm"
          @click="clearCover"
        >
          Remove
        </BaseButton>
      </div>
    </div>
  </div>
</template>

<style scoped>
.step-title {
  font-size: var(--font-size-lg);
  font-weight: var(--font-weight-bold);
  color: var(--color-text);
  margin: 0 0 var(--spacing-xs);
  text-align: center;
}

.step-hint {
  color: var(--color-text-muted);
  margin: 0 0 var(--spacing-xl);
  text-align: center;
}

.cover-area {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--spacing-md);
}

.cover-preview,
.cover-placeholder {
  width: 220px;
  height: 220px;
  border-radius: var(--radius-md);
  overflow: hidden;
}

.cover-preview img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.cover-placeholder {
  display: flex;
  align-items: center;
  justify-content: center;
  border: 2px dashed var(--color-border);
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
  cursor: pointer;
  text-align: center;
  padding: var(--spacing-md);
}

.cover-placeholder:hover {
  border-color: var(--color-primary);
}

.file-input {
  display: none;
}

.cover-actions {
  display: flex;
  gap: var(--spacing-sm);
}
</style>
