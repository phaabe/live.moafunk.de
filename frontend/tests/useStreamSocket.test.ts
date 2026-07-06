import { describe, it, expect } from 'vitest';
import { parsePong } from '../src/admin/composables/useStreamSocket';

describe('parsePong', () => {
  it('parses a full server pong frame', () => {
    const msg = 'pong:{"echo":12345.678,"chunks":40,"bytes":276000,"late":2,"dropped":0}';
    expect(parsePong(msg)).toEqual({
      echoMs: 12345.678,
      chunks: 40,
      bytes: 276000,
      late: 2,
      dropped: 0,
    });
  });

  it('defaults missing counters to zero', () => {
    expect(parsePong('pong:{"echo":1}')).toEqual({
      echoMs: 1,
      chunks: 0,
      bytes: 0,
      late: 0,
      dropped: 0,
    });
  });

  it('rejects frames without a numeric echo', () => {
    expect(parsePong('pong:{"echo":"nope"}')).toBeNull();
    expect(parsePong('pong:{"chunks":1}')).toBeNull();
  });

  it('rejects malformed JSON and non-pong frames', () => {
    expect(parsePong('pong:{oops')).toBeNull();
    expect(parsePong('connected')).toBeNull();
    expect(parsePong('error: nope')).toBeNull();
  });
});
