<script setup lang="ts">
import { ref, computed, onMounted, toRaw } from 'vue';
import type { OverlayParams, OverlayElementParams, OverlayFilterParams, OverlayPreset, OverlayShadowParams } from '../api';
import { presetsApi } from '../api';
import { getDefaultOverlayParams } from '../composables/useOverlayRenderer';

const props = defineProps<{
  modelValue: OverlayParams;
  artistName: string;
}>();

const emit = defineEmits<{
  (e: 'update:modelValue', value: OverlayParams): void;
}>();

// ---------------------------------------------------------------------------
// Helpers for two-way binding
// ---------------------------------------------------------------------------

function updateElement(key: keyof OverlayParams, field: keyof OverlayElementParams, value: unknown): void {
  const el = { ...(props.modelValue[key] as OverlayElementParams), [field]: value };
  emit('update:modelValue', { ...props.modelValue, [key]: el });
}

function updateShadow(key: keyof OverlayParams, field: keyof OverlayShadowParams, value: unknown): void {
  const el = props.modelValue[key] as OverlayElementParams;
  const shadow = { ...(el.shadow ?? { offsetX: 0, offsetY: 0, color: '#000000' }), [field]: value };
  emit('update:modelValue', { ...props.modelValue, [key]: { ...el, shadow } });
}

function updateFilter(field: keyof OverlayFilterParams, value: number): void {
  const filter = { ...props.modelValue.filter, [field]: value };
  emit('update:modelValue', { ...props.modelValue, filter });
}

const defaults = getDefaultOverlayParams();

// ---------------------------------------------------------------------------
// Collapsible sections
// ---------------------------------------------------------------------------

const expandedSections = ref<Record<string, boolean>>({
  presets: true,
  un: true,
  heard: true,
  logo: true,
  artistName: true,
  filter: true,
});

function toggleSection(key: string): void {
  expandedSections.value[key] = !expandedSections.value[key];
}

// ---------------------------------------------------------------------------
// Preset management
// ---------------------------------------------------------------------------

const presets = ref<OverlayPreset[]>([]);
const selectedPresetId = ref<number | null>(null);
const presetLoading = ref(false);
const presetSaving = ref(false);
const newPresetName = ref('');
const showSaveInput = ref(false);

const selectedPreset = computed(() =>
  presets.value.find((p) => p.id === selectedPresetId.value) ?? null,
);

async function fetchPresets(): Promise<void> {
  presetLoading.value = true;
  try {
    const { presets: list } = await presetsApi.list();
    presets.value = list;
  } catch (err) {
    console.error('Failed to fetch presets:', err);
  } finally {
    presetLoading.value = false;
  }
}

function loadPreset(): void {
  if (!selectedPreset.value) return;
  const raw = toRaw(selectedPreset.value.params);
  // params may be a parsed object or a JSON string from the backend
  const plain = typeof raw === 'string' ? JSON.parse(raw) : JSON.parse(JSON.stringify(raw));
  emit('update:modelValue', plain);
}

async function saveAsNew(): Promise<void> {
  const name = newPresetName.value.trim();
  if (!name) return;
  presetSaving.value = true;
  try {
    const created = await presetsApi.create(name, JSON.parse(JSON.stringify(toRaw(props.modelValue))));
    presets.value.push(created);
    selectedPresetId.value = created.id;
    newPresetName.value = '';
    showSaveInput.value = false;
  } catch (err) {
    console.error('Failed to save preset:', err);
    alert(err instanceof Error ? err.message : 'Save failed');
  } finally {
    presetSaving.value = false;
  }
}

async function updateCurrent(): Promise<void> {
  if (!selectedPreset.value) return;
  presetSaving.value = true;
  try {
    const updated = await presetsApi.update(selectedPreset.value.id, {
      params: JSON.parse(JSON.stringify(toRaw(props.modelValue))),
    });
    const idx = presets.value.findIndex((p) => p.id === updated.id);
    if (idx >= 0) presets.value[idx] = updated;
  } catch (err) {
    console.error('Failed to update preset:', err);
    alert(err instanceof Error ? err.message : 'Update failed');
  } finally {
    presetSaving.value = false;
  }
}

async function deleteCurrent(): Promise<void> {
  if (!selectedPreset.value) return;
  if (!confirm(`Delete preset "${selectedPreset.value.name}"?`)) return;
  presetSaving.value = true;
  try {
    await presetsApi.delete(selectedPreset.value.id);
    presets.value = presets.value.filter((p) => p.id !== selectedPresetId.value);
    selectedPresetId.value = null;
  } catch (err) {
    console.error('Failed to delete preset:', err);
  } finally {
    presetSaving.value = false;
  }
}

function resetToDefaults(): void {
  emit('update:modelValue', getDefaultOverlayParams());
}

onMounted(fetchPresets);

// ---------------------------------------------------------------------------
// Filter field descriptors for DRY template
// ---------------------------------------------------------------------------

interface FilterField {
  key: keyof OverlayFilterParams;
  label: string;
  min: number;
  max: number;
  step: number;
}

const filterFields: FilterField[] = [
  { key: 'brightness', label: 'Brightness', min: 0, max: 2, step: 0.01 },
  { key: 'contrast', label: 'Contrast', min: 0, max: 2, step: 0.01 },
  { key: 'saturate', label: 'Saturate', min: 0, max: 2, step: 0.01 },
  { key: 'hueRotate', label: 'Hue Rotate', min: 0, max: 360, step: 1 },
  { key: 'grayscale', label: 'Grayscale', min: 0, max: 1, step: 0.01 },
];

// We track which element sections have the "type" field (text vs logo)
const elementSections = computed(() => [
  { key: 'un' as const, label: '"UN"', isText: true, hasColor: true },
  { key: 'heard' as const, label: '"HEARD"', isText: true, hasColor: true },
  { key: 'logo' as const, label: 'Logo', isText: false, hasColor: false },
  { key: 'artistName' as const, label: `Artist Name`, isText: true, hasColor: true },
]);
</script>

<template>
  <div class="overlay-controls">
    <!-- Presets Section -->
    <div class="control-section">
      <button class="section-header" @click="toggleSection('presets')">
        <span class="section-chevron" :class="{ expanded: expandedSections.presets }">&#9654;</span>
        <span class="section-title">Presets</span>
      </button>

      <div v-show="expandedSections.presets" class="section-body">
        <div class="preset-row">
          <select class="form-select" :value="selectedPresetId ?? ''"
            @change="selectedPresetId = ($event.target as HTMLSelectElement).value ? Number(($event.target as HTMLSelectElement).value) : null">
            <option value="">— Select preset —</option>
            <option v-for="p in presets" :key="p.id" :value="p.id">{{ p.name }}</option>
          </select>
          <button class="btn-sm btn-primary" :disabled="!selectedPreset || presetLoading"
            @click="loadPreset">Load</button>
        </div>

        <div class="preset-actions">
          <button class="btn-sm btn-ghost" :disabled="!selectedPreset || presetSaving"
            @click="updateCurrent">Update</button>
          <button class="btn-sm btn-ghost" :disabled="!selectedPreset || presetSaving"
            @click="deleteCurrent">Delete</button>
          <button class="btn-sm btn-ghost" @click="showSaveInput = !showSaveInput">Save as…</button>
          <button class="btn-sm btn-ghost" @click="resetToDefaults">Defaults</button>
        </div>

        <div v-if="showSaveInput" class="preset-save-row">
          <input v-model="newPresetName" type="text" class="form-input" placeholder="Preset name"
            @keyup.enter="saveAsNew" />
          <button class="btn-sm btn-primary" :disabled="!newPresetName.trim() || presetSaving"
            @click="saveAsNew">Save</button>
        </div>
      </div>
    </div>

    <!-- Element Sections -->
    <div v-for="section in elementSections" :key="section.key" class="control-section">
      <button class="section-header" @click="toggleSection(section.key)">
        <span class="section-chevron" :class="{ expanded: expandedSections[section.key] }">&#9654;</span>
        <span class="section-title">{{ section.label }}</span>
        <label class="visibility-toggle" @click.stop>
          <input type="checkbox" :checked="(modelValue[section.key] as OverlayElementParams).visible"
            @change="updateElement(section.key, 'visible', ($event.target as HTMLInputElement).checked)" />
          <span class="toggle-label">Visible</span>
        </label>
      </button>

      <div v-show="expandedSections[section.key]" class="section-body">
        <!-- X position -->
        <div class="control-row">
          <label class="control-label">X</label>
          <input type="range" class="control-slider" min="0" max="100" step="0.5"
            :value="(modelValue[section.key] as OverlayElementParams).x"
            @input="updateElement(section.key, 'x', Number(($event.target as HTMLInputElement).value))" />
          <input type="number" class="control-number" min="0" max="100" step="0.5"
            :value="(modelValue[section.key] as OverlayElementParams).x"
            @input="updateElement(section.key, 'x', Number(($event.target as HTMLInputElement).value))" />
          <span class="control-unit">%</span>
        </div>

        <!-- Y position -->
        <div class="control-row">
          <label class="control-label">Y</label>
          <input type="range" class="control-slider" min="0" max="100" step="0.5"
            :value="(modelValue[section.key] as OverlayElementParams).y"
            @input="updateElement(section.key, 'y', Number(($event.target as HTMLInputElement).value))" />
          <input type="number" class="control-number" min="0" max="100" step="0.5"
            :value="(modelValue[section.key] as OverlayElementParams).y"
            @input="updateElement(section.key, 'y', Number(($event.target as HTMLInputElement).value))" />
          <span class="control-unit">%</span>
        </div>

        <!-- Size -->
        <div class="control-row">
          <label class="control-label">Size</label>
          <input type="range" class="control-slider" :min="section.isText ? 8 : 5" :max="section.isText ? 80 : 40"
            :step="section.isText ? 1 : 0.5" :value="(modelValue[section.key] as OverlayElementParams).size"
            @input="updateElement(section.key, 'size', Number(($event.target as HTMLInputElement).value))" />
          <input type="number" class="control-number" :min="section.isText ? 8 : 5" :max="section.isText ? 80 : 40"
            :step="section.isText ? 1 : 0.5" :value="(modelValue[section.key] as OverlayElementParams).size"
            @input="updateElement(section.key, 'size', Number(($event.target as HTMLInputElement).value))" />
          <span class="control-unit">{{ section.isText ? 'px' : '%' }}</span>
        </div>

        <!-- Color (text elements only) -->
        <div v-if="section.hasColor" class="control-row">
          <label class="control-label">Color</label>
          <input type="color" class="control-color" :value="(modelValue[section.key] as OverlayElementParams).color"
            @input="updateElement(section.key, 'color', ($event.target as HTMLInputElement).value)" />
          <input type="text" class="control-color-text" :value="(modelValue[section.key] as OverlayElementParams).color"
            @change="updateElement(section.key, 'color', ($event.target as HTMLInputElement).value)" />
        </div>

        <!-- Font Weight (text elements only) -->
        <div v-if="section.isText" class="control-row">
          <label class="control-label">Weight</label>
          <select class="form-select control-select"
            :value="(modelValue[section.key] as OverlayElementParams).fontWeight ?? '700'"
            @change="updateElement(section.key, 'fontWeight', ($event.target as HTMLSelectElement).value)">
            <option value="400">Regular (400)</option>
            <option value="600">Semi-Bold (600)</option>
            <option value="700">Bold (700)</option>
          </select>
        </div>

        <!-- Font Style (text elements only) -->
        <div v-if="section.isText" class="control-row">
          <label class="control-label">Style</label>
          <select class="form-select control-select"
            :value="(modelValue[section.key] as OverlayElementParams).fontStyle ?? 'normal'"
            @change="updateElement(section.key, 'fontStyle', ($event.target as HTMLSelectElement).value)">
            <option value="normal">Normal</option>
            <option value="italic">Italic</option>
          </select>
        </div>

        <!-- Shadow controls (text elements only) -->
        <template v-if="section.isText">
          <div class="control-divider">Shadow</div>
          <div class="control-row">
            <label class="control-label">X Offset</label>
            <input type="range" class="control-slider" min="-10" max="10" step="0.5"
              :value="(modelValue[section.key] as OverlayElementParams).shadow?.offsetX ?? 0"
              @input="updateShadow(section.key, 'offsetX', Number(($event.target as HTMLInputElement).value))" />
            <input type="number" class="control-number" min="-10" max="10" step="0.5"
              :value="(modelValue[section.key] as OverlayElementParams).shadow?.offsetX ?? 0"
              @input="updateShadow(section.key, 'offsetX', Number(($event.target as HTMLInputElement).value))" />
            <span class="control-unit">px</span>
          </div>
          <div class="control-row">
            <label class="control-label">Y Offset</label>
            <input type="range" class="control-slider" min="-10" max="10" step="0.5"
              :value="(modelValue[section.key] as OverlayElementParams).shadow?.offsetY ?? 0"
              @input="updateShadow(section.key, 'offsetY', Number(($event.target as HTMLInputElement).value))" />
            <input type="number" class="control-number" min="-10" max="10" step="0.5"
              :value="(modelValue[section.key] as OverlayElementParams).shadow?.offsetY ?? 0"
              @input="updateShadow(section.key, 'offsetY', Number(($event.target as HTMLInputElement).value))" />
            <span class="control-unit">px</span>
          </div>
          <div class="control-row">
            <label class="control-label">Shadow Color</label>
            <input type="color" class="control-color"
              :value="(modelValue[section.key] as OverlayElementParams).shadow?.color ?? '#000000'"
              @input="updateShadow(section.key, 'color', ($event.target as HTMLInputElement).value)" />
            <input type="text" class="control-color-text"
              :value="(modelValue[section.key] as OverlayElementParams).shadow?.color ?? '#000000'"
              @change="updateShadow(section.key, 'color', ($event.target as HTMLInputElement).value)" />
          </div>
        </template>
      </div>
    </div>

    <!-- Image Filter Section -->
    <div class="control-section">
      <button class="section-header" @click="toggleSection('filter')">
        <span class="section-chevron" :class="{ expanded: expandedSections.filter }">&#9654;</span>
        <span class="section-title">Image Filter</span>
      </button>

      <div v-show="expandedSections.filter" class="section-body">
        <div v-for="field in filterFields" :key="field.key" class="control-row">
          <label class="control-label">{{ field.label }}</label>
          <input type="range" class="control-slider" :min="field.min" :max="field.max" :step="field.step"
            :value="modelValue.filter[field.key]"
            @input="updateFilter(field.key, Number(($event.target as HTMLInputElement).value))" />
          <input type="number" class="control-number" :min="field.min" :max="field.max" :step="field.step"
            :value="modelValue.filter[field.key]"
            @input="updateFilter(field.key, Number(($event.target as HTMLInputElement).value))" />
          <button class="btn-reset" :title="`Reset to ${defaults.filter[field.key]}`"
            @click="updateFilter(field.key, defaults.filter[field.key])">↩</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.overlay-controls {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-xs);
  font-size: var(--font-size-sm);
  max-height: 100%;
  overflow-y: auto;
}

/* --- Section Container --- */
.control-section {
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-surface);
  overflow: hidden;
}

.section-header {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  width: 100%;
  padding: var(--spacing-sm) var(--spacing-md);
  background: none;
  border: none;
  color: var(--color-text);
  cursor: pointer;
  font-size: var(--font-size-sm);
  font-weight: var(--font-weight-medium);
  text-align: left;
}

.section-header:hover {
  background: var(--color-surface-hover);
}

.section-chevron {
  font-size: 10px;
  transition: transform var(--transition-fast);
  color: var(--color-text-muted);
}

.section-chevron.expanded {
  transform: rotate(90deg);
}

.section-title {
  flex: 1;
}

.section-body {
  padding: var(--spacing-sm) var(--spacing-md) var(--spacing-md);
  display: flex;
  flex-direction: column;
  gap: var(--spacing-sm);
  border-top: 1px solid var(--color-border-light);
}

/* --- Visibility toggle in section header --- */
.visibility-toggle {
  display: flex;
  align-items: center;
  gap: var(--spacing-xs);
  font-size: var(--font-size-xs);
  color: var(--color-text-muted);
  cursor: pointer;
}

.visibility-toggle input[type="checkbox"] {
  accent-color: var(--color-primary);
}

.toggle-label {
  user-select: none;
}

/* --- Control Row --- */
.control-row {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
}

.control-label {
  width: 56px;
  flex-shrink: 0;
  color: var(--color-text-muted);
  font-size: var(--font-size-xs);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.control-slider {
  flex: 1;
  min-width: 0;
  accent-color: var(--color-primary);
  height: 4px;
  cursor: pointer;
}

.control-number {
  width: 58px;
  flex-shrink: 0;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  color: var(--color-text);
  font-size: var(--font-size-xs);
  padding: 2px 4px;
  text-align: right;
}

.control-number:focus {
  outline: none;
  border-color: var(--color-primary);
}

.control-unit {
  width: 18px;
  flex-shrink: 0;
  color: var(--color-text-muted);
  font-size: var(--font-size-xs);
}

.control-divider {
  font-size: var(--font-size-xs);
  color: var(--color-text-muted);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  padding: 6px 0 2px;
  border-top: 1px solid var(--color-border);
  margin-top: 4px;
}

.control-color {
  width: 28px;
  height: 28px;
  padding: 0;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  cursor: pointer;
  background: none;
}

.control-color-text {
  width: 80px;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  color: var(--color-text);
  font-size: var(--font-size-xs);
  font-family: monospace;
  padding: 2px 6px;
}

.control-color-text:focus {
  outline: none;
  border-color: var(--color-primary);
}

.control-select {
  flex: 1;
  min-width: 0;
}

/* --- Form select (shared) --- */
.form-select {
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  color: var(--color-text);
  font-size: var(--font-size-xs);
  padding: 4px 6px;
  cursor: pointer;
}

.form-select:focus {
  outline: none;
  border-color: var(--color-primary);
}

.form-input {
  flex: 1;
  min-width: 0;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  color: var(--color-text);
  font-size: var(--font-size-xs);
  padding: 4px 6px;
}

.form-input:focus {
  outline: none;
  border-color: var(--color-primary);
}

/* --- Buttons --- */
.btn-sm {
  padding: 3px 8px;
  border-radius: var(--radius-sm);
  font-size: var(--font-size-xs);
  cursor: pointer;
  transition: all var(--transition-fast);
  white-space: nowrap;
}

.btn-primary {
  background: var(--color-bg);
  border: 1px solid var(--color-primary);
  color: var(--color-primary);
}

.btn-primary:hover:not(:disabled) {
  opacity: 0.85;
}

.btn-ghost {
  background: none;
  border: 1px solid var(--color-border);
  color: var(--color-text);
}

.btn-ghost:hover:not(:disabled) {
  background: var(--color-surface-hover);
}

.btn-sm:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.btn-reset {
  width: 22px;
  height: 22px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: none;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  color: var(--color-text-muted);
  font-size: 12px;
  cursor: pointer;
  padding: 0;
  transition: all var(--transition-fast);
}

.btn-reset:hover {
  color: var(--color-primary);
  border-color: var(--color-primary);
}

/* --- Preset rows --- */
.preset-row {
  display: flex;
  gap: var(--spacing-sm);
  align-items: center;
}

.preset-row .form-select {
  flex: 1;
  min-width: 0;
}

.preset-actions {
  display: flex;
  gap: var(--spacing-xs);
  flex-wrap: wrap;
}

.preset-save-row {
  display: flex;
  gap: var(--spacing-sm);
  align-items: center;
}
</style>
