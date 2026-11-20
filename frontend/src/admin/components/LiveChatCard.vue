<script setup lang="ts">
// Live chat card (Live Panel 2.0, #278; live panel look, #1053): the Telegram
// discussion group bridged into the panel. Messages fan out over /ws/chat;
// replies go back through the bot with a host badge.
import { computed, ref, watch, nextTick, toRef } from 'vue';
import { useLiveChat } from '@admin/composables';
import LivePanelCard from '@admin/components/live-panel/LivePanelCard.vue';
import PanelIcon from '@admin/components/live-panel/PanelIcon.vue';
import { formatClockTime } from '@admin/components/live-panel/onAir';

const props = defineProps<{
  /** Keep the chat socket open while true (i.e. while on air). */
  active: boolean;
}>();

const { messages, connected, error, send } = useLiveChat(toRef(props, 'active'));

const draft = ref('');
const sendError = ref<string | null>(null);
const listEl = ref<HTMLElement | null>(null);

/** "12:35", 24 h, from Unix seconds. */
const timeLabel = (ts: number) => formatClockTime(ts * 1000);

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

// On phones the card folds; then it counts what came in meanwhile. The
// history the bridge sends first is not new.
const open = ref(true);
const seen = ref(0);
let primed = false;
watch(
  [open, () => messages.value.length],
  ([isOpen, count]) => {
    if (isOpen || !primed) seen.value = count;
    if (count > 0) primed = true;
  },
  { immediate: true }
);
const unread = computed(() => Math.max(0, messages.value.length - seen.value));

const status = computed<{ text: string; tone: 'ok' | 'info' | 'muted' }>(() => {
  if (!open.value && unread.value > 0) return { text: `${unread.value} new`, tone: 'info' };
  return connected.value ? { text: 'Telegram', tone: 'ok' } : { text: 'Offline', tone: 'muted' };
});
</script>

<template>
  <LivePanelCard
    title="Live chat"
    icon="chat"
    :status="status.text"
    :tone="status.tone"
    flush
    @update:open="open = $event"
  >
    <div ref="listEl" class="chat-list">
      <p v-if="messages.length === 0" class="chat-empty">
        No messages yet. The Telegram discussion group shows here.
      </p>
      <ul v-else class="chat-msgs">
        <li v-for="m in messages" :key="`${m.id}-${m.ts}`" :class="['chat-msg', { host: m.host }]">
          <div class="chat-head">
            <span class="chat-author">{{ m.author }}<template v-if="m.host"> (host)</template></span
            ><span class="chat-time">{{ timeLabel(m.ts) }}</span>
          </div>
          <p class="chat-text">{{ m.text }}</p>
        </li>
      </ul>
    </div>

    <p v-if="error || sendError" class="chat-error">{{ sendError ?? error }}</p>

    <form class="chat-compose" @submit.prevent="onSend">
      <label class="chat-sr" for="live-chat-reply">Reply as host</label>
      <input
        id="live-chat-reply"
        v-model="draft"
        type="text"
        class="chat-input"
        placeholder="Reply as host"
        maxlength="4096"
        :disabled="!connected"
      />
      <button
        type="submit"
        class="chat-send"
        aria-label="Send"
        :disabled="!connected || !draft.trim()"
      >
        <PanelIcon name="send" :size="18" />
      </button>
    </form>
  </LivePanelCard>
</template>

<style scoped>
.chat-list {
  max-height: 360px;
  min-height: 96px;
  overflow-y: auto;
}

.chat-empty {
  margin: 0;
  padding: var(--spacing-lg) var(--spacing-md);
  font-size: var(--font-size-sm);
  color: #a8a8bc;
  text-align: center;
}

.chat-msgs {
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 14px;
  margin: 0;
  padding: var(--spacing-md);
}

.chat-msg {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.chat-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
}

.chat-author {
  font-weight: 700;
  font-size: var(--font-size-sm);
  color: #7dd3fc;
}

.chat-msg.host .chat-author {
  color: #6ee79a;
}

.chat-time {
  font: 12px var(--font-family);
  font-variant-numeric: tabular-nums;
  color: #a0a0b4;
}

.chat-text {
  margin: 0;
  font-size: var(--font-size-sm);
  color: #d4d4de;
  overflow-wrap: anywhere;
}

.chat-error {
  margin: 0;
  padding: 8px var(--spacing-md);
  font-size: var(--font-size-xs);
  color: #ff8a80;
}

.chat-compose {
  display: flex;
  gap: 8px;
  padding: 12px;
  border-top: 1px solid #2a2a42;
}

.chat-sr {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
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

.chat-input:disabled {
  opacity: 0.6;
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
  color: #6a6a80;
  cursor: default;
}

.chat-send:focus-visible {
  outline: 2px solid var(--color-primary);
  outline-offset: 2px;
}
</style>
