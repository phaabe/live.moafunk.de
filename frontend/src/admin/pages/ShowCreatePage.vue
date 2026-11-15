<script setup lang="ts">
import { computed, defineAsyncComponent } from 'vue';
import { useAuthStore } from '../stores/auth';

// /shows/new: hosts create a Show (series) on the flow's New Show screen.
// Admins, superadmins and guests keep today's ShowWizard.
const NewShowPage = defineAsyncComponent(() => import('./NewShowPage.vue'));
const ShowWizard = defineAsyncComponent(() => import('./ShowWizard.vue'));

const auth = useAuthStore();
const isHost = computed(() => auth.user?.role === 'host');
</script>

<template>
  <NewShowPage v-if="isHost" />
  <ShowWizard v-else />
</template>
