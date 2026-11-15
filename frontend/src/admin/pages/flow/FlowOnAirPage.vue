<script setup lang="ts">
import { defineAsyncComponent } from 'vue';
import { useHostFlow } from '@admin/composables';

// /stream/on-air: a pre-recorded set keeps today's waiting room and playback
// status; a live episode gets the flow's lean On Air (the router gate only
// lets it in while this tab is live).
const FlowOnAir = defineAsyncComponent(() => import('./FlowOnAir.vue'));
const StudioOnAir = defineAsyncComponent(() => import('./StudioOnAir.vue'));
const flow = useHostFlow();
</script>

<template>
  <FlowOnAir v-if="flow.uploadMode.value === 'prerecorded'" />
  <StudioOnAir v-else />
</template>
