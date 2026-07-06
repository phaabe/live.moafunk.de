<script setup lang="ts">
import { computed } from 'vue';
import type { ShowDetail } from '../../api';
import { BaseButton, BaseModal } from '@shared/components';

/**
 * Telegram announcement composer: live chat-bubble preview of what the bot
 * will post (cover + caption), with "Send preview" wired to the existing
 * backend preview endpoint. Caption editing and scheduling land with the
 * scheduled-announcements backend; until then the caption mirrors what the
 * backend composes from the show data.
 */
const props = defineProps<{
  open: boolean;
  show: ShowDetail;
  sending: boolean;
}>();

const emit = defineEmits<{
  close: [];
  send: [];
}>();

/** Frontend approximation of the backend-composed message. */
const caption = computed(() => {
  const s = props.show;
  const when = s.date
    ? new Date(s.date + 'T' + (s.start_time || '00:00') + ':00').toLocaleDateString('en-US', {
        weekday: 'long',
        month: 'short',
        day: 'numeric',
      })
    : '';
  const time = s.start_time ? ` · ${s.start_time}` : '';
  const host = s.host_username ? ` with ${s.host_username}` : '';
  return `${s.title}${host}\n${when}${time}\nTune in live on moafunk.de`;
});

const timeLabel = computed(() => props.show.start_time || '');
</script>

<template>
  <BaseModal :open="open" title="📱 Telegram · announcement" @close="emit('close')">
    <div class="tg-preview-wrap">
      <div class="tg-bubble">
        <img v-if="show.cover_url" :src="show.cover_url" alt="Show cover" class="tg-img" />
        <div class="tg-body">
          <p class="tg-channel">Moafunk</p>
          <p class="tg-text">{{ caption }}</p>
          <p class="tg-time">{{ timeLabel }}</p>
        </div>
      </div>
    </div>
    <p class="tg-note">
      The bot composes the final message from the show data. Caption editing &amp; scheduling
      arrive in a later update.
    </p>
    <template #footer>
      <BaseButton variant="ghost" @click="emit('close')">Cancel</BaseButton>
      <BaseButton variant="primary" :loading="sending" @click="emit('send')">
        Send preview to channel
      </BaseButton>
    </template>
  </BaseModal>
</template>

<style scoped>
.tg-preview-wrap {
  display: flex;
  justify-content: center;
  padding: var(--spacing-md);
  background: var(--color-surface-alt);
  border-radius: var(--radius-lg);
  font-family: var(--font-ui);
}

.tg-bubble {
  max-width: 280px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 12px 12px 12px 2px;
  overflow: hidden;
}

.tg-img {
  display: block;
  width: 100%;
  aspect-ratio: 1;
  object-fit: cover;
}

.tg-body {
  padding: var(--spacing-sm) var(--spacing-md);
}

.tg-channel {
  margin: 0 0 2px;
  font-size: var(--font-size-xs);
  font-weight: 700;
  color: #3b82f6;
}

.tg-text {
  margin: 0;
  font-size: var(--font-size-sm);
  line-height: 1.45;
  color: var(--color-text);
  white-space: pre-line;
}

.tg-time {
  margin: var(--spacing-xs) 0 0;
  font-size: var(--font-size-xs);
  color: var(--color-text-muted);
  text-align: right;
}

.tg-note {
  margin: var(--spacing-md) 0 0;
  font-family: var(--font-ui);
  font-size: var(--font-size-xs);
  color: var(--color-text-muted);
}
</style>
