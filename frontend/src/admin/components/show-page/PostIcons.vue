<script setup lang="ts">
import EpisodeIcon from '../episode/EpisodeIcon.vue';
import { BADGE, type TabItem } from '../episode/tabs';

// An episode's three Instagram posts as on the episode page's Social tab:
// one icon per post and a badge for its state. The label says the state
// (screen reader and tooltip).

defineProps<{
  items: TabItem[];
  /** Past rows use the smaller size. */
  small?: boolean;
}>();
</script>

<template>
  <ul :class="['post-icons', { small }]" aria-label="Instagram posts">
    <li
      v-for="(it, k) in items"
      :key="k"
      :class="['post', { todo: it.badge === 'todo' }]"
      role="img"
      :aria-label="it.label"
      :title="it.label"
    >
      <EpisodeIcon :name="it.icon" :size="small ? 16 : 18" />
      <span :class="['bdg', it.badge]" aria-hidden="true"
        ><svg
          v-if="BADGE[it.badge].d"
          viewBox="0 0 24 24"
          :fill="BADGE[it.badge].fill ? 'currentColor' : 'none'"
          stroke="currentColor"
          stroke-width="4"
          stroke-linecap="round"
          stroke-linejoin="round"
        >
          <path :d="BADGE[it.badge].d" /></svg
      ></span>
    </li>
  </ul>
</template>

<style scoped>
.post-icons {
  display: flex;
  gap: 12px;
  margin: 0;
  padding: 0 0 4px;
  list-style: none;
}

.post {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  border: 1.5px solid #34344c;
  border-radius: 10px;
  background: #16162e;
  color: #e6e6ef;
}

.post.todo {
  border-style: dashed;
  border-color: #55556a;
  background: transparent;
  color: #8e8ea4;
}

.small .post {
  width: 30px;
  height: 30px;
  border-radius: 9px;
}

/* Same badge colours as the episode page tabs. */
.bdg {
  position: absolute;
  right: -5px;
  bottom: -5px;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  box-shadow: 0 0 0 2px var(--color-surface);
  color: #000;
}

.bdg svg {
  width: 9px;
  height: 9px;
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
</style>
