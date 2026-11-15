import { defineAsyncComponent, defineComponent, h, type AsyncComponentLoader } from 'vue';
import { useAuthStore } from './stores/auth';

/**
 * One path, two pages: hosts get the flow screen, everyone else keeps
 * today's page. Both load lazily.
 */
export function hostPage(forHost: AsyncComponentLoader, forOthers: AsyncComponentLoader) {
  const Host = defineAsyncComponent(forHost);
  const Others = defineAsyncComponent(forOthers);
  return defineComponent({
    name: 'HostPage',
    setup() {
      const auth = useAuthStore();
      return () => h(auth.user?.role === 'host' ? Host : Others);
    },
  });
}
