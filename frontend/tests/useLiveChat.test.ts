import { describe, it, expect } from 'vitest';
import { parseChatFrame } from '../src/admin/composables/useLiveChat';

const MSG = { id: 7, author: 'anna', text: 'nice set!', ts: 1_760_000_000, host: false };

describe('parseChatFrame', () => {
  it('parses a history frame', () => {
    const raw = JSON.stringify({ type: 'history', messages: [MSG, { ...MSG, id: 8 }] });
    const frame = parseChatFrame(raw);
    expect(frame).toEqual({ type: 'history', messages: [MSG, { ...MSG, id: 8 }] });
  });

  it('parses a single message frame', () => {
    const raw = JSON.stringify({ type: 'message', message: { ...MSG, host: true } });
    expect(parseChatFrame(raw)).toEqual({ type: 'message', message: { ...MSG, host: true } });
  });

  it('parses an error frame', () => {
    expect(parseChatFrame('{"type":"error","error":"bot not configured"}')).toEqual({
      type: 'error',
      error: 'bot not configured',
    });
  });

  it('rejects frames with malformed messages', () => {
    expect(parseChatFrame('{"type":"message","message":{"id":"x"}}')).toBeNull();
    expect(parseChatFrame('{"type":"history","messages":[{"id":1}]}')).toBeNull();
  });

  it('rejects unknown types, malformed JSON, and non-objects', () => {
    expect(parseChatFrame('{"type":"nope"}')).toBeNull();
    expect(parseChatFrame('{oops')).toBeNull();
    expect(parseChatFrame('"connected"')).toBeNull();
    expect(parseChatFrame('42')).toBeNull();
  });
});
