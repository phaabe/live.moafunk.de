import { config } from './config';
import { checkStreamStatus } from './streamDetector';
import { initializePlayer, updateLiveStatus } from './player';

// Only initialize if we're on a page with a player
const hasPlayer = document.getElementById('player') || document.getElementById('videoElement');

if (hasPlayer) {
  // Check stream status and initialize player
  checkStreamStatus(config.stream.hls).then((live) => {
    updateLiveStatus(live);
  });

  // Initialize the appropriate player for the platform
  initializePlayer();
}
