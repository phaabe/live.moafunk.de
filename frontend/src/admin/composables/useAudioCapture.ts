import { ref, onUnmounted, shallowRef } from 'vue';

export interface AudioDevice {
  deviceId: string;
  label: string;
}

export interface UseAudioCaptureOptions {
  onData?: (data: ArrayBuffer) => void;
  onError?: (error: string) => void;
}

export function useAudioCapture(options: UseAudioCaptureOptions = {}) {
  const { onData, onError } = options;

  const devices = ref<AudioDevice[]>([]);
  const selectedDeviceId = ref<string>('');
  const isCapturing = ref(false);
  const isRecording = ref(false);
  const error = ref<string | null>(null);

  // Volume control (0-2, where 1 is normal, 0 is muted, 2 is 2x gain)
  const inputVolume = ref(1);

  // Use shallowRef for non-reactive objects
  const mediaStream = shallowRef<MediaStream | null>(null);
  // The processed stream (with gain applied) for MediaRecorder
  const processedStream = shallowRef<MediaStream | null>(null);
  let mediaRecorder: MediaRecorder | null = null;

  // Audio processing
  let audioContext: AudioContext | null = null;
  let gainNode: GainNode | null = null;
  let sourceNode: MediaStreamAudioSourceNode | null = null;
  let destinationNode: MediaStreamAudioDestinationNode | null = null;

  // Set input volume
  function setInputVolume(volume: number): void {
    inputVolume.value = Math.max(0, Math.min(2, volume));
    if (gainNode) {
      gainNode.gain.value = inputVolume.value;
    }
  }

  async function refreshDevices(): Promise<void> {
    try {
      // Request permission first to get device labels
      const tempStream = await navigator.mediaDevices.getUserMedia({ audio: true });
      tempStream.getTracks().forEach((track) => track.stop());

      const allDevices = await navigator.mediaDevices.enumerateDevices();
      const audioInputs = allDevices.filter((d) => d.kind === 'audioinput');

      devices.value = audioInputs.map((device, index) => ({
        deviceId: device.deviceId,
        label: device.label || `Audio Input ${index + 1}`,
      }));

      console.log('[AudioCapture] Found devices:', devices.value);
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Failed to list devices';
      error.value = msg;
      onError?.(msg);
    }
  }

  async function captureDevice(deviceId: string): Promise<boolean> {
    stopCapture();
    error.value = null;

    if (!deviceId) {
      error.value = 'Please select an audio device';
      return false;
    }

    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          deviceId: { exact: deviceId },
          echoCancellation: false,
          noiseSuppression: false,
          autoGainControl: false,
          sampleRate: 48000,
          channelCount: 2,
        },
      });

      // Set up audio processing chain with gain control
      setupAudioProcessing(stream);

      mediaStream.value = stream;
      selectedDeviceId.value = deviceId;
      isCapturing.value = true;

      console.log('[AudioCapture] Capturing from device:', deviceId);
      return true;
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Failed to capture audio';
      error.value = msg;
      onError?.(msg);
      return false;
    }
  }

  // Set up audio processing chain with gain node
  function setupAudioProcessing(stream: MediaStream): void {
    // Clean up previous audio context
    cleanupAudioProcessing();

    audioContext = new AudioContext({ sampleRate: 48000 });
    sourceNode = audioContext.createMediaStreamSource(stream);
    gainNode = audioContext.createGain();
    destinationNode = audioContext.createMediaStreamDestination();

    // Apply current volume
    gainNode.gain.value = inputVolume.value;

    // Connect: source -> gain -> destination
    sourceNode.connect(gainNode);
    gainNode.connect(destinationNode);

    // Use the processed stream for recording
    processedStream.value = destinationNode.stream;
  }

  // Clean up audio processing nodes
  function cleanupAudioProcessing(): void {
    if (sourceNode) {
      sourceNode.disconnect();
      sourceNode = null;
    }
    if (gainNode) {
      gainNode.disconnect();
      gainNode = null;
    }
    if (destinationNode) {
      destinationNode = null;
    }
    if (audioContext) {
      audioContext.close();
      audioContext = null;
    }
    processedStream.value = null;
  }

  async function captureScreenAudio(): Promise<boolean> {
    stopCapture();
    error.value = null;

    try {
      // getDisplayMedia requires video: true on most browsers
      const stream = await navigator.mediaDevices.getDisplayMedia({
        audio: {
          echoCancellation: false,
          noiseSuppression: false,
          autoGainControl: false,
          sampleRate: 48000,
          channelCount: 2,
        } as MediaTrackConstraints,
        video: true,
      });

      // Check for audio track
      const audioTracks = stream.getAudioTracks();
      if (audioTracks.length === 0) {
        stream.getTracks().forEach((track) => track.stop());
        error.value = 'No audio track. Make sure to check "Share audio" in the dialog.';
        return false;
      }

      // Stop video tracks - we only want audio
      stream.getVideoTracks().forEach((track) => track.stop());

      // Create audio-only stream
      const audioStream = new MediaStream(audioTracks);

      // Set up audio processing chain with gain control
      setupAudioProcessing(audioStream);

      mediaStream.value = audioStream;
      selectedDeviceId.value = 'screen';
      isCapturing.value = true;

      console.log('[AudioCapture] Capturing screen audio');
      return true;
    } catch (e) {
      if (e instanceof Error && e.name === 'NotAllowedError') {
        error.value = 'Permission denied. Please allow screen sharing with audio.';
      } else {
        error.value = e instanceof Error ? e.message : 'Failed to capture screen audio';
      }
      onError?.(error.value);
      return false;
    }
  }

  function startRecording(): boolean {
    // Use processed stream (with gain) if available, otherwise raw stream
    const streamToRecord = processedStream.value || mediaStream.value;

    if (!streamToRecord) {
      error.value = 'No audio source available';
      return false;
    }

    try {
      const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
        ? 'audio/webm;codecs=opus'
        : 'audio/webm';

      mediaRecorder = new MediaRecorder(streamToRecord, {
        mimeType,
        audioBitsPerSecond: 192000,
      });

      mediaRecorder.ondataavailable = async (event) => {
        if (event.data.size > 0 && onData) {
          const buffer = await event.data.arrayBuffer();
          onData(buffer);
        }
      };

      mediaRecorder.onerror = (e) => {
        console.error('[AudioCapture] Recorder error:', e);
        error.value = 'Recording error';
        onError?.('Recording error');
      };

      // 250ms chunks for low latency
      mediaRecorder.start(250);
      isRecording.value = true;

      console.log('[AudioCapture] Recording started');
      return true;
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Failed to start recording';
      error.value = msg;
      onError?.(msg);
      return false;
    }
  }

  function stopRecording(): void {
    if (mediaRecorder && mediaRecorder.state !== 'inactive') {
      mediaRecorder.stop();
      mediaRecorder = null;
    }
    isRecording.value = false;
    console.log('[AudioCapture] Recording stopped');
  }

  /**
   * Restart the MediaRecorder to get a fresh WebM container with proper EBML header.
   * Use this when starting a file recording to ensure the WebM is valid from the start.
   * The stream continues uninterrupted.
   */
  function restartRecording(): boolean {
    if (!mediaStream.value) {
      error.value = 'No audio source available';
      return false;
    }

    console.log('[AudioCapture] Restarting MediaRecorder for fresh WebM header');

    // Stop current recorder if active
    if (mediaRecorder && mediaRecorder.state !== 'inactive') {
      mediaRecorder.stop();
      mediaRecorder = null;
    }

    // Start a new recorder - this creates a new WebM container with proper header
    return startRecording();
  }

  function stopCapture(): void {
    stopRecording();
    cleanupAudioProcessing();

    if (mediaStream.value) {
      mediaStream.value.getTracks().forEach((track) => track.stop());
      mediaStream.value = null;
    }

    isCapturing.value = false;
    selectedDeviceId.value = '';
    console.log('[AudioCapture] Capture stopped');
  }

  onUnmounted(() => {
    stopCapture();
  });

  return {
    devices,
    selectedDeviceId,
    isCapturing,
    isRecording,
    error,
    mediaStream,
    processedStream,
    inputVolume,
    setInputVolume,
    refreshDevices,
    captureDevice,
    captureScreenAudio,
    startRecording,
    stopRecording,
    restartRecording,
    stopCapture,
  };
}
