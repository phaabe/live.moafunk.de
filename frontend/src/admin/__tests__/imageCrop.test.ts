import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApp, h, nextTick, ref, type App, type Component } from 'vue';
import CoverDrop from '../components/CoverDrop.vue';
import ShowHeader from '../components/show-detail/ShowHeader.vue';
import WizardTemplateCover from '../components/show-wizard/WizardTemplateCover.vue';
import { useShowWizard } from '../composables';
import type { ShowDetail } from '../api';
import {
  cropButton,
  cropClosed,
  cropDialog,
  cropper,
  loadCropImage,
  resetCropper,
  stubImages,
} from './cropFakes';

// Covers and photos: an image that does not have the shape it is shown in
// opens "Crop cover" / "Crop photo" first (#1106). The profile photo and the
// announcement photo are tested with their pages.

vi.mock('cropperjs', () => import('./cropFakes'));

// jsdom has no font loading, which the composables barrel uses (wizard).
vi.hoisted(() => {
  vi.stubGlobal(
    'FontFace',
    class {
      load() {
        return Promise.resolve(this);
      }
    }
  );
  Object.defineProperty(document, 'fonts', { value: { add() {} }, configurable: true });
});

let app: App | undefined;
let root: HTMLDivElement;
let revoked: string[];

beforeEach(() => {
  resetCropper();
  revoked = [];
  root = document.createElement('div');
  document.body.appendChild(root);
  // jsdom has no object URLs: name them after the file.
  URL.createObjectURL = (blob: Blob) => `blob:${(blob as File).name ?? 'cropped'}`;
  URL.revokeObjectURL = (url: string) => void revoked.push(url);
  stubImages({
    'blob:wide.jpg': [1600, 900],
    'blob:tall.png': [600, 1200],
    'blob:photo.heic': null,
  });
});

afterEach(() => {
  app?.unmount();
  app = undefined;
  root.remove();
  document.body.innerHTML = '';
  vi.unstubAllGlobals();
});

function mount(render: () => ReturnType<typeof h>) {
  app = createApp({ render });
  app.mount(root);
}

async function settle() {
  for (let i = 0; i < 3; i++) {
    await new Promise((done) => setTimeout(done));
    await nextTick();
  }
}

async function pick(name: string, type: string) {
  const input = root.querySelector<HTMLInputElement>('input[type=file]')!;
  Object.defineProperty(input, 'files', {
    configurable: true,
    value: [new File(['image'], name, { type })],
  });
  input.dispatchEvent(new Event('change'));
  await settle();
}

async function useCrop() {
  loadCropImage();
  cropButton('Use crop').click();
  await cropClosed();
}

describe('CoverDrop (New Show, My Show)', () => {
  const cover = ref<File | null>(null);

  beforeEach(() => {
    cover.value = null;
    mount(() =>
      h(CoverDrop, {
        modelValue: cover.value,
        'onUpdate:modelValue': (next: File | null) => (cover.value = next),
      })
    );
  });

  it('takes a square image as it is', async () => {
    await pick('square.jpg', 'image/jpeg');

    expect(cropDialog()).toBeNull();
    expect(cover.value?.name).toBe('square.jpg');
    expect(revoked).toContain('blob:square.jpg');
  });

  it('opens Crop cover for an image that is not square', async () => {
    await pick('wide.jpg', 'image/jpeg');

    expect(cropDialog()?.querySelector('.modal-title')?.textContent).toBe('Crop cover');
    expect(cropDialog()?.textContent).toContain('Drag and zoom to pick the square part.');
    expect(cover.value).toBeNull();

    loadCropImage();
    expect(cropper.options).toEqual([expect.objectContaining({ aspectRatio: 1, viewMode: 1 })]);
  });

  it('Use crop sets the square part as the cover', async () => {
    await pick('wide.jpg', 'image/jpeg');
    await useCrop();

    expect(cover.value?.name).toBe('wide.jpg');
    expect(cover.value?.type).toBe('image/jpeg');
    // The cropped bytes, not the 5 bytes of the picked file.
    expect(cover.value?.size).toBe('cropped'.length);
    expect(cropper.canvas.maxWidth).toBe(3000);
    expect(cropper.destroyed).toBe(1);
    expect(revoked).toContain('blob:wide.jpg');
  });

  it('keeps a PNG a PNG', async () => {
    await pick('tall.png', 'image/png');
    await useCrop();

    expect(cropper.canvas.type).toBe('image/png');
    expect(cover.value?.name).toBe('tall.png');
  });

  it('Cancel keeps the cover as it was', async () => {
    await pick('square.jpg', 'image/jpeg');
    await pick('wide.jpg', 'image/jpeg');
    loadCropImage();
    cropButton('Cancel').click();
    await cropClosed();

    expect(cover.value?.name).toBe('square.jpg');
    expect(cropper.destroyed).toBe(1);
    expect(revoked).toContain('blob:wide.jpg');
  });

  it('sends an image the browser cannot read as it is', async () => {
    await pick('photo.heic', 'image/heic');

    expect(cropDialog()).toBeNull();
    expect(cover.value?.name).toBe('photo.heic');
  });

  it('drops a file that is not an image', async () => {
    await pick('square.jpg', 'image/jpeg');
    await pick('notes.txt', 'text/plain');

    expect(cover.value).toBeNull();
  });
});

describe('ShowHeader (admin show page)', () => {
  let selected: File[];

  beforeEach(() => {
    selected = [];
    const show = { id: 1, title: 'Brunchtime', show_type: 'unheard', cover_url: null };
    mount(() =>
      h(ShowHeader as Component, {
        show: show as unknown as ShowDetail,
        phase: 'prep',
        editMode: false,
        canEdit: true,
        canEditSchedule: false,
        uploadingCover: false,
        saving: false,
        title: '',
        description: '',
        'onCover-selected': (file: File) => selected.push(file),
      })
    );
  });

  it('sends the cropped cover', async () => {
    await pick('wide.jpg', 'image/jpeg');
    expect(selected).toEqual([]);
    await useCrop();

    expect(selected.map((f) => [f.name, f.size])).toEqual([['wide.jpg', 'cropped'.length]]);
  });

  it('sends nothing on Cancel', async () => {
    await pick('wide.jpg', 'image/jpeg');
    cropButton('Cancel').click();
    await cropClosed();
    await settle();

    expect(selected).toEqual([]);
  });
});

describe('WizardTemplateCover (admin show wizard)', () => {
  const wizard = useShowWizard();

  beforeEach(() => {
    wizard.setCover(null);
    mount(() => h(WizardTemplateCover));
  });

  it('keeps the cropped cover', async () => {
    await pick('wide.jpg', 'image/jpeg');
    expect(wizard.coverFile.value).toBeNull();
    await useCrop();

    expect(wizard.coverFile.value?.size).toBe('cropped'.length);
  });
});
