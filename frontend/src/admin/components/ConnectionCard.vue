<script setup lang="ts">
// Connection & upload quality card (Live Panel 2.0, #275). All browser-side:
// throughput sparkline (measured egress vs "needed for target" dashed line),
// metric tiles, and the good/tight/slow verdict pill from useUploadHealth.
// RTT arrives with the backend ack telemetry (#277); the quality selector
// with the bitrate issue (#276).
import { ref, computed, watch, onMounted, toRef } from 'vue';
import { useUploadHealth, HISTORY_SECONDS, type UploadVerdict } from '@admin/composables';

const props = defineProps<{
  /** Poll the socket while true (i.e. while live / rehearsing). */
  active: boolean;
  /** Encoder target in bits/s (fixed 192k until #276). */
  targetBitsPerSecond: number;
}>();

const health = useUploadHealth(toRef(props, 'active'), toRef(props, 'targetBitsPerSecond'));
const { uploadKbps, bufferSeconds, lateCount, history, neededKbps, verdict, verdictText } = health;

const VERDICT_CLASS: Record<UploadVerdict, string> = {
  good: 'verdict-good',
  tight: 'verdict-tight',
  slow: 'verdict-slow',
};
const verdictClass = computed(() => VERDICT_CLASS[verdict.value]);

// ─── Sparkline ───────────────────────────────────────────────────────────────
const canvas = ref<HTMLCanvasElement | null>(null);

function draw() {
  const el = canvas.value;
  const ctx = el?.getContext('2d');
  if (!el || !ctx) return;

  const dpr = window.devicePixelRatio || 1;
  const w = el.clientWidth;
  const h = el.clientHeight;
  if (el.width !== w * dpr || el.height !== h * dpr) {
    el.width = w * dpr;
    el.height = h * dpr;
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  const needed = neededKbps.value;
  const max = Math.max(needed * 1.5, ...history.value.map((v) => v * 1.1), 1);
  const y = (v: number) => h - (Math.min(v, max) / max) * h;

  // Gridlines
  ctx.strokeStyle = 'rgba(128, 128, 128, 0.25)';
  ctx.lineWidth = 1;
  for (const f of [0.25, 0.5, 0.75]) {
    ctx.beginPath();
    ctx.moveTo(0, h * f);
    ctx.lineTo(w, h * f);
    ctx.stroke();
  }

  // "Needed for target" dashed line
  const ny = y(needed);
  ctx.strokeStyle = '#d85a30';
  ctx.setLineDash([5, 4]);
  ctx.beginPath();
  ctx.moveTo(0, ny);
  ctx.lineTo(w, ny);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = '#888';
  ctx.font = '10px monospace';
  ctx.fillText(`${Math.round(needed)} kbps`, 6, Math.max(10, ny - 4));

  // Measured egress line
  const data = history.value;
  if (data.length >= 2) {
    ctx.strokeStyle = '#1d9e75';
    ctx.lineWidth = 2;
    ctx.beginPath();
    data.forEach((v, i) => {
      const x = (i / (HISTORY_SECONDS - 1)) * w;
      if (i === 0) ctx.moveTo(x, y(v));
      else ctx.lineTo(x, y(v));
    });
    ctx.stroke();
  }
}

watch(history, draw, { deep: false });
onMounted(draw);
</script>

<template>
  <div class="panel-card conn-card">
    <div class="conn-head">
      <span class="conn-title">📶 Connection &amp; upload</span>
      <span :class="['conn-verdict', verdictClass]">{{ verdictText }}</span>
    </div>

    <div class="conn-body">
      <div class="conn-chart">
        <canvas ref="canvas" class="conn-canvas"></canvas>
        <div class="conn-chart-legend">
          <span class="conn-axis">-{{ HISTORY_SECONDS }} s</span>
          <span class="conn-legend">
            <span class="legend-swatch legend-upload"></span>upload ·
            <span class="legend-swatch legend-needed"></span>needed
          </span>
          <span class="conn-axis">now</span>
        </div>
      </div>

      <div class="conn-tiles">
        <div class="conn-tile">
          <p class="conn-tile-label">Upload</p>
          <p class="conn-tile-value">{{ uploadKbps }}k</p>
        </div>
        <div class="conn-tile">
          <p class="conn-tile-label">Buffer</p>
          <p class="conn-tile-value">{{ bufferSeconds.toFixed(1) }} s</p>
        </div>
        <div class="conn-tile">
          <p class="conn-tile-label">RTT</p>
          <p class="conn-tile-value conn-tile-muted" title="Arrives with the stream WS telemetry">
            —
          </p>
        </div>
        <div class="conn-tile">
          <p class="conn-tile-label">Late</p>
          <p class="conn-tile-value">{{ lateCount }}</p>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.conn-card {
  padding: var(--spacing-md) var(--spacing-lg);
}

.conn-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--spacing-sm);
  flex-wrap: wrap;
  margin-bottom: var(--spacing-sm);
}

.conn-title {
  font-family: var(--font-ui);
  font-size: var(--font-size-sm);
  font-weight: var(--font-weight-medium);
  color: var(--color-text-muted);
}

.conn-verdict {
  font-family: var(--font-ui);
  font-size: var(--font-size-xs);
  padding: 2px 10px;
  border-radius: var(--radius-full);
  white-space: nowrap;
}

.verdict-good {
  background: var(--color-success-bg);
  color: var(--color-success);
}

.verdict-tight {
  background: var(--color-warning-bg);
  color: var(--color-warning);
}

.verdict-slow {
  background: var(--color-error-bg);
  color: var(--color-error);
}

.conn-body {
  display: grid;
  grid-template-columns: 2fr 1fr;
  gap: var(--spacing-md);
  align-items: stretch;
}

@media (max-width: 600px) {
  .conn-body {
    grid-template-columns: 1fr;
  }
}

.conn-canvas {
  display: block;
  width: 100%;
  height: 96px;
}

.conn-chart-legend {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-top: 2px;
}

.conn-axis {
  font-size: 0.625rem;
  font-variant-numeric: tabular-nums;
  color: var(--color-text-muted);
}

.conn-legend {
  font-family: var(--font-ui);
  font-size: var(--font-size-xs);
  color: var(--color-text-muted);
}

.legend-swatch {
  display: inline-block;
  width: 12px;
  height: 2px;
  vertical-align: middle;
  margin: 0 4px 2px 0;
}

.legend-upload {
  background: #1d9e75;
}

.legend-needed {
  border-top: 2px dashed #d85a30;
  height: 0;
  margin-left: 4px;
}

.conn-tiles {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px;
  align-content: start;
}

.conn-tile {
  background: var(--color-surface-alt);
  border-radius: var(--radius-lg);
  padding: 6px 10px;
}

.conn-tile-label {
  margin: 0;
  font-family: var(--font-ui);
  font-size: 0.625rem;
  color: var(--color-text-muted);
}

.conn-tile-value {
  margin: 1px 0 0;
  font-size: var(--font-size-sm);
  font-weight: var(--font-weight-medium);
  font-variant-numeric: tabular-nums;
  color: var(--color-text);
}

.conn-tile-muted {
  color: var(--color-text-muted);
}
</style>
