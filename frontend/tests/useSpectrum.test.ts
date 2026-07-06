import { describe, it, expect } from 'vitest';
import { groupBands, SPECTRUM_BANDS } from '../src/admin/composables/useSpectrum';

const SAMPLE_RATE = 48000;
const FFT_SIZE = 2048;
const BIN_COUNT = FFT_SIZE / 2; // frequencyBinCount

function freqDataFilledWith(value: number): Uint8Array {
  return new Uint8Array(BIN_COUNT).fill(value);
}

describe('groupBands', () => {
  it('returns the requested number of bands (default SPECTRUM_BANDS)', () => {
    const bands = groupBands(freqDataFilledWith(0), SAMPLE_RATE, FFT_SIZE);
    expect(bands).toHaveLength(SPECTRUM_BANDS);
  });

  it('maps silence to all-zero bands', () => {
    const bands = groupBands(freqDataFilledWith(0), SAMPLE_RATE, FFT_SIZE);
    expect(bands.every((v) => v === 0)).toBe(true);
  });

  it('maps full-scale input to all-one bands', () => {
    const bands = groupBands(freqDataFilledWith(255), SAMPLE_RATE, FFT_SIZE);
    expect(bands.every((v) => v === 1)).toBe(true);
  });

  it('normalizes magnitudes to 0..1', () => {
    const bands = groupBands(freqDataFilledWith(128), SAMPLE_RATE, FFT_SIZE);
    for (const v of bands) {
      expect(v).toBeGreaterThan(0.49);
      expect(v).toBeLessThan(0.51);
    }
  });

  it('places a low-frequency tone in the bottom bands only', () => {
    // 100 Hz tone → bin ≈ 100 / (48000/2048) ≈ bin 4
    const data = freqDataFilledWith(0);
    const binHz = SAMPLE_RATE / FFT_SIZE;
    data[Math.round(100 / binHz)] = 255;

    const bands = groupBands(data, SAMPLE_RATE, FFT_SIZE);
    const hot = bands.map((v, i) => (v > 0 ? i : -1)).filter((i) => i >= 0);

    expect(hot.length).toBeGreaterThan(0);
    // All hot bands are in the lower third of the display.
    expect(Math.max(...hot)).toBeLessThan(SPECTRUM_BANDS / 3);
  });

  it('places a high-frequency tone in the top bands only', () => {
    // 12 kHz tone → upper end of the 40 Hz..16 kHz log scale
    const data = freqDataFilledWith(0);
    const binHz = SAMPLE_RATE / FFT_SIZE;
    data[Math.round(12000 / binHz)] = 255;

    const bands = groupBands(data, SAMPLE_RATE, FFT_SIZE);
    const hot = bands.map((v, i) => (v > 0 ? i : -1)).filter((i) => i >= 0);

    expect(hot.length).toBeGreaterThan(0);
    expect(Math.min(...hot)).toBeGreaterThan((SPECTRUM_BANDS * 2) / 3);
  });

  it('supports a custom band count', () => {
    const bands = groupBands(freqDataFilledWith(255), SAMPLE_RATE, FFT_SIZE, 12);
    expect(bands).toHaveLength(12);
    expect(bands.every((v) => v === 1)).toBe(true);
  });

  it('never reads past the end of the FFT data at 44.1 kHz', () => {
    // At 44.1 kHz the 16 kHz upper edge maps close to the last bin — must clamp.
    const bands = groupBands(freqDataFilledWith(255), 44100, FFT_SIZE);
    expect(bands).toHaveLength(SPECTRUM_BANDS);
    expect(bands.every((v) => v === 1)).toBe(true);
  });
});
