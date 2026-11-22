import { describe, expect, it } from 'vitest';
import {
  FADE_IN_S,
  FADE_OUT_S,
  RAMP_S,
  clampGain,
  clock,
  dbToGain,
  fmt,
  gainAt,
  loudness,
  newEdit,
  parseTime,
  problems,
  signed,
  span,
  type RecordingEdit,
} from '../components/recording-editor/edit';

const edit = (over: Partial<RecordingEdit> = {}): RecordingEdit => ({
  ...newEdit(600),
  start: 60,
  end: 540,
  ...over,
});

describe('gainAt', () => {
  it('cuts everything outside the set', () => {
    expect(gainAt(edit(), 59.9)).toBeNull();
    expect(gainAt(edit(), 540.1)).toBeNull();
  });

  it('fades in and out at the cuts', () => {
    expect(gainAt(edit(), 60 + FADE_IN_S / 2)).toBeCloseTo(0.5);
    expect(gainAt(edit(), 540 - FADE_OUT_S / 2)).toBeCloseTo(0.5);
    expect(gainAt(edit(), 300)).toBe(1);
  });

  it('applies the set volume', () => {
    expect(gainAt(edit({ gainDb: -6 }), 300)).toBeCloseTo(dbToGain(-6));
  });

  it('ramps a section in and out and keeps the set volume', () => {
    const e = edit({ gainDb: -6, sections: [{ id: 's', start: 100, end: 200, gainDb: 6 }] });
    expect(gainAt(e, 99)).toBeCloseTo(dbToGain(-6));
    expect(gainAt(e, 100 + RAMP_S / 2)).toBeCloseTo(dbToGain(-6) * (1 + (dbToGain(6) - 1) / 2));
    expect(gainAt(e, 150)).toBeCloseTo(1);
    expect(gainAt(e, 201)).toBeCloseTo(dbToGain(-6));
  });
});

describe('problems', () => {
  it('accepts a fresh edit', () => {
    expect(problems(newEdit(600), 600)).toEqual([]);
  });

  it('rejects an end before the start', () => {
    expect(problems(edit({ start: 300, end: 200 }), 600)).toHaveLength(1);
  });

  it('rejects snippets outside the set', () => {
    expect(problems(edit({ snippets: [{ slot: 2, start: 520, length: 30 }] }), 600)).toEqual([
      'Snippet 2 must be inside the set.',
    ]);
  });

  it('rejects overlapping sections', () => {
    const sections = [
      { id: 'a', start: 100, end: 200, gainDb: 3 },
      { id: 'b', start: 150, end: 250, gainDb: -3 },
    ];
    expect(problems(edit({ sections }), 600)).toEqual(['Volume sections must not overlap.']);
  });
});

describe('loudness', () => {
  it('counts the places over 0 dB after the volume', () => {
    // 20 values per second; two loud spots in the set.
    const peaks = new Array(600 * 20).fill(0.5);
    for (let i = 2000; i < 2040; i++) peaks[i] = 0.9;
    for (let i = 6000; i < 6040; i++) peaks[i] = 0.9;
    expect(loudness(edit(), peaks, 20).places).toBe(0);
    const loud = loudness(edit({ gainDb: 3 }), peaks, 20);
    expect(loud.places).toBe(2);
    expect(loud.peakDb).toBeCloseTo(20 * Math.log10(0.9 * dbToGain(3)));
  });
});

describe('text', () => {
  it('formats times, lengths and volumes', () => {
    expect(fmt(3909.46)).toBe('1:05:09.4');
    expect(fmt(369.6)).toBe('0:06:09.6');
    expect(clock(6830.4)).toBe('1:53:50');
    expect(span(300)).toBe('5:00');
    expect(span(6460.8)).toBe('1:47:41');
    expect(signed(2.5)).toBe('+2.5 dB');
    expect(signed(-3)).toBe('−3.0 dB');
    expect(clampGain(13)).toBe(12);
    expect(clampGain(2.3)).toBe(2.5);
  });

  it('reads a typed time', () => {
    expect(parseTime('0:06:09.6')).toBeCloseTo(369.6);
    expect(parseTime('6:09,6')).toBeCloseTo(369.6);
    expect(parseTime('369.6')).toBeCloseTo(369.6);
    expect(parseTime('1:2:3:4')).toBeNull();
    expect(parseTime('abc')).toBeNull();
    expect(parseTime('')).toBeNull();
  });
});
