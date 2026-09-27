import { getCurrentInstance, onActivated, onDeactivated, onScopeDispose, ref } from 'vue';

/** Refresh server metadata without replacing editable page state. */
export function useMetadataRefresh<T>(load: () => Promise<T>, apply: (value: T) => void) {
  const error = ref<string | null>(null);
  let active = true;
  let generation = 0;

  async function refresh(): Promise<void> {
    if (!active) return;
    const request = ++generation;
    try {
      const value = await load();
      if (!active || request !== generation) return;
      apply(value);
      error.value = null;
    } catch (cause) {
      if (!active || request !== generation) return;
      error.value = cause instanceof Error ? cause.message : 'Failed to refresh host login status';
    }
  }

  const onFocus = () => {
    void refresh();
  };
  let timer: ReturnType<typeof setInterval> | undefined;
  function pause() {
    active = false;
    generation++;
    if (timer !== undefined) clearInterval(timer);
    timer = undefined;
    window.removeEventListener('focus', onFocus);
  }
  function resume() {
    const wasPaused = !active;
    active = true;
    if (timer === undefined) {
      timer = setInterval(onFocus, 30_000);
      window.addEventListener('focus', onFocus);
    }
    if (wasPaused) void refresh();
  }
  resume();
  if (getCurrentInstance()) {
    onDeactivated(pause);
    onActivated(resume);
  }
  onScopeDispose(pause);

  return { refresh, error };
}
