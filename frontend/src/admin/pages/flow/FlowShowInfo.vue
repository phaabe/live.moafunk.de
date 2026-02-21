<script setup lang="ts">
import { computed } from 'vue';
import { useRouter } from 'vue-router';
import { useArtistFlow } from '@admin/composables';

const router = useRouter();
const flow = useArtistFlow();

const show = computed(() => flow.show.value);

const formattedDate = computed(() => {
  if (!show.value?.date) return '';
  try {
    const d = new Date(show.value.date + 'T00:00:00');
    return d.toLocaleDateString('en-US', {
      weekday: 'long',
      year: 'numeric',
      month: 'long',
      day: 'numeric',
    });
  } catch {
    return show.value.date;
  }
});

function proceed() {
  flow.goToStep('mode');
  router.push('/stream/mode');
}
</script>

<template>
  <div class="flow-info">
    <div v-if="!flow.assigned.value" class="flow-info-empty">
      <p>You are not assigned to a show.</p>
    </div>

    <template v-else-if="show">
      <h1 class="flow-info-title">{{ show.title }}</h1>

      <div class="flow-info-meta">
        <div class="meta-item">
          <span class="meta-label">Date</span>
          <span class="meta-value">{{ formattedDate }}</span>
        </div>
        <div v-if="show.start_time" class="meta-item">
          <span class="meta-label">Time</span>
          <span class="meta-value">{{ show.start_time }}</span>
        </div>
      </div>

      <div v-if="show.description" class="flow-info-description">
        <h3>Description</h3>
        <p>{{ show.description }}</p>
      </div>

      <div class="flow-info-artists">
        <h3>Artists</h3>
        <div class="artist-badges">
          <span v-for="artist in show.artists" :key="artist.id" class="artist-badge">
            {{ artist.name }}
          </span>
        </div>
      </div>

      <div class="flow-info-actions">
        <button class="btn-primary" @click="proceed">
          Next →
        </button>
      </div>
    </template>
  </div>
</template>

<style scoped>
.flow-info-title {
  font-size: var(--font-size-3xl);
  font-weight: var(--font-weight-bold);
  color: var(--color-primary);
  margin: 0 0 var(--spacing-xl);
}

.flow-info-meta {
  display: flex;
  gap: var(--spacing-xl);
  margin-bottom: var(--spacing-xl);
}

.meta-item {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
}

.meta-label {
  font-size: var(--font-size-xs);
  color: var(--color-text-muted);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.meta-value {
  font-size: var(--font-size-lg);
  color: var(--color-text);
}

.flow-info-description {
  margin-bottom: var(--spacing-xl);
}

.flow-info-description h3,
.flow-info-artists h3 {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  margin: 0 0 var(--spacing-sm);
}

.flow-info-description p {
  color: var(--color-text);
  line-height: var(--line-height-relaxed);
  margin: 0;
}

.flow-info-artists {
  margin-bottom: var(--spacing-2xl);
}

.artist-badges {
  display: flex;
  flex-wrap: wrap;
  gap: var(--spacing-sm);
}

.artist-badge {
  background: var(--color-surface-alt);
  color: var(--color-text);
  padding: var(--spacing-xs) var(--spacing-md);
  border-radius: var(--radius-full);
  font-size: var(--font-size-sm);
  border: 1px solid var(--color-border);
}

.flow-info-actions {
  display: flex;
  justify-content: flex-end;
}

.btn-primary {
  background: var(--color-primary);
  color: var(--color-primary-text);
  border: none;
  padding: var(--spacing-sm) var(--spacing-xl);
  border-radius: var(--radius-md);
  font-family: var(--font-family);
  font-size: var(--font-size-md);
  font-weight: var(--font-weight-bold);
  cursor: pointer;
  transition: background var(--transition-fast);
}

.btn-primary:hover {
  background: var(--color-primary-hover);
}

.flow-info-empty {
  text-align: center;
  color: var(--color-text-muted);
  padding: var(--spacing-2xl);
}
</style>
