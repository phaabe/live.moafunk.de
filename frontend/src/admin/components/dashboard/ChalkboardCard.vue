<script setup lang="ts">
import { nextTick, onMounted, onUnmounted, ref } from 'vue';
import { chalkboardApi, type ChalkNote } from '../../api';
import BoardIcon from '../BoardIcon.vue';

// Dashboard card "Chalkboard" (#1049): short notes that every host and admin
// sees. Stored by the backend, no Telegram. Reloads every 15 s.
const props = defineProps<{ username: string | undefined }>();

const REFRESH_MS = 15_000;
const MAX_CHARS = 500;

const notes = ref<ChalkNote[]>([]);
const loaded = ref(false);
const loadError = ref<string | null>(null);
const sendError = ref<string | null>(null);
const sending = ref(false);
const draft = ref('');
const listEl = ref<HTMLElement | null>(null);

const mine = (n: ChalkNote) => !!props.username && n.author === props.username;

/** A host's next note within this many seconds goes under the same name and time. */
const STACK_S = 3600;
function stacked(i: number): boolean {
  const prev = notes.value[i - 1];
  const n = notes.value[i];
  return !!prev && prev.author === n.author && n.ts - prev.ts <= STACK_S;
}

const BERLIN = { timeZone: 'Europe/Berlin' } as const;
const dayOf = (at: Date) => at.toLocaleDateString('sv-SE', BERLIN);

/** "10:20" today, "18.10. 10:20" on another day (Berlin time, like the episodes). */
function when(ts: number): string {
  const at = new Date(ts * 1000);
  const time = at.toLocaleTimeString('de-DE', { ...BERLIN, hour: '2-digit', minute: '2-digit' });
  if (dayOf(at) === dayOf(new Date())) return time;
  const day = at.toLocaleDateString('de-DE', { ...BERLIN, day: '2-digit', month: '2-digit' });
  return `${day} ${time}`;
}

/** Show `next`, and follow it to the bottom unless the reader scrolled up. */
async function show(next: ChalkNote[], follow = false) {
  const el = listEl.value;
  const atBottom = !el || el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  notes.value = next;
  await nextTick();
  if (listEl.value && (follow || atBottom)) listEl.value.scrollTop = listEl.value.scrollHeight;
}

async function load() {
  try {
    await show((await chalkboardApi.list()).notes);
    loadError.value = null;
  } catch (e) {
    console.error('Failed to load the chalkboard:', e);
    loadError.value = 'Could not load the chalkboard.';
  } finally {
    loaded.value = true;
  }
}

async function onPost() {
  const text = draft.value.trim();
  if (!text || sending.value) return;
  sending.value = true;
  try {
    const note = await chalkboardApi.add(text);
    draft.value = '';
    sendError.value = null;
    await show([...notes.value.filter((n) => n.id !== note.id), note], true);
  } catch (e) {
    console.error('Failed to write on the chalkboard:', e);
    sendError.value = 'The note was not saved.';
  } finally {
    sending.value = false;
  }
}

let timer: ReturnType<typeof setInterval> | null = null;
onMounted(() => {
  void load();
  timer = setInterval(() => void load(), REFRESH_MS);
});
onUnmounted(() => {
  if (timer) clearInterval(timer);
  timer = null;
});
</script>

<template>
  <section class="board-card chalkboard-card">
    <div class="board-card-head">
      <h2 class="board-card-title"><BoardIcon name="pencil" />Chalkboard</h2>
      <span class="chalk-sub">All hosts</span>
    </div>
    <div ref="listEl" class="chalk-notes">
      <p v-if="loadError" class="chalk-empty field-error" role="alert">{{ loadError }}</p>
      <p v-else-if="loaded && !notes.length" class="chalk-empty">The board is empty.</p>
      <ul v-if="notes.length" class="chalk-list" aria-label="Notes">
        <li
          v-for="(n, i) in notes"
          :key="n.id"
          :class="['chalk-note', { mine: mine(n), stacked: stacked(i) }]"
        >
          <span v-if="!stacked(i)" class="chalk-meta"
            ><span class="chalk-author">{{ mine(n) ? `${n.author} · you` : n.author }}</span>
            <span class="chalk-time">{{ when(n.ts) }}</span></span
          >
          <span class="chalk-text">{{ n.text }}</span>
        </li>
      </ul>
    </div>
    <p v-if="sendError" class="chalk-problem" role="alert">{{ sendError }}</p>
    <form class="chalk-compose" @submit.prevent="onPost">
      <label class="sr-only" for="dashboard-chalk-input">Note for all hosts</label>
      <input
        id="dashboard-chalk-input"
        v-model="draft"
        type="text"
        class="chalk-input"
        placeholder="Write on the board"
        :maxlength="MAX_CHARS"
      />
      <button
        type="submit"
        class="chalk-post"
        aria-label="Post"
        :disabled="sending || !draft.trim()"
      >
        <BoardIcon name="send" />
      </button>
    </form>
  </section>
</template>

<style scoped>
.chalkboard-card {
  display: flex;
  flex-direction: column;
  width: 100%;
  min-width: 0;
}

.host-page .chalkboard-card .board-card-head {
  justify-content: flex-start;
  flex-wrap: wrap;
  gap: 10px;
}

.chalk-sub {
  margin-left: auto;
  font: 600 12px var(--font-ui);
  color: #a0a0b4;
}

/* The board: slate with chalk-white writing. Beside the episodes it fills the
   card's height and scrolls; it never makes the column taller. */
.chalk-notes {
  flex: 1 1 0;
  min-height: 240px;
  overflow-y: auto;
  margin: 12px;
  padding: 14px 16px;
  border: 1px solid #2c3532;
  border-radius: var(--radius-lg);
  background: #161c1b;
}

.chalk-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
}

.chalk-note {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 10px 0;
}

.chalk-note + .chalk-note {
  border-top: 1px dashed #3a4441;
}

/* A follow-up note of the same host: no line, no name, close to the one above. */
.chalk-note + .chalk-note.stacked {
  border-top: none;
  margin-top: -6px;
  padding-top: 0;
}

.chalk-meta {
  display: flex;
  align-items: baseline;
  gap: 8px;
}

.chalk-author {
  font-weight: 700;
  font-size: var(--font-size-sm);
  color: #a9b4b0;
}

.chalk-note.mine .chalk-author {
  color: #fff;
}

.chalk-time {
  font: 12px var(--font-family);
  color: #87928e;
}

.chalk-text {
  font: 15px/1.45 var(--font-family);
  color: #eef2ef;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}

.chalk-empty {
  margin: 0;
  color: #a9b4b0;
  font-size: var(--font-size-sm);
  text-align: center;
}

.chalk-problem {
  margin: 0;
  padding: 0 20px 8px;
  color: var(--color-error);
  font-size: var(--font-size-sm);
}

.chalk-compose {
  display: flex;
  gap: 8px;
  padding: 0 12px 12px;
}

.chalk-input {
  flex: 1;
  min-width: 0;
  height: 44px;
  padding: 0 12px;
  border: 1px solid #34344c;
  border-radius: var(--radius-lg);
  background: #06060f;
  color: #eee;
  font: 15px var(--font-ui);
}

.chalk-input:focus {
  outline: none;
  border-color: #8e8ea4;
}

.chalk-post {
  flex: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 44px;
  height: 44px;
  border: 1px solid #4a4a60;
  border-radius: var(--radius-lg);
  background: transparent;
  color: #fff;
  cursor: pointer;
}

.chalk-post:disabled {
  color: #5c5c78;
  cursor: default;
}

.chalk-post:focus-visible {
  outline: 2px solid var(--color-primary);
  outline-offset: 2px;
}

.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
}

@media (max-width: 640px) {
  .chalk-notes {
    flex: none;
    min-height: 120px;
    max-height: 320px;
  }
}
</style>
