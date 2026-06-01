// Minimal NodeMediaServer 2.4.9 config mirroring prod: RTMP ingest on 1935,
// HTTP/HLS on 8000. The `trans` task transcodes each /live stream to HLS so you
// can confirm the existing index.m3u8 path keeps working (untouched by Icecast).
const NodeMediaServer = require('node-media-server');

const config = {
  rtmp: {
    port: 1935,
    chunk_size: 60000,
    gop_cache: true,
    ping: 30,
    ping_timeout: 60,
  },
  http: {
    port: 8000,
    mediaroot: './media',
    allow_origin: '*',
  },
  trans: {
    ffmpeg: '/usr/bin/ffmpeg',
    tasks: [
      {
        app: 'live',
        hls: true,
        hlsFlags: '[hls_time=2:hls_list_size=3:hls_flags=delete_segments]',
      },
    ],
  },
};

new NodeMediaServer(config).run();
