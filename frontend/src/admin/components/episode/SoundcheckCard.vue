<script setup lang="ts">
import { computed } from 'vue';
import { BaseButton } from '@shared/components';
import type { ShowDetail } from '../../api';
import { setLength } from '../../episodeRow';
import AudioPlayer from '../AudioPlayer.vue';
import BoardIcon from '../BoardIcon.vue';
import EpisodeCard from './EpisodeCard.vue';
import type { EpisodePhase, Status, StepState } from './phase';

// Soundcheck card (step 2) of a pre-recorded episode: the uploaded set with
// Replace file, or Upload your set. Its main button is yellow only while
// Soundcheck is the current step. A live episode shows the Studio instead
// (FlowLive, or StudioView read-only).

const props = defineProps<{
  show: ShowDetail;
  phase: EpisodePhase;
  /** Where the Soundcheck step is. */
  state: StepState;
  /** Another host's episode: status only, no buttons. */
  readOnly?: boolean;
  busy?: boolean;
}>();
const emit = defineEmits<{ replaceFile: [] }>();

const hasFile = computed(() => !!props.show.prerecorded_filename);
const variant = computed(() => (props.state === 'now' ? 'primary' : 'secondary'));

/** "brunchtime-0811.mp3 · 1:58:40" */
const setFile = computed(() => {
  const name = props.show.prerecorded_filename;
  const length = props.show.prerecorded_duration_secs;
  if (!name) return '';
  return length ? `${name} · ${setLength(length)}` : name;
});

const status = computed<Status>(() =>
  hasFile.value
    ? { tone: 'green', text: 'Set uploaded' }
    : { tone: props.phase === 'pre' ? 'amber' : 'red', text: 'No file yet' }
);
</script>

<template>
  <EpisodeCard title="Soundcheck" :step="2" :state="state" :status="status">
    <template #icon><BoardIcon name="headphones" :size="16" /></template>

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

    <template v-if="!readOnly && phase === 'pre'" #actions>
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
        :variant="variant"
        :disabled="busy || !show.series_slug"
        @click="emit('replaceFile')"
      >
        <BoardIcon name="upload" />Upload your set
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
