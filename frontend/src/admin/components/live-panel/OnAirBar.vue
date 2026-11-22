<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import { BaseButton } from '@shared/components';
import { useLiveSession } from '@admin/liveSession';
import PanelIcon from './PanelIcon.vue';

/**
 * The on-air bar (flow screens `onair-bar`, `onair-bar-warn` and
 * `onair-bar-end`, #1063): under the nav bar on every admin page while this
 * tab is live, except while the live panel is on screen. It only shows the
 * live session; the session runs without it.
 */
const router = useRouter();
const { session, panelShown } = useLiveSession();
const live = computed(() => (panelShown.value ? null : session.value));

const confirming = ref(false);
const ending = ref(false);
// A new session, or none, starts without the question.
watch(session, () => {
  confirming.value = false;
  ending.value = false;
});

// End Episode asks once, as on On Air: one slip must not end the broadcast.
async function endEpisode() {
  if (!live.value) return;
  ending.value = true;
  const id = await live.value.end();
  // On this episode's own page the push changes nothing: the page watches the
  // session's end and opens the Recording tab itself.
  await router.push({ path: `/shows/${id}`, state: { episodeEnded: id } });
}
</script>

<template>
  <section
    v-if="live"
    :class="['onair-bar', { warn: live.warnings.value.length }]"
    aria-label="On air"
  >
    <div class="bar-row">
      <p class="pill onair-pill">
        <span class="badge"><span class="pulse" aria-hidden="true"></span>On air</span
        ><span class="sep"> · </span><span class="clock">{{ live.elapsed.value }}</span>
      </p>
      <p class="show">
        <b>{{ live.title }}</b
        ><span v-if="live.slot"> · {{ live.slot }}</span>
      </p>
      <div v-if="confirming" class="actions confirming">
        <span class="question">End the episode?</span>
        <BaseButton variant="secondary" size="sm" :disabled="ending" @click="confirming = false"
          >Keep on air</BaseButton
        >
        <BaseButton variant="danger" size="sm" :loading="ending" @click="endEpisode"
          >End Episode</BaseButton
        >
      </div>
      <div v-else class="actions">
        <router-link :to="live.panelPath" class="back">Back to On Air</router-link>
        <BaseButton variant="danger" size="sm" class="end" @click="confirming = true"
          ><PanelIcon name="stop" :size="14" />End Episode</BaseButton
        >
      </div>
    </div>
    <p
      v-for="warning in live.warnings.value"
      :key="warning.text"
      class="notice"
      :role="warning.urgent ? 'alert' : 'status'"
    >
      {{ warning.text }}
    </p>
  </section>
</template>

<style scoped>
.onair-bar {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 10px var(--spacing-lg);
  border-bottom: 1px solid #5a2424;
  background: #140a0d;
  color: #fff;
}

.onair-bar.warn {
  border-bottom-color: #7a5a12;
  background: #171106;
}

.bar-row,
.notice {
  width: 100%;
  max-width: 1400px;
  margin: 0 auto;
}

.bar-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 16px;
}

.onair-pill {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  margin: 0;
}

.badge {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 4px 10px;
  border-radius: var(--radius-lg);
  background: #dc2626;
  font: 700 12px/1.2 var(--font-ui);
  letter-spacing: 0.1em;
  text-transform: uppercase;
}

.pulse {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #fff;
  animation: onair-bar-pulse 1.6s infinite;
}

@keyframes onair-bar-pulse {
  0% {
    box-shadow: 0 0 0 0 rgba(255, 255, 255, 0.6);
  }
  70% {
    box-shadow: 0 0 0 7px rgba(255, 255, 255, 0);
  }
  100% {
    box-shadow: 0 0 0 0 rgba(255, 255, 255, 0);
  }
}

@media (prefers-reduced-motion: reduce) {
  .pulse {
    animation: none;
  }
}

/* The pill reads "On air · 00:42:10"; the dot is drawn by the badge. */
.sep {
  display: none;
}

.clock {
  font: 700 1.25rem/1 var(--font-family);
  font-variant-numeric: tabular-nums;
}

.onair-bar.warn .clock {
  color: #fbbf24;
}

.show {
  flex: 1 1 12rem;
  min-width: 0;
  margin: 0;
  color: #d0c4c4;
  font-size: var(--font-size-sm);
  overflow-wrap: anywhere;
}

.show b {
  color: #fff;
}

.actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
}

.question {
  font-weight: 700;
}

.back {
  color: #ffd0cc;
  font-weight: 700;
  text-decoration: underline;
  text-underline-offset: 3px;
}

.end {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.notice {
  padding: 6px 10px;
  border-left: 4px solid var(--color-warning);
  border-radius: var(--radius-md);
  background: rgba(245, 158, 11, 0.12);
  color: #fde7b0;
  font-size: var(--font-size-sm);
  font-weight: 600;
}

/* Phones: the actions get their own row, in thumb reach. */
@media (max-width: 640px) {
  .onair-bar {
    padding: 8px var(--spacing-md);
  }

  .actions {
    width: 100%;
    justify-content: space-between;
  }
}
</style>
