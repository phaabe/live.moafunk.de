<script setup lang="ts">
// Static placeholder card matching the dashboard mockup. Per-show social handles
// are not part of the data model yet, so these rows are visual-only for now.
const channels = [
  { icon: '📸', label: 'Instagram', handle: '@yourhandle' },
  { icon: '✈️', label: 'Telegram', handle: 't.me/yourchannel' },
];
</script>

<template>
  <div class="card social-card">
    <h2 class="section-title"><span class="ico">🔗</span> Social channels</h2>

    <div class="channel-list">
      <div v-for="c in channels" :key="c.label" class="channel-row">
        <span class="channel-ico">{{ c.icon }}</span>
        <span class="channel-handle">{{ c.handle }}</span>
        <span class="channel-badge">Linked</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.social-card {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
}

.section-title {
  display: flex;
  align-items: center;
  gap: var(--spacing-xs);
  font-size: var(--font-size-sm);
  font-weight: 700;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  color: var(--color-text-muted);
  margin: 0;
}

.channel-list {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-sm);
}

.channel-row {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  padding: var(--spacing-sm) var(--spacing-md);
  background: var(--color-surface);
  border-radius: var(--radius-md);
}

.channel-ico {
  font-size: 1.1rem;
}

.channel-handle {
  flex: 1 1 auto;
  font-size: var(--font-size-sm);
  color: var(--color-text);
}

.channel-badge {
  font-size: var(--font-size-xs);
  font-weight: 600;
  color: var(--color-success);
  background: var(--color-success-bg);
  padding: 2px var(--spacing-sm);
  border-radius: var(--radius-full);
}
</style>
