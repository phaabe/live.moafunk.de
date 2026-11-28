<script setup lang="ts">
import { onUnmounted, ref } from 'vue';
import Cropper from 'cropperjs';
import 'cropperjs/dist/cropper.css';
import { BaseButton, BaseModal } from '@shared/components';

// "Crop cover" / "Crop photo": an image that does not have the shape it is
// shown in opens this dialog first. Callers use `crop()` through a template ref.

withDefaults(defineProps<{ title?: string }>(), { title: 'Crop cover' });

// Largest side of a cropped image. Big enough for any use, small enough for
// the canvas limit of phone browsers.
const MAX_SIDE = 3000;
// An image this close to the shape is taken as it is.
const TOLERANCE = 0.01;

const url = ref<string | null>(null);
const img = ref<HTMLImageElement | null>(null);
const error = ref<string | null>(null);
const aspect = ref(1);
let source: File | null = null;
let cropper: Cropper | null = null;
let finish: ((file: File | null) => void) | null = null;

function imageSize(src: string): Promise<{ width: number; height: number }> {
  return new Promise((resolve, reject) => {
    const probe = new Image();
    probe.onload = () => resolve({ width: probe.naturalWidth, height: probe.naturalHeight });
    probe.onerror = () => reject(new Error('Could not read the image'));
    probe.src = src;
  });
}

/**
 * The image cut to `wanted` (width / height). The same file when it already
 * has that shape or the browser cannot read it (for example HEIC in Chrome);
 * null on Cancel.
 */
async function crop(file: File, wanted = 1): Promise<File | null> {
  const next = URL.createObjectURL(file);
  try {
    const { width, height } = await imageSize(next);
    if (Math.abs(width / height / wanted - 1) < TOLERANCE) {
      URL.revokeObjectURL(next);
      return file;
    }
  } catch {
    URL.revokeObjectURL(next);
    return file;
  }
  close(null);
  source = file;
  aspect.value = wanted;
  url.value = next;
  return new Promise((resolve) => (finish = resolve));
}

defineExpose({ crop });

function start() {
  if (!img.value) return;
  cropper?.destroy();
  cropper = new Cropper(img.value, {
    aspectRatio: aspect.value,
    viewMode: 1,
    dragMode: 'move',
    autoCropArea: 1,
    background: false,
    toggleDragModeOnDblclick: false,
  });
}

function close(result: File | null) {
  cropper?.destroy();
  cropper = null;
  if (url.value) URL.revokeObjectURL(url.value);
  url.value = null;
  source = null;
  error.value = null;
  const done = finish;
  finish = null;
  done?.(result);
}
onUnmounted(() => close(null));

async function useCrop() {
  if (!cropper || !source) return;
  // PNG keeps its transparency; everything else becomes JPEG.
  const type = source.type === 'image/png' ? 'image/png' : 'image/jpeg';
  const name = `${source.name.replace(/\.[^.]*$/, '')}.${type === 'image/png' ? 'png' : 'jpg'}`;
  const canvas = cropper.getCroppedCanvas({
    maxWidth: MAX_SIDE,
    maxHeight: MAX_SIDE,
    imageSmoothingQuality: 'high',
  });
  const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, type, 0.92));
  if (!blob) {
    error.value = 'Could not crop this image. Try another one.';
    return;
  }
  close(new File([blob], name, { type }));
}
</script>

<template>
  <BaseModal :open="url !== null" :title="title" @close="close(null)">
    <p class="crop-hint">
      {{
        aspect === 1
          ? 'Drag and zoom to pick the square part.'
          : 'Drag and zoom to pick the part to show.'
      }}
    </p>
    <div class="crop-area">
      <img v-if="url" ref="img" :src="url" alt="Image to crop" @load="start" />
    </div>
    <p v-if="error" class="crop-error" role="alert">{{ error }}</p>
    <template #footer>
      <BaseButton variant="ghost" @click="close(null)">Cancel</BaseButton>
      <BaseButton @click="useCrop">Use crop</BaseButton>
    </template>
  </BaseModal>
</template>

<style scoped>
.crop-hint {
  margin: 0 0 var(--spacing-md);
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
}

/* Cropper takes its size from this box. */
.crop-area {
  height: min(60vh, 420px);
}

.crop-area img {
  display: block;
  max-width: 100%;
}

.crop-error {
  margin: var(--spacing-sm) 0 0;
  color: var(--color-error);
  font-size: var(--font-size-sm);
}
</style>
