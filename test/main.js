// ==============================
// Streaming and Playback Setup
// ==============================

// Determine the platform.
const platform = navigator?.userAgentData?.platform || navigator?.platform || 'unknown';

// Stream URLs.
const hlsUrl = 'https://stream.moafunk.de/live/stream-io/index.m3u8';
const flvUrl = 'https://stream.moafunk.de/live/stream-io.flv';

// Define a variable for the media element (this will be either a video or an audio element).
let mediaElement;
const btn = document.getElementById('btn-play');
let live = false;

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

// Platform-specific media element setup.
if (/iPhone|iPod|iPad/.test(platform)) {
  console.log('is iOS - using audio element for playback and analysis');
  // Use an audio element on iOS.
  // Either select an existing audio element...
  mediaElement = document.getElementById('playerAudio');
  if (!mediaElement) {
    // ...or create one if it doesn't exist.
    mediaElement = document.createElement('audio');
    mediaElement.id = 'playerAudio';
    document.body.appendChild(mediaElement);
  }
  mediaElement.src = hlsUrl;
  mediaElement.setAttribute("playsinline", "true");
  mediaElement.setAttribute("webkit-playsinline", "true");
  // Optionally, style it to be minimally visible.
  mediaElement.style.width = "1px";
  mediaElement.style.height = "1px";
  mediaElement.style.opacity = "0";
} else {
  // On non-iOS platforms, use the existing video element.
  mediaElement = document.getElementById('player');
  if (/flvjs/i.test(navigator.userAgent) || flvjs.isSupported()) {
    console.log('flvjs is supported, using video element with flv.js');
    const flvPlayer = flvjs.createPlayer({
      type: 'flv',
      url: flvUrl
    });
    flvPlayer.attachMediaElement(mediaElement);
    flvPlayer.load();
  } else {
    // Fallback to native HLS support.
    mediaElement.src = hlsUrl;
  }
}

// Play/pause toggle function.
function play() {
  // Resume AudioContext if needed (see below).
  if (audioCtx && audioCtx.state === 'suspended') {
    audioCtx.resume();
  }
  if (!mediaElement) return;
  if (!mediaElement.paused) {
    mediaElement.pause();
    btn.className = "btn";
  } else {
    if (live) {
      mediaElement.play();
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

// Connect the media element's audio to the analyser.
const source = audioCtx.createMediaElementSource(mediaElement);
source.connect(analyser);
// Ensure we still hear the audio:
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
      canvasCtx.fillStyle = (i === totalCircles - 1) ? "yellow" : "grey";
      canvasCtx.fill();
      canvasCtx.closePath();
    }
  }
}

// Start the drawing loop for the dB meter.
drawMeterCircles();
