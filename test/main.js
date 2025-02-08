// ==============================
// Streaming and Playback Setup
// ==============================

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
  // iOS supports HLS natively in <video>
  video.src = hlsUrl;
  video.setAttribute("playsinline", "true");
  video.setAttribute("webkit-playsinline", "true");
  // Instead of moving the element off-screen, use minimal size and transparency:
  video.style.width = "1px";
  video.style.height = "1px";
  video.style.opacity = "0";
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
  if (audioCtx.state === 'suspended') {
    audioCtx.resume();
  }
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

// ==============================
// Web Audio & dB Meter Setup
// ==============================

// Create an AudioContext and an AnalyserNode.
const AudioContext = window.AudioContext || window.webkitAudioContext;
const audioCtx = new AudioContext();
const analyser = audioCtx.createAnalyser();
analyser.fftSize = 256;  // Adjust FFT size if needed
const bufferLength = analyser.frequencyBinCount;
const dataArray = new Uint8Array(bufferLength);

// Connect the video element's audio to the analyser.
// Note: createMediaElementSource can only be called once per element.
const source = audioCtx.createMediaElementSource(video);
source.connect(analyser);
// Optionally, connect the analyser to the destination if you need to hear the audio.
analyser.connect(audioCtx.destination);

// ==============================
// dB Meter Drawing (6 Circles)
// ==============================

// Set up the canvas for the level meter.
const canvas = document.getElementById('meterCanvas');
const canvasCtx = canvas.getContext('2d');

// Function to draw 6 circles for the dB level meter.
function drawMeterCircles() {
  requestAnimationFrame(drawMeterCircles);

  // Retrieve frequency data from the analyser.
  analyser.getByteFrequencyData(dataArray);

  // Compute an average level from the frequency data.
  let sum = 0;
  for (let i = 0; i < bufferLength; i++) {
    sum += dataArray[i];
  }
  const average = sum / bufferLength;

  // Normalize the average to a 0–1 scale.
  const normalized = average / 255;

  // Determine how many circles should be filled (0 to 6).
  const totalCircles = 6;
  let circlesFilled = Math.floor(normalized * totalCircles);
  if (circlesFilled > totalCircles) {
    circlesFilled = totalCircles;
  }

  // Clear the canvas.
  canvasCtx.clearRect(0, 0, canvas.width, canvas.height);

  // Calculate spacing and radius for the circles.
  const spacing = canvas.width / (totalCircles + 1);
  const centerY = canvas.height / 2;
  const radius = Math.min(spacing / 2 - 5, centerY - 5);

  // Draw each circle.
  for (let i = 0; i < totalCircles; i++) {
    const centerX = spacing * (i + 1);

    // Draw the circle outline.
    canvasCtx.beginPath();
    canvasCtx.arc(centerX, centerY, radius, 0, Math.PI * 2);
    canvasCtx.strokeStyle = "black";
    canvasCtx.lineWidth = 2;
    canvasCtx.stroke();
    canvasCtx.closePath();

    // If this circle is active (i.e. below the current level), fill it.
    if (i < circlesFilled) {
      canvasCtx.beginPath();
      canvasCtx.arc(centerX, centerY, radius, 0, Math.PI * 2);
      // For the last (highest) circle, use yellow; otherwise, use grey.
      canvasCtx.fillStyle = (i === totalCircles - 4) ? "#ffe95f" : "grey";
      canvasCtx.fill();
      canvasCtx.closePath();
    }
  }
}

// Start the drawing loop for the dB meter.
drawMeterCircles();

