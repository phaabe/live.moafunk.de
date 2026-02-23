import { createRouter, createWebHashHistory } from 'vue-router';
import { useAuthStore } from './stores/auth';

// Lazy-loaded route components
const LoginPage = () => import('./pages/LoginPage.vue');
const ArtistsPage = () => import('./pages/ArtistsPage.vue');
const ArtistDetailPage = () => import('./pages/ArtistDetailPage.vue');
const ShowsPage = () => import('./pages/ShowsPage.vue');
const ShowDetailPage = () => import('./pages/ShowDetailPage.vue');
const RecordingPage = () => import('./pages/RecordingPage.vue');
const UsersPage = () => import('./pages/UsersPage.vue');
const UserEditPage = () => import('./pages/UserEditPage.vue');
const ChangePasswordPage = () => import('./pages/ChangePasswordPage.vue');
const OverlayEditorPage = () => import('./pages/OverlayEditorPage.vue');
const CalendarPage = () => import('./pages/CalendarPage.vue');
const DashboardPage = () => import('./pages/DashboardPage.vue');

// Host flow pages
const FlowLayout = () => import('./pages/flow/FlowLayout.vue');
const FlowShowSelect = () => import('./pages/flow/FlowShowSelect.vue');
const FlowSelectMode = () => import('./pages/flow/FlowSelectMode.vue');
const FlowUpload = () => import('./pages/flow/FlowUpload.vue');
const FlowConfirm = () => import('./pages/flow/FlowConfirm.vue');
const FlowLive = () => import('./pages/flow/FlowLive.vue');
const FlowOnAir = () => import('./pages/flow/FlowOnAir.vue');
const FlowNotAssigned = () => import('./pages/flow/FlowNotAssigned.vue');

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
      // Redirect handled by beforeEach guard (role-aware)
      redirect: '/dashboard',
    },
    {
      path: '/dashboard',
      name: 'dashboard',
      component: DashboardPage,
      meta: { requiresAuth: true, roles: ['admin', 'superadmin'] },
    },
    {
      path: '/artists',
      name: 'artists',
      component: ArtistsPage,
      meta: { requiresAuth: true, roles: ['admin', 'superadmin'] },
    },
    {
      path: '/artists/:id',
      name: 'artist-detail',
      component: ArtistDetailPage,
      meta: { requiresAuth: true, roles: ['admin', 'superadmin'] },
    },
    {
      path: '/shows',
      name: 'shows',
      component: ShowsPage,
      meta: { requiresAuth: true, roles: ['admin', 'superadmin'] },
    },
    {
      path: '/shows/:id',
      name: 'show-detail',
      component: ShowDetailPage,
      meta: { requiresAuth: true, roles: ['admin', 'superadmin'] },
    },
    {
      path: '/stream',
      component: FlowLayout,
      meta: { requiresAuth: true },
      children: [
        {
          path: '',
          name: 'stream',
          // Smart redirect: fetch show state, then route to correct child
          beforeEnter: async (_to, _from, next) => {
            const { useHostFlow } = await import('./composables');
            const flow = useHostFlow();
            await flow.fetchMyShow();
            const stepRouteMap: Record<string, string> = {
              'not-assigned': '/stream/not-assigned',
              select: '/stream/select',
              mode: '/stream/mode',
              upload: '/stream/upload',
              confirm: '/stream/confirm',
              live: '/stream/live',
              'on-air': '/stream/on-air',
            };
            const target = stepRouteMap[flow.currentStep.value] ?? '/stream/select';
            next(target);
          },
          // Placeholder component (never actually renders due to redirect)
          component: FlowShowSelect,
        },
        {
          path: 'select',
          name: 'stream-select',
          component: FlowShowSelect,
        },
        {
          // Legacy redirect: info step removed, redirect to mode
          path: 'info',
          redirect: '/stream/mode',
        },
        {
          path: 'mode',
          name: 'stream-mode',
          component: FlowSelectMode,
        },
        {
          path: 'upload',
          name: 'stream-upload',
          component: FlowUpload,
        },
        {
          path: 'confirm',
          name: 'stream-confirm',
          component: FlowConfirm,
        },
        {
          path: 'live',
          name: 'stream-live',
          component: FlowLive,
        },
        {
          path: 'on-air',
          name: 'stream-on-air',
          component: FlowOnAir,
        },
        {
          // Legacy redirects
          path: 'waiting',
          redirect: '/stream/on-air',
        },
        {
          path: 'streaming',
          redirect: '/stream/on-air',
        },
        {
          path: 'not-assigned',
          name: 'stream-not-assigned',
          component: FlowNotAssigned,
        },
      ],
    },
    {
      path: '/recording',
      name: 'recording',
      component: RecordingPage,
      meta: { requiresAuth: true, roles: ['admin', 'superadmin'] },
    },
    {
      path: '/users',
      name: 'users',
      component: UsersPage,
      meta: { requiresAuth: true, roles: ['admin', 'superadmin'] },
    },
    {
      path: '/users/:id',
      name: 'user-edit',
      component: UserEditPage,
      meta: { requiresAuth: true, roles: ['admin', 'superadmin'] },
    },
    {
      path: '/calendar',
      name: 'calendar',
      component: CalendarPage,
      meta: { requiresAuth: true, roles: ['admin', 'superadmin'] },
    },
    {
      path: '/overlay-editor/:id?',
      name: 'overlay-editor',
      component: OverlayEditorPage,
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
    next({ name: 'stream' }); // Redirect to stream if insufficient role
    return;
  }

  if (to.name === 'login' && authStore.isAuthenticated) {
    const defaultRoute = authStore.user?.role === 'host' ? '/stream' : '/dashboard';
    next(defaultRoute);
    return;
  }

  // Redirect '/' based on role
  if (to.path === '/' && authStore.isAuthenticated) {
    const defaultRoute = authStore.user?.role === 'host' ? '/stream' : '/dashboard';
    next(defaultRoute);
    return;
  }

  next();
});

export default router;
