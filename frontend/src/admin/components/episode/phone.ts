import { onScopeDispose, ref, type Ref } from 'vue';

/** The host pages' phone width (host.css uses the same breakpoint). */
export const PHONE_QUERY = '(max-width: 640px)';

/**
 * True at phone width, following window resizes. False where the browser has
 * no matchMedia (jsdom). Call it inside a component's setup.
 */
export function usePhone(): Readonly<Ref<boolean>> {
  const phone = ref(false);
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return phone;
  const query = window.matchMedia(PHONE_QUERY);
  phone.value = query.matches;
  const update = (e: MediaQueryListEvent) => (phone.value = e.matches);
  query.addEventListener?.('change', update);
  onScopeDispose(() => query.removeEventListener?.('change', update));
  return phone;
}
