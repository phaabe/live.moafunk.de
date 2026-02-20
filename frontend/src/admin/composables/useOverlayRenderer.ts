/**
 * Composable for rendering the UNHEARD overlay on a canvas.
 *
 * Extracts font loading, logo loading and the drawOverlayOnCanvas logic
 * from ImageCropper.vue so both the existing cropper and the new
 * Overlay Editor page can share the same rendering pipeline.
 *
 * Positions are expressed as percentages (0-100) of the canvas size.
 * Text sizes are in canvas pixels (at 1024×1024).
 * Logo size is a percentage of the canvas width.
 */

import type {
  OverlayParams,
  OverlayElementParams,
  OverlayFilterParams,
  OverlayShadowParams,
} from '../api';
import type Cropper from 'cropperjs';

// ---------------------------------------------------------------------------
// Font & logo loading (singleton promises — loaded once per page)
// ---------------------------------------------------------------------------

let _moafunkLogoImg: HTMLImageElement | null = null;

const moafunkLogoPromise: Promise<HTMLImageElement | null> = new Promise((resolve) => {
  const img = new Image();
  img.crossOrigin = 'anonymous';
  img.onload = () => {
    _moafunkLogoImg = img;
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

// ---------------------------------------------------------------------------
// Default overlay params (match the hardcoded values in ImageCropper.vue)
// ---------------------------------------------------------------------------

export function getDefaultOverlayParams(): OverlayParams {
  return {
    un: {
      visible: true,
      x: 15,
      y: 85,
      size: 44,
      color: '#ffec44',
      fontWeight: '600',
      fontStyle: 'italic',
      shadow: { offsetX: 2, offsetY: 2, color: '#000000' },
    },
    heard: {
      visible: true,
      x: 15,
      y: 89,
      size: 19,
      color: '#ffffff',
      fontWeight: '400',
      fontStyle: 'italic',
      shadow: { offsetX: 1, offsetY: 1, color: '#000000' },
    },
    logo: {
      visible: true,
      x: 50,
      y: 87.5,
      size: 15,
      color: '',
    },
    artistName: {
      visible: true,
      x: 80,
      y: 87.5,
      size: 22,
      color: '#ffffff',
      fontWeight: '700',
      fontStyle: 'normal',
      shadow: { offsetX: 1, offsetY: 1, color: '#000000' },
    },
    filter: {
      brightness: 1,
      contrast: 1.08,
      saturate: 0.85,
      hueRotate: 0,
      grayscale: 0,
      sepia: 0,
      blur: 0,
    },
  };
}

// ---------------------------------------------------------------------------
// CSS filter string builder
// ---------------------------------------------------------------------------

export function buildFilterString(filter: OverlayFilterParams): string {
  const parts: string[] = [];
  if (filter.brightness !== 1) parts.push(`brightness(${filter.brightness})`);
  if (filter.contrast !== 1) parts.push(`contrast(${filter.contrast})`);
  if (filter.saturate !== 1) parts.push(`saturate(${filter.saturate})`);
  if (filter.hueRotate !== 0) parts.push(`hue-rotate(${filter.hueRotate}deg)`);
  if (filter.grayscale !== 0) parts.push(`grayscale(${filter.grayscale})`);
  if (filter.sepia !== 0) parts.push(`sepia(${filter.sepia})`);
  if (filter.blur !== 0) parts.push(`blur(${filter.blur}px)`);
  return parts.length > 0 ? parts.join(' ') : 'none';
}

// ---------------------------------------------------------------------------
// Canvas-based overlay drawing (params-driven, no DOM dependency)
// ---------------------------------------------------------------------------

/**
 * Draw the UNHEARD overlay onto an existing canvas using OverlayParams.
 *
 * All positions are computed from percentage values + font sizes in the
 * params object.  No DOM measurements are needed.
 *
 * @param canvas  - Target canvas (expected 1024×1024 but works at any size)
 * @param params  - Full overlay parameters
 * @param artistName - Artist display name (uppercased automatically)
 */
export async function drawOverlayOnCanvas(
  canvas: HTMLCanvasElement,
  params: OverlayParams,
  artistName: string
): Promise<void> {
  const ctx = canvas.getContext('2d');
  if (!ctx) return;

  const w = canvas.width;
  const h = canvas.height;

  // Ensure fonts & logo are loaded
  await Promise.all([moafunkLogoPromise, shoikaFontsPromise]);
  if (document.fonts?.ready) {
    try {
      await document.fonts.ready;
    } catch {
      // ignore
    }
  }

  ctx.save();
  ctx.filter = 'none';

  // --- UN text ---
  if (params.un.visible) {
    drawText(ctx, 'UN', params.un, w, h, 'left', 'top');
  }

  // --- HEARD text ---
  if (params.heard.visible) {
    drawText(ctx, 'HEARD', params.heard, w, h, 'left', 'top');
  }

  // --- Logo ---
  if (params.logo.visible && _moafunkLogoImg) {
    drawLogo(ctx, params.logo, w, h);
  }

  // --- Artist name ---
  if (params.artistName.visible && artistName) {
    drawArtistName(ctx, artistName.toUpperCase(), params.artistName, w, h);
  }

  ctx.restore();
}

// ---------------------------------------------------------------------------
// Internal drawing helpers
// ---------------------------------------------------------------------------

function drawText(
  ctx: CanvasRenderingContext2D,
  text: string,
  el: OverlayElementParams,
  canvasW: number,
  canvasH: number,
  align: CanvasTextAlign,
  baseline: CanvasTextBaseline
): void {
  const x = (el.x / 100) * canvasW;
  const y = (el.y / 100) * canvasH;
  const fontStyle = el.fontStyle ?? 'normal';
  const fontWeight = el.fontWeight ?? '700';

  ctx.textAlign = align;
  ctx.textBaseline = baseline;
  ctx.font = `${fontStyle} ${fontWeight} ${el.size}px Shoika, sans-serif`;

  // Draw shadow first (behind the main text)
  if (el.shadow) {
    ctx.fillStyle = el.shadow.color;
    ctx.fillText(text, x + el.shadow.offsetX, y + el.shadow.offsetY);
  }

  ctx.fillStyle = el.color;
  ctx.fillText(text, x, y);
}

function drawLogo(
  ctx: CanvasRenderingContext2D,
  el: OverlayElementParams,
  canvasW: number,
  canvasH: number
): void {
  if (!_moafunkLogoImg) return;

  const centerX = (el.x / 100) * canvasW;
  const centerY = (el.y / 100) * canvasH;
  const boxSize = (el.size / 100) * canvasW; // size is % of canvas width

  const srcW = _moafunkLogoImg.naturalWidth || _moafunkLogoImg.width || 1;
  const srcH = _moafunkLogoImg.naturalHeight || _moafunkLogoImg.height || 1;
  const srcAspect = srcW / srcH;

  // Contain-fit within the square box
  let drawW = boxSize;
  let drawH = boxSize;
  if (srcAspect >= 1) {
    drawH = boxSize / srcAspect;
  } else {
    drawW = boxSize * srcAspect;
  }

  const drawX = centerX - drawW / 2;
  const drawY = centerY - drawH / 2;
  ctx.drawImage(_moafunkLogoImg, drawX, drawY, drawW, drawH);
}

function drawArtistName(
  ctx: CanvasRenderingContext2D,
  text: string,
  el: OverlayElementParams,
  canvasW: number,
  canvasH: number
): void {
  const centerX = (el.x / 100) * canvasW;
  const centerY = (el.y / 100) * canvasH;
  const fontStyle = el.fontStyle ?? 'normal';
  const fontWeight = el.fontWeight ?? '700';

  // Auto-fit: shrink font from el.size down to minPx until it fits in ~35% of canvas width
  const maxWidth = canvasW * 0.35;
  const maxPx = el.size;
  const minPx = 12;

  let fontSize = maxPx;
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';

  // Binary search for best fit
  let low = minPx;
  let high = maxPx;
  for (let i = 0; i < 12; i++) {
    const mid = (low + high) / 2;
    ctx.font = `${fontStyle} ${fontWeight} ${mid}px Shoika, sans-serif`;
    const measured = ctx.measureText(text);
    if (measured.width <= maxWidth) {
      low = mid;
    } else {
      high = mid;
    }
  }
  fontSize = Math.floor(low);

  ctx.font = `${fontStyle} ${fontWeight} ${fontSize}px Shoika, sans-serif`;

  // Draw shadow first (behind the main text)
  if (el.shadow) {
    ctx.fillStyle = el.shadow.color;
    ctx.fillText(text, centerX + el.shadow.offsetX, centerY + el.shadow.offsetY);
  }

  ctx.fillStyle = el.color;
  ctx.fillText(text, centerX, centerY);
}

// ---------------------------------------------------------------------------
// Apply image filter to canvas in-place
// ---------------------------------------------------------------------------

export function applyFilterToCanvas(canvas: HTMLCanvasElement, filter: OverlayFilterParams): void {
  const ctx = canvas.getContext('2d');
  if (!ctx) return;

  const filterStr = buildFilterString(filter);
  if (filterStr === 'none') return;

  const filtered = document.createElement('canvas');
  filtered.width = canvas.width;
  filtered.height = canvas.height;
  const fctx = filtered.getContext('2d');
  if (!fctx) return;

  fctx.filter = filterStr;
  fctx.drawImage(canvas, 0, 0);

  ctx.save();
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.restore();
  ctx.filter = 'none';
  ctx.drawImage(filtered, 0, 0);
}

// ---------------------------------------------------------------------------
// High-level render helpers
// ---------------------------------------------------------------------------

/**
 * Render the full preview from a CropperJS instance.
 *
 * 1. Gets the 1024×1024 cropped canvas
 * 2. Applies the image filter
 * 3. Snapshots the cropped (filtered, no overlay) blob
 * 4. Draws the UNHEARD overlay
 * 5. Snapshots the branded (filtered + overlay) blob
 *
 * Returns both blobs (JPEG 0.92 quality).
 */
export async function renderPreview(
  cropperInstance: Cropper,
  params: OverlayParams,
  artistName: string
): Promise<{ croppedBlob: Blob; brandedBlob: Blob }> {
  const canvas = cropperInstance.getCroppedCanvas({
    width: 1024,
    height: 1024,
    imageSmoothingEnabled: true,
    imageSmoothingQuality: 'high',
  });

  if (!canvas) {
    throw new Error('CropperJS returned no canvas');
  }

  // Apply filter
  applyFilterToCanvas(canvas, params.filter);

  // Cropped blob (filtered, without overlay)
  const croppedBlob = await canvasToBlob(canvas);

  // Draw overlay
  await drawOverlayOnCanvas(canvas, params, artistName);

  // Branded blob (filtered + overlay)
  const brandedBlob = await canvasToBlob(canvas);

  return { croppedBlob, brandedBlob };
}

// ---------------------------------------------------------------------------
// DOM-based overlay drawing (used by ImageCropper.vue's existing flow)
// ---------------------------------------------------------------------------

/**
 * Draw the overlay by sampling positions from actual DOM elements.
 * This preserves the exact existing behaviour of ImageCropper.vue.
 *
 * @param canvas         - Target canvas (1024×1024)
 * @param cropperWrapper - The .cropper-wrapper element containing the crop UI
 * @param overlayRoot    - The .uploader-overlay root element
 * @param overlayNameEl  - The .uploader-overlay-right element with the artist name
 */
export async function drawOverlayFromDOM(
  canvas: HTMLCanvasElement,
  cropperWrapper: HTMLElement,
  overlayRoot: HTMLElement,
  overlayNameEl: HTMLElement | null
): Promise<void> {
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
  if (!viewBox) return;

  const viewRect = viewBox.getBoundingClientRect();
  if (viewRect.width <= 0 || viewRect.height <= 0) return;

  const scale = size / viewRect.width;
  const toCanvasX = (px: number) => (px - viewRect.left) * scale;
  const toCanvasY = (px: number) => (px - viewRect.top) * scale;
  const toCanvasLen = (px: number) => px * scale;

  // UN + HEARD
  const leftUn = overlayRoot.querySelector<HTMLElement>('.uploader-overlay-left .un');
  const leftHeard = overlayRoot.querySelector<HTMLElement>('.uploader-overlay-left .heard');
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
      toCanvasY(heardRect.top)
    );
  }

  // Logo
  const logoEl = overlayRoot.querySelector<HTMLImageElement>('img.uploader-overlay-logo');
  if (logoEl && _moafunkLogoImg) {
    const logoRect = logoEl.getBoundingClientRect();

    const boxX = toCanvasX(logoRect.left);
    const boxY = toCanvasY(logoRect.top);
    const boxW = toCanvasLen(logoRect.width);
    const boxH = toCanvasLen(logoRect.height);

    const srcW = _moafunkLogoImg.naturalWidth || _moafunkLogoImg.width || 1;
    const srcH = _moafunkLogoImg.naturalHeight || _moafunkLogoImg.height || 1;
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
    ctx.drawImage(_moafunkLogoImg, drawX, drawY, drawW, drawH);
  }

  // Artist name
  if (overlayNameEl) {
    const nameText = (overlayNameEl.textContent || '').trim();
    if (nameText) {
      const nameRect = overlayNameEl.getBoundingClientRect();
      const nameStyle = getComputedStyle(overlayNameEl);
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

// ---------------------------------------------------------------------------
// Utility
// ---------------------------------------------------------------------------

function canvasToBlob(canvas: HTMLCanvasElement): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob(
      (b) => {
        if (b) resolve(b);
        else reject(new Error('canvas.toBlob returned null'));
      },
      'image/jpeg',
      0.92
    );
  });
}

export { moafunkLogoPromise, shoikaFontsPromise };
