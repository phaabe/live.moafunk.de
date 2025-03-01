// ==============================
// Streaming and Playback Setup
// ==============================

// Determine the platform
const platform = navigator?.userAgentData?.platform || navigator?.platform || 'unknown';
const isIOS = /iPhone|iPod|iPad/.test(platform);
console.log('Platform detected:', platform, 'isIOS:', isIOS);

// Stream URLs
const hlsUrl = 'https://stream.moafunk.de/live/stream-io/index.m3u8';
const flvUrl = 'https://stream.moafunk.de/live/stream-io.flv';

// UI elements
const btn = document.getElementById('btn-play');
const canvas = document.getElementById('meterCanvas');
const canvasCtx = canvas.getContext('2d');
let live = false;

// Audio analysis variables
let audioCtx = null;
let mediaElement = null;
let analyser = null;
let dataArray = null;
let animationFrame = null;
let audioSource = null;

// Check if the stream is live
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

// ==============================
// Audio Setup - Platform specific
// ==============================

// Set up the appropriate media element
function setupMediaElement() {
  if (isIOS) {
    console.log('iOS device detected, using audio element');
    
    // Get or create audio element
    mediaElement = document.getElementById('playerAudio');
    
    // Make it visible on iOS (helps with permissions)
    mediaElement.style.display = 'block';
    
    // Set source
    mediaElement.src = hlsUrl;
  } else {
    console.log('Non-iOS device detected, using video element');
    
    // Use the video element
    mediaElement = document.getElementById('player');
    
    // Set up FLV.js if supported
    if (flvjs && flvjs.isSupported()) {
      console.log('FLV.js is supported');
      const flvPlayer = flvjs.createPlayer({
        type: 'flv',
        url: flvUrl
      });
      flvPlayer.attachMediaElement(mediaElement);
      flvPlayer.load();
    } else {
      // Fall back to HLS
      console.log('Using HLS fallback');
      mediaElement.src = hlsUrl;
    }
  }
  
  return mediaElement;
}

// Initialize Web Audio API
function initWebAudio() {
  try {
    // Create audio context
    const AudioContext = window.AudioContext || window.webkitAudioContext;
    audioCtx = new AudioContext();
    console.log('Audio context created, state:', audioCtx.state);
    
    // Create analyzer
    analyser = audioCtx.createAnalyser();
    analyser.fftSize = 256;
    
    // Create data array for frequency analysis
    dataArray = new Uint8Array(analyser.frequencyBinCount);
    
    return true;
  } catch (error) {
    console.error('Failed to initialize Web Audio API:', error);
    return false;
  }
}

// Connect media element to audio analyzer
function connectAudioAnalyzer() {
  if (!audioCtx || !mediaElement || !analyser) {
    console.error('Cannot connect audio: missing context, media element, or analyzer');
    return false;
  }
  
  try {
    // Create source from media element (if not already created)
    if (!audioSource) {
      audioSource = audioCtx.createMediaElementSource(mediaElement);
      
      // Connect to analyzer and destination
      audioSource.connect(analyser);
      analyser.connect(audioCtx.destination);
      
      console.log('Audio connections established');
    }
    
    return true;
  } catch (error) {
    console.error('Failed to connect audio:', error);
    return false;
  }
}

// ==============================
// Play/Pause Functionality
// ==============================

// Play/pause toggle function
function play() {
  // Early return if not live
  if (!live) return;
  
  // Make sure media element is set up
  if (!mediaElement) {
    setupMediaElement();
  }
  
  // Toggle playback state
  if (!mediaElement.paused) {
    // Currently playing - pause it
    mediaElement.pause();
    btn.className = "btn";
    
    // Stop animation
    if (animationFrame) {
      cancelAnimationFrame(animationFrame);
      animationFrame = null;
    }
  } else {
    // Currently paused - play it
    
    // For iOS, we need to initialize audio on user interaction
    if (!audioCtx && isIOS) {
      initWebAudio();
    }
    
    // Resume audio context if suspended
    if (audioCtx && audioCtx.state === 'suspended') {
      audioCtx.resume().then(() => {
        console.log('AudioContext resumed');
      });
    }
    
    // Attempt to play
    mediaElement.play().then(() => {
      console.log('Playback started');
      btn.className = "btn btn-pause";
      
      // Connect audio analyzer after playback starts
      // This is especially important for iOS
      if (audioCtx && !audioSource) {
        connectAudioAnalyzer();
      }
      
      // Start visualization
      if (!animationFrame) {
        drawMeterCircles();
      }
    }).catch(error => {
      console.error('Error starting playback:', error);
      
      // Show the native audio player on iOS to help with permissions
      if (isIOS) {
        mediaElement.style.display = 'block';
        alert('Please tap the audio controls to start playing');
      }
    });
  }
}

// Setup when document is ready
document.addEventListener('DOMContentLoaded', () => {
  console.log('Document ready');
  
  // Set up media element
  setupMediaElement();
  
  // For non-iOS devices, we can initialize audio right away
  if (!isIOS) {
    initWebAudio();
  }
  
  // For iOS, set up audio element event handlers
  if (isIOS && mediaElement) {
    // This is crucial for iOS - connect analyzer when playback actually starts
    mediaElement.addEventListener('playing', () => {
      console.log('Media playback started');
      
      // Make sure audio context is initialized and resumed
      if (!audioCtx) {
        initWebAudio();
      }
      
      if (audioCtx.state === 'suspended') {
        audioCtx.resume();
      }
      
      // Connect analyzer if not already connected
      if (!audioSource) {
        connectAudioAnalyzer();
      }
      
      // Start visualization if not already running
      if (!animationFrame) {
        drawMeterCircles();
      }
      
      // Update button state
      btn.className = "btn btn-pause";
    });
  }
});

// ==============================
// dB Meter Visualization
// ==============================

// Draw the db meter visualization
function drawMeterCircles() {
  // Schedule next frame
  animationFrame = requestAnimationFrame(drawMeterCircles);
  
  // Clear canvas
  canvasCtx.clearRect(0, 0, canvas.width, canvas.height);
  
  // Set up drawing constants
  const totalCircles = 6;
  const spacing = canvas.width / (totalCircles + 1);
  const centerY = canvas.height / 2;
  const radius = Math.min(spacing / 2 - 5, centerY - 5);
  
  // Default to 0 circles filled
  let circlesFilled = 0;
  
  // Only analyze audio if everything is properly set up and playing
  if (audioCtx && 
      analyser && 
      dataArray && 
      mediaElement && 
      !mediaElement.paused) {
    
    try {
      // Get frequency data
      analyser.getByteFrequencyData(dataArray);
      
      // Calculate average volume
      let sum = 0;
      for (let i = 0; i < dataArray.length; i++) {
        sum += dataArray[i];
      }
      const average = sum / dataArray.length;
      
      // Map to number of circles (0-6)
      const normalized = average / 255;
      circlesFilled = Math.min(totalCircles, Math.floor(normalized * totalCircles));
    } catch (error) {
      console.error('Error analyzing audio:', error);
    }
  }
  
  // Draw all circles
  for (let i = 0; i < totalCircles; i++) {
    const centerX = spacing * (i + 1);
    
    // Draw outline
    canvasCtx.beginPath();
    canvasCtx.arc(centerX, centerY, radius, 0, Math.PI * 2);
    canvasCtx.strokeStyle = "black";
    canvasCtx.lineWidth = 2;
    canvasCtx.stroke();
    canvasCtx.closePath();
    
    // Fill active circles
    if (i < circlesFilled) {
      canvasCtx.beginPath();
      canvasCtx.arc(centerX, centerY, radius, 0, Math.PI * 2);
      canvasCtx.fillStyle = (i === totalCircles - 1) ? "yellow" : "grey";
      canvasCtx.fill();
      canvasCtx.closePath();
    }
  }
}