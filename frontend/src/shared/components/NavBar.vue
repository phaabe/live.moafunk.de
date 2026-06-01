<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { useRoute } from 'vue-router';
import { useAuthStore } from '@admin/stores/auth';

const route = useRoute();
const authStore = useAuthStore();

const mobileMenuOpen = ref(false);

interface NavItem {
  label: string;
  path: string;
  roles?: string[];
  category: string;
}

const navItems: NavItem[] = [
  {
    label: 'Dashboard',
    path: '/dashboard',
    roles: ['admin', 'superadmin', 'host'],
    category: 'Radio',
  },
  {
    label: 'Calendar',
    path: '/calendar',
    roles: ['admin', 'superadmin', 'host'],
    category: 'Radio',
  },
  { label: 'Shows', path: '/shows', roles: ['admin', 'superadmin', 'host'], category: 'Radio' },
  { label: 'Artists', path: '/artists', roles: ['admin', 'superadmin'], category: 'UNHEARD' },
  {
    label: 'Overlay',
    path: '/overlay-editor',
    roles: ['admin', 'superadmin'],
    category: 'UNHEARD',
  },
  { label: 'Recording', path: '/recording', roles: ['admin', 'superadmin'], category: 'UNHEARD' },
  { label: 'Users', path: '/users', roles: ['admin', 'superadmin'], category: 'Configuration' },
  { label: 'Config', path: '/config', roles: ['admin', 'superadmin'], category: 'Configuration' },
];

// Preserve category order
const categoryOrder = ['Radio', 'UNHEARD', 'Configuration'];

// Categories whose items are rendered inline (always visible, side by side)
// on desktop instead of behind a hover dropdown.
const flatCategories = ['Radio'];

const visibleNavItems = computed(() =>
  navItems.filter(
    (item) => !item.roles || (authStore.user && item.roles.includes(authStore.user.role))
  )
);

const groupedNavItems = computed(() => {
  const groups: Record<string, NavItem[]> = {};
  visibleNavItems.value.forEach((item) => {
    if (!groups[item.category]) {
      groups[item.category] = [];
    }
    groups[item.category].push(item);
  });
  // Return in defined order
  const ordered: Record<string, NavItem[]> = {};
  categoryOrder.forEach((cat) => {
    if (groups[cat]) ordered[cat] = groups[cat];
  });
  return ordered;
});

function isActive(path: string): boolean {
  return route.path.startsWith(path);
}

// Mobile: track which section is expanded (click to toggle)
const mobileExpandedSection = ref<string | null>(null);

function toggleMobileSection(category: string) {
  mobileExpandedSection.value = mobileExpandedSection.value === category ? null : category;
}

function toggleMobileMenu() {
  mobileMenuOpen.value = !mobileMenuOpen.value;
}

function closeMobileMenu() {
  mobileMenuOpen.value = false;
  mobileExpandedSection.value = null;
}

// Close mobile menu on route change
watch(
  () => route.path,
  () => {
    closeMobileMenu();
  }
);
</script>

<template>
  <nav class="nav-bar">
    <div class="nav-content">
      <div class="nav-brand">
        <img src="/assets/brand/moafunk.png" alt="Moafunk" class="logo" />
        <span class="brand-text">Admin</span>
      </div>

      <!-- Desktop nav links -->
      <div class="nav-links desktop-only">
        <template v-for="(items, category) in groupedNavItems" :key="category">
          <!-- Flat category: items always visible, side by side -->
          <div v-if="flatCategories.includes(category as string)" class="nav-group-flat">
            <router-link
              v-for="item in items"
              :key="item.path"
              :to="item.path"
              :class="['nav-link', { active: isActive(item.path) }]"
            >
              {{ item.label }}
            </router-link>
          </div>
          <!-- Collapsible category: label + hover dropdown -->
          <div v-else class="nav-group">
            <span class="nav-category">{{ category }}</span>
            <div class="nav-group-items">
              <router-link
                v-for="item in items"
                :key="item.path"
                :to="item.path"
                :class="['nav-link', { active: isActive(item.path) }]"
              >
                {{ item.label }}
              </router-link>
            </div>
          </div>
          <span
            v-if="
              Object.keys(groupedNavItems).indexOf(category as string) <
              Object.keys(groupedNavItems).length - 1
            "
            class="nav-delimiter"
            >|</span
          >
        </template>
      </div>

      <!-- Desktop user info -->
      <div class="nav-user desktop-only">
        <div class="user-dropdown">
          <span class="username">{{ authStore.user?.username }}</span>
          <div class="user-dropdown-menu">
            <router-link to="/change-password" class="dropdown-link"> Change Password </router-link>
            <button class="dropdown-logout" @click="authStore.logout()">Logout</button>
          </div>
        </div>
      </div>

      <!-- Mobile hamburger button -->
      <button
        class="hamburger mobile-only"
        :class="{ open: mobileMenuOpen }"
        @click="toggleMobileMenu"
        aria-label="Toggle menu"
      >
        <span class="hamburger-line"></span>
        <span class="hamburger-line"></span>
        <span class="hamburger-line"></span>
      </button>
    </div>

    <!-- Mobile menu overlay -->
    <div v-if="mobileMenuOpen" class="mobile-menu-overlay" @click="closeMobileMenu"></div>

    <!-- Mobile menu -->
    <div class="mobile-menu" :class="{ open: mobileMenuOpen }">
      <div class="mobile-menu-header">
        <button class="close-button" @click="closeMobileMenu" aria-label="Close menu">✕</button>
      </div>
      <div class="mobile-menu-links">
        <template v-for="(items, category) in groupedNavItems" :key="category">
          <div class="mobile-nav-group">
            <button class="mobile-nav-category" @click="toggleMobileSection(category as string)">
              {{ category }}
              <span
                class="mobile-nav-chevron"
                :class="{ expanded: mobileExpandedSection === category }"
                >›</span
              >
            </button>
            <div class="mobile-nav-items" :class="{ expanded: mobileExpandedSection === category }">
              <router-link
                v-for="item in items"
                :key="item.path"
                :to="item.path"
                :class="['mobile-nav-link', { active: isActive(item.path) }]"
                @click="closeMobileMenu"
              >
                {{ item.label }}
              </router-link>
            </div>
          </div>
        </template>
      </div>
      <div class="mobile-menu-footer">
        <span class="mobile-username">{{ authStore.user?.username }}</span>
        <router-link to="/change-password" class="mobile-change-password" @click="closeMobileMenu">
          Change Password
        </router-link>
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
  flex-shrink: 0;
}

.brand-text {
  font-size: var(--font-size-lg);
  font-weight: var(--font-weight-bold);
  color: var(--color-primary);
}

.nav-links {
  display: flex;
  gap: var(--spacing-md);
  align-items: center;
}

/* Flat group: items rendered inline, always visible */
.nav-group-flat {
  display: flex;
  align-items: center;
  gap: var(--spacing-xs);
}

/* ===== Desktop collapsible nav groups ===== */
.nav-group {
  position: relative;
  display: flex;
  align-items: center;
}

.nav-category {
  color: var(--color-primary);
  font-size: var(--font-size-base);
  font-weight: var(--font-weight-bold);
  text-transform: uppercase;
  opacity: 0.8;
  letter-spacing: 0.05em;
  user-select: none;
  cursor: default;
  padding: var(--spacing-sm) var(--spacing-md);
}

.nav-group-items {
  position: absolute;
  top: 100%;
  left: 0;
  display: flex;
  flex-direction: column;
  min-width: 160px;
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
  padding: var(--spacing-xs) 0;
  opacity: 0;
  visibility: hidden;
  transform: translateY(-4px);
  transition:
    opacity 0.2s ease,
    transform 0.2s ease,
    visibility 0.2s ease;
  z-index: calc(var(--z-dropdown) + 1);
}

.nav-group:hover .nav-group-items {
  opacity: 1;
  visibility: visible;
  transform: translateY(0);
}

.nav-delimiter {
  color: var(--color-border);
  user-select: none;
}

.nav-link {
  color: var(--color-text-muted);
  padding: var(--spacing-sm) var(--spacing-md);
  border-radius: var(--radius-md);
  transition: all var(--transition-fast);
  white-space: nowrap;
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

.user-dropdown {
  position: relative;
}

.username {
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
  cursor: pointer;
  padding: var(--spacing-xs) var(--spacing-sm);
  border-radius: var(--radius-sm);
  transition: all var(--transition-fast);
}

.username:hover {
  color: var(--color-text);
  background-color: var(--color-surface-alt);
}

.user-dropdown-menu {
  position: absolute;
  top: 100%;
  right: 0;
  margin-top: var(--spacing-xs);
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
  min-width: 180px;
  opacity: 0;
  visibility: hidden;
  transform: translateY(-10px);
  transition: all var(--transition-fast);
  z-index: calc(var(--z-dropdown) + 1);
}

.user-dropdown:hover .user-dropdown-menu {
  opacity: 1;
  visibility: visible;
  transform: translateY(0);
}

.dropdown-link {
  display: block;
  padding: var(--spacing-sm) var(--spacing-md);
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
  transition: all var(--transition-fast);
}

.dropdown-link:hover {
  color: var(--color-text);
  background-color: var(--color-surface-alt);
}

.dropdown-logout {
  display: block;
  width: 100%;
  padding: var(--spacing-sm) var(--spacing-md);
  background: none;
  border: none;
  border-top: 1px solid var(--color-border);
  color: var(--color-text-muted);
  font-family: var(--font-family);
  font-size: var(--font-size-sm);
  text-align: left;
  cursor: pointer;
  transition: all var(--transition-fast);
}

.dropdown-logout:hover {
  color: var(--color-text);
  background-color: var(--color-surface-alt);
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

/* ===== Mobile menu styles ===== */
.hamburger {
  display: none;
  flex-direction: column;
  justify-content: center;
  gap: 5px;
  width: 32px;
  height: 32px;
  background: none;
  border: none;
  cursor: pointer;
  padding: 4px;
}

.hamburger-line {
  display: block;
  width: 100%;
  height: 2px;
  background-color: var(--color-text);
  transition: all 0.3s ease;
  transform-origin: center;
}

.hamburger.open .hamburger-line:nth-child(1) {
  transform: translateY(7px) rotate(45deg);
}

.hamburger.open .hamburger-line:nth-child(2) {
  opacity: 0;
}

.hamburger.open .hamburger-line:nth-child(3) {
  transform: translateY(-7px) rotate(-45deg);
}

.mobile-menu-overlay {
  display: none;
  position: fixed;
  inset: 0;
  background-color: rgba(0, 0, 0, 0.5);
  z-index: calc(var(--z-dropdown) - 1);
}

.mobile-menu {
  display: none;
  position: fixed;
  top: 0;
  right: -280px;
  width: 280px;
  height: 100vh;
  background-color: var(--color-surface);
  z-index: calc(var(--z-dropdown) + 1);
  flex-direction: column;
  transition: right 0.3s ease;
  box-shadow: -2px 0 10px rgba(0, 0, 0, 0.3);
}

.mobile-menu.open {
  right: 0;
}

.mobile-menu-header {
  display: flex;
  justify-content: flex-end;
  padding: var(--spacing-md);
  border-bottom: 1px solid var(--color-border);
}

.close-button {
  background: none;
  border: none;
  color: var(--color-text-muted);
  font-size: 24px;
  cursor: pointer;
  padding: var(--spacing-xs);
  line-height: 1;
  transition: color var(--transition-fast);
  width: 36px;
  height: 36px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.close-button:hover {
  color: var(--color-text);
}

.mobile-menu-links {
  display: flex;
  flex-direction: column;
  padding: var(--spacing-lg);
  padding-top: var(--spacing-md);
  gap: var(--spacing-sm);
  flex: 1;
}

.mobile-nav-group {
  display: flex;
  flex-direction: column;
}

.mobile-nav-category {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  background: none;
  border: none;
  color: var(--color-primary);
  font-family: var(--font-family);
  font-size: var(--font-size-sm);
  font-weight: var(--font-weight-medium);
  text-transform: uppercase;
  opacity: 0.7;
  letter-spacing: 0.05em;
  padding: var(--spacing-sm) var(--spacing-md);
  cursor: pointer;
  border-radius: var(--radius-md);
  transition: all var(--transition-fast);
}

.mobile-nav-category:hover {
  opacity: 1;
  background-color: var(--color-surface-alt);
}

.mobile-nav-chevron {
  font-size: var(--font-size-lg);
  transition: transform 0.2s ease;
  transform: rotate(0deg);
}

.mobile-nav-chevron.expanded {
  transform: rotate(90deg);
}

.mobile-nav-items {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
  max-height: 0;
  overflow: hidden;
  transition: max-height 0.3s ease;
}

.mobile-nav-items.expanded {
  max-height: 300px;
  padding-top: var(--spacing-xs);
}

.mobile-nav-link {
  color: var(--color-text-muted);
  padding: var(--spacing-md);
  padding-left: var(--spacing-xl);
  border-radius: var(--radius-md);
  font-size: var(--font-size-lg);
  transition: all var(--transition-fast);
}

.mobile-nav-link:hover {
  color: var(--color-text);
  background-color: var(--color-surface-alt);
}

.mobile-nav-link.active {
  color: var(--color-primary);
  background-color: var(--color-surface-alt);
}

.mobile-menu-footer {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
  padding: var(--spacing-lg);
  border-top: 1px solid var(--color-border);
}

.mobile-username {
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
}

.mobile-change-password {
  color: var(--color-text-muted);
  font-size: var(--font-size-sm);
  padding: var(--spacing-xs) 0;
  transition: color var(--transition-fast);
}

.mobile-change-password:hover {
  color: var(--color-text);
}

.mobile-menu-footer .logout-button {
  width: 100%;
  padding: var(--spacing-sm);
}

/* Responsive breakpoints */
.desktop-only {
  display: flex;
}

.mobile-only {
  display: none;
}

@media (max-width: 768px) {
  .desktop-only {
    display: none !important;
  }

  .mobile-only {
    display: flex !important;
  }

  .mobile-menu-overlay {
    display: block;
  }

  .mobile-menu {
    display: flex;
  }

  .nav-content {
    padding: var(--spacing-sm) var(--spacing-md);
  }
}
</style>
