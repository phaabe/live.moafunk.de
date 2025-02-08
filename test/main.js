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
//
// --- New Code for dB Level Meter ---

// Set up Web Audio API components.
const AudioContext = window.AudioContext || window.webkitAudioContext;
const audioCtx = new AudioContext();

// Create an analyser node.
const analyser = audioCtx.createAnalyser();
analyser.fftSize = 256; // Adjust for smoother or more detailed readings.
const bufferLength = analyser.frequencyBinCount;
const dataArray = new Uint8Array(bufferLength);

// Connect the video element to the analyser.
// Note: createMediaElementSource can only be called once per element,
// so make sure not to call it multiple times if re-initializing.
const source = audioCtx.createMediaElementSource(video);
source.connect(analyser);
// You may want to connect the analyser to the destination if you want audio playback.
// However, if the video element is already outputting sound, this is optional.
analyser.connect(audioCtx.destination);

// Set up the canvas for the meter.
const canvas = document.getElementById('meterCanvas');
const canvasCtx = canvas.getContext('2d');

// Function to draw the meter.
function drawMeter() {
  requestAnimationFrame(drawMeter);

  // Get frequency data.
  analyser.getByteFrequencyData(dataArray);

  // Compute an average volume from frequency data.
  let sum = 0;
  for (let i = 0; i < bufferLength; i++) {
    sum += dataArray[i];
  }
  let average = sum / bufferLength;

  // Convert average to dB.
  // Note: The conversion here is approximate. The raw data ranges from 0 to 255.
  // We normalize and convert to dB: 20 * log10(normalizedValue).
  let normalized = average / 255;
  let dB = normalized > 0 ? 20 * Math.log10(normalized) : -Infinity;

  // Clear the canvas.
  canvasCtx.clearRect(0, 0, canvas.width, canvas.height);

  // Draw a simple meter bar.
  let meterWidth = normalized * canvas.width;
  canvasCtx.fillStyle = 'lime';
  canvasCtx.fillRect(0, 0, meterWidth, canvas.height);

  // Optionally, display the numerical dB value.
  canvasCtx.fillStyle = 'white';
  canvasCtx.font = '16px sans-serif';
  canvasCtx.fillText(dB.toFixed(1) + ' dB', 10, canvas.height / 2 + 6);
}

// Start the drawing loop.
drawMeter();
