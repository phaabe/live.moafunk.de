/**
 * Waveform Peaks Extractor
 *
 * Extracts waveform peaks data from an audio file using Web Audio API.
 * The peaks array can be stored alongside the audio file and used later
 * by WaveSurfer.js to render the waveform without decoding the entire file.
 */

export interface WaveformPeaks {
  /** Array of normalized peak values (0-1) */
  peaks: number[];
  /** Number of peaks samples */
  length: number;
  /** Audio duration in seconds */
  duration: number;
  /** Sample rate of the original audio */
  sampleRate: number;
}

/**
 * Extract waveform peaks from an audio file.
 *
 * @param file - The audio file to analyze
 * @param numberOfPeaks - Number of peak samples to extract (default: 500)
 * @returns Promise resolving to WaveformPeaks data
 */
export async function extractWaveformPeaks(
  file: File,
  numberOfPeaks: number = 500,
): Promise<WaveformPeaks> {
  const audioContext = new (window.AudioContext || (window as any).webkitAudioContext)();

  try {
    const arrayBuffer = await file.arrayBuffer();
    const audioBuffer = await audioContext.decodeAudioData(arrayBuffer);

    const channelData = audioBuffer.getChannelData(0); // Use first channel
    const samplesPerPeak = Math.floor(channelData.length / numberOfPeaks);
    const peaks: number[] = [];

    for (let i = 0; i < numberOfPeaks; i++) {
      const start = i * samplesPerPeak;
      const end = Math.min(start + samplesPerPeak, channelData.length);

      let min = 0;
      let max = 0;

      for (let j = start; j < end; j++) {
        const sample = channelData[j];
        if (sample < min) min = sample;
        if (sample > max) max = sample;
      }

      // Store the absolute max of min/max for a cleaner waveform
      peaks.push(Math.max(Math.abs(min), Math.abs(max)));
    }

    // Normalize peaks to 0-1 range
    const maxPeak = Math.max(...peaks, 0.001); // Avoid division by zero
    const normalizedPeaks = peaks.map((p) => p / maxPeak);

    return {
      peaks: normalizedPeaks,
      length: numberOfPeaks,
      duration: audioBuffer.duration,
      sampleRate: audioBuffer.sampleRate,
    };
  } finally {
    await audioContext.close();
  }
}

/**
 * Extract peaks and return as a JSON string suitable for storage.
 */
export async function extractWaveformPeaksJson(
  file: File,
  numberOfPeaks: number = 500,
): Promise<string> {
  const peaksData = await extractWaveformPeaks(file, numberOfPeaks);
  return JSON.stringify(peaksData);
}
