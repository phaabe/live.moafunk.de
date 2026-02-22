<script setup lang="ts">
import { ref, computed, onMounted } from 'vue';
import { useRouter } from 'vue-router';
import { Calendar } from 'v-calendar';
import { showsApi, type Show } from '../api';
import { BaseButton, BaseModal, FormInput } from '@shared/components';
import { useFlash } from '../composables/useFlash';
import ShowList from '../components/ShowList.vue';

const router = useRouter();
const flash = useFlash();

const shows = ref<Show[]>([]);
const loading = ref(true);
const error = ref<string | null>(null);

// View mode
type ViewMode = 'month' | 'week' | 'list';
const viewMode = ref<ViewMode>('month');

// Selected date state
const selectedDate = ref<string | null>(null);

// Week view navigation
const weekStart = ref<Date>(getMonday(new Date()));

function getMonday(d: Date): Date {
  const date = new Date(d);
  date.setHours(0, 0, 0, 0);
  const day = date.getDay();
  const diff = day === 0 ? -6 : 1 - day; // Monday = 1
  date.setDate(date.getDate() + diff);
  return date;
}

function toDateStr(d: Date): string {
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  return `${yyyy}-${mm}-${dd}`;
}

const weekDays = computed(() => {
  const days: { date: Date; dateStr: string; label: string; dayName: string }[] = [];
  for (let i = 0; i < 7; i++) {
    const d = new Date(weekStart.value);
    d.setDate(d.getDate() + i);
    days.push({
      date: d,
      dateStr: toDateStr(d),
      label: d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }),
      dayName: d.toLocaleDateString('en-US', { weekday: 'short' }),
    });
  }
  return days;
});

const weekLabel = computed(() => {
  const start = weekDays.value[0];
  const end = weekDays.value[6];
  const startMonth = start.date.toLocaleDateString('en-US', { month: 'short' });
  const endMonth = end.date.toLocaleDateString('en-US', { month: 'short' });
  const year = end.date.getFullYear();
  if (startMonth === endMonth) {
    return `${start.date.getDate()} – ${end.date.getDate()} ${startMonth} ${year}`;
  }
  return `${start.date.getDate()} ${startMonth} – ${end.date.getDate()} ${endMonth} ${year}`;
});

function prevWeek() {
  const d = new Date(weekStart.value);
  d.setDate(d.getDate() - 7);
  weekStart.value = d;
}

function nextWeek() {
  const d = new Date(weekStart.value);
  d.setDate(d.getDate() + 7);
  weekStart.value = d;
}

function goToCurrentWeek() {
  weekStart.value = getMonday(new Date());
}

function showsForDate(dateStr: string): Show[] {
  return shows.value.filter((s) => s.date === dateStr);
}

// List view: all shows sorted by date (upcoming first)
type ListFilter = 'all' | 'upcoming' | 'past';
const listFilter = ref<ListFilter>('upcoming');

const listShows = computed(() => {
  let filtered = shows.value;
  if (listFilter.value === 'upcoming') {
    filtered = shows.value.filter((s) => getDaysUntil(s.date) >= 0);
  } else if (listFilter.value === 'past') {
    filtered = shows.value.filter((s) => getDaysUntil(s.date) < 0);
  }
  return [...filtered].sort((a, b) => new Date(a.date).getTime() - new Date(b.date).getTime());
});

// Create show modal state
const showCreateModal = ref(false);
const creating = ref(false);
const newShow = ref({ title: '', date: '', start_time: '', description: '', show_type: 'unheard' });

// Map shows to v-calendar attributes (dots on dates)
const calendarAttributes = computed(() => {
  const attrs: Record<string, unknown>[] = [];

  // Highlight today
  attrs.push({
    key: 'today',
    highlight: {
      color: 'yellow',
      fillMode: 'solid',
    },
    dates: new Date(),
  });

  for (const show of shows.value) {
    const daysUntil = getDaysUntil(show.date);
    let color = 'yellow'; // default: unheard
    if (daysUntil < 0) {
      color = 'gray'; // past
    } else {
      const type = show.show_type || 'unheard';
      if (type === 'brunchtime') color = 'green';
      else if (type === 'external') color = 'blue';
      else color = 'yellow'; // unheard
    }

    attrs.push({
      key: `show-${show.id}`,
      dot: { color, class: 'show-dot' },
      dates: new Date(show.date + 'T12:00:00'),
      popover: {
        label: show.title,
        visibility: 'hover' as const,
      },
      customData: show,
    });
  }

  return attrs;
});

// Shows for a selected date
const showsOnSelectedDate = computed(() => {
  if (!selectedDate.value) return [];
  return shows.value.filter((s) => s.date === selectedDate.value);
});

function getDaysUntil(dateStr: string): number {
  const showDate = new Date(dateStr);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  showDate.setHours(0, 0, 0, 0);
  const diffTime = showDate.getTime() - today.getTime();
  return Math.ceil(diffTime / (1000 * 60 * 60 * 24));
}

function getDaysClass(days: number): string {
  if (days < 0) return 'days-past';
  if (days <= 7) return 'days-critical';
  if (days <= 15) return 'days-warning';
  return 'days-ok';
}

function getDotColor(show: Show): string {
  const daysUntil = getDaysUntil(show.date);
  if (daysUntil < 0) return 'dot-gray';
  const type = show.show_type || 'unheard';
  if (type === 'brunchtime') return 'dot-green';
  if (type === 'external') return 'dot-blue';
  return 'dot-yellow';
}

function formatDate(dateStr: string): string {
  const d = new Date(dateStr + 'T12:00:00');
  return d.toLocaleDateString('en-US', {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  });
}

function isToday(dateStr: string): boolean {
  return dateStr === toDateStr(new Date());
}

function onDayClick(day: { id: string; date: Date }) {
  const date = day.date;
  selectedDate.value = toDateStr(date);
}

function openCreateModal(prefilledDate?: string) {
  newShow.value = {
    title: '',
    date: prefilledDate || selectedDate.value || '',
    start_time: '',
    description: '',
    show_type: 'unheard',
  };
  showCreateModal.value = true;
}

function goToShow(showId: number) {
  router.push(`/shows/${showId}`);
}

async function loadShows() {
  loading.value = true;
  error.value = null;
  try {
    const response = await showsApi.list();
    shows.value = response.shows;
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to load shows';
  } finally {
    loading.value = false;
  }
}

async function createShow() {
  creating.value = true;
  try {
    const created = await showsApi.create(newShow.value);
    flash.success('Show created successfully');
    showCreateModal.value = false;
    newShow.value = { title: '', date: '', start_time: '', description: '', show_type: 'unheard' };
    await loadShows();
    if (created?.id) {
      router.push(`/shows/${created.id}`);
    }
  } catch (e) {
    flash.error(e instanceof Error ? e.message : 'Failed to create show');
  } finally {
    creating.value = false;
  }
}

onMounted(loadShows);
</script>

<template>
  <div class="calendar-page">
    <div class="page-header">
      <h1 class="page-title">Calendar</h1>
      <div class="view-switcher">
        <button :class="['view-btn', { active: viewMode === 'month' }]" @click="viewMode = 'month'">Month</button>
        <button :class="['view-btn', { active: viewMode === 'week' }]" @click="viewMode = 'week'">Week</button>
        <button :class="['view-btn', { active: viewMode === 'list' }]" @click="viewMode = 'list'">List</button>
      </div>
      <div class="page-header-actions">
        <BaseButton variant="primary" @click="openCreateModal()">+ New Show</BaseButton>
      </div>
    </div>

    <div v-if="error" class="flash-message error">{{ error }}</div>
    <div v-if="loading" class="loading-spinner"></div>

    <!-- ===== MONTH VIEW ===== -->
    <div v-else-if="viewMode === 'month'" class="calendar-layout">
      <div class="calendar-card card">
        <Calendar :attributes="calendarAttributes" :is-dark="true" :first-day-of-week="2" is-expanded
          @dayclick="onDayClick" />
        <div class="calendar-legend">
          <span class="legend-item"><span class="legend-dot dot-yellow"></span> UNHEARD</span>
          <span class="legend-item"><span class="legend-dot dot-green"></span> Brunchtime</span>
          <span class="legend-item"><span class="legend-dot dot-blue"></span> External</span>
          <span class="legend-item"><span class="legend-dot dot-gray"></span> Past</span>
        </div>
      </div>

      <!-- Day detail sidebar -->
      <div class="day-detail card">
        <template v-if="selectedDate">
          <div class="day-detail-header">
            <h2 class="day-detail-title">{{ formatDate(selectedDate) }}</h2>
            <BaseButton variant="primary" size="sm" @click="openCreateModal(selectedDate)">
              + New Show
            </BaseButton>
          </div>

          <div v-if="showsOnSelectedDate.length === 0" class="day-detail-empty">
            <p class="text-muted">No shows on this date</p>
          </div>

          <div v-else class="day-shows-list">
            <div v-for="show in showsOnSelectedDate" :key="show.id" class="day-show-item" @click="goToShow(show.id)">
              <div class="day-show-info">
                <span class="day-show-title">{{ show.title }}</span>
                <span :class="['badge', 'show-type-badge', `type-${show.show_type || 'unheard'}`]">
                  {{ (show.show_type || 'unheard').toUpperCase() }}
                </span>
                <span v-if="show.show_type === 'unheard' || !show.show_type" class="day-show-artists text-muted">
                  {{show.artists.map((a) => a.name).join(', ') || 'No artists assigned'}}
                </span>
              </div>
              <div class="day-show-meta">
                <span :class="['badge', 'days-badge', getDaysClass(getDaysUntil(show.date))]">
                  {{ getDaysUntil(show.date) < 0 ? 'Past' : getDaysUntil(show.date) + 'd' }} </span>
                    <span v-if="show.show_type === 'unheard' || !show.show_type" :class="[
                      'badge',
                      'artist-badge',
                      {
                        'count-empty': show.artists.length === 0,
                        'count-partial': show.artists.length > 0 && show.artists.length < 4,
                        'count-full': show.artists.length >= 4,
                      },
                    ]">
                      {{ show.artists.length }}/4
                    </span>
              </div>
            </div>
          </div>
        </template>

        <template v-else>
          <div class="day-detail-empty">
            <p class="text-muted">Click a date to see shows</p>
          </div>
        </template>
      </div>
    </div>

    <!-- ===== WEEK VIEW ===== -->
    <div v-else-if="viewMode === 'week'" class="week-view">
      <div class="week-nav">
        <button class="week-nav-btn" @click="prevWeek">&lt;</button>
        <span class="week-nav-label">{{ weekLabel }}</span>
        <button class="week-nav-btn" @click="nextWeek">&gt;</button>
        <button class="week-nav-today" @click="goToCurrentWeek">Today</button>
      </div>

      <div class="week-grid">
        <div v-for="day in weekDays" :key="day.dateStr"
          :class="['week-day-column', { 'is-today': isToday(day.dateStr) }]">
          <div class="week-day-header">
            <span class="week-day-name">{{ day.dayName }}</span>
            <span class="week-day-date">{{ day.label }}</span>
          </div>
          <div class="week-day-shows">
            <div v-for="show in showsForDate(day.dateStr)" :key="show.id" class="week-show-item"
              @click="goToShow(show.id)">
              <span :class="['week-show-dot', getDotColor(show)]"></span>
              <div class="week-show-info">
                <span class="week-show-title">{{ show.title }}</span>
                <span class="week-show-artists text-muted">
                  {{show.artists.map((a) => a.name).join(', ') || '—'}}
                </span>
              </div>
              <span :class="[
                'badge',
                'artist-badge',
                {
                  'count-empty': show.artists.length === 0,
                  'count-partial': show.artists.length > 0 && show.artists.length < 4,
                  'count-full': show.artists.length >= 4,
                },
              ]">
                {{ show.artists.length }}/4
              </span>
            </div>
            <div v-if="showsForDate(day.dateStr).length === 0" class="week-day-empty text-muted">
              —
            </div>
          </div>
          <button class="week-day-add" @click="openCreateModal(day.dateStr)">+</button>
        </div>
      </div>
    </div>

    <!-- ===== LIST VIEW ===== -->
    <div v-else-if="viewMode === 'list'" class="list-view">
      <div class="list-toolbar">
        <div class="list-filters">
          <button :class="['view-btn', { active: listFilter === 'upcoming' }]"
            @click="listFilter = 'upcoming'">Upcoming</button>
          <button :class="['view-btn', { active: listFilter === 'all' }]" @click="listFilter = 'all'">All</button>
          <button :class="['view-btn', { active: listFilter === 'past' }]" @click="listFilter = 'past'">Past</button>
        </div>
        <span class="list-count text-muted">{{ listShows.length }} shows</span>
      </div>

      <ShowList :shows="shows" :filter="listFilter" @show-click="(show) => goToShow(show.id)" />
    </div>

    <!-- Create show modal -->
    <BaseModal :open="showCreateModal" title="Create New Show" @close="showCreateModal = false">
      <form class="create-form" @submit.prevent="createShow">
        <div class="form-group">
          <label class="form-label">Show Type</label>
          <select v-model="newShow.show_type" class="type-select">
            <option value="unheard">UNHEARD</option>
            <option value="brunchtime">Brunchtime</option>
            <option value="external">External</option>
          </select>
        </div>
        <FormInput v-model="newShow.title" label="Title" required />
        <FormInput v-model="newShow.date" label="Date" type="date" required />
        <FormInput v-model="newShow.start_time" label="Start Time" type="time" />
        <FormInput v-model="newShow.description" label="Description" />
      </form>
      <template #footer>
        <BaseButton variant="ghost" @click="showCreateModal = false">Cancel</BaseButton>
        <BaseButton variant="primary" :loading="creating" @click="createShow">
          Create Show
        </BaseButton>
      </template>
    </BaseModal>
  </div>
</template>

<style scoped>
.calendar-page {
  max-width: 1400px;
}

/* Page header — three-column: title | switcher (centered) | actions */
.page-header {
  display: grid;
  grid-template-columns: 1fr auto 1fr;
  align-items: center;
  gap: var(--spacing-md);
  margin-bottom: var(--spacing-lg);
}

.page-title {
  justify-self: start;
}

.page-header-actions {
  display: flex;
  align-items: center;
  gap: var(--spacing-md);
  justify-self: end;
}

/* View switcher */
.view-switcher {
  display: flex;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  overflow: hidden;
  justify-self: center;
}

.view-btn {
  background: transparent;
  border: none;
  color: var(--color-text-muted);
  font-family: var(--font-family);
  font-size: var(--font-size-lg);
  padding: var(--spacing-sm) var(--spacing-xl);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.view-btn:hover {
  background: var(--color-surface-hover);
  color: var(--color-text);
}

.view-btn.active {
  background: var(--color-primary);
  color: var(--color-primary-text);
  font-weight: var(--font-weight-bold);
}

/* Month view layout */
.calendar-layout {
  display: grid;
  grid-template-columns: 1fr 360px;
  gap: var(--spacing-lg);
  align-items: start;
}

/* Calendar card */
.calendar-card {
  overflow: hidden;
}

/* v-calendar dark theme overrides */
.calendar-card :deep(.vc-container) {
  --vc-bg: var(--color-surface);
  --vc-border: var(--color-border);
  --vc-color: var(--color-text);
  --vc-font-family: var(--font-family);
  --vc-text-lg: var(--font-size-lg);
  --vc-text-base: var(--font-size-base);
  --vc-text-sm: var(--font-size-sm);
  --vc-white: var(--color-text);
  /* Accent scale (yellow primary) */
  --vc-accent-50: rgba(255, 236, 68, 0.05);
  --vc-accent-100: rgba(255, 236, 68, 0.1);
  --vc-accent-200: rgba(255, 236, 68, 0.2);
  --vc-accent-300: rgba(255, 236, 68, 0.3);
  --vc-accent-400: rgba(255, 236, 68, 0.5);
  --vc-accent-500: #ffec44;
  --vc-accent-600: #ffec44;
  --vc-accent-700: #e6d43e;
  --vc-accent-800: #ccbc37;
  --vc-accent-900: #b3a530;
  /* Gray scale: keep 50=lightest, 900=darkest for v-calendar internals */
  --vc-gray-50: rgba(255, 255, 255, 0.05);
  --vc-gray-100: rgba(255, 255, 255, 0.08);
  --vc-gray-200: rgba(255, 255, 255, 0.12);
  --vc-gray-300: var(--color-border);
  --vc-gray-400: var(--color-border-light);
  --vc-gray-500: var(--color-text-muted);
  --vc-gray-600: #aaa;
  --vc-gray-700: var(--color-border);
  --vc-gray-800: var(--color-surface-alt);
  --vc-gray-900: var(--color-surface);
  /* Header */
  --vc-header-title-color: var(--color-text);
  --vc-header-arrow-color: var(--color-text-muted);
  --vc-header-arrow-hover-bg: var(--color-surface-hover);
  /* Weekdays */
  --vc-weekday-color: var(--color-text-muted);
  /* Popover (month/year picker, day popover) */
  --vc-popover-content-color: var(--color-text);
  --vc-popover-content-bg: var(--color-surface-alt);
  --vc-popover-content-border: var(--color-border);
  /* Nav (month/year grid) */
  --vc-nav-hover-bg: var(--color-surface-hover);
  --vc-nav-title-color: var(--color-text);
  --vc-nav-item-active-color: var(--color-primary-text);
  --vc-nav-item-active-bg: var(--color-primary);
  --vc-nav-item-current-color: var(--color-primary);
  /* Hover */
  --vc-hover-bg: var(--color-surface-hover);
  background: var(--color-surface);
  border: none;
  width: 100%;
}

.calendar-card :deep(.vc-header) {
  padding: var(--spacing-lg) var(--spacing-lg) var(--spacing-xl);
}

.calendar-card :deep(.vc-header .vc-title),
.calendar-card :deep(.vc-header .vc-prev),
.calendar-card :deep(.vc-header .vc-next) {
  background: transparent;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  font-family: var(--font-family);
  color: var(--color-text-muted);
}

.calendar-card :deep(.vc-header .vc-title) {
  color: var(--color-text);
  font-weight: var(--font-weight-bold);
}

.calendar-card :deep(.vc-header .vc-title:hover),
.calendar-card :deep(.vc-header .vc-prev:hover),
.calendar-card :deep(.vc-header .vc-next:hover) {
  background: var(--color-surface-hover);
}

/* Nav popover (month/year picker) */
.calendar-card :deep(.vc-popover-content) {
  background: var(--color-surface-alt) !important;
  border-color: var(--color-border) !important;
  color: var(--color-text);
  font-family: var(--font-family);
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.5);
}

.calendar-card :deep(.vc-nav-title),
.calendar-card :deep(.vc-nav-arrow) {
  font-family: var(--font-family);
  color: var(--color-text);
  background: transparent;
}

.calendar-card :deep(.vc-nav-title:hover),
.calendar-card :deep(.vc-nav-arrow:hover) {
  background: var(--color-surface-hover) !important;
}

.calendar-card :deep(.vc-nav-item) {
  font-family: var(--font-family);
  color: var(--color-text-muted);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
}

.calendar-card :deep(.vc-nav-item:hover) {
  background: var(--color-surface-hover) !important;
  color: var(--color-text);
}

.calendar-card :deep(.vc-nav-item.is-active) {
  color: var(--color-primary-text) !important;
  background: var(--color-primary) !important;
  border-color: var(--color-primary) !important;
}

.calendar-card :deep(.vc-nav-item.is-current) {
  color: var(--color-primary) !important;
  border-color: var(--color-primary) !important;
}

.calendar-card :deep(.vc-weekday) {
  color: var(--color-text-muted);
  font-family: var(--font-family);
  font-weight: var(--font-weight-medium);
}

.calendar-card :deep(.vc-day) {
  min-height: 60px;
}

.calendar-card :deep(.vc-day-content) {
  font-family: var(--font-family);
  color: var(--color-text);
  border-radius: var(--radius-md);
  width: 32px;
  height: 32px;
  transition: background var(--transition-fast);
}

.calendar-card :deep(.vc-day-content:hover) {
  background: var(--color-surface-hover);
}

.calendar-card :deep(.vc-day-content:focus) {
  background: var(--color-surface-alt);
}

.calendar-card :deep(.vc-highlight) {
  background: var(--color-primary) !important;
  border-radius: var(--radius-md);
}

.calendar-card :deep(.vc-highlight + .vc-day-content),
.calendar-card :deep(.vc-day.is-today .vc-day-content),
.calendar-card :deep(.vc-highlights + .vc-day-content) {
  color: #000 !important;
}

.calendar-card :deep(.vc-dot) {
  width: 8px;
  height: 8px;
}

/* v-calendar popover dark theme */
.calendar-card :deep(.vc-popover-content) {
  background: var(--color-surface-alt);
  border: 1px solid var(--color-border);
  color: var(--color-text);
  font-family: var(--font-family);
  border-radius: var(--radius-md);
}

/* Legend */
.calendar-legend {
  display: flex;
  gap: var(--spacing-lg);
  padding: var(--spacing-md) var(--spacing-lg);
  border-top: 1px solid var(--color-border);
  justify-content: center;
}

.legend-item {
  display: flex;
  align-items: center;
  gap: var(--spacing-xs);
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}

.legend-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  display: inline-block;
}

.dot-yellow {
  background-color: #ffec44;
}

.dot-orange {
  background-color: #ff9500;
}

.dot-green {
  background-color: #34c759;
}

.dot-blue {
  background-color: #3478f6;
}

.dot-gray {
  background-color: #888;
}

/* Day detail sidebar */
.day-detail {
  position: sticky;
  top: calc(var(--spacing-lg) + 60px);
  /* below navbar */
}

.day-detail-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: var(--spacing-md);
}

.day-detail-title {
  font-size: var(--font-size-lg);
  font-weight: var(--font-weight-bold);
  color: var(--color-text);
  margin: 0;
}

.day-detail-empty {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--spacing-2xl) var(--spacing-md);
}

/* Show list items */
.day-shows-list {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-sm);
}

.day-show-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: var(--spacing-md);
  border-radius: var(--radius-md);
  background: var(--color-surface-alt);
  cursor: pointer;
  transition: background var(--transition-fast);
}

.day-show-item:hover {
  background: var(--color-surface-hover);
}

.day-show-info {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
  min-width: 0;
}

.day-show-title {
  color: var(--color-primary);
  font-weight: var(--font-weight-medium);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.day-show-artists {
  font-size: var(--font-size-sm);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.day-show-meta {
  display: flex;
  gap: var(--spacing-xs);
  flex-shrink: 0;
  margin-left: var(--spacing-sm);
}

/* Badges */
.badge {
  font-weight: var(--font-weight-bold);
  font-size: var(--font-size-sm);
  padding: 0.25rem 0.5rem;
  border-radius: var(--radius-sm);
  white-space: nowrap;
}

.days-badge {
  min-width: 40px;
  text-align: center;
}

.days-past {
  background-color: rgba(128, 128, 128, 0.2);
  color: #888;
  border: 1px solid #888;
}

.days-critical {
  background-color: rgba(255, 59, 48, 0.2);
  color: #ff3b30;
  border: 1px solid #ff3b30;
}

.days-warning {
  background-color: rgba(255, 149, 0, 0.2);
  color: #ff9500;
  border: 1px solid #ff9500;
}

.days-ok {
  background-color: rgba(52, 199, 89, 0.2);
  color: #34c759;
  border: 1px solid #34c759;
}

.count-empty {
  background-color: rgba(255, 59, 48, 0.2);
  color: #ff3b30;
  border: 1px solid #ff3b30;
}

.count-partial {
  background-color: rgba(255, 149, 0, 0.2);
  color: #ff9500;
  border: 1px solid #ff9500;
}

.count-full {
  background-color: rgba(52, 199, 89, 0.2);
  color: #34c759;
  border: 1px solid #34c759;
}

/* Create form */
.create-form {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
}

.form-group {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
}

.form-label {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
  font-weight: var(--font-weight-medium);
}

.type-select {
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  color: var(--color-text);
  font-family: var(--font-family);
  padding: var(--spacing-sm) var(--spacing-md);
}

/* Show type badges */
.show-type-badge {
  font-weight: var(--font-weight-bold);
  font-size: var(--font-size-xs, 0.65rem);
  padding: 0.15rem 0.4rem;
  border-radius: var(--radius-sm);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.type-unheard {
  background-color: rgba(255, 236, 68, 0.2);
  color: #ffec44;
  border: 1px solid #ffec44;
}

.type-brunchtime {
  background-color: rgba(52, 199, 89, 0.2);
  color: #34c759;
  border: 1px solid #34c759;
}

.type-external {
  background-color: rgba(52, 120, 246, 0.2);
  color: #3478f6;
  border: 1px solid #3478f6;
}

/* ===== WEEK VIEW ===== */
.week-nav {
  display: flex;
  align-items: center;
  gap: var(--spacing-md);
  margin-bottom: var(--spacing-lg);
}

.week-nav-btn {
  background: transparent;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  color: var(--color-text-muted);
  font-family: var(--font-family);
  font-size: var(--font-size-lg);
  width: 36px;
  height: 36px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all var(--transition-fast);
}

.week-nav-btn:hover {
  background: var(--color-surface-hover);
  color: var(--color-text);
}

.week-nav-label {
  font-weight: var(--font-weight-bold);
  font-size: var(--font-size-lg);
  color: var(--color-text);
  min-width: 260px;
  text-align: center;
}

.week-nav-today {
  background: transparent;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  color: var(--color-text-muted);
  font-family: var(--font-family);
  font-size: var(--font-size-sm);
  padding: var(--spacing-xs) var(--spacing-md);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.week-nav-today:hover {
  background: var(--color-surface-hover);
  color: var(--color-text);
}

.week-grid {
  display: grid;
  grid-template-columns: repeat(7, 1fr);
  gap: var(--spacing-sm);
}

.week-day-column {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  display: flex;
  flex-direction: column;
  min-height: 200px;
}

.week-day-column.is-today {
  border-color: var(--color-primary);
  box-shadow: 0 0 0 1px var(--color-primary);
}

.week-day-header {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: var(--spacing-sm) var(--spacing-sm) var(--spacing-xs);
  border-bottom: 1px solid var(--color-border);
}

.week-day-name {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
  font-weight: var(--font-weight-medium);
}

.week-day-date {
  font-size: var(--font-size-sm);
  color: var(--color-text);
}

.is-today .week-day-name,
.is-today .week-day-date {
  color: var(--color-primary);
  font-weight: var(--font-weight-bold);
}

.week-day-shows {
  flex: 1;
  padding: var(--spacing-xs);
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
}

.week-day-empty {
  display: flex;
  align-items: center;
  justify-content: center;
  flex: 1;
  font-size: var(--font-size-sm);
}

.week-show-item {
  display: flex;
  align-items: center;
  gap: var(--spacing-xs);
  padding: var(--spacing-xs) var(--spacing-sm);
  background: var(--color-surface-alt);
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: background var(--transition-fast);
}

.week-show-item:hover {
  background: var(--color-surface-hover);
}

.week-show-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  flex-shrink: 0;
}

.week-show-info {
  display: flex;
  flex-direction: column;
  gap: 1px;
  min-width: 0;
  flex: 1;
}

.week-show-title {
  font-size: var(--font-size-sm);
  color: var(--color-primary);
  font-weight: var(--font-weight-medium);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.week-show-artists {
  font-size: 0.65rem;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.week-day-add {
  background: transparent;
  border: none;
  border-top: 1px solid var(--color-border);
  color: var(--color-text-muted);
  font-family: var(--font-family);
  font-size: var(--font-size-base);
  padding: var(--spacing-xs);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.week-day-add:hover {
  background: var(--color-surface-hover);
  color: var(--color-primary);
}

/* ===== LIST VIEW ===== */
.list-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--spacing-lg);
}

.list-filters {
  display: flex;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  overflow: hidden;
}

.list-count {
  font-size: var(--font-size-sm);
}

/* Responsive */
@media (max-width: 900px) {
  .calendar-layout {
    grid-template-columns: 1fr;
  }

  .day-detail {
    position: static;
  }

  .calendar-legend {
    flex-wrap: wrap;
    gap: var(--spacing-md);
  }

  .week-grid {
    grid-template-columns: 1fr;
  }

  .week-day-column {
    min-height: auto;
  }

  .page-header {
    grid-template-columns: 1fr 1fr;
    grid-template-rows: auto auto;
  }

  .view-switcher {
    grid-column: 1 / -1;
    justify-self: center;
  }

  .page-header-actions {
    flex-wrap: wrap;
    justify-self: end;
  }
}
</style>
