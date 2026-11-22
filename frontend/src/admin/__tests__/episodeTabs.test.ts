import { describe, expect, it } from 'vitest';
import type { ShowDetail } from '../api';
import type { SlotReading } from '../promotion';
import {
  recordingItems,
  showClock,
  socialItems,
  soundcheckItems,
} from '../components/episode/tabs';

// The tab tiles of the episode page (#1052): which badge each item gets.

/** So 25.10.2026, 12:00–14:00 Berlin (winter time, UTC+1). */
const show = (overrides: Partial<ShowDetail> = {}) =>
  ({
    id: 42,
    date: '2026-10-25',
    start_time: '12:00',
    end_time: '14:00',
    stream_mode: 'live',
    episode_status: 'upcoming',
    on_air: false,
    ...overrides,
  }) as ShowDetail;
const berlin = (day: number, hh: number, mm = 0) => Date.UTC(2026, 9, day, hh - 1, mm);
const slot = (s: Partial<SlotReading> & Pick<SlotReading, 'slot'>): SlotReading => ({
  state: 'scheduled',
  scheduled_at: null,
  sent_at: null,
  ...s,
});
const badges = (items: { badge: string }[]) => items.map((i) => i.badge);

describe('socialItems', () => {
  it('gives every slot its own badge', () => {
    const list = [
      slot({ slot: 'pre', state: 'pending_approval' }),
      slot({ slot: 'onair', state: 'cancelled' }),
      slot({ slot: 'post', state: 'scheduled' }),
    ];
    expect(badges(socialItems(list, false))).toEqual(['approve', 'off', 'lock']);
    // The post slot waits for the public track only until it is public.
    expect(badges(socialItems(list, true))[2]).toBe('sched');
  });

  it('reads a draft as not set up, and names sent and failed posts', () => {
    const list = [
      slot({ slot: 'pre', state: 'draft' }),
      slot({ slot: 'onair', state: 'sent' }),
      slot({ slot: 'post', state: 'failed', error: 'Instagram did not take the post.' }),
    ];
    const items = socialItems(list, true);
    expect(badges(items)).toEqual(['todo', 'ok', 'fail']);
    expect(items[2].label).toBe('Post-announcement: Failed · Instagram did not take the post.');
  });
});

describe('soundcheckItems', () => {
  it('shows the Studio checks while it is open, else only "passed in this tab"', () => {
    const checks = [
      { id: 'music', name: 'Music', state: 'ok', detail: 'Good' },
      { id: 'mic', name: 'Mic', state: 'warn', detail: 'Quiet' },
      { id: 'server', name: 'Server', state: 'fail', detail: 'No audio' },
      { id: 'return', name: 'Return feed', state: 'skipped', detail: 'Skipped' },
    ] as const;
    expect(badges(soundcheckItems(show(), 'pre', false, [...checks]))).toEqual([
      'ok',
      'warn',
      'fail',
      'off',
    ]);
    expect(badges(soundcheckItems(show(), 'pre', true, null))).toEqual(['ok', 'ok', 'ok', 'ok']);
    expect(soundcheckItems(show(), 'post', false, null)[0].label).toBe(
      'Music: No result in this browser'
    );
  });

  it('has the set instead of the checks for a pre-recorded episode', () => {
    expect(soundcheckItems(show({ stream_mode: 'prerecorded' }), 'pre', false, null)).toEqual([
      { icon: 'music', badge: 'todo', label: 'Set: no file yet' },
    ]);
  });
});

describe('recordingItems', () => {
  const ok = { state: 'ok', text: 'Recording ready' } as const;

  it('follows the recording through the show', () => {
    expect(badges(recordingItems(show(), 'pre', ok, false, false))).toEqual(['wait', 'wait']);
    expect(badges(recordingItems(show(), 'live', ok, true, false))[0]).toBe('rec');
    const after = (state: 'ok' | 'processing' | 'failed') =>
      recordingItems(show(), 'post', { state, text: '' }, false, false)[0].badge;
    expect([after('ok'), after('processing'), after('failed')]).toEqual(['ok', 'run', 'fail']);
  });

  it('tells a private from a public track, and an admin switch-off', () => {
    const url = 'https://soundcloud.com/radio-moafunk/brunchtime-31';
    const sc = (s: Partial<ShowDetail>, off = false) =>
      recordingItems(show(s), 'post', ok, false, off)[1].badge;
    expect(sc({ soundcloud_url: url })).toBe('lock');
    expect(sc({ soundcloud_url: url, soundcloud_public: true })).toBe('ok');
    expect(sc({ soundcloud_url: url }, true)).toBe('off');
  });
});

describe('showClock', () => {
  it('counts down on the day, names the days before it', () => {
    expect(showClock(show(), 'pre', berlin(25, 11, 40), false)).toEqual({
      kind: 'pre',
      clock: '00:20:00',
      sub: 'until 12:00',
    });
    expect(showClock(show(), 'pre', berlin(23, 11), false).clock).toBe('in 2 days');
  });

  it('runs on air and shows the aired length after the show', () => {
    expect(showClock(show(), 'live', berlin(25, 12, 42), true)).toEqual({
      kind: 'live',
      clock: '00:42:00',
      sub: 'of 2:00:00',
    });
    expect(showClock(show(), 'live', berlin(25, 12, 10), false).sub).toBe('not on air yet');
    const aired = show({
      latest_recording: { id: 1, version: 'v1', status: 'raw', duration_ms: 7120000 },
    } as Partial<ShowDetail>);
    expect(showClock(aired, 'post', berlin(25, 14, 20), false)).toEqual({
      kind: 'post',
      clock: '1:58:40',
      sub: '12:00–14:00',
    });
  });
});
