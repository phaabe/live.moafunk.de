import { describe, it, expect } from 'vitest';
import { audioContextOptionsForStream } from '../src/admin/composables/useAudioCapture';

/** Minimal MediaStream stub exposing just the audio-track sample rate. */
function streamWithRate(rate: number | undefined): MediaStream {
  return {
    getAudioTracks: () => [{ getSettings: () => (rate === undefined ? {} : { sampleRate: rate }) }],
  } as unknown as MediaStream;
}

describe('audioContextOptionsForStream', () => {
  it('matches the context to a 96 kHz pro interface (Xone:23C)', () => {
    expect(audioContextOptionsForStream(streamWithRate(96000))).toEqual({
      sampleRate: 96000,
    });
  });

  it('matches the context to a 44.1 kHz device', () => {
    expect(audioContextOptionsForStream(streamWithRate(44100))).toEqual({
      sampleRate: 44100,
    });
  });

  it('falls back to the default context when the rate is unknown', () => {
    expect(audioContextOptionsForStream(streamWithRate(undefined))).toBeUndefined();
  });

  it('falls back to the default context when there is no audio track', () => {
    const empty = { getAudioTracks: () => [] } as unknown as MediaStream;
    expect(audioContextOptionsForStream(empty)).toBeUndefined();
  });
});
