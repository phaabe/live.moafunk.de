<script setup lang="ts">
import BoardIcon from '../BoardIcon.vue';
import EpisodeIcon from './EpisodeIcon.vue';
import type { Segment } from './phase';
import { usePhone } from './phone';

// Step icons (#1052, option E): Set up social media › Soundcheck › Show time ›
// Publish recording, each a round icon with its name and a status line. The
// current step is yellow, done steps dim yellow with a check, next steps
// gray. The line after the current step fills as the step goes on. The card
// of each step has a frame in the same colour.

defineProps<{ segments: Segment[] }>();
const phone = usePhone();
</script>

<template>
  <div class="ep-progress">
    <ol class="segs" aria-label="Episode progress">
      <li
        v-for="(seg, i) in segments"
        :key="seg.name"
        :class="['seg', seg.state]"
        :aria-current="seg.state === 'now' ? 'step' : undefined"
        :data-fill="seg.fill"
        :style="{ '--fill': `${seg.fill}%` }"
      >
        <span class="ic" aria-hidden="true">
          <EpisodeIcon v-if="i === 0" name="instagram" :size="phone ? 18 : 24" />
          <BoardIcon v-else-if="i === 1" name="headphones" :size="phone ? 18 : 24" />
          <BoardIcon v-else-if="i === 2" name="broadcast" :size="phone ? 18 : 24" />
          <EpisodeIcon v-else name="cloud" :size="phone ? 18 : 24" />
          <span v-if="seg.state === 'done'" class="chk"
            ><BoardIcon name="check" :size="phone ? 9 : 12"
          /></span>
        </span>
        <b
          >{{ i + 1 }} · {{ phone ? seg.short : seg.name
          }}<span v-if="seg.state === 'done'" class="sep"> (done)</span></b
        >
        <span class="seg-label">{{ seg.label }}</span>
      </li>
    </ol>
  </div>
</template>

<style scoped>
.ep-progress {
  padding: 18px var(--spacing-lg) 16px;
  border-top: 1px solid #2a2a42;
  background: #0a0a1c;
}

.segs {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  margin: 0;
  padding: 0;
  list-style: none;
}

.seg {
  --c: #8e8ea4;
  --line: #26263e;
  --size: 56px;
  --gap: 12px;
  position: relative;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
  min-width: 0;
  padding: 0 4px;
  text-align: center;
  line-height: 1.3;
}

.seg.done {
  --c: #8f8536;
  --line: #8f8536;
}

.seg.now {
  --c: var(--color-primary);
  --line: linear-gradient(to right, var(--color-primary) var(--fill), #26263e var(--fill));
}

/* The line to the next step, through the middle of the icons. */
.seg:not(:last-child)::after {
  content: '';
  position: absolute;
  top: calc(var(--size) / 2 - 2px);
  left: calc(50% + var(--size) / 2 + var(--gap));
  width: calc(100% - var(--size) - 2 * var(--gap));
  height: 4px;
  border-radius: 2px;
  background: var(--line);
}

.ic {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  width: var(--size);
  height: var(--size);
  margin-bottom: 6px;
  border: 2px solid #34344c;
  border-radius: 50%;
  background: var(--color-surface);
  color: #8e8ea4;
}

.done .ic {
  border-color: #8f8536;
  background: #1c1a0a;
  color: #8f8536;
}

.now .ic {
  border-color: var(--color-primary);
  background: var(--color-primary);
  color: #000;
  box-shadow: 0 0 0 5px rgba(255, 236, 68, 0.22);
}

.chk {
  position: absolute;
  top: -4px;
  right: -4px;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  border-radius: 50%;
  background: #8f8536;
  color: #000;
}

.seg b {
  font-size: 14px;
  color: var(--c);
  overflow-wrap: break-word;
}

.seg-label {
  font: 700 12px var(--font-family);
  color: #a8a8bc;
  overflow-wrap: break-word;
}

.seg.now .seg-label {
  color: #e4e4ee;
}

@media (max-width: 640px) {
  .ep-progress {
    padding: 14px var(--spacing-sm) 14px;
  }

  .seg {
    --size: 40px;
    --gap: 6px;
    padding: 0 2px;
  }

  .chk {
    width: 16px;
    height: 16px;
  }

  .seg b {
    font-size: 12px;
  }

  .seg-label {
    font-size: 11px;
  }
}
</style>
