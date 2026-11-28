<script setup lang="ts">
import BoardIcon from '../BoardIcon.vue';
import EpisodeIcon from './EpisodeIcon.vue';
import type { Segment } from './phase';
import { BADGE, type TabClock, type TabItem } from './tabs';

// The four steps of the episode page as tabs (#1052, icon draft): Social media,
// Soundcheck, Show time, Recording. Each tile shows one icon per item with a
// badge for its status; Show time shows its clock. The frame has the colour
// of its step (current yellow, done dim yellow, next gray). A click shows the
// step's page in the one main card below.

const props = defineProps<{
  segments: Segment[];
  /** The selected tab, 0–3. */
  selected: number;
  social: TabItem[];
  soundcheck: TabItem[];
  clock: TabClock;
  recording: TabItem[];
  /** The id of the main card (the tab panel). */
  panelId: string;
}>();
const emit = defineEmits<{ select: [index: number] }>();

const NAMES = ['Social media', 'Soundcheck', 'Show time', 'Recording'];

const tabId = (i: number) => `${props.panelId}-tab-${i}`;
const items = (i: number) =>
  i === 0 ? props.social : i === 1 ? props.soundcheck : i === 3 ? props.recording : [];

/** Arrow keys move between the tabs, as a tab list does. */
function onKey(e: KeyboardEvent, i: number) {
  const step = e.key === 'ArrowRight' ? 1 : e.key === 'ArrowLeft' ? -1 : 0;
  if (!step) return;
  e.preventDefault();
  const next = (i + step + 4) % 4;
  emit('select', next);
  (document.getElementById(tabId(next)) as HTMLElement | null)?.focus();
}
</script>

<template>
  <div class="ep-tabs" role="tablist" aria-label="Episode steps">
    <button
      v-for="(seg, i) in segments"
      :id="tabId(i)"
      :key="seg.name"
      type="button"
      role="tab"
      :class="['ep-tab', seg.state, { sel: selected === i }]"
      :aria-selected="selected === i"
      :aria-controls="panelId"
      :tabindex="selected === i ? 0 : -1"
      @click="emit('select', i)"
      @keydown="onKey($event, i)"
    >
      <span class="tn"
        ><b>{{ i + 1 }}</b
        ><span class="sep"> · </span>{{ NAMES[i]
        }}<span v-if="seg.state === 'done'" class="sep"> (done)</span></span
      >
      <span v-if="i === 2" class="tb clock">
        <span :class="['n', clock.kind]">
          <span v-if="clock.kind === 'live'" class="dot" role="img" aria-label="On air"></span>
          <span v-else-if="clock.kind === 'post'" class="mark" role="img" aria-label="Aired"
            ><BoardIcon name="check" :size="22"
          /></span>
          <span v-else class="mark" role="img" aria-label="Starts in"
            ><EpisodeIcon name="timer" :size="22"
          /></span>
          <span class="sep"> · </span>{{ clock.clock }}
        </span>
        <span v-if="clock.sub" class="s"><span class="sep"> · </span>{{ clock.sub }}</span>
      </span>
      <span v-else class="tb">
        <span
          v-for="(it, k) in items(i)"
          :key="k"
          :class="['ico', { todo: it.badge === 'todo' }]"
          role="img"
          :aria-label="it.label"
          :title="it.label"
        >
          <EpisodeIcon :name="it.icon" :size="24" />
          <span :class="['bdg', it.badge]" aria-hidden="true"
            ><svg
              v-if="BADGE[it.badge].d"
              width="12"
              height="12"
              viewBox="0 0 24 24"
              :fill="BADGE[it.badge].fill ? 'currentColor' : 'none'"
              stroke="currentColor"
              stroke-width="3.5"
              stroke-linecap="round"
              stroke-linejoin="round"
            >
              <path :d="BADGE[it.badge].d" /></svg
          ></span>
        </span>
      </span>
    </button>
  </div>
</template>

<style scoped>
.ep-tabs {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: var(--spacing-lg);
}

.ep-tab {
  --c: #8e8ea4;
  --b: #34344c;
  --bg: var(--color-surface);
  position: relative;
  display: flex;
  flex-direction: column;
  width: 100%;
  min-width: 0;
  margin: 0;
  padding: 0;
  border: 2px solid var(--b);
  border-radius: var(--radius-xl);
  background: var(--bg);
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.ep-tab.done {
  --c: #8f8536;
  --b: #8f8536;
}

.ep-tab.now {
  --c: var(--color-primary);
  --b: var(--color-primary);
}

.ep-tab:hover {
  --bg: #15152c;
}

.ep-tab:focus-visible {
  outline: 3px solid #fff;
  outline-offset: 3px;
}

/* The selected tab: lighter, a bar at the bottom and a notch towards the card. */
.ep-tab.sel {
  --bg: #1b1b38;
  box-shadow: inset 0 -5px 0 var(--b);
}

.ep-tab.sel::after {
  content: '';
  position: absolute;
  left: 50%;
  bottom: -14px;
  margin-left: -11px;
  border-style: solid;
  border-width: 12px 11px 0;
  border-color: var(--b) transparent transparent;
}

.tn {
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding: 12px 16px 0;
  font: 700 12px var(--font-family);
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: #ddd;
}

.tn b {
  color: var(--c);
}

.tb {
  display: flex;
  flex: 1;
  align-items: center;
  justify-content: center;
  gap: 10px;
  min-height: 92px;
  padding: 14px 12px 20px;
}

.ico {
  position: relative;
  display: flex;
  flex: none;
  align-items: center;
  justify-content: center;
  width: 48px;
  height: 48px;
  border: 1.5px solid #34344c;
  border-radius: 12px;
  background: #16162e;
  color: #e6e6ef;
}

.ico.todo {
  border-style: dashed;
  border-color: #55556a;
  background: transparent;
  color: #8e8ea4;
}

.bdg {
  position: absolute;
  right: -8px;
  bottom: -8px;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  border-radius: 50%;
  box-shadow: 0 0 0 3px var(--bg);
  color: #000;
}

.bdg.todo,
.bdg.run {
  background: var(--color-primary);
}
.bdg.sched {
  background: #3478f6;
}
.bdg.approve,
.bdg.warn {
  background: #f59e0b;
}
.bdg.lock {
  background: #ff7a2f;
}
.bdg.ok {
  background: #34c759;
}
.bdg.fail {
  background: #ef4444;
}
.bdg.off {
  background: #8e8ea4;
}
.bdg.rec {
  background: #ef4444;
  color: #fff;
}
.bdg.wait {
  border: 1.5px dashed #55556a;
  background: var(--bg);
}

.clock {
  flex-direction: column;
  gap: 6px;
}

.clock .n {
  display: flex;
  align-items: center;
  gap: 10px;
  font: 700 30px/1 var(--font-family);
  font-variant-numeric: tabular-nums;
  color: #fff;
}

.clock .s {
  font: 700 12px var(--font-family);
  color: #a8a8bc;
}

.mark {
  display: flex;
  color: #8e8ea4;
}

.post .mark {
  color: #6ee79a;
}

.dot {
  flex: none;
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background: #ef4444;
  box-shadow: 0 0 0 4px rgba(239, 68, 68, 0.25);
}

@media (max-width: 1000px) {
  .ep-tabs {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 12px;
  }

  .ep-tab.sel::after {
    display: none;
  }
}

@media (max-width: 640px) {
  .tn {
    padding: 10px 10px 0;
    font-size: 11px;
    letter-spacing: 0.08em;
  }

  /* Four icons and their badges fit a half-width tile from 360 px on. */
  .tb {
    gap: 5px;
    min-height: 66px;
    padding: 12px 10px 16px 4px;
  }

  .ico {
    width: 30px;
    height: 30px;
    border-radius: 8px;
  }

  .ico :deep(svg) {
    width: 16px;
    height: 16px;
  }

  .bdg {
    right: -5px;
    bottom: -5px;
    width: 16px;
    height: 16px;
    box-shadow: 0 0 0 2px var(--bg);
  }

  .bdg svg {
    width: 9px;
    height: 9px;
  }

  .clock .n {
    gap: 6px;
    font-size: 20px;
  }
}
</style>
