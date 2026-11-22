<script setup lang="ts">
import { computed } from 'vue';
import { useRoute } from 'vue-router';
import { useAuthStore } from './stores/auth';
import { NavBar } from '@shared/components';
import FlashToast from './components/FlashToast.vue';
import BrowserWarning from './components/BrowserWarning.vue';
import OnAirBar from './components/live-panel/OnAirBar.vue';

const route = useRoute();
const authStore = useAuthStore();

const showNav = computed(() => authStore.isAuthenticated && route.name !== 'login');
</script>

<template>
  <div class="admin-app">
    <!-- The on-air bar sticks with the nav bar: a warning stays in sight on a long page. -->
    <div v-if="showNav" class="shell-top">
      <NavBar />
      <OnAirBar />
    </div>
    <BrowserWarning v-if="showNav" />
    <main class="main-content">
      <router-view v-slot="{ Component, route }">
        <keep-alive :include="['ShowDetailPage', 'ArtistDetailPage']">
          <component :is="Component" :key="route.fullPath" />
        </keep-alive>
      </router-view>
    </main>
    <FlashToast />
  </div>
</template>

<style scoped>
.shell-top {
  position: sticky;
  top: 0;
  z-index: var(--z-dropdown);
}

.main-content {
  max-width: 1400px;
  margin: 0 auto;
  padding: var(--spacing-lg);
}
</style>
