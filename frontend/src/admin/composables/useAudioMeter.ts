import { ref, onUnmounted, watch, type ShallowRef } from 'vue';

export function useAudioMeter(mediaStream: ShallowRef<MediaStream | null>) {
  const level = ref(0);
  
  let audioContext: AudioContext | null = null;
  let analyser: AnalyserNode | null = null;
  let animationId: number | null = null;

  function start() {
    if (!mediaStream.value) return;

    try {
      audioContext = new AudioContext();
      analyser = audioContext.createAnalyser();
      analyser.fftSize = 256;

      const source = audioContext.createMediaStreamSource(mediaStream.value);
      source.connect(analyser);

      const dataArray = new Uint8Array(analyser.frequencyBinCount);

      const updateMeter = () => {
        if (!analyser) return;

        analyser.getByteFrequencyData(dataArray);
        const average = dataArray.reduce((a, b) => a + b, 0) / dataArray.length;
        level.value = Math.min(100, (average / 128) * 100);

        animationId = requestAnimationFrame(updateMeter);
      };

      updateMeter();
      console.log('[AudioMeter] Started');
    } catch (e) {
      console.error('[AudioMeter] Failed to start:', e);
    }
  }

  function stop() {
    if (animationId !== null) {
      cancelAnimationFrame(animationId);
      animationId = null;
    }

    if (audioContext) {
      audioContext.close();
      audioContext = null;
      analyser = null;
    }

    level.value = 0;
    console.log('[AudioMeter] Stopped');
  }

  // Auto-start/stop when stream changes
  watch(
    () => mediaStream.value,
    (stream) => {
      if (stream) {
        start();
      } else {
        stop();
      }
    },
    { immediate: true }
  );

  onUnmounted(() => {
    stop();
  });

  return {
    level,
    start,
    stop,
  };
}
