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

  // Use shallowRef for non-reactive objects
  const mediaStream = shallowRef<MediaStream | null>(null);
  let mediaRecorder: MediaRecorder | null = null;

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
    if (!mediaStream.value) {
      error.value = 'No audio source available';
      return false;
    }

    try {
      const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
        ? 'audio/webm;codecs=opus'
        : 'audio/webm';

      mediaRecorder = new MediaRecorder(mediaStream.value, {
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

  function stopCapture(): void {
    stopRecording();

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
    refreshDevices,
    captureDevice,
    captureScreenAudio,
    startRecording,
    stopRecording,
    stopCapture,
  };
}
