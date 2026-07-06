<script setup lang="ts">
// Live chat card (Live Panel 2.0, #278): the Telegram discussion group
// bridged into the panel. Messages fan out over /ws/chat; replies go back
// through the bot with a host badge.
import { ref, watch, nextTick, toRef } from 'vue';
import { useLiveChat } from '@admin/composables';

const props = defineProps<{
  /** Keep the chat socket open while true (i.e. while on air). */
  active: boolean;
}>();

const { messages, connected, error, send } = useLiveChat(toRef(props, 'active'));

const draft = ref('');
const sendError = ref<string | null>(null);
const listEl = ref<HTMLElement | null>(null);

function timeLabel(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function onSend() {
  const text = draft.value.trim();
  if (!text) return;
  if (send(text)) {
    draft.value = '';
    sendError.value = null;
  } else {
    sendError.value = 'Not connected — reply not sent';
  }
}

// Follow new messages unless the host scrolled up to read history.
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
  <div class="panel-card chat-card">
    <div class="chat-head">
      <span class="chat-title">💬 Live chat · Moafunk channel</span>
      <span :class="['chat-pill', connected ? 'chat-pill-on' : 'chat-pill-off']">
        {{ connected ? 'Connected' : 'Offline' }}
      </span>
    </div>

    <div ref="listEl" class="chat-list">
      <p v-if="messages.length === 0" class="chat-empty">
        No messages yet — the channel's discussion group appears here.
      </p>
      <div v-for="m in messages" :key="`${m.id}-${m.ts}`" class="chat-msg">
        <span :class="['chat-author', { 'chat-author-host': m.host }]"
          >{{ m.author }}<template v-if="m.host"> (host)</template></span
        >
        <span class="chat-text">{{ m.text }}</span>
        <span class="chat-time">{{ timeLabel(m.ts) }}</span>
      </div>
    </div>

    <p v-if="error || sendError" class="chat-error">{{ sendError ?? error }}</p>

    <form class="chat-input-row" @submit.prevent="onSend">
      <input
        v-model="draft"
        type="text"
        class="chat-input"
        placeholder="Reply as host…"
        maxlength="4096"
        :disabled="!connected"
      />
      <button type="submit" class="chat-send" :disabled="!connected || !draft.trim()">Send</button>
    </form>
  </div>
</template>

<style scoped>
.chat-card {
  padding: var(--spacing-md) var(--spacing-lg);
  display: flex;
  flex-direction: column;
  gap: var(--spacing-sm);
}

.chat-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--spacing-sm);
}

.chat-title {
  font-family: var(--font-ui);
  font-size: var(--font-size-sm);
  font-weight: var(--font-weight-medium);
  color: var(--color-text-muted);
}

.chat-pill {
  font-family: var(--font-ui);
  font-size: var(--font-size-xs);
  padding: 2px 10px;
  border-radius: var(--radius-full);
  white-space: nowrap;
}

.chat-pill-on {
  background: var(--color-success-bg);
  color: var(--color-success);
}

.chat-pill-off {
  background: var(--color-surface-alt);
  color: var(--color-text-muted);
}

.chat-list {
  max-height: 240px;
  min-height: 96px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding-right: 4px;
}

.chat-empty {
  margin: auto 0;
  font-family: var(--font-ui);
  font-size: var(--font-size-xs);
  color: var(--color-text-muted);
  text-align: center;
}

.chat-msg {
  font-size: var(--font-size-sm);
  line-height: 1.4;
}

.chat-author {
  font-family: var(--font-ui);
  font-weight: var(--font-weight-medium);
  color: var(--color-accent, #7f77dd);
  margin-right: 6px;
}

.chat-author-host {
  color: var(--color-success);
}

.chat-text {
  color: var(--color-text);
  overflow-wrap: anywhere;
}

.chat-time {
  margin-left: 6px;
  font-family: var(--font-ui);
  font-size: 0.625rem;
  font-variant-numeric: tabular-nums;
  color: var(--color-text-muted);
}

.chat-error {
  margin: 0;
  font-family: var(--font-ui);
  font-size: var(--font-size-xs);
  color: var(--color-error);
}

.chat-input-row {
  display: flex;
  gap: var(--spacing-sm);
}

.chat-input {
  flex: 1;
  font-size: var(--font-size-sm);
  padding: 6px 12px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-full);
  background: var(--color-surface-alt);
  color: var(--color-text);
}

.chat-input:focus {
  outline: none;
  border-color: var(--color-text-muted);
}

.chat-send {
  font-family: var(--font-ui);
  font-size: var(--font-size-sm);
  padding: 6px 16px;
  border: none;
  border-radius: var(--radius-full);
  background: var(--color-success);
  color: #fff;
  cursor: pointer;
}

.chat-send:disabled {
  opacity: 0.5;
  cursor: default;
}
</style>
