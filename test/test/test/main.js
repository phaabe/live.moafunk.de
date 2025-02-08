// Define the media element and play button.
const video = document.getElementById('player');
const btn = document.getElementById('btn-play');
let live = false;

// Stream URLs.
const hlsUrl = 'https://stream.moafunk.de/live/stream-io/index.m3u8';
const flvUrl = 'https://stream.moafunk.de/live/stream-io.flv';

// Determine the platform.
const platform = navigator?.userAgentData?.platform || navigator?.platform || 'unknown';

// Check if the stream is live.
fetch(hlsUrl, { method: 'HEAD' })
  .then(response => {
    if (response.status === 200) {
      document.querySelector('#status').innerHTML = 'Live now';
      live = true;
    } else {
      document.querySelector('#status').innerHTML = 'Off air<br/><span style="font-size:13pt;">(we announce shows via Tele- and Instagram)</span>';
      live = false;
    }
  })
  .catch(error => {
    console.error('Error:', error);
    document.querySelector('#status').innerHTML = 'Off';
    live = false;
  });

// Initialize playback based on platform support.
if (/iPhone|iPod|iPad/.test(platform)) {
  console.log('is iOS');
  // iOS supports HLS natively in <video>, so set the source.
  video.src = hlsUrl;
} else if (flvjs.isSupported()) {
  console.log('flvjs is supported, this is not iOS');
  // Use flv.js to play the FLV stream.
  const flvPlayer = flvjs.createPlayer({
    type: 'flv',
    url: flvUrl
  });
  flvPlayer.attachMediaElement(video);
  flvPlayer.load();
} else {
  console.log(platform + ' not supported as platform for streaming!');
}

// Play/pause toggle function.
function play() {
  if (!video) return;
  if (!video.paused) {
    video.pause();
    btn.className = "btn";
  } else {
    if (live) {
      video.play();
      btn.className = "btn btn-pause";
    }
  }
}
