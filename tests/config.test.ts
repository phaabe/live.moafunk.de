import { describe, it, expect } from 'vitest';
import { config } from '../src/config';

describe('config', () => {
  it('should have default stream URLs', () => {
    expect(config.stream.hls).toBeDefined();
    expect(config.stream.flv).toBeDefined();
    expect(config.stream.hls).toContain('stream.moafunk.de');
    expect(config.stream.flv).toContain('stream.moafunk.de');
  });

  it('should have analytics configuration', () => {
    expect(config.analytics.domain).toBeDefined();
    expect(config.analytics.scriptUrl).toBeDefined();
  });
});
