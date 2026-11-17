<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue';

// "Show cover · drop an image": click or drop an image. Shows the new image,
// else the current cover.

const file = defineModel<File | null>({ required: true });
const props = withDefaults(defineProps<{ currentUrl?: string | null; size?: number }>(), {
  currentUrl: null,
  size: 220,
});

const input = ref<HTMLInputElement | null>(null);
const dragging = ref(false);
const preview = ref<string | null>(null);

watch(file, (next) => {
  if (preview.value) URL.revokeObjectURL(preview.value);
  preview.value = next ? URL.createObjectURL(next) : null;
});
onUnmounted(() => {
  if (preview.value) URL.revokeObjectURL(preview.value);
});

const shown = computed(() => preview.value ?? props.currentUrl ?? null);

function take(next: File | null | undefined) {
  file.value = next && next.type.startsWith('image/') ? next : null;
}

function onDrop(event: DragEvent) {
  dragging.value = false;
  take(event.dataTransfer?.files?.[0]);
}
</script>

<template>
  <div
    class="cover-drop"
    :class="{ dragging }"
    :style="{ width: `${size}px`, height: `${size}px` }"
    role="button"
    tabindex="0"
    @click="input?.click()"
    @keydown.enter.prevent="input?.click()"
    @dragover.prevent="dragging = true"
    @dragleave="dragging = false"
    @drop.prevent="onDrop"
  >
    <img v-if="shown" :src="shown" alt="Show cover" />
    <span v-else>Show cover · drop an image</span>
  </div>
  <!-- Outside the drop zone, so its click does not bubble back into it. -->
  <input
    ref="input"
    type="file"
    accept="image/*"
    class="file-input"
    @change="take(($event.target as HTMLInputElement).files?.[0])"
  />
</template>

<style scoped>
.cover-drop {
  flex-shrink: 0;
  background-color: var(--color-surface-alt);
  border: 1px dashed var(--color-border);
  border-radius: var(--radius-md);
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  cursor: pointer;
  color: var(--color-text-muted);
  text-align: center;
  font-size: var(--font-size-sm);
}

.cover-drop.dragging {
  border-color: var(--color-primary);
}

.cover-drop img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.file-input {
  display: none;
}
</style>
