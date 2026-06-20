import { describe, it, expect } from 'vitest';
import { resolveMediaMode } from '../showMediaMode';

describe('resolveMediaMode', () => {
  it("opens on the live tab for a show created as stream_mode 'live'", () => {
    expect(resolveMediaMode({ stream_mode: 'live', prerecorded_key: undefined })).toBe('live');
  });

  it("opens on the upload tab for a show created as stream_mode 'prerecorded'", () => {
    expect(resolveMediaMode({ stream_mode: 'prerecorded', prerecorded_key: undefined })).toBe(
      'upload'
    );
  });

  it('defaults to upload when stream_mode is unset', () => {
    expect(resolveMediaMode({ stream_mode: undefined, prerecorded_key: undefined })).toBe('upload');
  });

  it('forces upload when a prerecorded file is staged, even for a live show', () => {
    expect(resolveMediaMode({ stream_mode: 'live', prerecorded_key: 'shows/1/audio.mp3' })).toBe(
      'upload'
    );
  });
});
