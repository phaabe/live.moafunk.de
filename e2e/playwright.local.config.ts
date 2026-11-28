import { defineConfig } from '@playwright/test';
import staging from './playwright.config';

// The Studio soundcheck in a real browser against the local mock
// (frontend/scripts/studio-mock.mjs): no staging, no secrets.
export default defineConfig({
  ...staging,
  forbidOnly: !!process.env.CI,
  // CI shards by test, not by file (--shard in e2e-unit.yml). Still one
  // worker: the tests share one mock.
  fullyParallel: true,
  testDir: './tests-local',
  globalSetup: './local-setup.ts',
  globalTeardown: undefined,
  use: { ...staging.use, baseURL: 'http://localhost:3000' },
  projects: [
    { name: 'chromium' },
    {
      name: 'firefox',
      // Chromium fake media is enough here. Left out, not skipped in the spec:
      // --shard counts a test skipped at run time as work.
      testIgnore: /(background-stream|studio-log)\.spec\.ts$/,
      use: {
        browserName: 'firefox',
        // Firefox has no "microphone" permission in Playwright: prefs grant it.
        permissions: [],
        launchOptions: {
          firefoxUserPrefs: {
            // Firefox's own fake mic: a tone, not our WAV.
            'media.navigator.streams.fake': true,
            'media.navigator.permission.disabled': true,
            'media.autoplay.default': 0,
          },
        },
      },
    },
  ],
  webServer: [
    {
      command: 'node scripts/studio-mock.mjs',
      cwd: '../frontend',
      url: 'http://localhost:8000/api/auth/me',
      reuseExistingServer: !process.env.CI,
    },
    {
      command: 'npm run dev',
      cwd: '../frontend',
      env: { VITE_STREAM_ICECAST_TEST_URL: 'http://localhost:8000/test.mp3' },
      url: 'http://localhost:3000/admin/',
      reuseExistingServer: !process.env.CI,
    },
  ],
});
