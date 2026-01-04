import { describe, it, expect } from 'vitest';
import { isIOSDevice, checkStreamStatus } from '../src/streamDetector';

describe('streamDetector', () => {
  describe('isIOSDevice', () => {
    it('should detect iOS devices from platform string', () => {
      // Mock navigator
      const originalNavigator = global.navigator;
      
      // Test iPhone
      Object.defineProperty(global, 'navigator', {
        value: { platform: 'iPhone' },
        writable: true,
      });
      expect(isIOSDevice()).toBe(true);
      
      // Test iPad
      Object.defineProperty(global, 'navigator', {
        value: { platform: 'iPad' },
        writable: true,
      });
      expect(isIOSDevice()).toBe(true);
      
      // Test non-iOS
      Object.defineProperty(global, 'navigator', {
        value: { platform: 'Win32' },
        writable: true,
      });
      expect(isIOSDevice()).toBe(false);
      
      // Restore
      global.navigator = originalNavigator;
    });
  });

  describe('checkStreamStatus', () => {
    it('should return true for successful status check', async () => {
      // Mock fetch
      global.fetch = async () => ({
        status: 200,
      }) as Response;
      
      const result = await checkStreamStatus('https://example.com/stream');
      expect(result).toBe(true);
    });

    it('should return false for failed status check', async () => {
      global.fetch = async () => ({
        status: 404,
      }) as Response;
      
      const result = await checkStreamStatus('https://example.com/stream');
      expect(result).toBe(false);
    });

    it('should return false on network error', async () => {
      global.fetch = async () => {
        throw new Error('Network error');
      };
      
      const result = await checkStreamStatus('https://example.com/stream');
      expect(result).toBe(false);
    });
  });
});
