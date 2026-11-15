import { createRouter, createWebHashHistory } from 'vue-router';
import { useAuthStore } from './stores/auth';

// Lazy-loaded route components
const LoginPage = () => import('./pages/LoginPage.vue');
const ArtistsPage = () => import('./pages/ArtistsPage.vue');
const ArtistDetailPage = () => import('./pages/ArtistDetailPage.vue');
const ShowsPage = () => import('./pages/ShowsPage.vue');
const ShowCreatePage = () => import('./pages/ShowCreatePage.vue');
const ShowDetailPage = () => import('./pages/ShowDetailPage.vue');
const ShowSeriesPage = () => import('./pages/ShowSeriesPage.vue');
const BookSlotPage = () => import('./pages/BookSlotPage.vue');
const RecordingPage = () => import('./pages/RecordingPage.vue');
const UsersPage = () => import('./pages/UsersPage.vue');
const UserEditPage = () => import('./pages/UserEditPage.vue');
const ChangePasswordPage = () => import('./pages/ChangePasswordPage.vue');
const OverlayEditorPage = () => import('./pages/OverlayEditorPage.vue');
const CalendarPage = () => import('./pages/CalendarPage.vue');
const DashboardPage = () => import('./pages/DashboardPage.vue');
const ConfigPage = () => import('./pages/ConfigPage.vue');

// Host flow pages
const FlowLayout = () => import('./pages/flow/FlowLayout.vue');
const FlowUpload = () => import('./pages/flow/FlowUpload.vue');
const FlowConfirm = () => import('./pages/flow/FlowConfirm.vue');
const FlowLive = () => import('./pages/flow/FlowLive.vue');
const FlowOnAir = () => import('./pages/flow/FlowOnAir.vue');
const FlowNotAssigned = () => import('./pages/flow/FlowNotAssigned.vue');

/**
 * Guard for the stateful flow steps (upload / confirm / live / on-air). These
 * read the selected show from the `useHostFlow` singleton, which is lost on a
 * hard reload or when the step URL is opened directly. Without a show the page
 * renders blank (no date, frozen countdown, no auto-start). Refetch once; if a
 * show still can't be resolved, bounce through the smart `/stream` redirect.
 */
export async function ensureFlowReady(
  _to: unknown,
  _from: unknown,
  next: (target?: string) => void
): Promise<void> {
  const { useHostFlow } = await import('./composables');
  const flow = useHostFlow();
  if (!flow.show.value) {
    await flow.fetchMyShow();
  }
  if (!flow.show.value) {
    next('/stream');
    return;
  }
  next();
}

/**
 * Hosts upload and book on Book Slot now, so the old upload and confirm
 * steps ("Review & Confirm", "Continue to Waiting Room") send them to their
 * episode. Admins and guests keep the steps.
 */
export async function sendHostToEpisode(
  to: unknown,
  from: unknown,
  next: (target?: string) => void
): Promise<void> {
  if (useAuthStore().user?.role !== 'host') {
    await ensureFlowReady(to, from, next);
    return;
  }
  const { useHostFlow } = await import('./composables');
  const flow = useHostFlow();
  if (!flow.show.value) {
    await flow.fetchMyShow();
  }
  next(flow.show.value ? `/shows/${flow.show.value.id}` : '/shows');
}

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
    ...['forgot-password', 'reset-password', 'verify-email'].map((name) => ({
      path: `/${name}`,
      name,
      component: () => import('./pages/RecoveryPage.vue'),
      meta: { requiresAuth: false, recovery: true },
    })),
    {
      path: '/recovery-email',
      name: 'recovery-email',
      component: () => import('./pages/RecoverySettingsPage.vue'),
      meta: { requiresAuth: true, roles: ['host', 'admin'] },
    },
    {
      path: '/dashboard',
      name: 'dashboard',
      component: DashboardPage,
      // Landing page for everyone except guests.
      meta: { requiresAuth: true, roles: ['admin', 'superadmin', 'host'] },
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
      // Merged Shows + My Shows page — everyone except guests.
      meta: { requiresAuth: true, roles: ['admin', 'superadmin', 'host'] },
    },
    {
      // Hosts get the New Show screen; everyone else keeps the ShowWizard.
      path: '/shows/new',
      name: 'show-create',
      component: ShowCreatePage,
      meta: { requiresAuth: true },
    },
    {
      // Book Slot, or Upload Your Set with ?mode=prerecorded.
      path: '/shows/:slug/book',
      name: 'show-book',
      component: BookSlotPage,
      meta: { requiresAuth: true, roles: ['admin', 'superadmin', 'host'] },
    },
    {
      // A Show (series) by its slug, with its episodes. Owner only (API 404).
      path: '/shows/:slug',
      name: 'show-series',
      component: ShowSeriesPage,
      meta: { requiresAuth: true, roles: ['admin', 'superadmin', 'host'] },
    },
    {
      // One episode (dated show). Numbers only, so a slug never lands here.
      path: '/shows/:id(\\d+)',
      name: 'show-detail',
      component: ShowDetailPage,
      // Hosts may view any show and edit the ones they host; guests reach the
      // detail of the show they're assigned to. Finer-grained edit gating lives
      // in ShowDetailPage + the backend (which restricts guests to their show).
      meta: { requiresAuth: true, roles: ['admin', 'superadmin', 'host', 'guest'] },
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
            // The media type is chosen on the show dashboard now, so an
            // unconfigured ("mode") show redirects to its detail page.
            if (flow.currentStep.value === 'mode' && flow.show.value) {
              next(`/shows/${flow.show.value.id}`);
              return;
            }
            const stepRouteMap: Record<string, string> = {
              'not-assigned': '/stream/not-assigned',
              select: '/shows',
              upload: '/stream/upload',
              confirm: '/stream/confirm',
              live: '/stream/live',
              'on-air': '/stream/on-air',
            };
            const target = stepRouteMap[flow.currentStep.value] ?? '/shows';
            next(target);
          },
          // Placeholder component (never actually renders due to redirect)
          component: FlowNotAssigned,
        },
        {
          // Show selection now lives on the merged /shows page.
          path: 'select',
          redirect: '/shows',
        },
        {
          // Legacy redirects: the info and mode steps have been removed.
          path: 'info',
          redirect: '/shows',
        },
        {
          path: 'mode',
          redirect: '/shows',
        },
        {
          path: 'upload',
          name: 'stream-upload',
          component: FlowUpload,
          beforeEnter: sendHostToEpisode,
        },
        {
          path: 'confirm',
          name: 'stream-confirm',
          component: FlowConfirm,
          beforeEnter: sendHostToEpisode,
        },
        {
          path: 'live',
          name: 'stream-live',
          component: FlowLive,
          beforeEnter: ensureFlowReady,
        },
        {
          path: 'on-air',
          name: 'stream-on-air',
          component: FlowOnAir,
          beforeEnter: ensureFlowReady,
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
      // Read-only schedule — everyone except guests.
      meta: { requiresAuth: true, roles: ['admin', 'superadmin', 'host'] },
    },
    {
      path: '/config',
      name: 'config',
      component: ConfigPage,
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

/**
 * Where an authenticated user should land. Guests go to the detail page of the
 * show they're assigned to (falling back to the not-assigned screen); everyone
 * else gets the dashboard.
 */
async function landingRoute(role: string | undefined): Promise<string> {
  if (role === 'guest') {
    const { useHostFlow } = await import('./composables');
    const flow = useHostFlow();
    await flow.fetchMyShow();
    const first = flow.shows.value[0];
    return first ? `/shows/${first.id}` : '/stream/not-assigned';
  }
  return '/dashboard';
}

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

  // Force a self-chosen password before anything else is reachable.
  if (
    authStore.isAuthenticated &&
    authStore.user?.must_change_password &&
    to.name !== 'change-password' &&
    !to.meta.recovery
  ) {
    next({ name: 'change-password' });
    return;
  }

  if (requiredRoles && authStore.user && !requiredRoles.includes(authStore.user.role)) {
    // Insufficient role → send the user to their natural landing page.
    next(await landingRoute(authStore.user.role));
    return;
  }

  if (to.name === 'login' && authStore.isAuthenticated) {
    next(await landingRoute(authStore.user?.role));
    return;
  }

  // Redirect '/' based on role
  if (to.path === '/' && authStore.isAuthenticated) {
    next(await landingRoute(authStore.user?.role));
    return;
  }

  next();
});

export default router;
