import { createRouter, createWebHashHistory } from 'vue-router';
import { useAuthStore } from './stores/auth';

// Lazy-loaded route components
const LoginPage = () => import('./pages/LoginPage.vue');
const ArtistsPage = () => import('./pages/ArtistsPage.vue');
const ArtistDetailPage = () => import('./pages/ArtistDetailPage.vue');
const ShowsPage = () => import('./pages/ShowsPage.vue');
const ShowDetailPage = () => import('./pages/ShowDetailPage.vue');
const StreamPage = () => import('./pages/StreamPage.vue');
const UsersPage = () => import('./pages/UsersPage.vue');
const ChangePasswordPage = () => import('./pages/ChangePasswordPage.vue');

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: LoginPage,
      meta: { requiresAuth: false },
    },
    {
      path: '/',
      redirect: '/artists',
    },
    {
      path: '/artists',
      name: 'artists',
      component: ArtistsPage,
      meta: { requiresAuth: true },
    },
    {
      path: '/artists/:id',
      name: 'artist-detail',
      component: ArtistDetailPage,
      meta: { requiresAuth: true },
    },
    {
      path: '/shows',
      name: 'shows',
      component: ShowsPage,
      meta: { requiresAuth: true },
    },
    {
      path: '/shows/:id',
      name: 'show-detail',
      component: ShowDetailPage,
      meta: { requiresAuth: true },
    },
    {
      path: '/stream',
      name: 'stream',
      component: StreamPage,
      meta: { requiresAuth: true },
    },
    {
      path: '/users',
      name: 'users',
      component: UsersPage,
      meta: { requiresAuth: true, roles: ['admin', 'superadmin'] },
    },
    {
      path: '/change-password',
      name: 'change-password',
      component: ChangePasswordPage,
      meta: { requiresAuth: true },
    },
  ],
});

// Navigation guard for auth
router.beforeEach(async (to, _from, next) => {
  const authStore = useAuthStore();

  // Try to restore session on first load
  if (!authStore.initialized) {
    await authStore.checkAuth();
  }

  const requiresAuth = to.meta.requiresAuth !== false;
  const requiredRoles = to.meta.roles as string[] | undefined;

  if (requiresAuth && !authStore.isAuthenticated) {
    next({ name: 'login', query: { redirect: to.fullPath } });
    return;
  }

  if (requiredRoles && authStore.user && !requiredRoles.includes(authStore.user.role)) {
    next({ name: 'artists' }); // Redirect to default page if insufficient role
    return;
  }

  if (to.name === 'login' && authStore.isAuthenticated) {
    next({ name: 'artists' });
    return;
  }

  next();
});

export default router;
