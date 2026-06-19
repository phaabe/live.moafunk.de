// Environment configuration
/// <reference types="vite/client" />

export const config = {
  stream: {
    hls:
      import.meta.env.VITE_STREAM_HLS_URL || 'https://stream.moafunk.de/live/stream-io/index.m3u8',
    flv: import.meta.env.VITE_STREAM_FLV_URL || 'https://stream.moafunk.de/live/stream-io.flv',
    // Icecast `/test` mount for the broadcaster preview player (#175). Empty by
    // default → the preview stays hidden until the Phase-2 Icecast stack is live.
    icecastTestUrl: import.meta.env.VITE_STREAM_ICECAST_TEST_URL || '',
  },
  analytics: {
    domain: import.meta.env.VITE_ANALYTICS_DOMAIN || 'live.moafunk.de',
    scriptUrl:
      import.meta.env.VITE_ANALYTICS_SCRIPT_URL || 'https://plausible.moafunk.de/js/plausible.js',
  },
} as const;
