<script setup lang="ts">
import { ref, computed, watch, onMounted, nextTick } from 'vue';
import type { ScheduleItem } from '../../api';

/**
 * Apple-Calendar-style single-day timeline. Hours run top→bottom; clicking an
 * empty slot drops a default one-hour show, and the block's top/bottom handles
 * drag to resize. Works purely in "minutes from midnight"; the parent maps that
 * onto the selected calendar date. Existing shows render as read-only blocks so
 * the user can see (and avoid) conflicts.
 */
const props = defineProps<{
  /** Selected day, "YYYY-MM-DD". Used to filter `shows` to this day. */
  date: string;
  /** Current show window, minutes from midnight, or null when nothing placed. */
  startMinutes: number | null;
  endMinutes: number | null;
  /** All scheduled shows; the component renders those on `date`. */
  shows: ScheduleItem[];
  /** Whether the placed block overlaps an existing show (drives styling). */
  conflict?: boolean;
  /** Visible height of the scroll viewport, in px (matched to the calendar). */
  height?: number;
}>();

const emit = defineEmits<{
  (e: 'update', value: { start: number; end: number }): void;
}>();

const HOUR_PX = 48;
const GUTTER_PX = 52;
const SNAP_MIN = 15;
const DAY_MIN = 24 * 60;
const DEFAULT_DURATION = 60;
const MIN_DURATION = 15;

const gridHeight = `${24 * HOUR_PX}px`;
const gutter = `${GUTTER_PX}px`;
const hours = Array.from({ length: 24 }, (_, h) => h);

const scrollHeight = computed(() => `${props.height ?? 420}px`);

const scrollEl = ref<HTMLElement | null>(null);
const innerEl = ref<HTMLElement | null>(null);

function clamp(v: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, v));
}

function fmt(min: number): string {
  const h = Math.floor(min / 60);
  const m = min % 60;
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
}

function parseTime(t: string): number {
  const [h, m] = t.split(':').map(Number);
  return h * 60 + m;
}

/** Pointer Y → snapped minutes, relative to the grid's content box. */
function pointerMinutes(e: PointerEvent): number {
  const rect = innerEl.value!.getBoundingClientRect();
  const raw = ((e.clientY - rect.top) / HOUR_PX) * 60;
  return clamp(Math.round(raw / SNAP_MIN) * SNAP_MIN, 0, DAY_MIN);
}

const hasBlock = computed(() => props.startMinutes !== null && props.endMinutes !== null);

const selectedStyle = computed(() => {
  if (!hasBlock.value) return {};
  const top = (props.startMinutes! / 60) * HOUR_PX;
  const height = ((props.endMinutes! - props.startMinutes!) / 60) * HOUR_PX;
  return { top: `${top}px`, height: `${height}px` };
});

const selectedLabel = computed(() =>
  hasBlock.value ? `${fmt(props.startMinutes!)}–${fmt(props.endMinutes!)}` : ''
);

/** Existing shows on this day, as positioned read-only blocks. */
const dayEvents = computed(() => {
  return props.shows
    .filter((s) => s.date === props.date && s.start_time)
    .map((s) => {
      const start = parseTime(s.start_time!);
      const end = s.end_time ? parseTime(s.end_time) : start + DEFAULT_DURATION;
      return {
        id: s.id,
        title: s.title,
        label: `${fmt(start)}–${fmt(end)}`,
        top: (start / 60) * HOUR_PX,
        height: (Math.max(end - start, MIN_DURATION) / 60) * HOUR_PX,
      };
    });
});

// ── Click-to-create ─────────────────────────────────────────────────────────

function onGridPointerDown(e: PointerEvent): void {
  // Snap the click to the hour it lands in; default to a one-hour show.
  const start = clamp(Math.floor(pointerMinutes(e) / 60) * 60, 0, DAY_MIN - DEFAULT_DURATION);
  emit('update', { start, end: start + DEFAULT_DURATION });
}

// ── Drag to move / resize ────────────────────────────────────────────────────

type DragMode = 'move' | 'start' | 'end';
let dragMode: DragMode | null = null;
let dragOffset = 0; // for move: pointer minute − block start

function startDrag(mode: DragMode, e: PointerEvent): void {
  if (!hasBlock.value) return;
  dragMode = mode;
  dragOffset = pointerMinutes(e) - props.startMinutes!;
  window.addEventListener('pointermove', onDrag);
  window.addEventListener('pointerup', endDrag);
}

function onDrag(e: PointerEvent): void {
  if (!dragMode || !hasBlock.value) return;
  let start = props.startMinutes!;
  let end = props.endMinutes!;
  const ptr = pointerMinutes(e);
  if (dragMode === 'start') {
    start = clamp(ptr, 0, end - MIN_DURATION);
  } else if (dragMode === 'end') {
    end = clamp(ptr, start + MIN_DURATION, DAY_MIN);
  } else {
    const dur = end - start;
    start = clamp(ptr - dragOffset, 0, DAY_MIN - dur);
    end = start + dur;
  }
  emit('update', { start, end });
}

function endDrag(): void {
  dragMode = null;
  window.removeEventListener('pointermove', onDrag);
  window.removeEventListener('pointerup', endDrag);
}

// ── Auto-scroll so the placed block is in view when the day changes ───────────

function scrollToBlock(): void {
  if (!scrollEl.value || props.startMinutes === null) return;
  const y = (props.startMinutes / 60) * HOUR_PX;
  scrollEl.value.scrollTop = Math.max(0, y - HOUR_PX * 1.5);
}

onMounted(() => nextTick(scrollToBlock));
watch(
  () => props.date,
  () => nextTick(scrollToBlock)
);
</script>

<template>
  <div class="day-timeline">
    <div ref="scrollEl" class="tl-scroll" :style="{ height: scrollHeight }">
      <div
        ref="innerEl"
        class="tl-grid"
        :style="{ height: gridHeight }"
        @pointerdown="onGridPointerDown"
      >
        <div v-for="h in hours" :key="h" class="tl-hour" :style="{ top: `${h * HOUR_PX}px` }">
          <span class="tl-hour-label">{{ String(h).padStart(2, '0') }}:00</span>
        </div>

        <!-- Existing shows (read-only) -->
        <div
          v-for="ev in dayEvents"
          :key="ev.id"
          class="tl-event tl-event--existing"
          :style="{ top: `${ev.top}px`, height: `${ev.height}px` }"
        >
          <span class="tl-event-title">{{ ev.title }}</span>
          <span class="tl-event-time">{{ ev.label }}</span>
        </div>

        <!-- The show being scheduled (draggable) -->
        <div
          v-if="hasBlock"
          class="tl-event tl-event--selected"
          :class="{ 'tl-event--conflict': conflict }"
          :style="selectedStyle"
          @pointerdown.stop="startDrag('move', $event)"
        >
          <div class="tl-handle tl-handle--top" @pointerdown.stop="startDrag('start', $event)" />
          <span class="tl-event-time">{{ selectedLabel }}</span>
          <div class="tl-handle tl-handle--bottom" @pointerdown.stop="startDrag('end', $event)" />
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.day-timeline {
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-surface);
  overflow: hidden;
}

.tl-scroll {
  overflow-y: auto;
}

.tl-grid {
  position: relative;
  width: 100%;
  /* Empty area is the click target; block + handles set their own cursors. */
  cursor: copy;
  touch-action: pan-y;
}

.tl-hour {
  position: absolute;
  left: 0;
  right: 0;
  height: 0;
  border-top: 1px solid var(--color-border);
  pointer-events: none;
}

.tl-hour-label {
  position: absolute;
  top: -0.6em;
  left: var(--spacing-sm);
  font-size: var(--font-size-sm);
  font-variant-numeric: tabular-nums;
  color: var(--color-text-muted);
  background: var(--color-surface);
  padding-right: var(--spacing-xs);
}

.tl-event {
  position: absolute;
  left: v-bind(gutter);
  right: var(--spacing-sm);
  border-radius: var(--radius-sm);
  padding: 2px var(--spacing-sm);
  overflow: hidden;
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: var(--font-size-sm);
  box-sizing: border-box;
}

.tl-event-title {
  font-weight: var(--font-weight-bold);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.tl-event-time {
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.tl-event--existing {
  background: var(--color-surface-alt);
  border: 1px solid var(--color-border-light);
  color: var(--color-text-muted);
  pointer-events: none;
}

.tl-event--selected {
  background: var(--color-primary);
  color: var(--color-primary-text, #000);
  cursor: grab;
  touch-action: none;
  z-index: 2;
  justify-content: center;
  font-weight: var(--font-weight-bold);
}

.tl-event--selected:active {
  cursor: grabbing;
}

.tl-event--conflict {
  background: var(--color-error);
  color: #fff;
}

.tl-handle {
  position: absolute;
  left: 0;
  right: 0;
  height: 10px;
  cursor: ns-resize;
  touch-action: none;
}

.tl-handle--top {
  top: 0;
}

.tl-handle--bottom {
  bottom: 0;
}

/* A grab affordance line on each handle. */
.tl-handle::after {
  content: '';
  position: absolute;
  left: 50%;
  transform: translateX(-50%);
  width: 28px;
  height: 3px;
  border-radius: 2px;
  background: currentColor;
  opacity: 0.5;
}

.tl-handle--top::after {
  top: 2px;
}

.tl-handle--bottom::after {
  bottom: 2px;
}
</style>
