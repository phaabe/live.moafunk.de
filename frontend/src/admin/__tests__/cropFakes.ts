import { vi } from 'vitest';

// Test stand-ins for the crop dialog (ImageCropDialog.vue). jsdom loads no
// images and has no canvas. Use in a test file:
//
//   vi.mock('cropperjs', () => import('./cropFakes'));
//   beforeEach(() => stubImages({ 'blob:wide.jpg': [1600, 900] }));

/** What the fake cropper was asked for. Reset with `resetCropper()`. */
export const cropper = {
  options: [] as Record<string, unknown>[],
  destroyed: 0,
  canvas: { maxWidth: 0, type: '' },
};

export function resetCropper() {
  cropper.options = [];
  cropper.destroyed = 0;
  cropper.canvas = { maxWidth: 0, type: '' };
}

/** cropperjs: the cropped image is the 7 bytes "cropped". */
export default class FakeCropper {
  constructor(_img: HTMLImageElement, options: Record<string, unknown>) {
    cropper.options.push(options);
  }
  getCroppedCanvas(options: { maxWidth: number }) {
    cropper.canvas.maxWidth = options.maxWidth;
    return {
      toBlob(done: (blob: Blob) => void, type: string) {
        cropper.canvas.type = type;
        done(new Blob(['cropped'], { type }));
      },
    };
  }
  destroy() {
    cropper.destroyed += 1;
  }
}

/**
 * `new Image()` loads at once with the size given for its URL; any other URL
 * is a square 800×800. A size of null fails to load, like HEIC in Chrome.
 */
export function stubImages(sizes: Record<string, [number, number] | null> = {}) {
  vi.stubGlobal(
    'Image',
    class {
      naturalWidth = 0;
      naturalHeight = 0;
      onload: (() => void) | null = null;
      onerror: (() => void) | null = null;
      set src(url: string) {
        const size = url in sizes ? sizes[url] : [800, 800];
        // A microtask, not a timer: tests with fake timers still get it.
        queueMicrotask(() => {
          if (!size) return this.onerror?.();
          [this.naturalWidth, this.naturalHeight] = size;
          this.onload?.();
        });
      }
    }
  );
}

/** The open crop dialog, if any. It fades out, so it leaves the page a little later. */
export const cropDialog = () => document.body.querySelector<HTMLElement>('[role=dialog]');

export async function cropClosed() {
  await vi.waitFor(() => {
    if (cropDialog()) throw new Error('The crop dialog is still open');
  });
}

/** Starts the cropper: in a browser, the image in the dialog loads. */
export function loadCropImage() {
  cropDialog()!.querySelector('img')!.dispatchEvent(new Event('load'));
}

export function cropButton(text: 'Cancel' | 'Use crop'): HTMLButtonElement {
  const found = [...(cropDialog()?.querySelectorAll('button') ?? [])].find(
    (b) => b.textContent?.trim() === text
  );
  if (!found) throw new Error(`No button ${text} in the crop dialog`);
  return found;
}
