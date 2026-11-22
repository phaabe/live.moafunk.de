<script setup lang="ts">
import ShowCover from './ShowCover.vue';

// Cover, name and two short lines: the top of the On air and My next show cards.
defineProps<{
  coverUrl: string | null | undefined;
  coverSrcset?: string | null;
  name: string;
  line: string;
  time: string;
}>();
</script>

<template>
  <div class="episode-head">
    <ShowCover :url="coverUrl" :srcset="coverSrcset" sizes="84px" class="head-cover" />
    <div class="head-text">
      <p class="head-name">{{ name }}</p>
      <p class="head-line">{{ line }}</p>
      <p class="head-time">{{ time }}</p>
    </div>
  </div>
</template>

<style scoped>
.episode-head {
  display: flex;
  align-items: center;
  gap: 14px;
}

.head-cover {
  width: 84px;
  height: 84px;
}

.head-text {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.head-text p {
  margin: 0;
  overflow-wrap: anywhere;
}

.head-name {
  font: 700 1.5rem/1.1 var(--font-family);
  color: #fff;
}

.head-line {
  font-size: var(--font-size-sm);
  color: #c8c8d4;
}

.head-time {
  font: 700 13px var(--font-family);
  color: #b0b0c4;
}

@media (max-width: 640px) {
  .head-cover {
    width: 72px;
    height: 72px;
  }

  .head-name {
    font-size: 1.25rem;
  }
}
</style>
