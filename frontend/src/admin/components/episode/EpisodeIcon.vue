<script setup lang="ts">
// Line icons the episode page needs on top of BoardIcon. Decorative: the text
// next to each one names the thing.

const PATHS = {
  edit: 'M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z',
  lock: 'M5 11h14v10H5zM8 11V7a4 4 0 0 1 8 0v4',
  cloud: 'M18 10h-1.3A8 8 0 1 0 9 20h9a5 5 0 0 0 0-10z',
  instagram:
    'M7 2h10a5 5 0 0 1 5 5v10a5 5 0 0 1-5 5H7a5 5 0 0 1-5-5V7a5 5 0 0 1 5-5zM16 11.4A4 4 0 1 1 12.6 8a4 4 0 0 1 3.4 3.4zM17.5 6.5h.01',
  external: 'M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6M15 3h6v6M10 14L21 3',
  down: 'M6 9l6 6 6-6',
  // The tab tiles (#1052): what each item is.
  megaphone: 'M3 10v4a1 1 0 0 0 1 1h3l7 5V4L7 9H4a1 1 0 0 0-1 1zM18 9a4 4 0 0 1 0 6',
  story:
    'M8 2h8a2 2 0 0 1 2 2v16a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2zM12 7.5a2 2 0 1 0 0 4a2 2 0 1 0 0-4zM9.5 16h5',
  postPlay:
    'M5 3h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2zM10 8.5v7l6-3.5z',
  music: 'M9 18V5l12-2v13M9 18a3 3 0 1 1-6 0a3 3 0 1 1 6 0zM21 16a3 3 0 1 1-6 0a3 3 0 1 1 6 0z',
  mic: 'M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3zM19 10v2a7 7 0 0 1-14 0v-2M12 19v4M8 23h8',
  server: 'M4 3h16v6H4zM4 15h16v6H4zM8 6h.01M8 18h.01',
  returnFeed: 'M9 14L4 9l5-5M4 9h10.5a5.5 5.5 0 0 1 0 11H11',
  soundcloud: 'M4 17v-3M7 17v-6M10 17V9M13 17V7.5a5 5 0 0 1 8.4 3.6A3 3 0 0 1 20 17z',
  timer: 'M10 2h4M12 14l3-3M12 6a8 8 0 1 0 0 16a8 8 0 1 0 0-16z',
} as const;

export type EpisodeIconName = keyof typeof PATHS;

withDefaults(defineProps<{ name: keyof typeof PATHS; size?: number }>(), { size: 16 });
</script>

<template>
  <svg
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="2"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    <path :d="PATHS[name]" />
  </svg>
</template>
