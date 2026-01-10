<script setup lang="ts">
import { computed } from 'vue';
import { useRoute } from 'vue-router';
import { useAuthStore } from './stores/auth';
import { NavBar } from '@shared/components';

const route = useRoute();
const authStore = useAuthStore();

const showNav = computed(() => authStore.isAuthenticated && route.name !== 'login');
</script>

<template>
  <div class="admin-app">
    <NavBar v-if="showNav" />
    <main class="main-content">
      <router-view />
    </main>
  </div>
</template>

<style scoped>
.main-content {
  max-width: 1400px;
  margin: 0 auto;
  padding: var(--spacing-lg);
}
</style>
