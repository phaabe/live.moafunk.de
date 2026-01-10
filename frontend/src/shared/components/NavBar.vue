<script setup lang="ts">
import { computed } from 'vue';
import { useRoute } from 'vue-router';
import { useAuthStore } from '@admin/stores/auth';

const route = useRoute();
const authStore = useAuthStore();

interface NavItem {
  label: string;
  path: string;
  roles?: string[];
}

const navItems: NavItem[] = [
  { label: 'Artists', path: '/artists' },
  { label: 'Shows', path: '/shows' },
  { label: 'Stream', path: '/stream' },
  { label: 'Users', path: '/users', roles: ['admin', 'superadmin'] },
];

const visibleNavItems = computed(() =>
  navItems.filter(
    (item) => !item.roles || (authStore.user && item.roles.includes(authStore.user.role))
  )
);

function isActive(path: string): boolean {
  return route.path.startsWith(path);
}
</script>

<template>
  <nav class="nav-bar">
    <div class="nav-content">
      <div class="nav-brand">
        <img src="/assets/brand/moafunk.png" alt="Moafunk" class="logo" />
        <span class="brand-text">Admin</span>
      </div>

      <div class="nav-links">
        <router-link
          v-for="item in visibleNavItems"
          :key="item.path"
          :to="item.path"
          :class="['nav-link', { active: isActive(item.path) }]"
        >
          {{ item.label }}
        </router-link>
      </div>

      <div class="nav-user">
        <span class="username">{{ authStore.user?.username }}</span>
        <button class="logout-button" @click="authStore.logout()">Logout</button>
      </div>
    </div>
  </nav>
</template>

<style scoped>
.nav-bar {
  background-color: var(--color-surface);
  border-bottom: 3px solid var(--color-primary);
  position: sticky;
  top: 0;
  z-index: var(--z-dropdown);
}

.nav-content {
  display: flex;
  align-items: center;
  justify-content: space-between;
  max-width: 1400px;
  margin: 0 auto;
  padding: var(--spacing-sm) var(--spacing-lg);
}

.nav-brand {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
}

.logo {
  height: 32px;
  width: auto;
}

.brand-text {
  font-size: var(--font-size-lg);
  font-weight: var(--font-weight-bold);
  color: var(--color-primary);
}

.nav-links {
  display: flex;
  gap: var(--spacing-md);
}

.nav-link {
  color: var(--color-text-muted);
  padding: var(--spacing-sm) var(--spacing-md);
  border-radius: var(--radius-md);
  transition: all var(--transition-fast);
}

.nav-link:hover {
  color: var(--color-text);
  background-color: var(--color-surface-alt);
}

.nav-link.active {
  color: var(--color-primary);
  background-color: var(--color-surface-alt);
}

.nav-user {
  display: flex;
  align-items: center;
  gap: var(--spacing-md);
}

.username {
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
}

.logout-button {
  background: none;
  border: 1px solid var(--color-border);
  color: var(--color-text-muted);
  font-family: var(--font-family);
  font-size: var(--font-size-sm);
  padding: var(--spacing-xs) var(--spacing-sm);
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.logout-button:hover {
  color: var(--color-text);
  border-color: var(--color-text-muted);
}
</style>
