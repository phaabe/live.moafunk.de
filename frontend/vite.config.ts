import { defineConfig, loadEnv, type Plugin } from 'vite';
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

const BUGDROP_ORIGIN = 'https://bugdrop.neonwatty.workers.dev';

// Inject the BugDrop feedback widget (https://github.com/mean-weasel/bugdrop)
// into the admin dashboard only — and only for dev/review builds. The target repo
// is read from VITE_FEEDBACK_WIDGET (e.g. "phaabe/live.moafunk.de"); feedback
// becomes a GitHub issue with a screenshot + page/browser context. Production
// pipelines (GitHub Pages, admin Docker) never set the var, so nothing is injected
// and the prod bundles stay clean. Emits BugDrop's documented synchronous tag —
// deliberately no async/defer, so the widget can read data-repo off its own tag.
//
// Scope: admin SPA (src/admin/index.html) only. The public pages are intentionally
// left untouched, so visitors never see the widget.
function bugdropFeedbackWidget(repo: string | undefined): Plugin {
  return {
    name: 'bugdrop-feedback-widget',
    transformIndexHtml(html, ctx) {
      if (!repo) return html;
      const isAdmin = ctx.filename.replace(/\\/g, '/').includes('/src/admin/');
      if (!isAdmin) return html;
      return {
        html,
        tags: [
          {
            tag: 'script',
            attrs: { src: `${BUGDROP_ORIGIN}/widget.js`, 'data-repo': repo },
            // End of <body>, not <head>: the widget appends its UI to
            // document.body synchronously on load, so body must already exist.
            injectTo: 'body',
          },
        ],
      };
    },
  };
}

export default defineConfig(({ mode }) => {
  // Read .env files (and CLI/process env) so review builds can opt in via
  // VITE_FEEDBACK_WIDGET=owner/repo in .env.local or on the command line.
  const env = loadEnv(mode, process.cwd(), '');
  const feedbackRepo = env.VITE_FEEDBACK_WIDGET || process.env.VITE_FEEDBACK_WIDGET;

  return {
    define: { __BUILD_ID__: JSON.stringify(BUILD_ID) },
    plugins: [vue(), emitVersionManifest(), bugdropFeedbackWidget(feedbackRepo)],
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
  };
});
