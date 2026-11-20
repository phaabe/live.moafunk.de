import { ref, shallowRef, onUnmounted, watch } from 'vue';
import { useSpectrum } from './useSpectrum';

/**
 * Broadcaster self-monitoring (#175) + stream spectrum (#274): plays an
 * Icecast mount through a Web-Audio graph so the host can hear AND see what
 * listeners receive. Playback is opt-in (no autoplay) so it never starts
 * feedback on its own. Used by StreamPreviewPlayer (the `/test` mount) and
 * On Air's audio card (the public mount, #1053).
 *
 * CORS: reading spectrum data from a cross-origin stream requires
 * `Access-Control-Allow-Origin` on the mount. We try CORS mode first
 * (crossorigin=anonymous + MediaElementSource → analyser → destination). If the
 * mount lacks the header the CORS load errors out, and we rebuild a plain
 * element without the graph — audible preview, flat spectrum, visible hint.
 * (A MediaElementSource is permanent for its element, so the fallback needs a
 * fresh element — that's why the <audio> is created programmatically.)
 */
export function useStreamMonitor(src: () => string, initialVolume = 70) {
  const playing = ref(false);
  const starting = ref(false);
  /** Monitor volume in % — element volume only; listeners are unaffected. */
  const monitorVolume = ref(initialVolume);
  /** True when CORS mode failed and we fell back to plain (no-spectrum) playback. */
  const corsBlocked = ref(false);
  const playError = ref<string | null>(null);

  const analyser = shallowRef<AnalyserNode | null>(null);
  const { bands } = useSpectrum(analyser);

  let el: HTMLAudioElement | null = null;
  let audioContext: AudioContext | null = null;
  let sourceNode: MediaElementAudioSourceNode | null = null;

  function teardownElement() {
    if (el) {
      el.onerror = null;
      el.pause();
      el.removeAttribute('src');
      el.load();
      el = null;
    }
    if (sourceNode) {
      sourceNode.disconnect();
      sourceNode = null;
    }
    if (analyser.value) {
      analyser.value.disconnect();
      analyser.value = null;
    }
  }

  async function startPlayback(withCors: boolean): Promise<void> {
    teardownElement();
    playError.value = null;

    const url = src();
    el = new Audio();
    el.preload = 'none';
    if (withCors) el.crossOrigin = 'anonymous';
    el.volume = monitorVolume.value / 100;
    // Cache-bust so a stop→start rejoins the live edge instead of a stale buffer.
    el.src = url + (url.includes('?') ? '&' : '?') + '_=' + Date.now();

    el.onerror = () => {
      if (withCors) {
        // Most likely a missing Access-Control-Allow-Origin on the mount.
        console.warn('[StreamPreview] CORS load failed — falling back to plain playback');
        corsBlocked.value = true;
        startPlayback(false).catch(() => {});
      } else {
        playError.value = 'Could not play the relay stream.';
        playing.value = false;
      }
    };

    if (withCors) {
      try {
        audioContext = audioContext ?? new AudioContext();
        if (audioContext.state === 'suspended') await audioContext.resume();
        sourceNode = audioContext.createMediaElementSource(el);
        const node = audioContext.createAnalyser();
        node.fftSize = 2048;
        node.smoothingTimeConstant = 0.75;
        sourceNode.connect(node);
        node.connect(audioContext.destination);
        analyser.value = node;
      } catch (e) {
        console.warn('[StreamPreview] Web-Audio graph failed, plain playback only:', e);
        corsBlocked.value = true;
      }
    }

    await el.play();
    playing.value = true;
    // Clear any error a raced CORS attempt set before this (fallback) play won.
    playError.value = null;
  }

  async function toggle() {
    if (starting.value) return;
    if (playing.value) {
      teardownElement();
      playing.value = false;
      playError.value = null;
      return;
    }
    starting.value = true;
    try {
      await startPlayback(!corsBlocked.value);
    } catch (e) {
      // A CORS failure usually surfaces via onerror (handled above); anything
      // else lands here.
      if (!playing.value && !corsBlocked.value) {
        playError.value = e instanceof Error ? e.message : 'Could not play the relay stream.';
      }
    } finally {
      starting.value = false;
    }
  }

  watch(monitorVolume, (v) => {
    if (el) el.volume = v / 100;
  });

  onUnmounted(() => {
    teardownElement();
    if (audioContext) {
      audioContext.close();
      audioContext = null;
    }
  });

  return { playing, starting, monitorVolume, corsBlocked, playError, bands, toggle };
}
