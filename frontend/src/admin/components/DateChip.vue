<script setup lang="ts">
import { computed } from 'vue';
import { episodeDay } from '../episodeRow';

// The date at the start of a host-page row: weekday over day and month. Its
// text stays the flow's "So 25.10.", so row checks see the same words.
const props = defineProps<{ date: string; tone?: 'mine' | 'wrapup' | '' }>();
const parts = computed(() => {
  const [weekday, dayMonth] = episodeDay(props.date).split(' ');
  return { weekday, dayMonth };
});
</script>

<template>
  <span :class="['date-chip', tone]"
    ><small>{{ parts.weekday }}</small> <b>{{ parts.dayMonth }}</b></span
  >
</template>
