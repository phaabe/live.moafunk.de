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

const activeSection = ref<string | null>('elements');

function toggleSection(key: string): void {
  activeSection.value = activeSection.value === key ? null : key;
}

// Active element tab
const activeElementTab = ref<'un' | 'heard' | 'logo' | 'artistName'>('un');

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
    <!-- ================================================================= -->
    <!-- SECTION 1: Presets                                                -->
    <!-- ================================================================= -->
    <div class="top-section" :class="{ 'is-active': activeSection === 'presets' }">
      <button class="section-header" @click="toggleSection('presets')">
        <span class="section-chevron" :class="{ expanded: activeSection === 'presets' }">&#9654;</span>
        <span class="section-title">Presets</span>
      </button>

      <Transition name="accordion">
        <div v-if="activeSection === 'presets'" class="section-body">
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
      </Transition>
    </div>

    <!-- ================================================================= -->
    <!-- SECTION 2: Elements (tabbed)                                      -->
    <!-- ================================================================= -->
    <div class="top-section" :class="{ 'is-active': activeSection === 'elements' }">
      <button class="section-header" @click="toggleSection('elements')">
        <span class="section-chevron" :class="{ expanded: activeSection === 'elements' }">&#9654;</span>
        <span class="section-title">Elements</span>
      </button>

      <Transition name="accordion">
        <div v-if="activeSection === 'elements'" class="section-body section-body--tabbed">
          <!-- Tab bar -->
          <div class="element-tabs">
            <button v-for="section in elementSections" :key="section.key" class="element-tab"
              :class="{ active: activeElementTab === section.key }" @click="activeElementTab = section.key">
              {{ section.label }}
            </button>
          </div>

          <!-- Tab content: render controls for the active element -->
          <template v-for="section in elementSections" :key="section.key">
            <div v-show="activeElementTab === section.key" class="element-tab-content">
              <!-- Visibility -->
              <div class="control-row">
                <label class="visibility-toggle">
                  <input type="checkbox" :checked="(modelValue[section.key] as OverlayElementParams).visible"
                    @change="updateElement(section.key, 'visible', ($event.target as HTMLInputElement).checked)" />
                  <span class="toggle-label">Visible</span>
                </label>
              </div>

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
                <input type="number" class="control-number" :min="section.isText ? 8 : 5"
                  :max="section.isText ? 80 : 40" :step="section.isText ? 1 : 0.5"
                  :value="(modelValue[section.key] as OverlayElementParams).size"
                  @input="updateElement(section.key, 'size', Number(($event.target as HTMLInputElement).value))" />
                <span class="control-unit">{{ section.isText ? 'px' : '%' }}</span>
              </div>

              <!-- Color (text elements only) -->
              <div v-if="section.hasColor" class="control-row">
                <label class="control-label">Color</label>
                <input type="color" class="control-color"
                  :value="(modelValue[section.key] as OverlayElementParams).color"
                  @input="updateElement(section.key, 'color', ($event.target as HTMLInputElement).value)" />
                <input type="text" class="control-color-text"
                  :value="(modelValue[section.key] as OverlayElementParams).color"
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
          </template>
        </div>
      </Transition>
    </div>

    <!-- ================================================================= -->
    <!-- SECTION 3: Image Filter                                           -->
    <!-- ================================================================= -->
    <div class="top-section" :class="{ 'is-active': activeSection === 'filter' }">
      <button class="section-header" @click="toggleSection('filter')">
        <span class="section-chevron" :class="{ expanded: activeSection === 'filter' }">&#9654;</span>
        <span class="section-title">Image</span>
      </button>

      <Transition name="accordion">
        <div v-if="activeSection === 'filter'" class="section-body">
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
      </Transition>
    </div>
  </div>
</template>

<style scoped>
.overlay-controls {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
  font-size: 11px;
  max-height: 100%;
  overflow-y: auto;
}

/* --- Top-level Sections (Presets / Elements / Image) --- */
.top-section {
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-surface);
  overflow: hidden;
  transition: border-color 0.2s ease, box-shadow 0.2s ease;
}

.top-section.is-active {
  border-color: color-mix(in srgb, var(--color-primary) 40%, transparent);
  box-shadow: 0 0 0 1px color-mix(in srgb, var(--color-primary) 12%, transparent);
}

.section-header {
  display: flex;
  align-items: center;
  gap: var(--spacing-xs);
  width: 100%;
  padding: 8px var(--spacing-sm);
  background: none;
  border: none;
  color: var(--color-text);
  cursor: pointer;
  font-size: 11px;
  font-weight: var(--font-weight-medium);
  text-align: left;
  transition: background 0.15s ease;
}

.section-header:hover {
  background: var(--color-surface-hover);
}

.section-chevron {
  font-size: 10px;
  transition: transform 0.2s ease;
  color: var(--color-text-muted);
}

.section-chevron.expanded {
  transform: rotate(90deg);
}

.section-title {
  flex: 1;
  letter-spacing: 0.02em;
}

.section-body {
  padding: 6px var(--spacing-sm) 8px;
  display: flex;
  flex-direction: column;
  gap: 5px;
  border-top: 1px solid var(--color-border-light);
}

.section-body--tabbed {
  padding-top: 0;
  gap: 0;
}

/* --- Accordion transition --- */
.accordion-enter-active,
.accordion-leave-active {
  transition: all 0.2s ease;
  overflow: hidden;
}

.accordion-enter-from,
.accordion-leave-to {
  opacity: 0;
  max-height: 0;
  padding-top: 0;
  padding-bottom: 0;
}

.accordion-enter-to,
.accordion-leave-from {
  opacity: 1;
}

/* --- Element Tab Bar --- */
.element-tabs {
  display: flex;
  border-bottom: 1px solid var(--color-border);
  margin: 0 calc(-1 * var(--spacing-sm));
  padding: 0 var(--spacing-sm);
}

.element-tab {
  flex: 1;
  padding: 5px 2px;
  background: none;
  border: none;
  border-bottom: 2px solid transparent;
  color: var(--color-text-muted);
  font-size: 10px;
  font-weight: var(--font-weight-medium);
  text-transform: uppercase;
  letter-spacing: 0.03em;
  cursor: pointer;
  transition: all var(--transition-fast);
  white-space: nowrap;
  text-align: center;
}

.element-tab:hover {
  color: var(--color-text);
}

.element-tab.active {
  color: var(--color-primary);
  border-bottom-color: var(--color-primary);
}

/* --- Element Tab Content --- */
.element-tab-content {
  display: flex;
  flex-direction: column;
  gap: 5px;
  padding-top: 6px;
}

/* --- Visibility toggle --- */
.visibility-toggle {
  display: flex;
  align-items: center;
  gap: 3px;
  font-size: 10px;
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
  gap: 6px;
}

.control-label {
  width: 52px;
  flex-shrink: 0;
  color: var(--color-text-muted);
  font-size: 10px;
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
  width: 48px;
  flex-shrink: 0;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  color: var(--color-text);
  font-size: 10px;
  padding: 1px 3px;
  text-align: right;
}

.control-number:focus {
  outline: none;
  border-color: var(--color-primary);
}

.control-unit {
  width: 16px;
  flex-shrink: 0;
  color: var(--color-text-muted);
  font-size: 10px;
}

.control-divider {
  font-size: 10px;
  color: var(--color-text-muted);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  padding: 4px 0 2px;
  border-top: 1px solid var(--color-border);
  margin-top: 3px;
}

.control-color {
  width: 22px;
  height: 22px;
  padding: 0;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  cursor: pointer;
  background: none;
}

.control-color-text {
  width: 70px;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  color: var(--color-text);
  font-size: 10px;
  font-family: monospace;
  padding: 1px 4px;
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
  font-size: 10px;
  padding: 2px 4px;
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
  font-size: 10px;
  padding: 2px 4px;
}

.form-input:focus {
  outline: none;
  border-color: var(--color-primary);
}

/* --- Buttons --- */
.btn-sm {
  padding: 2px 6px;
  border-radius: var(--radius-sm);
  font-size: 10px;
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
  width: 18px;
  height: 18px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: none;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  color: var(--color-text-muted);
  font-size: 10px;
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
  gap: 6px;
  align-items: center;
}

.preset-row .form-select {
  flex: 1;
  min-width: 0;
}

.preset-actions {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
}

.preset-save-row {
  display: flex;
  gap: 6px;
  align-items: center;
}
</style>
