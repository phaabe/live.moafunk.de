import flvjs from 'flv.js';
import { config } from './config';
import { isIOSDevice } from './streamDetector';

let video: HTMLMediaElement | null = null;
let btn: HTMLElement | null = null;
let isLive = false;

/**
 * Initializes the appropriate video player based on platform
 */
export function initializePlayer(): void {
  if (isIOSDevice()) {
    console.log('Detected iOS - using native HLS player');
    video = document.getElementById('player') as HTMLMediaElement;

    // Add error handler for iOS HLS
    if (video) {
      video.addEventListener('error', () => {
        console.log('HLS stream error - stream may be offline');
      });
    }
  } else if (flvjs.isSupported()) {
    console.log('flv.js is supported - using FLV player');
    const videoElement = document.getElementById('videoElement') as HTMLMediaElement;
    video = videoElement;

    try {
      const flvPlayer = flvjs.createPlayer({
        type: 'flv',
        url: config.stream.flv,
      });

      // Add error handlers
      flvPlayer.on('error', (...args: unknown[]) => {
        console.log('FLV player error:', args);
        console.log('Stream may be offline - this is normal when not broadcasting');
      });

      flvPlayer.attachMediaElement(videoElement);
      flvPlayer.load();
    } catch (error) {
      console.log('Error initializing FLV player:', error);
    }
  } else {
    console.log(`Platform ${navigator.platform} not supported for streaming!`);
  }
}

/**
 * Updates the live status indicator
 */
export function updateLiveStatus(live: boolean): void {
  isLive = live;
  const statusElement = document.querySelector('#status');

  if (statusElement) {
    if (live) {
      statusElement.innerHTML = 'Live now';
    } else {
      statusElement.innerHTML =
        'Off air<br/><span style="font-size:13pt;">(we announce shows via Tele- and Instagram)</span>';
    }
  }
}

/**
 * Toggles play/pause state
 */
export function play(): void {
  btn = document.getElementById('btn-play');
  if (!btn || !video) return;

  const playing = btn.className.includes('btn-pause');

  if (playing) {
    video.pause();
    btn.className = 'btn';
  } else {
    if (isLive) {
      video.play();
      btn.className = 'btn btn-pause';
    }
  }
}

// Make play function globally available for onclick handler
(window as unknown as Window & { play: typeof play }).play = play;
