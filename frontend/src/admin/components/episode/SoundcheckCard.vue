<script setup lang="ts">
import { computed } from 'vue';
import { BaseButton } from '@shared/components';
import type { ShowDetail } from '../../api';
import { episodeDay, setLength } from '../../episodeRow';
import AudioPlayer from '../AudioPlayer.vue';
import BoardIcon from '../BoardIcon.vue';
import EpisodeCard from './EpisodeCard.vue';
import { berlinTime, episodeTimes, type EpisodePhase, type Status } from './phase';

// Soundcheck card (pre-show). Live: the Studio and Open Studio. Pre-recorded:
// the uploaded set with Replace file, or Upload your set. The Studio does not
// save the test result, so the card says "tested" only for a soundcheck passed
// in this browser tab.

const props = defineProps<{
  show: ShowDetail;
  phase: EpisodePhase;
  now: number;
  /** The soundcheck passed in this tab. */
  tested?: boolean;
  /** Another host's episode: status only, no buttons. */
  readOnly?: boolean;
  busy?: boolean;
}>();
const emit = defineEmits<{ openStudio: []; replaceFile: [] }>();

const prerecorded = computed(() => props.show.stream_mode === 'prerecorded');
const hasFile = computed(() => !!props.show.prerecorded_filename);
const openMs = computed(() => episodeTimes(props.show).openMs);
const studioOpen = computed(
  () => !!props.show.on_air || (openMs.value !== null && props.now >= openMs.value)
);
const current = computed(() => props.phase === 'pre');

/** "brunchtime-0811.mp3 · 1:58:40" */
const setFile = computed(() => {
  const name = props.show.prerecorded_filename;
  const length = props.show.prerecorded_duration_secs;
  if (!name) return '';
  return length ? `${name} · ${setLength(length)}` : name;
});

/** "Open since 11:30", "Opens 11:30", "Opens So 25.10. · 11:30". */
const studio = computed(() => {
  if (openMs.value === null) return '';
  const at = berlinTime(openMs.value);
  if (props.phase !== 'pre') return `Opened ${at}`;
  if (studioOpen.value) return `Open since ${at}`;
  const today = new Date(props.now).toLocaleDateString('en-CA', { timeZone: 'Europe/Berlin' });
  return props.show.date === today ? `Opens ${at}` : `Opens ${episodeDay(props.show.date)} · ${at}`;
});

const status = computed<Status>(() => {
  if (prerecorded.value) {
    return hasFile.value
      ? { tone: 'green', text: 'Set uploaded' }
      : { tone: props.phase === 'pre' ? 'amber' : 'red', text: 'No file yet' };
  }
  if (props.tested) return { tone: 'green', text: 'Tested' };
  if (props.phase !== 'pre') return { tone: 'grey', text: 'Over' };
  if (studioOpen.value) return { tone: 'amber', text: 'Studio open' };
  return { tone: 'grey', text: 'Studio closed' };
});
</script>

<template>
  <EpisodeCard title="Soundcheck" phase="Pre-show" :status="status" :current="current">
    <template #icon><BoardIcon name="headphones" :size="16" /></template>

    <template v-if="prerecorded">
      <template v-if="hasFile">
        <p class="ep-lead">
          {{
            phase === 'pre'
              ? 'Nothing to do. The set plays at air time.'
              : 'The set was uploaded before the show.'
          }}
        </p>
        <div v-if="setFile" class="set">
          <p class="set-file"><BoardIcon name="music" :size="16" />{{ setFile }}</p>
          <!-- The player shows no file name, and 0:00 until its first Play. -->
          <AudioPlayer v-if="show.prerecorded_url && !readOnly" :src="show.prerecorded_url" />
        </div>
      </template>
      <p v-else class="ep-lead">Upload your set before air time. Without a file nothing plays.</p>
    </template>
    <template v-else>
      <p class="ep-lead">
        {{
          phase === 'pre'
            ? 'Test your audio before air time. The Studio opens 30 min before the start.'
            : 'The soundcheck is done in the Studio before the show.'
        }}
      </p>
      <dl class="ep-kv">
        <div v-if="studio">
          <dt>Studio</dt>
          <dd>{{ studio }}</dd>
        </div>
        <div v-if="tested">
          <dt>Soundcheck</dt>
          <dd class="ok">Passed in this browser</dd>
        </div>
      </dl>
    </template>

    <template v-if="!readOnly && phase === 'pre'" #actions>
      <template v-if="prerecorded">
        <BaseButton
          v-if="hasFile"
          variant="secondary"
          :disabled="busy || !show.series_slug"
          @click="emit('replaceFile')"
        >
          <BoardIcon name="upload" :size="16" />Replace file
        </BaseButton>
        <BaseButton
          v-else
          class="xl"
          :disabled="busy || !show.series_slug"
          @click="emit('replaceFile')"
        >
          <BoardIcon name="upload" />Upload your set
        </BaseButton>
      </template>
      <BaseButton
        v-else
        class="xl"
        :disabled="!studioOpen"
        :loading="busy"
        @click="emit('openStudio')"
      >
        <BoardIcon name="mic" />Open Studio
      </BaseButton>
    </template>
  </EpisodeCard>
</template>

<style scoped>
.set {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 14px;
  border: 1px solid #2a2a42;
  border-radius: var(--radius-lg);
  background: #07071a;
}

.set-file {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0;
  overflow-wrap: anywhere;
  font: 700 var(--font-size-sm) var(--font-family);
}

.set-file svg {
  flex: none;
  color: var(--color-primary);
}
</style>
