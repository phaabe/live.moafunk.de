<script setup lang="ts">
import { ref } from 'vue';
import type { UploadQualityMode } from '@admin/composables';
import LiveChatCard from '@admin/components/LiveChatCard.vue';
import LiveAudioCard from './LiveAudioCard.vue';
import LiveConnectionCard from './LiveConnectionCard.vue';
import LiveListenersCard from './LiveListenersCard.vue';
import LiveMomentsCard from './LiveMomentsCard.vue';
import PanelIcon from './PanelIcon.vue';
import type { Moment, PanelAudio, PanelBar, PanelLink, PanelListeners } from './onAir';

/**
 * The live panel's look (#1053): status bar, Audio, Connection, Listeners,
 * Live chat and Moments. It only shows values. StudioOnAir fills it from this
 * tab's broadcast; the episode page fills it read-only from what the host's
 * browser saved, or with no values before the show.
 *
 * Slots: `end` (the bar's buttons, in a `.end` box), `notices` (below the
 * bar) and `input` (the input picker in the Audio card).
 */
withDefaults(
  defineProps<{
    embedded?: boolean;
    /** No controls: no gain, input, quality, chat reply or Mark moment. */
    readOnly?: boolean;
    bar: PanelBar;
    audio: PanelAudio;
    link: PanelLink;
    listeners: PanelListeners;
    /** Unix ms: the right end of the listener line. */
    now: number;
    moments: Moment[];
    /** Keep the chat socket open. */
    chatActive: boolean;
  }>(),
  { embedded: false, readOnly: false }
);
const emit = defineEmits<{
  changeInput: [];
  'update:gain': [gain: number];
  'update:quality': [mode: UploadQualityMode];
  mark: [];
}>();

const BADGE = { live: 'On air', off: 'Not on air', aired: 'Aired' } as const;

/** The viewer listens to the stream: their own player is counted too. */
const monitoring = ref(false);
</script>

<template>
  <div :class="['on-air', 'host-page', { embedded, docked: $slots.end }]">
    <h1 v-if="!embedded" class="page-title studio-title">Studio</h1>

    <section :class="['bar', bar.badge, { warn: bar.alert }]" aria-label="Broadcast status">
      <!-- The flow's pill "On air · 00:42:10 · REC"; its parts sit in the bar's grid. -->
      <p class="pill on-air-pill">
        <span class="onair-badge"
          ><span v-if="bar.badge === 'live'" class="pulse" aria-hidden="true"></span
          >{{ BADGE[bar.badge] }}</span
        ><span class="sep"> · </span><span class="on-air-clock">{{ bar.clock }}</span
        ><template v-if="bar.rec"
          ><span class="sep"> · </span
          ><span class="rec"><i aria-hidden="true"></i>REC</span></template
        >
      </p>
      <span v-if="bar.screenOn" class="wake-chip"
        ><PanelIcon name="monitor" :size="14" />Screen stays on</span
      >
      <span v-if="bar.listeners !== null" class="bar-listeners">{{ bar.listeners }} listening</span>
      <div class="bar-title">
        <b>{{ bar.title }}</b
        ><span v-if="bar.slot">{{ bar.slot }}</span>
      </div>
      <span v-if="bar.left" :class="['bar-left', { soon: bar.soon }]">{{ bar.left }}</span>
      <p v-if="bar.alert" class="bar-alert" role="status">
        <span class="warn-chip">{{ bar.alert.chip }}</span
        ><span class="bar-alert-text">{{ bar.alert.text }}</span>
      </p>
      <slot name="end" />
      <div
        v-if="bar.progress !== null"
        class="progress"
        role="img"
        :aria-label="`Show progress ${bar.progress} percent`"
      >
        <i :style="{ width: `${bar.progress}%` }"></i>
      </div>
    </section>

    <slot name="notices" />

    <div class="cols">
      <div class="main">
        <LiveAudioCard
          v-bind="audio"
          :read-only="readOnly"
          @change-input="emit('changeInput')"
          @update:gain="emit('update:gain', $event)"
          @monitoring="monitoring = $event"
        >
          <template #input><slot name="input" /></template>
        </LiveAudioCard>
        <LiveConnectionCard
          v-bind="link"
          :read-only="readOnly"
          @update:quality="emit('update:quality', $event)"
        />
      </div>
      <aside class="side">
        <LiveListenersCard
          v-bind="listeners"
          :now="now"
          :monitoring="monitoring"
          :read-only="readOnly"
        />
        <LiveChatCard :active="chatActive" :read-only="readOnly" />
        <LiveMomentsCard :moments="moments" :read-only="readOnly" @mark="emit('mark')" />
      </aside>
    </div>
    <div v-if="$slots.end" class="dock" aria-hidden="true"></div>
  </div>
</template>

<style scoped>
.on-air {
  max-width: 1240px;
  margin: 0 auto;
}

.studio-title {
  font-size: 1.25rem;
  letter-spacing: 0.06em;
  color: #a8a8bc;
}

/* ---------- status bar ---------- */
.bar {
  position: relative;
  display: grid;
  grid-template-columns: auto auto minmax(0, 1fr) auto minmax(0, 1fr);
  grid-template-areas:
    'onair rec chip clock end'
    'title title title left end'
    'alert alert alert alert alert';
  align-items: center;
  gap: 10px 12px;
  padding: 20px 24px 26px;
  border: 1px solid #5a2424;
  border-radius: var(--radius-xl);
  background: #140a0d;
  overflow: hidden;
}

.bar.warn {
  border-color: #7a5a12;
  background: #171106;
}

.on-air-pill {
  display: contents;
}

.onair-badge {
  grid-area: onair;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  justify-self: start;
  padding: 6px 12px;
  border-radius: var(--radius-lg);
  background: #dc2626;
  color: #fff;
  font: 700 13px/1.2 var(--font-ui);
  letter-spacing: 0.1em;
  text-transform: uppercase;
}

.pulse {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: #fff;
  animation: on-air-pulse 1.6s infinite;
}

@keyframes on-air-pulse {
  0% {
    box-shadow: 0 0 0 0 rgba(255, 255, 255, 0.6);
  }
  70% {
    box-shadow: 0 0 0 8px rgba(255, 255, 255, 0);
  }
  100% {
    box-shadow: 0 0 0 0 rgba(255, 255, 255, 0);
  }
}

@media (prefers-reduced-motion: reduce) {
  .pulse {
    animation: none;
    box-shadow: 0 0 0 4px rgba(255, 255, 255, 0.25);
  }
}

.rec {
  grid-area: rec;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  justify-self: start;
  padding: 5px 10px;
  border: 1px solid var(--color-error);
  border-radius: var(--radius-full);
  color: #ffb3ab;
  font: 700 12px/1.2 var(--font-family);
  letter-spacing: 0.08em;
}

.rec i {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--color-error);
}

.wake-chip {
  grid-area: chip;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  justify-self: start;
  padding: 5px 10px;
  border: 1px solid #4a4a60;
  border-radius: var(--radius-full);
  color: #c4c4cc;
  font: 600 12px/1.2 var(--font-ui);
  white-space: nowrap;
}

.bar-listeners {
  display: none;
  grid-area: listen;
  justify-self: end;
  font: 700 13px var(--font-family);
  color: #ffd0cc;
  white-space: nowrap;
}

.bar-title {
  grid-area: title;
  min-width: 0;
}

.bar-title b {
  display: block;
  font: 700 22px/1.15 var(--font-family);
  color: #fff;
  overflow-wrap: anywhere;
}

.bar-title span {
  font-size: var(--font-size-sm);
  color: #d0c4c4;
}

.on-air-clock {
  grid-area: clock;
  justify-self: center;
  font: 700 clamp(2.75rem, 6vw, 3.75rem) / 1 var(--font-family);
  font-variant-numeric: tabular-nums;
  letter-spacing: 0.02em;
  color: #fff;
}

.bar.warn .on-air-clock {
  color: #fbbf24;
}

.bar-left {
  grid-area: left;
  justify-self: center;
  align-self: start;
  font: 700 14px var(--font-family);
  color: #ffb3ab;
  white-space: nowrap;
}

.bar-left.soon {
  color: #fbbf24;
}

.bar-alert {
  grid-area: alert;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
  margin: 0;
  color: #f4e6c8;
  font-size: var(--font-size-sm);
}

.warn-chip {
  display: inline-flex;
  align-items: center;
  padding: 5px 10px;
  border: 1px solid var(--color-warning);
  border-radius: var(--radius-lg);
  background: rgba(245, 158, 11, 0.16);
  color: #fbbf24;
  font: 700 12px/1.2 var(--font-ui);
  letter-spacing: 0.06em;
  text-transform: uppercase;
  white-space: nowrap;
}

.progress {
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  height: 6px;
  background: #3a1c1c;
}

.progress i {
  display: block;
  height: 100%;
  background: var(--color-error);
}

.bar.warn .progress {
  background: #3a2a10;
}

.bar.warn .progress i {
  background: var(--color-warning);
}

.bar.off,
.bar.aired {
  border-color: #34344c;
  background: var(--color-surface);
}

.bar.off .onair-badge {
  background: #34344c;
  color: #c4c4cc;
}

.bar.aired .onair-badge {
  background: #14532d;
  color: #bbf7d0;
}

.bar.off .on-air-clock {
  color: #8e8ea4;
}

.bar.off .progress,
.bar.aired .progress {
  background: #26263a;
}

.bar.aired .progress i {
  background: #22c55e;
}

/* ---------- the bar's buttons (slot `end`) ---------- */
:slotted(.end) {
  grid-area: end;
  justify-self: end;
}

:slotted(.end.confirming) {
  grid-area: auto / 1 / auto / -1;
  justify-self: stretch;
  display: flex;
  flex-direction: column;
  gap: var(--spacing-sm);
  padding: var(--spacing-md);
  border: 2px solid #5c2020;
  border-radius: var(--radius-xl);
  background: #120808;
}

/* ---------- columns ---------- */
.cols > .main,
.cols > .side {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-lg);
}

.dock {
  display: none;
}

/* Narrow screens: the clock gets its own row. */
/* Narrow screens: the clock gets its own row. */
@media (max-width: 1000px) {
  .bar {
    grid-template-columns: auto auto minmax(0, 1fr);
    grid-template-areas:
      'onair rec chip'
      'clock clock clock'
      'title title left'
      'alert alert alert'
      'end end end';
  }

  .bar-left {
    justify-self: end;
    align-self: center;
  }

  :slotted(.end) {
    justify-self: stretch;
  }
}

@media (max-width: 640px) {
  .on-air.docked {
    padding-bottom: calc(96px + env(safe-area-inset-bottom, 0px));
  }

  .bar {
    grid-template-columns: auto auto minmax(0, 1fr);
    grid-template-areas:
      'onair rec listen'
      'clock clock clock'
      'title title left'
      'alert alert alert';
    gap: 8px;
    padding: 14px 16px 18px;
  }

  .bar-listeners {
    display: block;
  }

  .wake-chip {
    grid-area: auto / 1 / auto / -1;
  }

  .on-air-clock {
    font-size: 46px;
  }

  .bar-title b {
    font-size: 16px;
  }

  .bar-left {
    font-size: 13px;
  }

  .dock {
    display: block;
    position: fixed;
    left: 0;
    right: 0;
    bottom: 0;
    z-index: 20;
    height: calc(88px + env(safe-area-inset-bottom, 0px));
    border-top: 1px solid #2a2a42;
    background: #05050f;
  }

  :slotted(.end) {
    position: fixed;
    left: calc(50% + 5px);
    right: 16px;
    bottom: calc(16px + env(safe-area-inset-bottom, 0px));
    z-index: 21;
  }

  :slotted(.end.confirming) {
    left: 16px;
    z-index: 22;
    box-shadow: 0 -12px 32px rgba(0, 0, 0, 0.6);
  }
}
</style>
