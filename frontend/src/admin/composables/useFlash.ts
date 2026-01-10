import { ref } from 'vue';

export interface FlashMessage {
  id: number;
  type: 'success' | 'error' | 'info';
  message: string;
}

const messages = ref<FlashMessage[]>([]);
let nextId = 1;

export function useFlash() {
  function show(type: FlashMessage['type'], message: string, duration = 5000) {
    const id = nextId++;
    messages.value.push({ id, type, message });

    if (duration > 0) {
      setTimeout(() => {
        dismiss(id);
      }, duration);
    }

    return id;
  }

  function success(message: string, duration = 5000) {
    return show('success', message, duration);
  }

  function error(message: string, duration = 0) {
    // Errors don't auto-dismiss by default
    return show('error', message, duration);
  }

  function info(message: string, duration = 5000) {
    return show('info', message, duration);
  }

  function dismiss(id: number) {
    const index = messages.value.findIndex((m) => m.id === id);
    if (index > -1) {
      messages.value.splice(index, 1);
    }
  }

  function clear() {
    messages.value = [];
  }

  return {
    messages,
    show,
    success,
    error,
    info,
    dismiss,
    clear,
  };
}
