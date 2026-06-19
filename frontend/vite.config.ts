import { defineConfig, type Plugin } from 'vite';
import { resolve } from 'path';
import { execSync } from 'node:child_process';
import vue from '@vitejs/plugin-vue';

// A build identity that changes on every deploy. Prefer the git short SHA (stable,
// available in CI); fall back to a build timestamp for detached/no-git builds.
function resolveBuildId(): string {
  try {
    return execSync('git rev-parse --short HEAD', {
      stdio: ['ignore', 'pipe', 'ignore'],
    })
      .toString()
      .trim();
  } catch {
    return `t${Date.now().toString(36)}`;
  }
}

const BUILD_ID = resolveBuildId();

// Emit /version.json so the running page can detect a new deploy and refresh
// stale clients (GitHub Pages caches index.html for 10 min and we can't set
// headers). See src/versionWatcher.ts.
function emitVersionManifest(): Plugin {
  return {
    name: 'emit-version-json',
    generateBundle() {
      this.emitFile({
        type: 'asset',
        fileName: 'version.json',
        source: JSON.stringify({ hash: BUILD_ID }),
      });
    },
  };
}

export default defineConfig({
  define: { __BUILD_ID__: JSON.stringify(BUILD_ID) },
  plugins: [vue(), emitVersionManifest()],
  root: 'src',
  publicDir: '../public',
  build: {
    outDir: '../dist',
    emptyOutDir: true,
    rollupOptions: {
      input: {
        main: resolve(__dirname, 'src/index.html'),
        relisten: resolve(__dirname, 'src/pages/re-listen.html'),
        techrider: resolve(__dirname, 'src/pages/tech-rider.html'),
        unheardform: resolve(__dirname, 'src/pages/unheard-artists-form.html'),
        admin: resolve(__dirname, 'src/admin/index.html'),
      },
    },
  },
  server: {
    port: 3000,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/ws': {
        target: 'ws://localhost:8000',
        ws: true,
      },
      // Download routes (must not conflict with SPA hash routes)
      '^/shows/\\d+/download': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '^/artists/\\d+/download': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src'),
      '@admin': resolve(__dirname, 'src/admin'),
      '@shared': resolve(__dirname, 'src/shared'),
    },
  },
});
