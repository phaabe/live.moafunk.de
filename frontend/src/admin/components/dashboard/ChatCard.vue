<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue';
import { useLiveChat, type ChatMessage } from '../../composables/useLiveChat';
import BoardIcon from '../BoardIcon.vue';

// Dashboard card "Chat" (#1049): the hosts' Telegram group through the chat
// bridge (/ws/chat), the same one as the Live Panel. Small and simple: the
// latest messages and one line to write.
const props = defineProps<{ username: string | undefined }>();

const { messages, connected, error, send } = useLiveChat(ref(true));

const draft = ref('');
const sendError = ref<string | null>(null);
const listEl = ref<HTMLElement | null>(null);
const problem = computed(() => sendError.value ?? error.value);

/** The user's own replies; the bridge sends them with the username as author. */
function mine(m: ChatMessage): boolean {
  return m.host && !!props.username && m.author === props.username;
}

const BERLIN = { timeZone: 'Europe/Berlin' } as const;
const dayOf = (at: Date) => at.toLocaleDateString('sv-SE', BERLIN);

/** "10:20" today, "18.10. 10:20" on another day (Berlin time, like the episodes). */
function when(ts: number): string {
  const at = new Date(ts * 1000);
  const time = at.toLocaleTimeString('de-DE', { ...BERLIN, hour: '2-digit', minute: '2-digit' });
  if (dayOf(at) === dayOf(new Date())) return time;
  const day = at.toLocaleDateString('de-DE', { ...BERLIN, day: '2-digit', month: '2-digit' });
  return `${day} ${time}`;
}

function onSend() {
  const text = draft.value.trim();
  if (!text) return;
  if (send(text)) {
    draft.value = '';
    sendError.value = null;
  } else {
    sendError.value = 'Not connected. The message was not sent.';
  }
}

// Follow new messages unless the reader scrolled up.
watch(
  () => messages.value.length,
  async () => {
    const el = listEl.value;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    await nextTick();
    if (nearBottom) el.scrollTop = el.scrollHeight;
  }
);
</script>

<template>
  <section class="board-card chat-card">
    <div class="board-card-head">
      <h2 class="board-card-title"><BoardIcon name="chat" />Chat</h2>
      <span class="chat-sub">Telegram group</span>
    </div>
    <div ref="listEl" class="chat-messages">
      <p v-if="!messages.length" class="chat-empty">
        {{ connected ? 'No messages yet.' : 'The chat is not connected.' }}
      </p>
      <ul v-else class="chat-list" aria-label="Messages">
        <li v-for="m in messages" :key="`${m.id}-${m.ts}`" :class="['chat-msg', { mine: mine(m) }]">
          <span class="chat-meta"
            ><span class="chat-author">{{ mine(m) ? `${m.author} · you` : m.author }}</span>
            <span class="chat-time">{{ when(m.ts) }}</span></span
          >
          <span class="chat-text">{{ m.text }}</span>
        </li>
      </ul>
    </div>
    <p v-if="problem" class="chat-problem" role="alert">{{ problem }}</p>
    <form class="chat-compose" @submit.prevent="onSend">
      <label class="sr-only" for="dashboard-chat-input">Message to the group</label>
      <input
        id="dashboard-chat-input"
        v-model="draft"
        type="text"
        class="chat-input"
        placeholder="Write to the group"
        maxlength="4096"
        :disabled="!connected"
      />
      <button
        type="submit"
        class="chat-send"
        aria-label="Send"
        :disabled="!connected || !draft.trim()"
      >
        <BoardIcon name="send" />
      </button>
    </form>
  </section>
</template>

<style scoped>
.chat-card {
  display: flex;
  flex-direction: column;
  width: 100%;
  min-width: 0;
}

.host-page .chat-card .board-card-head {
  justify-content: flex-start;
  flex-wrap: wrap;
  gap: 10px;
}

.chat-sub {
  margin-left: auto;
  font: 600 12px var(--font-ui);
  color: #a0a0b4;
}

/* Beside the episodes the list fills the card's height and scrolls; it never
   makes the column taller. */
.chat-messages {
  flex: 1 1 0;
  min-height: 240px;
  overflow-y: auto;
  padding: 16px 20px;
}

.chat-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.chat-msg {
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.chat-meta {
  display: flex;
  align-items: baseline;
  gap: 8px;
}

.chat-author {
  font-weight: 700;
  font-size: var(--font-size-sm);
  color: #fff;
}

.chat-msg.mine .chat-author {
  color: var(--color-primary);
}

.chat-time {
  font: 12px var(--font-family);
  color: #a0a0b4;
}

.chat-text {
  font-size: var(--font-size-sm);
  color: #d4d4de;
  overflow-wrap: anywhere;
}

.chat-empty {
  margin: 0;
  color: #a0a0b4;
  font-size: var(--font-size-sm);
  text-align: center;
}

.chat-problem {
  margin: 0;
  padding: 0 20px 8px;
  color: var(--color-error);
  font-size: var(--font-size-sm);
}

.chat-compose {
  display: flex;
  gap: 8px;
  padding: 12px;
  border-top: 1px solid #2a2a42;
}

.chat-input {
  flex: 1;
  min-width: 0;
  height: 44px;
  padding: 0 12px;
  border: 1px solid #34344c;
  border-radius: var(--radius-lg);
  background: #06060f;
  color: #eee;
  font: 15px var(--font-ui);
}

.chat-input:focus {
  outline: none;
  border-color: var(--color-primary);
}

.chat-send {
  flex: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 44px;
  height: 44px;
  border: 1px solid #4a4a60;
  border-radius: var(--radius-lg);
  background: transparent;
  color: var(--color-primary);
  cursor: pointer;
}

.chat-send:disabled {
  color: #5c5c78;
  cursor: default;
}

.chat-send:focus-visible {
  outline: 2px solid var(--color-primary);
  outline-offset: 2px;
}

.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
}

@media (max-width: 640px) {
  .chat-messages {
    flex: none;
    min-height: 120px;
    max-height: 320px;
    padding: 14px 16px;
  }
}
</style>
