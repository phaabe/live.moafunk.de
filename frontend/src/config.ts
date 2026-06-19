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
    // Public Icecast `/live.mp3` mount (#176). When set, the public player
    // streams this single MP3 mount via native <audio> on every platform
    // (iOS + desktop) instead of the legacy NMS HLS/FLV pair below. Empty →
    // the NMS HLS/FLV path stays active, so this is a no-op env flip.
    icecast: import.meta.env.VITE_STREAM_ICECAST_URL || '',
    // Backend on-air endpoint (`GET /api/stream/status` → { active }). The
    // authoritative live signal: the Icecast mount is mksafe-backed and thus
    // always up, so mount presence can't tell "show live" from "dead air".
    // Empty → fall back to a HEAD poll of `hls` (legacy NMS behaviour).
    statusUrl: import.meta.env.VITE_STREAM_STATUS_URL || '',
  },
  analytics: {
    domain: import.meta.env.VITE_ANALYTICS_DOMAIN || 'live.moafunk.de',
    scriptUrl:
      import.meta.env.VITE_ANALYTICS_SCRIPT_URL || 'https://plausible.moafunk.de/js/plausible.js',
  },
} as const;
