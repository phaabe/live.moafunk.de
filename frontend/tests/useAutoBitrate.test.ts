import { describe, it, expect } from 'vitest';
import {
  decideBitrate,
  initialAutoState,
  QUALITY_STEPS_KBPS,
  AUTO_CEILING_KBPS,
  STEP_DOWN_TICKS,
  STEP_UP_TICKS,
  type AutoBitrateState,
} from '../src/admin/composables/useAutoBitrate';

const CONGESTED = 1.0; // > STEP_DOWN_BUFFER_S
const CLEAN = 0.0;

/** Run N identical ticks, returning the last decision. */
function run(currentKbps: number, bufferSeconds: number, ticks: number, state?: AutoBitrateState) {
  let s = state ?? initialAutoState();
  let last: ReturnType<typeof decideBitrate> = { nextKbps: null, state: s };
  for (let i = 0; i < ticks; i++) {
    last = decideBitrate(currentKbps, bufferSeconds, s);
    s = last.state;
  }
  return last;
}

describe('decideBitrate', () => {
  it('steps down one notch after STEP_DOWN_TICKS congested ticks', () => {
    expect(run(192, CONGESTED, STEP_DOWN_TICKS - 1).nextKbps).toBeNull();
    expect(run(192, CONGESTED, STEP_DOWN_TICKS).nextKbps).toBe(128);
  });

  it('steps down only one notch at a time', () => {
    expect(run(320, CONGESTED, STEP_DOWN_TICKS).nextKbps).toBe(192);
    expect(run(128, CONGESTED, STEP_DOWN_TICKS).nextKbps).toBe(96);
  });

  it('has nowhere to go below the lowest step', () => {
    const floor = QUALITY_STEPS_KBPS[QUALITY_STEPS_KBPS.length - 1];
    expect(run(floor, CONGESTED, STEP_DOWN_TICKS).nextKbps).toBeNull();
  });

  it('a clean tick resets the congestion counter', () => {
    let s = run(192, CONGESTED, STEP_DOWN_TICKS - 1).state;
    s = decideBitrate(192, CLEAN, s).state;
    // Needs the full window again after the interruption.
    expect(run(192, CONGESTED, STEP_DOWN_TICKS - 1, s).nextKbps).toBeNull();
    expect(run(192, CONGESTED, STEP_DOWN_TICKS, s).nextKbps).toBe(128);
  });

  it('steps back up after STEP_UP_TICKS stable ticks', () => {
    expect(run(96, CLEAN, STEP_UP_TICKS - 1).nextKbps).toBeNull();
    expect(run(96, CLEAN, STEP_UP_TICKS).nextKbps).toBe(128);
    expect(run(128, CLEAN, STEP_UP_TICKS).nextKbps).toBe(192);
  });

  it('never climbs above the auto ceiling', () => {
    expect(run(AUTO_CEILING_KBPS, CLEAN, STEP_UP_TICKS * 2).nextKbps).toBeNull();
    expect(run(320, CLEAN, STEP_UP_TICKS * 2).nextKbps).toBeNull();
  });

  it('a congested tick resets the stability counter', () => {
    let s = run(96, CLEAN, STEP_UP_TICKS - 1).state;
    s = decideBitrate(96, CONGESTED, s).state;
    expect(run(96, CLEAN, STEP_UP_TICKS - 1, s).nextKbps).toBeNull();
    expect(run(96, CLEAN, STEP_UP_TICKS, s).nextKbps).toBe(128);
  });

  it('resets both counters after a step so switches cannot cascade', () => {
    const down = run(192, CONGESTED, STEP_DOWN_TICKS);
    expect(down.nextKbps).toBe(128);
    expect(down.state).toEqual(initialAutoState());
    const up = run(96, CLEAN, STEP_UP_TICKS);
    expect(up.nextKbps).toBe(128);
    expect(up.state).toEqual(initialAutoState());
  });
});
