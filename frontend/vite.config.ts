import { defineConfig } from 'vite';
import { resolve } from 'path';
import vue from '@vitejs/plugin-vue';

export default defineConfig({
  plugins: [vue()],
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
