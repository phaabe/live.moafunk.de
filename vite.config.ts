import { defineConfig } from 'vite';
import { resolve } from 'path';

export default defineConfig({
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
      },
    },
  },
  server: {
    port: 3000,
  },
});
