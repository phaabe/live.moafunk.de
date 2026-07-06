import { ref, watch, onUnmounted, type Ref } from 'vue';

/**
 * Live-chat bridge client (#278): connects to the backend's `/ws/chat`,
 * which fans out the Telegram discussion group and posts host replies
 * back through the bot.
 */

export interface ChatMessage {
  id: number;
  author: string;
  text: string;
  /** Unix seconds. */
  ts: number;
  /** True for replies sent from the panel. */
  host: boolean;
}

export type ChatFrame =
  | { type: 'history'; messages: ChatMessage[] }
  | { type: 'message'; message: ChatMessage }
  | { type: 'error'; error: string };

function isChatMessage(o: unknown): o is ChatMessage {
  if (typeof o !== 'object' || o === null) return false;
  const m = o as Record<string, unknown>;
  return (
    typeof m.id === 'number' &&
    typeof m.author === 'string' &&
    typeof m.text === 'string' &&
    typeof m.ts === 'number' &&
    typeof m.host === 'boolean'
  );
}

/** Parse a server chat frame; null for anything malformed. Pure — unit tested. */
export function parseChatFrame(raw: string): ChatFrame | null {
  let o: unknown;
  try {
    o = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof o !== 'object' || o === null) return null;
  const f = o as Record<string, unknown>;
  if (f.type === 'history' && Array.isArray(f.messages) && f.messages.every(isChatMessage)) {
    return { type: 'history', messages: f.messages };
  }
  if (f.type === 'message' && isChatMessage(f.message)) {
    return { type: 'message', message: f.message };
  }
  if (f.type === 'error' && typeof f.error === 'string') {
    return { type: 'error', error: f.error };
  }
  return null;
}

/** Messages kept client-side (server history is 100; leave headroom for live). */
const MESSAGE_CAP = 200;
const MAX_RECONNECT_ATTEMPTS = 3;

/**
 * Connect to the chat bridge while `active` is true. Per-component (the chat
 * card owns its connection) — unlike the stream socket there is no singleton
 * to protect, and unmount tears the socket down.
 */
export function useLiveChat(active: Ref<boolean>) {
  const messages = ref<ChatMessage[]>([]);
  const connected = ref(false);
  const error = ref<string | null>(null);

  let socket: WebSocket | null = null;
  let reconnectTimeout: ReturnType<typeof setTimeout> | null = null;
  let reconnectAttempts = 0;

  function append(msg: ChatMessage) {
    // Dedupe by id: the server subscribes before snapshotting history, so a
    // message published in between arrives via both paths.
    if (messages.value.some((m) => m.id === msg.id)) return;
    messages.value = [...messages.value.slice(-(MESSAGE_CAP - 1)), msg];
  }

  function connect() {
    disconnect();
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    socket = new WebSocket(`${protocol}//${window.location.host}/ws/chat`);

    socket.onopen = () => {
      connected.value = true;
      error.value = null;
      reconnectAttempts = 0;
    };

    socket.onmessage = (event) => {
      if (typeof event.data !== 'string') return;
      const frame = parseChatFrame(event.data);
      if (!frame) return;
      if (frame.type === 'history') {
        messages.value = frame.messages.slice(-MESSAGE_CAP);
      } else if (frame.type === 'message') {
        append(frame.message);
      } else {
        error.value = frame.error;
      }
    };

    socket.onclose = () => {
      connected.value = false;
      if (active.value && reconnectAttempts < MAX_RECONNECT_ATTEMPTS) {
        reconnectAttempts++;
        const delay = Math.pow(2, reconnectAttempts - 1) * 1000;
        reconnectTimeout = setTimeout(connect, delay);
      }
    };
  }

  function disconnect() {
    if (reconnectTimeout) {
      clearTimeout(reconnectTimeout);
      reconnectTimeout = null;
    }
    if (socket) {
      socket.onclose = null; // no auto-reconnect on deliberate teardown
      socket.close(1000, 'Chat closed');
      socket = null;
    }
    connected.value = false;
  }

  /** Send a host reply. Returns false when the socket isn't open. */
  function send(text: string): boolean {
    const trimmed = text.trim();
    if (!trimmed || !socket || socket.readyState !== WebSocket.OPEN) return false;
    socket.send(trimmed);
    return true;
  }

  watch(
    active,
    (on) => {
      reconnectAttempts = 0;
      if (on) connect();
      else disconnect();
    },
    { immediate: true }
  );

  onUnmounted(disconnect);

  return { messages, connected, error, send };
}
