import { config } from './config';
import { checkStreamStatus } from './streamDetector';
import { initializePlayer, updateLiveStatus, destroyPlayer, restartPlayer } from './player';

// Only initialize if we're on a page with a player
const hasPlayer = document.getElementById('player') || document.getElementById('videoElement');

if (hasPlayer) {
  let wasLive = false;

  // Initial check + player init
  checkStreamStatus(config.stream.hls).then((live) => {
    wasLive = live;
    updateLiveStatus(live);
    if (live) {
      initializePlayer();
    }
  });

  // Poll stream status every 8 seconds to detect live↔offline transitions
  setInterval(async () => {
    const live = await checkStreamStatus(config.stream.hls);

    if (live && !wasLive) {
      // Transition: offline → live
      console.log('[StreamPoll] Stream went live');
      updateLiveStatus(true);
      restartPlayer();
    } else if (!live && wasLive) {
      // Transition: live → offline
      console.log('[StreamPoll] Stream went offline');
      updateLiveStatus(false);
      destroyPlayer();
    }

    wasLive = live;
  }, 8000);
}

// Emoji explosion effect for logo
function getRandomEmoji(): string {
  const random = Math.random();
  if (random < 0.7) return '💕'; // 70% pink hearts
  if (random < 0.75) return '❤️'; // 5% red heart
  return '🛸'; // 25% UFO
}

function createFlyingEmoji(x: number, y: number) {
  const emoji = document.createElement('div');
  emoji.textContent = getRandomEmoji();
  emoji.style.position = 'fixed';
  emoji.style.left = `${x}px`;
  emoji.style.top = `${y}px`;
  emoji.style.fontSize = '24px';
  emoji.style.pointerEvents = 'none';
  emoji.style.zIndex = '9999';
  emoji.style.userSelect = 'none';

  document.body.appendChild(emoji);

  // Random direction and distance
  const angle = Math.random() * Math.PI * 2;
  const distance = 100 + Math.random() * 150;
  const endX = x + Math.cos(angle) * distance;
  const endY = y + Math.sin(angle) * distance;

  // Animate
  const duration = 1000 + Math.random() * 500;
  emoji.animate(
    [
      { transform: 'translate(0, 0) scale(1)', opacity: 1 },
      { transform: `translate(${endX - x}px, ${endY - y}px) scale(0.5)`, opacity: 0 },
    ],
    {
      duration,
      easing: 'cubic-bezier(0.25, 0.46, 0.45, 0.94)',
    }
  ).onfinish = () => emoji.remove();
}

// Add click handler to logo
const logo = document.querySelector('.logo') as HTMLElement;
if (logo) {
  logo.style.cursor = 'pointer';
  logo.addEventListener('click', (_e) => {
    const rect = logo.getBoundingClientRect();
    const centerX = rect.left + rect.width / 2;
    const centerY = rect.top + rect.height / 2;

    // Create multiple emojis
    const count = 5 + Math.floor(Math.random() * 6); // 5-10 emojis
    for (let i = 0; i < count; i++) {
      setTimeout(() => createFlyingEmoji(centerX, centerY), i * 50);
    }
  });
}
