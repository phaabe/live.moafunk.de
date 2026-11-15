import { defineConfig } from '@playwright/test';
import { STAGING_URL, TONE_WAV } from './lib/staging';

// One show at a time: both tests book slots on the shared staging server.
export default defineConfig({
  testDir: './tests',
  workers: 1,
  retries: 0,
  globalSetup: './global-setup.ts',
  globalTeardown: './global-teardown.ts',
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL: STAGING_URL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    permissions: ['microphone'],
    launchOptions: {
      args: [
        '--use-fake-device-for-media-stream',
        '--use-fake-ui-for-media-stream',
        `--use-file-for-fake-audio-capture=${TONE_WAV}`,
        '--autoplay-policy=no-user-gesture-required',
      ],
    },
  },
});
