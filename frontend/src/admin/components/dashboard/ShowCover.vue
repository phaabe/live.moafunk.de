<script setup lang="ts">
import BoardIcon from '../BoardIcon.vue';

// A Show's cover on the Dashboard, or the host pages' placeholder without one.
// Decorative: the Show's name always stands next to it. `srcset` and `sizes`
// load a small copy of the cover instead of the full upload.
defineProps<{ url: string | null | undefined; srcset?: string | null; sizes?: string }>();
</script>

<template>
  <span class="cover show-cover">
    <img v-if="url" :src="url" :srcset="srcset ?? undefined" :sizes="sizes" alt="" />
    <BoardIcon v-else name="image" :size="28" />
  </span>
</template>

<style scoped>
.show-cover {
  border-radius: var(--radius-lg);
}
</style>
