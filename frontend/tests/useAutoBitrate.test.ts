import { describe, it, expect } from 'vitest';
import { nextTick, ref } from 'vue';
import {
  decideBitrate,
  initialAutoState,
  useAutoBitrate,
  QUALITY_STEPS_KBPS,
  AUTO_CEILING_KBPS,
  STEP_DOWN_TICKS,
  STEP_UP_TICKS,
  STEP_SETTLE_TICKS,
  type AutoBitrateState,
  type UploadQualityMode,
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
    expect(run(128, CONGESTED, STEP_DOWN_TICKS).nextKbps).toBe(96);
  });

  it('has nowhere to go below the lowest step', () => {
    const floor = QUALITY_STEPS_KBPS[QUALITY_STEPS_KBPS.length - 1];
    const last = run(floor, CONGESTED, STEP_DOWN_TICKS);
    expect(last.nextKbps).toBeNull();
    // No switch happened, so no settle window either.
    expect(last.state.settleTicks).toBe(0);
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
  });

  it('steps straight down to the ceiling when entering auto above it', () => {
    // Manual 320 → Auto on a clean link: without this, 320 would stick forever.
    const clean = decideBitrate(320, CLEAN, initialAutoState());
    expect(clean.nextKbps).toBe(AUTO_CEILING_KBPS);
    const congested = decideBitrate(320, CONGESTED, initialAutoState());
    expect(congested.nextKbps).toBe(AUTO_CEILING_KBPS);
  });

  it('a congested tick resets the stability counter', () => {
    let s = run(96, CLEAN, STEP_UP_TICKS - 1).state;
    s = decideBitrate(96, CONGESTED, s).state;
    expect(run(96, CLEAN, STEP_UP_TICKS - 1, s).nextKbps).toBeNull();
    expect(run(96, CLEAN, STEP_UP_TICKS, s).nextKbps).toBe(128);
  });

  it('every switch starts a settle window so steps cannot cascade', () => {
    const down = run(192, CONGESTED, STEP_DOWN_TICKS);
    expect(down.nextKbps).toBe(128);
    expect(down.state.settleTicks).toBe(STEP_SETTLE_TICKS);
    // The backlog still reads congested at the new lower target, but the
    // settle window absorbs it — no decision until it elapses.
    const settling = run(128, CONGESTED, STEP_SETTLE_TICKS, down.state);
    expect(settling.nextKbps).toBeNull();
    expect(settling.state.settleTicks).toBe(0);
    // After settling, a real congestion streak still steps down normally.
    expect(run(128, CONGESTED, STEP_DOWN_TICKS, settling.state).nextKbps).toBe(96);
    // Step-up also settles.
    expect(run(96, CLEAN, STEP_UP_TICKS).state.settleTicks).toBe(STEP_SETTLE_TICKS);
  });
});

describe('useAutoBitrate (watcher glue)', () => {
  function harness(bufferSeconds = CONGESTED) {
    const mode = ref<UploadQualityMode>('auto');
    const currentKbps = ref(192);
    const buffer = ref(bufferSeconds);
    const ticks = ref(0);
    const changes: number[] = [];
    useAutoBitrate({
      mode,
      currentKbps,
      bufferSeconds: buffer,
      ticks,
      onChange: (kbps) => changes.push(kbps),
    });
    const tick = async () => {
      ticks.value++;
      await nextTick();
    };
    return { mode, currentKbps, buffer, tick, changes };
  }

  it('calls onChange with the new kbps after the congestion window', async () => {
    const h = harness();
    for (let i = 0; i < STEP_DOWN_TICKS; i++) await h.tick();
    expect(h.changes).toEqual([128]);
  });

  it('makes no decisions while a fixed quality is selected', async () => {
    const h = harness();
    h.mode.value = 192;
    for (let i = 0; i < STEP_DOWN_TICKS * 2; i++) await h.tick();
    expect(h.changes).toEqual([]);
  });

  it('flipping the mode resets the observation window', async () => {
    const h = harness();
    for (let i = 0; i < STEP_DOWN_TICKS - 1; i++) await h.tick();
    h.mode.value = 192;
    await nextTick();
    h.mode.value = 'auto';
    await nextTick();
    // Counter restarted — the partial streak from before doesn't carry over.
    for (let i = 0; i < STEP_DOWN_TICKS - 1; i++) await h.tick();
    expect(h.changes).toEqual([]);
    await h.tick();
    expect(h.changes).toEqual([128]);
  });
});
