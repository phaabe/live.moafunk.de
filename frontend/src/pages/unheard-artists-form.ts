import Cropper from 'cropperjs';
import 'cropperjs/dist/cropper.css';

const API_URL =
  window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1'
    ? 'http://localhost:8000/api/submit'
    : 'https://admin.live.moafunk.de/api/submit';

const MAX_SINGLE_FILE_SIZE_MB = 100;
const MAX_TOTAL_UPLOAD_MB = 250;
const MAX_SINGLE_FILE_SIZE_BYTES = MAX_SINGLE_FILE_SIZE_MB * 1024 * 1024;
const MAX_TOTAL_UPLOAD_BYTES = MAX_TOTAL_UPLOAD_MB * 1024 * 1024;

function getRequiredElement<T extends HTMLElement>(id: string): T {
  const element = document.getElementById(id);
  if (!element) {
    throw new Error(`Missing required element #${id}`);
  }
  return element as T;
}

const form = getRequiredElement<HTMLFormElement>('uaf-form');
const voiceMessageFile = getRequiredElement<HTMLInputElement>('voice-message');
const noVoiceMessageCheckbox = getRequiredElement<HTMLInputElement>('no-voice-message');
const submitBtn = getRequiredElement<HTMLButtonElement>('submit-btn');
const uploadProgress = getRequiredElement<HTMLElement>('upload-progress');
const progressFill = getRequiredElement<HTMLElement>('progress-fill');
const progressText = getRequiredElement<HTMLElement>('progress-text');
const formMessage = getRequiredElement<HTMLElement>('form-message');

const artistPicInput = getRequiredElement<HTMLInputElement>('artist-pic');
const artistNameInput = getRequiredElement<HTMLInputElement>('artist-name');
const cropperWrapper = getRequiredElement<HTMLElement>('artist-pic-cropper');
const cropperImg = getRequiredElement<HTMLImageElement>('artist-pic-cropper-img');
const zoomOutBtn = getRequiredElement<HTMLButtonElement>('artist-pic-zoom-out');
const zoomInBtn = getRequiredElement<HTMLButtonElement>('artist-pic-zoom-in');
const overlayName = getRequiredElement<HTMLElement>('artist-pic-overlay-name');
const overlayRoot = cropperWrapper.querySelector<HTMLElement>('.uploader-overlay');

let artistPicCropper: Cropper | null = null;
let artistPicCroppedFilteredBlob: Blob | null = null;
let artistPicBrandedBlob: Blob | null = null;
let artistPicOriginalFilename = 'artist.jpg';
let artistPicObjectUrl: string | null = null;

let cropBlobUpdateTimer: number | null = null;
function scheduleCroppedBlobUpdate(): void {
  if (!artistPicCropper) return;
  if (cropBlobUpdateTimer) window.clearTimeout(cropBlobUpdateTimer);
  cropBlobUpdateTimer = window.setTimeout(() => {
    cropBlobUpdateTimer = null;
    requestAnimationFrame(() => void updateCroppedBlob());
  }, 150);
}

function fitOverlayName(): void {
  const maxPx = window.matchMedia('(max-width: 520px)').matches ? 18 : 22;
  const minPx = window.matchMedia('(max-width: 520px)').matches ? 12 : 12;

  if (overlayName.clientWidth <= 0) return;

  let low = minPx;
  let high = maxPx;
  for (let i = 0; i < 12; i++) {
    const mid = (low + high) / 2;
    overlayName.style.fontSize = `${mid}px`;
    if (overlayName.scrollWidth <= overlayName.clientWidth) {
      low = mid;
    } else {
      high = mid;
    }
  }
  overlayName.style.fontSize = `${Math.floor(low)}px`;
}

function updateOverlayName(): void {
  const name = (artistNameInput.value || '').trim();
  overlayName.textContent = name.toUpperCase();
  requestAnimationFrame(fitOverlayName);
  scheduleCroppedBlobUpdate();
}

artistNameInput.addEventListener('input', updateOverlayName);
updateOverlayName();
window.addEventListener('resize', () => {
  requestAnimationFrame(fitOverlayName);
  scheduleCroppedBlobUpdate();
});

function detachOverlayToFrame(): void {
  const frame = cropperWrapper.querySelector<HTMLElement>('.cropper-frame');
  if (!frame || !overlayRoot) return;
  if (overlayRoot.parentElement !== frame) frame.appendChild(overlayRoot);
}

function attachOverlayToActiveCropArea(): void {
  if (!overlayRoot) return;

  const cropBox = cropperWrapper.querySelector<HTMLElement>('.cropper-crop-box');
  const viewBox = cropperWrapper.querySelector<HTMLElement>('.cropper-view-box');
  const target = viewBox || cropBox;
  if (!target) return;

  if (overlayRoot.parentElement !== target) target.appendChild(overlayRoot);
}

function setCropBoxPadding(paddingFraction = 0.05): void {
  if (!artistPicCropper) return;

  // CropperJS provides left/top at runtime, but its TS types don't always include them.
  const container = artistPicCropper.getContainerData() as unknown as {
    left?: number;
    top?: number;
    width: number;
    height: number;
  };
  if (!container || container.width <= 0 || container.height <= 0) return;

  const side = Math.min(container.width, container.height);
  const pad = side * paddingFraction;
  const cropSide = Math.max(1, side - 2 * pad);

  const leftBase = container.left ?? 0;
  const topBase = container.top ?? 0;
  const left = leftBase + (container.width - cropSide) / 2;
  const top = topBase + (container.height - cropSide) / 2;

  artistPicCropper.setCropBoxData({ left, top, width: cropSide, height: cropSide });
}

window.addEventListener('resize', () => requestAnimationFrame(() => setCropBoxPadding(0.05)));

noVoiceMessageCheckbox.addEventListener('change', function (this: HTMLInputElement) {
  if (this.checked) {
    voiceMessageFile.disabled = true;
    voiceMessageFile.required = false;
    voiceMessageFile.value = '';
  } else {
    voiceMessageFile.disabled = false;
  }
});

function validateFileSize(file: File | undefined, fieldName: string): true {
  if (file && file.size > MAX_SINGLE_FILE_SIZE_BYTES) {
    throw new Error(`${fieldName} exceeds maximum size of ${MAX_SINGLE_FILE_SIZE_MB}MB`);
  }
  return true;
}

function validateTotalSize(files: Array<File | undefined>): true {
  const total = files.reduce((sum, file) => sum + (file ? file.size : 0), 0);
  if (total > MAX_TOTAL_UPLOAD_BYTES) {
    throw new Error(`Total upload exceeds maximum size of ${MAX_TOTAL_UPLOAD_MB}MB`);
  }
  return true;
}

function validateFiles(): true {
  const artistPic = artistPicInput.files?.[0];
  const track1 = getRequiredElement<HTMLInputElement>('track1-file').files?.[0];
  const track2 = getRequiredElement<HTMLInputElement>('track2-file').files?.[0];
  const voiceMsg = voiceMessageFile.files?.[0];

  validateFileSize(artistPic, 'Artist picture');
  validateFileSize(track1, 'Track 1');
  validateFileSize(track2, 'Track 2');
  if (voiceMsg) validateFileSize(voiceMsg, 'Voice message');

  validateTotalSize([artistPic, track1, track2, voiceMsg]);
  return true;
}

function showMessage(message: string, isError = false): void {
  formMessage.textContent = message;
  formMessage.className = `form-message ${isError ? 'error' : 'success'}`;
  formMessage.style.display = 'block';
}

form.addEventListener('submit', async (e) => {
  e.preventDefault();

  try {
    validateFiles();
  } catch (error) {
    const message = error instanceof Error ? error.message : 'File validation failed';
    showMessage(message, true);
    return;
  }

  if (artistPicCropper) {
    await updateCroppedBlob();
  }

  const formData = new FormData(form);

  const safeName = (artistPicOriginalFilename || 'artist.jpg').replace(/\.[^/.]+$/, '');
  if (artistPicCroppedFilteredBlob) {
    formData.set('artist-pic-cropped', artistPicCroppedFilteredBlob, `${safeName}.jpg`);
  }
  if (artistPicBrandedBlob) {
    formData.set('artist-pic-branded', artistPicBrandedBlob, `${safeName}.jpg`);
  }

  submitBtn.disabled = true;
  submitBtn.textContent = 'Uploading...';
  uploadProgress.style.display = 'block';
  formMessage.style.display = 'none';

  try {
    const response = await fetch(API_URL, {
      method: 'POST',
      body: formData,
    });

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(errorText || 'Upload failed');
    }

    const result: unknown = await response.json();
    const message =
      typeof result === 'object' && result && 'message' in result
        ? String((result as { message: unknown }).message)
        : "Thank you for your submission! We'll be in touch soon.";

    progressFill.style.width = '100%';
    progressText.textContent = 'Upload complete!';
    showMessage(message);

    setTimeout(() => {
      form.reset();
      uploadProgress.style.display = 'none';
      progressFill.style.width = '0%';
    }, 2000);
  } catch (error) {
    const message = error instanceof Error ? error.message : 'An error occurred. Please try again.';
    showMessage(message || 'An error occurred. Please try again.', true);
    uploadProgress.style.display = 'none';
  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = 'Submit Application';
  }
});

function destroyArtistCropper(): void {
  if (artistPicCropper) {
    detachOverlayToFrame();
    artistPicCropper.destroy();
    artistPicCropper = null;
  }
  if (artistPicObjectUrl) {
    URL.revokeObjectURL(artistPicObjectUrl);
    artistPicObjectUrl = null;
  }
}

function resetCropUi(): void {
  artistPicCroppedFilteredBlob = null;
  artistPicBrandedBlob = null;
  cropperWrapper.style.display = 'none';
}

let moafunkLogoImg: HTMLImageElement | null = null;
const moafunkLogoPromise: Promise<HTMLImageElement | null> = new Promise((resolve) => {
  const img = new Image();
  img.crossOrigin = 'anonymous';
  img.onload = () => {
    moafunkLogoImg = img;
    resolve(img);
  };
  img.onerror = () => resolve(null);
  img.src = '/moafunk.png';
});

const shoikaFontsPromise: Promise<FontFace[] | null> = Promise.all([
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

async function drawOverlayOnCanvas(canvas: HTMLCanvasElement): Promise<void> {
  const ctx = canvas.getContext('2d');
  if (!ctx) return;

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
    cropperWrapper.querySelector<HTMLElement>('.cropper-view-box') ||
    cropperWrapper.querySelector<HTMLElement>('.cropper-crop-box');
  if (!viewBox || !overlayRoot) return;

  const viewRect = viewBox.getBoundingClientRect();
  if (viewRect.width <= 0 || viewRect.height <= 0) return;

  const scale = size / viewRect.width;
  const toCanvasX = (px: number) => (px - viewRect.left) * scale;
  const toCanvasY = (px: number) => (px - viewRect.top) * scale;
  const toCanvasLen = (px: number) => px * scale;

  const leftUn = overlayRoot.querySelector<HTMLElement>('.uploader-overlay-left .un');
  const leftHeard = overlayRoot.querySelector<HTMLElement>('.uploader-overlay-left .heard');
  if (leftUn && leftHeard) {
    const unRect = leftUn.getBoundingClientRect();
    const heardRect = leftHeard.getBoundingClientRect();
    const unStyle = getComputedStyle(leftUn);
    const heardStyle = getComputedStyle(leftHeard);

    const unFontPx = parseFloat(unStyle.fontSize) || 16;
    const heardFontPx = parseFloat(heardStyle.fontSize) || 16;

    if (document.fonts?.load) {
      try {
        await Promise.all([
          document.fonts.load(`${unStyle.fontWeight} ${unFontPx}px Shoika`),
          document.fonts.load(`${heardStyle.fontWeight} ${heardFontPx}px Shoika`),
        ]);
      } catch {
        // ignore
      }
    }

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

  const logoEl = overlayRoot.querySelector<HTMLImageElement>('img.uploader-overlay-logo');
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

  const nameText = (overlayName.textContent || '').trim();
  if (nameText) {
    const nameRect = overlayName.getBoundingClientRect();
    const nameStyle = getComputedStyle(overlayName);
    const nameFontPx = parseFloat(nameStyle.fontSize) || 16;

    if (document.fonts?.load) {
      try {
        await document.fonts.load(`${nameStyle.fontWeight} ${nameFontPx}px Shoika`);
      } catch {
        // ignore
      }
    }

    ctx.fillStyle = nameStyle.color || '#ffffff';
    ctx.textBaseline = 'middle';
    ctx.textAlign = 'center';
    ctx.font = `${nameStyle.fontStyle} ${nameStyle.fontWeight} ${toCanvasLen(nameFontPx)}px Shoika, sans-serif`;

    const centerX = toCanvasX(nameRect.left + nameRect.width / 2);
    const centerY = toCanvasY(nameRect.top + nameRect.height / 2);
    ctx.fillText(nameText, centerX, centerY);
  }
}

async function updateCroppedBlob(): Promise<void> {
  if (!artistPicCropper) return;

  if (overlayRoot) overlayRoot.style.visibility = 'hidden';

  const canvas = artistPicCropper.getCroppedCanvas({
    width: 1024,
    height: 1024,
    imageSmoothingEnabled: true,
    imageSmoothingQuality: 'high',
  });

  if (overlayRoot) overlayRoot.style.visibility = 'visible';
  if (!canvas) return;

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

  const croppedFilteredBlob = await new Promise<Blob | null>((resolve) => {
    canvas.toBlob((b) => resolve(b), 'image/jpeg', 0.92);
  });
  if (croppedFilteredBlob) artistPicCroppedFilteredBlob = croppedFilteredBlob;

  await drawOverlayOnCanvas(canvas);

  const brandedBlob = await new Promise<Blob | null>((resolve) => {
    canvas.toBlob((b) => resolve(b), 'image/jpeg', 0.92);
  });
  if (brandedBlob) artistPicBrandedBlob = brandedBlob;
}

artistPicInput.addEventListener('change', (e) => {
  const input = e.currentTarget as HTMLInputElement;
  const file = input.files?.[0];

  resetCropUi();
  destroyArtistCropper();

  if (!file) return;

  if (file.size > MAX_SINGLE_FILE_SIZE_BYTES) {
    alert(`Artist picture exceeds maximum size of ${MAX_SINGLE_FILE_SIZE_MB}MB`);
    input.value = '';
    return;
  }

  artistPicOriginalFilename = file.name || 'artist.jpg';
  artistPicObjectUrl = URL.createObjectURL(file);
  cropperImg.src = artistPicObjectUrl;
  cropperWrapper.style.display = 'block';

  cropperImg.onload = () => {
    if (artistPicCropper) artistPicCropper.destroy();

    artistPicCropper = new Cropper(cropperImg, {
      aspectRatio: 1,
      viewMode: 3,
      dragMode: 'move',
      autoCropArea: 0.9,
      background: false,
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
        requestAnimationFrame(() => setCropBoxPadding(0.05));
        void updateCroppedBlob();
      },
      cropend() {
        void updateCroppedBlob();
      },
      zoom() {
        void updateCroppedBlob();
      },
    });
  };
});

zoomInBtn.addEventListener('click', () => {
  if (artistPicCropper) artistPicCropper.zoom(0.1);
});
zoomOutBtn.addEventListener('click', () => {
  if (artistPicCropper) artistPicCropper.zoom(-0.1);
});

(['track1-file', 'track2-file', 'voice-message'] as const).forEach((id) => {
  getRequiredElement<HTMLInputElement>(id).addEventListener('change', (e) => {
    const input = e.currentTarget as HTMLInputElement;
    const file = input.files?.[0];
    if (file && file.size > MAX_SINGLE_FILE_SIZE_BYTES) {
      alert(`File exceeds maximum size of ${MAX_SINGLE_FILE_SIZE_MB}MB`);
      input.value = '';
    }
  });
});
