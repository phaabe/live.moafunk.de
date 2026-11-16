import { defineConfig } from 'vitest/config';
import vue from '@vitejs/plugin-vue';
import { fileURLToPath, URL } from 'node:url';

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
      '@admin': fileURLToPath(new URL('./src/admin', import.meta.url)),
      '@shared': fileURLToPath(new URL('./src/shared', import.meta.url)),
    },
  },
  test: {
    globals: true,
    environment: 'jsdom',
    coverage: {
      // istanbul, not v8: v8 in vitest 1.6 counts every CSS and template line of .vue files.
      provider: 'istanbul',
      reporter: ['text', 'html', 'json', 'json-summary', 'lcov'],
      include: ['src/**'],
      exclude: [
        'node_modules/',
        'dist/',
        'tests/',
        '*.config.ts',
        '*.config.js',
      ],
    },
  },
});
