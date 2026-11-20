<script setup lang="ts">
import { BaseButton } from '@shared/components';
import type { SeriesSlot } from '../../api';
import BoardIcon from '../BoardIcon.vue';
import DateChip from '../DateChip.vue';
import { slotTime } from '../../episodeRow';

// The next free slots of the Show's rhythm (they were on Schedule), each with
// Book. "Book Slot" in the header books any other date. Owner only.

defineProps<{
  slots: SeriesSlot[];
  error: string | null;
  /** "No fixed rhythm": Book Slot picks any free date and time. */
  noRhythm: boolean;
}>();
const emit = defineEmits<{ book: [slot: SeriesSlot | null] }>();
</script>

<template>
  <section class="board-card free-slots">
    <div class="board-card-head">
      <h2 class="board-card-title"><BoardIcon name="calendar-plus" />Free slots</h2>
      <BaseButton variant="secondary" class="book-any" @click="emit('book', null)"
        >Book Slot</BaseButton
      >
    </div>
    <p v-if="error" class="empty field-error" role="alert">{{ error }}</p>
    <p v-else-if="noRhythm" class="empty">No fixed rhythm. Book Slot picks any free date.</p>
    <p v-else-if="!slots.length" class="empty">No free slots in your rhythm.</p>
    <!-- Row text stays "So 01.11. · 12:00–14:00 | Free"; Book is its own item. -->
    <ul v-else class="board-rows slots">
      <li v-for="slot in slots" :key="`${slot.date} ${slot.start_time}`">
        <div class="board-row slot-row">
          <DateChip :date="slot.date" class="free" />
          <span class="slot-text">
            <span class="sep"> · </span>
            <b class="row-time">{{ slotTime(slot) }}</b>
            <!-- The leading space keeps "12:00–14:00 Free" apart in the text. -->
            <span class="row-sub"> Free</span>
          </span>
          <BaseButton variant="secondary" class="book" @click="emit('book', slot)">Book</BaseButton>
        </div>
      </li>
    </ul>
  </section>
</template>

<style scoped>
.board-card-head {
  min-height: 52px;
  padding-top: 6px;
  padding-bottom: 6px;
}

.host-page button.base-button.book-any,
.host-page button.base-button.book {
  min-height: 40px;
  padding: 0 14px;
  font-size: 12px;
}

/* Date, time over "Free", Book: one line on every width. */
.host-page .board-card > .board-rows .slot-row {
  display: flex;
  align-items: center;
  gap: 14px;
  min-height: 72px;
}

.date-chip.free {
  background: transparent;
  box-shadow: inset 0 0 0 1.5px #4a4a60;
}

.slot-text {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.slot-text .row-time {
  color: #fff;
}
</style>
