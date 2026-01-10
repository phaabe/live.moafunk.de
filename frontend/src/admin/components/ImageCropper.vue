<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch, nextTick } from 'vue';
import Cropper from 'cropperjs';
import 'cropperjs/dist/cropper.css';

const props = defineProps<{
  artistName: string;
  initialImageUrl?: string;
}>();

const emit = defineEmits<{
  (e: 'cancel'): void;
  (e: 'save', data: { original: File; cropped: Blob; branded: Blob }): void;
}>();

const cropperWrapper = ref<HTMLElement | null>(null);
const cropperImg = ref<HTMLImageElement | null>(null);
const overlayRoot = ref<HTMLElement | null>(null);
const overlayName = ref<HTMLElement | null>(null);
const fileInput = ref<HTMLInputElement | null>(null);

let cropper: Cropper | null = null;
let originalFile: File | null = null;
let objectUrl: string | null = null;
let moafunkLogoImg: HTMLImageElement | null = null;
let cropBlobUpdateTimer: number | null = null;

// Cropped blobs
const croppedBlob = ref<Blob | null>(null);
const brandedBlob = ref<Blob | null>(null);
const saving = ref(false);
const hasImage = ref(false);

// Load logo
const moafunkLogoPromise = new Promise<HTMLImageElement | null>((resolve) => {
  const img = new Image();
  img.crossOrigin = 'anonymous';
  img.onload = () => {
    moafunkLogoImg = img;
    resolve(img);
  };
  img.onerror = () => resolve(null);
  img.src = '/moafunk.png';
});

// Load fonts
const shoikaFontsPromise = Promise.all([
  new FontFace('Shoika', "url('/Shoika-font/Shoika%20Bold%20Italic.ttf')", {
    weight: '600',
    style: 'italic',
  }).load(),
  new FontFace('Shoika', "url('/Shoika-font/Shoika%20Regular%20Italic.ttf')", {
    weight: '400',
    style: 'italic',
  }).load(),
  new FontFace('Shoika', "url('/Shoika-font/Shoika%20Bold.ttf')", {
    weight: '700',
    style: 'normal',
  }).load(),
])
  .then((fonts) => {
    fonts.forEach((font) => document.fonts.add(font));
    return fonts;
  })
  .catch(() => null);

function fitOverlayName(): void {
  if (!overlayName.value || overlayName.value.clientWidth <= 0) return;
  
  const maxPx = 22;
  const minPx = 12;

  let low = minPx;
  let high = maxPx;
  for (let i = 0; i < 12; i++) {
    const mid = (low + high) / 2;
    overlayName.value.style.fontSize = `${mid}px`;
    if (overlayName.value.scrollWidth <= overlayName.value.clientWidth) {
      low = mid;
    } else {
      high = mid;
    }
  }
  overlayName.value.style.fontSize = `${Math.floor(low)}px`;
}

function updateOverlayName(): void {
  if (!overlayName.value) return;
  overlayName.value.textContent = props.artistName.toUpperCase();
  requestAnimationFrame(fitOverlayName);
  scheduleCroppedBlobUpdate();
}

function scheduleCroppedBlobUpdate(): void {
  if (!cropper) return;
  if (cropBlobUpdateTimer) window.clearTimeout(cropBlobUpdateTimer);
  cropBlobUpdateTimer = window.setTimeout(() => {
    cropBlobUpdateTimer = null;
    requestAnimationFrame(() => void updateCroppedBlob());
  }, 150);
}

function attachOverlayToActiveCropArea(): void {
  if (!overlayRoot.value || !cropperWrapper.value) return;

  const cropBox = cropperWrapper.value.querySelector<HTMLElement>('.cropper-crop-box');
  const viewBox = cropperWrapper.value.querySelector<HTMLElement>('.cropper-view-box');
  const target = viewBox || cropBox;
  if (!target) return;

  if (overlayRoot.value.parentElement !== target) {
    target.appendChild(overlayRoot.value);
  }
}

function detachOverlayToFrame(): void {
  const frame = cropperWrapper.value?.querySelector<HTMLElement>('.cropper-frame');
  if (!frame || !overlayRoot.value) return;
  if (overlayRoot.value.parentElement !== frame) {
    frame.appendChild(overlayRoot.value);
  }
}

function setCropBoxPadding(paddingFraction = 0.05): void {
  if (!cropper) return;

  const container = cropper.getContainerData() as unknown as {
    left?: number;
    top?: number;
    width: number;
    height: number;
  };
  if (!container || container.width <= 0 || container.height <= 0) return;

  // Use the container width since we have a square aspect ratio and full-width layout
  const containerSize = container.width;
  const pad = containerSize * paddingFraction;
  const cropSide = Math.max(1, containerSize - 2 * pad);

  const leftBase = container.left ?? 0;
  const topBase = container.top ?? 0;
  const left = leftBase + pad;
  const top = topBase + (container.height - cropSide) / 2;

  cropper.setCropBoxData({ left, top, width: cropSide, height: cropSide });
}

async function drawOverlayOnCanvas(canvas: HTMLCanvasElement): Promise<void> {
  const ctx = canvas.getContext('2d');
  if (!ctx || !cropperWrapper.value || !overlayRoot.value) return;

  const size = canvas.width;
  ctx.filter = 'none';

  await Promise.all([moafunkLogoPromise, shoikaFontsPromise]);
  if (document.fonts?.ready) {
    try {
      await document.fonts.ready;
    } catch {
      // ignore
    }
  }

  const viewBox =
    cropperWrapper.value.querySelector<HTMLElement>('.cropper-view-box') ||
    cropperWrapper.value.querySelector<HTMLElement>('.cropper-crop-box');
  if (!viewBox) return;

  const viewRect = viewBox.getBoundingClientRect();
  if (viewRect.width <= 0 || viewRect.height <= 0) return;

  const scale = size / viewRect.width;
  const toCanvasX = (px: number) => (px - viewRect.left) * scale;
  const toCanvasY = (px: number) => (px - viewRect.top) * scale;
  const toCanvasLen = (px: number) => px * scale;

  const leftUn = overlayRoot.value.querySelector<HTMLElement>('.uploader-overlay-left .un');
  const leftHeard = overlayRoot.value.querySelector<HTMLElement>('.uploader-overlay-left .heard');
  if (leftUn && leftHeard) {
    const unRect = leftUn.getBoundingClientRect();
    const heardRect = leftHeard.getBoundingClientRect();
    const unStyle = getComputedStyle(leftUn);
    const heardStyle = getComputedStyle(leftHeard);

    const unFontPx = parseFloat(unStyle.fontSize) || 16;
    const heardFontPx = parseFloat(heardStyle.fontSize) || 16;

    ctx.textAlign = 'left';
    ctx.textBaseline = 'top';

    ctx.font = `${unStyle.fontStyle} ${unStyle.fontWeight} ${toCanvasLen(unFontPx)}px Shoika, sans-serif`;
    ctx.fillStyle = unStyle.color || '#ffec44';
    ctx.fillText((leftUn.textContent || '').trim(), toCanvasX(unRect.left), toCanvasY(unRect.top));

    ctx.font = `${heardStyle.fontStyle} ${heardStyle.fontWeight} ${toCanvasLen(heardFontPx)}px Shoika, sans-serif`;
    ctx.fillStyle = heardStyle.color || '#ffffff';
    ctx.fillText(
      (leftHeard.textContent || '').trim(),
      toCanvasX(heardRect.left),
      toCanvasY(heardRect.top),
    );
  }

  const logoEl = overlayRoot.value.querySelector<HTMLImageElement>('img.uploader-overlay-logo');
  if (logoEl && moafunkLogoImg) {
    const logoRect = logoEl.getBoundingClientRect();

    const boxX = toCanvasX(logoRect.left);
    const boxY = toCanvasY(logoRect.top);
    const boxW = toCanvasLen(logoRect.width);
    const boxH = toCanvasLen(logoRect.height);

    const srcW = moafunkLogoImg.naturalWidth || moafunkLogoImg.width || 1;
    const srcH = moafunkLogoImg.naturalHeight || moafunkLogoImg.height || 1;
    const srcAspect = srcW / srcH;
    const boxAspect = boxW / boxH;

    let drawW = boxW;
    let drawH = boxH;
    if (srcAspect >= boxAspect) {
      drawW = boxW;
      drawH = boxW / srcAspect;
    } else {
      drawH = boxH;
      drawW = boxH * srcAspect;
    }

    const drawX = boxX + (boxW - drawW) / 2;
    const drawY = boxY + (boxH - drawH) / 2;
    ctx.drawImage(moafunkLogoImg, drawX, drawY, drawW, drawH);
  }

  if (overlayName.value) {
    const nameText = (overlayName.value.textContent || '').trim();
    if (nameText) {
      const nameRect = overlayName.value.getBoundingClientRect();
      const nameStyle = getComputedStyle(overlayName.value);
      const nameFontPx = parseFloat(nameStyle.fontSize) || 16;

      ctx.fillStyle = nameStyle.color || '#ffffff';
      ctx.textBaseline = 'middle';
      ctx.textAlign = 'center';
      ctx.font = `${nameStyle.fontStyle} ${nameStyle.fontWeight} ${toCanvasLen(nameFontPx)}px Shoika, sans-serif`;

      const centerX = toCanvasX(nameRect.left + nameRect.width / 2);
      const centerY = toCanvasY(nameRect.top + nameRect.height / 2);
      ctx.fillText(nameText, centerX, centerY);
    }
  }
}

async function updateCroppedBlob(): Promise<void> {
  if (!cropper) return;

  if (overlayRoot.value) overlayRoot.value.style.visibility = 'hidden';

  const canvas = cropper.getCroppedCanvas({
    width: 1024,
    height: 1024,
    imageSmoothingEnabled: true,
    imageSmoothingQuality: 'high',
  });

  if (overlayRoot.value) overlayRoot.value.style.visibility = 'visible';
  if (!canvas) return;

  // Apply filter
  {
    const ctx = canvas.getContext('2d');
    if (ctx) {
      const filtered = document.createElement('canvas');
      filtered.width = canvas.width;
      filtered.height = canvas.height;
      const fctx = filtered.getContext('2d');
      if (fctx) {
        fctx.filter = 'saturate(0.85) contrast(1.08)';
        fctx.drawImage(canvas, 0, 0);

        ctx.save();
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.restore();

        ctx.filter = 'none';
        ctx.drawImage(filtered, 0, 0);
      }
    }
  }

  // Get cropped blob (without overlay)
  const croppedFilteredBlob = await new Promise<Blob | null>((resolve) => {
    canvas.toBlob((b) => resolve(b), 'image/jpeg', 0.92);
  });
  if (croppedFilteredBlob) croppedBlob.value = croppedFilteredBlob;

  // Draw overlay and get branded blob
  await drawOverlayOnCanvas(canvas);

  const brandedBlobResult = await new Promise<Blob | null>((resolve) => {
    canvas.toBlob((b) => resolve(b), 'image/jpeg', 0.92);
  });
  if (brandedBlobResult) brandedBlob.value = brandedBlobResult;
}

function destroyCropper(): void {
  if (cropper) {
    detachOverlayToFrame();
    cropper.destroy();
    cropper = null;
  }
  if (objectUrl) {
    URL.revokeObjectURL(objectUrl);
    objectUrl = null;
  }
}

function onFileChange(event: Event): void {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  
  if (!file) return;

  destroyCropper();
  originalFile = file;
  objectUrl = URL.createObjectURL(file);
  
  if (cropperImg.value) {
    cropperImg.value.src = objectUrl;
    hasImage.value = true;
  }
}

function initCropper(): void {
  if (!cropperImg.value) return;
  
  cropper = new Cropper(cropperImg.value, {
    aspectRatio: 1,
    viewMode: 1,
    dragMode: 'move',
    autoCropArea: 1.0,
    background: false,
    minContainerWidth: 200,
    minContainerHeight: 200,
    guides: false,
    center: false,
    highlight: false,
    cropBoxMovable: false,
    cropBoxResizable: false,
    toggleDragModeOnDblclick: false,
    responsive: true,
    zoomOnWheel: true,
    ready() {
      attachOverlayToActiveCropArea();
      requestAnimationFrame(fitOverlayName);
      requestAnimationFrame(() => {
        // First zoom image to fill the container
        zoomImageToContainer();
        // Then set the crop box padding (inactive area)
        requestAnimationFrame(() => {
          setCropBoxPadding(0.05);
          void updateCroppedBlob();
        });
      });
    },
    cropend() {
      void updateCroppedBlob();
    },
    zoom() {
      void updateCroppedBlob();
    },
  });
}

function zoomIn(): void {
  if (cropper) cropper.zoom(0.1);
}

function zoomOut(): void {
  if (cropper) cropper.zoom(-0.1);
}

function zoomImageToContainer(): void {
  if (!cropper) return;
  
  const container = cropper.getContainerData();
  const canvasData = cropper.getCanvasData();
  
  if (!container || !canvasData || container.width <= 0 || container.height <= 0) return;
  
  // Calculate the zoom ratio needed to fill the container
  const scaleX = container.width / canvasData.width;
  const scaleY = container.height / canvasData.height;
  const scale = Math.max(scaleX, scaleY);
  
  if (scale > 1) {
    // Need to zoom in to fill
    const newWidth = canvasData.width * scale;
    const newHeight = canvasData.height * scale;
    
    // Center the image in the container
    const newLeft = (container.width - newWidth) / 2;
    const newTop = (container.height - newHeight) / 2;
    
    cropper.setCanvasData({
      left: newLeft,
      top: newTop,
      width: newWidth,
      height: newHeight,
    });
  }
}

function zoomToFill(): void {
  if (!cropper) return;
  
  const cropBoxData = cropper.getCropBoxData();
  const canvasData = cropper.getCanvasData();
  
  if (!cropBoxData || !canvasData || cropBoxData.width <= 0 || cropBoxData.height <= 0) return;
  
  // Calculate the zoom ratio needed to fill the crop box
  const scaleX = cropBoxData.width / canvasData.width;
  const scaleY = cropBoxData.height / canvasData.height;
  const scale = Math.max(scaleX, scaleY);
  
  if (scale > 1) {
    // Need to zoom in to fill
    const newWidth = canvasData.width * scale;
    const newHeight = canvasData.height * scale;
    
    // Center the image in the crop box
    const newLeft = cropBoxData.left - (newWidth - cropBoxData.width) / 2;
    const newTop = cropBoxData.top - (newHeight - cropBoxData.height) / 2;
    
    cropper.setCanvasData({
      left: newLeft,
      top: newTop,
      width: newWidth,
      height: newHeight,
    });
  }
}

async function save(): Promise<void> {
  if (!originalFile || !croppedBlob.value || !brandedBlob.value) return;
  
  saving.value = true;
  try {
    await updateCroppedBlob();
    emit('save', {
      original: originalFile,
      cropped: croppedBlob.value,
      branded: brandedBlob.value,
    });
  } finally {
    saving.value = false;
  }
}

function cancel(): void {
  emit('cancel');
}

watch(() => props.artistName, () => {
  updateOverlayName();
});

// Handle window resize to keep crop box properly sized
function handleResize(): void {
  requestAnimationFrame(() => {
    setCropBoxPadding(0.05);
    fitOverlayName();
  });
}

onMounted(async () => {
  await nextTick();
  updateOverlayName();
  
  // Listen for window resize
  window.addEventListener('resize', handleResize);
  
  // If initial image, load it
  if (props.initialImageUrl && cropperImg.value) {
    cropperImg.value.src = props.initialImageUrl;
    hasImage.value = true;
  }
});

onUnmounted(() => {
  destroyCropper();
  window.removeEventListener('resize', handleResize);
  if (cropBlobUpdateTimer) {
    window.clearTimeout(cropBlobUpdateTimer);
  }
});

// Watch for image load
function onImageLoad(): void {
  if (cropperImg.value?.src) {
    initCropper();
  }
}
</script>

<template>
  <div class="image-cropper" :class="{ 'is-saving': saving }">
    <!-- Saving overlay -->
    <div v-if="saving" class="saving-overlay">
      <div class="saving-spinner"></div>
      <span class="saving-text">Processing image...</span>
    </div>

    <div class="file-input-wrapper">
      <input 
        ref="fileInput"
        type="file" 
        accept="image/*" 
        @change="onFileChange"
        class="file-input"
      />
    </div>

    <div v-show="hasImage" ref="cropperWrapper" class="cropper-wrapper">
      <div class="cropper-frame">
        <img 
          ref="cropperImg" 
          alt="Crop your image" 
          @load="onImageLoad"
        />

        <div ref="overlayRoot" class="uploader-overlay" aria-hidden="true">
          <div class="uploader-overlay-left">
            <div class="un">UN</div>
            <div class="heard">HEARD</div>
          </div>
          <img class="uploader-overlay-logo" src="/moafunk.png" alt="" crossorigin="anonymous" />
          <div ref="overlayName" class="uploader-overlay-right"></div>
        </div>
      </div>

      <div class="cropper-controls">
        <button type="button" @click="zoomOut" aria-label="Zoom out">−</button>
        <button type="button" @click="zoomIn" aria-label="Zoom in">+</button>
        <span class="cropper-note">Drag to move. Use +/- to zoom.</span>
      </div>
    </div>

    <div class="actions">
      <button type="button" class="btn-ghost" @click="cancel">Cancel</button>
      <button 
        type="button" 
        class="btn-primary" 
        :disabled="!hasImage || saving" 
        @click="save"
      >
        {{ saving ? 'Saving...' : 'Save' }}
      </button>
    </div>
  </div>
</template>

<style scoped>
.image-cropper {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-md);
  position: relative;
}

.image-cropper.is-saving {
  pointer-events: none;
  opacity: 0.7;
}

.saving-overlay {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background-color: rgba(0, 0, 0, 0.6);
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  z-index: 9999;
  border-radius: var(--radius-md);
  gap: var(--spacing-md);
}

.saving-spinner {
  width: 40px;
  height: 40px;
  border: 3px solid rgba(255, 236, 68, 0.3);
  border-top-color: #ffec44;
  border-radius: 50%;
  animation: spin 1s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

.saving-text {
  color: #ffec44;
  font-size: var(--font-size-md);
  font-weight: 500;
}

.file-input-wrapper {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-sm);
}

.file-input {
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  color: var(--color-text);
  font-size: var(--font-size-sm);
  padding: var(--spacing-sm);
  cursor: pointer;
}

.file-input::file-selector-button {
  background-color: var(--color-background);
  color: #ffec44;
  border: 1px solid #ffec44;
  border-radius: var(--radius-sm);
  padding: var(--spacing-xs) var(--spacing-sm);
  margin-right: var(--spacing-sm);
  cursor: pointer;
}

.cropper-wrapper {
  width: 100%;
}

.cropper-frame {
  position: relative;
  width: 100%;
  aspect-ratio: 1;
  background-color: #1a1a1a;
  overflow: hidden;
}

.cropper-frame img {
  display: block;
  max-width: 100%;
}

.cropper-controls {
  display: flex;
  align-items: center;
  gap: var(--spacing-sm);
  padding: var(--spacing-sm) 0;
}

.cropper-controls button {
  width: 36px;
  height: 36px;
  border: 1px solid var(--color-border);
  background: var(--color-surface);
  color: var(--color-text);
  border-radius: var(--radius-sm);
  font-size: 18px;
  cursor: pointer;
  transition: all var(--transition-fast);
}

.cropper-controls button:hover {
  background: var(--color-primary);
  border-color: var(--color-primary);
}

.cropper-note {
  font-size: var(--font-size-sm);
  color: var(--color-text-muted);
}

/* Overlay styles - matching public frontend */
.uploader-overlay {
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  top: 0;
  padding: 0;
  height: 100%;
  pointer-events: none;
  z-index: 2000;
  --overlay-side: 18px;
  --overlay-bottom: 5%;
  --overlay-logo: 15%;
  --overlay-gap: 3%;
  font-family: 'Shoika', system-ui, -apple-system, 'Segoe UI', Roboto, Arial, sans-serif;
}

.uploader-overlay-left {
  position: absolute;
  left: 15%;
  bottom: var(--overlay-bottom);
  height: var(--overlay-logo);
  display: flex;
  flex-direction: column;
  justify-content: center;
  text-align: center;
  font-weight: 700;
  font-style: italic;
  line-height: 0.9;
  letter-spacing: 0.02em;
  text-transform: uppercase;
}

.uploader-overlay-left .un {
  color: #ffec44;
  font-size: 44px;
  font-family: 'Shoika', system-ui, -apple-system, 'Segoe UI', Roboto, Arial, sans-serif;
  font-style: italic;
  font-weight: 600;
}

.uploader-overlay-left .heard {
  color: #fff;
  font-size: 19px;
  font-family: 'Shoika', system-ui, -apple-system, 'Segoe UI', Roboto, Arial, sans-serif;
  font-style: italic;
  font-weight: 400;
}

.uploader-overlay-logo {
  position: absolute;
  left: 50%;
  bottom: var(--overlay-bottom);
  transform: translateX(-50%);
  height: var(--overlay-logo);
  width: var(--overlay-logo);
  object-fit: contain;
}

.uploader-overlay-right {
  position: absolute;
  left: calc(50% + (var(--overlay-logo) / 2) + var(--overlay-gap));
  right: var(--overlay-side);
  bottom: var(--overlay-bottom);
  height: var(--overlay-logo);
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  font-weight: 700;
  font-style: normal;
  font-family: 'Shoika', system-ui, -apple-system, 'Segoe UI', Roboto, Arial, sans-serif;
  text-transform: uppercase;
  font-size: 22px;
  line-height: 1;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  text-align: center;
}

.actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--spacing-sm);
  padding-top: var(--spacing-md);
  border-top: 1px solid var(--color-border);
}

.btn-ghost {
  background: none;
  border: 1px solid var(--color-border);
  color: var(--color-text);
  padding: var(--spacing-xs) var(--spacing-md);
  border-radius: var(--radius-md);
  cursor: pointer;
  font-size: var(--font-size-sm);
}

.btn-ghost:hover {
  background: var(--color-surface);
}

.btn-primary {
  background: var(--color-background);
  border: 1px solid #ffec44;
  color: #ffec44;
  padding: var(--spacing-xs) var(--spacing-md);
  border-radius: var(--radius-md);
  cursor: pointer;
  font-size: var(--font-size-sm);
}

.btn-primary:hover:not(:disabled) {
  opacity: 0.9;
}

.btn-primary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* CropperJS overrides */
:deep(.cropper-crop-box .uploader-overlay),
:deep(.cropper-view-box .uploader-overlay) {
  width: 100%;
  box-sizing: border-box;
}

:deep(.cropper-view-box) {
  position: relative;
  outline: none;
}

:deep(.cropper-view-box img:not(.uploader-overlay-logo)),
:deep(.cropper-canvas img) {
  filter: saturate(0.85) contrast(1.08);
}

/* CropperJS applies `width/height: 100%` to any `img` inside `.cropper-view-box`.
   Our overlay logo is also an `img`, so we must override that rule. */
:deep(.cropper-view-box img.uploader-overlay-logo) {
  position: absolute !important;
  width: var(--overlay-logo) !important;
  height: var(--overlay-logo) !important;
  max-width: none !important;
  max-height: none !important;
}

/* Reduce brightness of inactive area */
:deep(.cropper-modal) {
  background-color: #000 !important;
  opacity: 0.8 !important;
}
</style>
